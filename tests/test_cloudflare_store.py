"""Python adapter -> real HTTP -> production JS StoreEngine -> SQLite.

No Cloudflare account, email or Canvas credentials are used. The deployed DO
wrapper is separately checked in workerd by cloudflare/tests/store.test.mjs.
"""
from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time

from cryptography.fernet import Fernet
import httpx
import pytest

from canvas_connector.cloudflare_store import CloudflareStore, CloudflareStorageError, CHUNK_BYTES
from canvas_connector.storage import OwnerRevokedError, open_store

ROOT = Path(__file__).resolve().parents[1]


class Bridge:
    def __init__(self, path):
        self.path = path
        self.requests = []
        self.lose = None
        self.advance_on_chunk = False
        self.start()

    def start(self):
        self.process = subprocess.Popen(["node", str(ROOT / "cloudflare/tests/bridge.mjs"), str(self.path)],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError(self.process.stderr.read())
        self.url = f"http://127.0.0.1:{json.loads(line)['port']}"
        self.client = httpx.Client(trust_env=False, timeout=10)

    def close(self):
        self.client.close()
        self.process.terminate()
        self.process.wait(timeout=10)

    def handle(self, request):
        assert str(request.url) == "http://canvas-store.internal/rpc"
        body = json.loads(request.content)
        self.requests.append(body)
        response = self.client.post(self.url + "/rpc", json=body)
        if self.advance_on_chunk and body["op"] == "blob_chunk":
            self.advance_on_chunk = False
            self.advance(61_000)
        if self.lose == body["op"]:
            self.lose = None
            raise httpx.ReadError("synthetic lost private response", request=request)
        return httpx.Response(response.status_code, content=response.content)

    def advance(self, ms):
        self.client.post(self.url + "/test/advance", json={"milliseconds": ms}).raise_for_status()


@pytest.fixture
def bridge(tmp_path):
    if not shutil.which("node"):
        pytest.skip("Cloudflare adapter integration requires Node.js 22.13+ with node:sqlite")
    value = Bridge(tmp_path / "durable.sqlite")
    yield value
    value.close()


@pytest.fixture
def key():
    return Fernet.generate_key()


@pytest.fixture
def store(bridge, key):
    value = CloudflareStore(key, transport=httpx.MockTransport(bridge.handle))
    yield value
    value.close()


def put(store, key="one", value=None, **kwargs):
    store.put("test", key, value or {"value": key}, expires=time.time() + 600, **kwargs)


def test_is_empty_ignores_transaction_and_revision_bookkeeping(store):
    assert store.is_empty()
    with store.transaction():
        assert store.is_empty()
    assert store.is_empty()
    put(store)
    assert not store.is_empty()
    store.delete("test", "one")
    assert store.is_empty()


@pytest.mark.parametrize("state", ["record", "expired_record", "lease", "tombstone", "upload"])
def test_is_empty_detects_uncommitted_application_state(store, state):
    with pytest.raises(ValueError), store.transaction():
        assert store.is_empty()
        if state in {"record", "expired_record"}:
            store.put("unknown_application_kind", "key", {},
                      expires=time.time() + (-1 if state == "expired_record" else 60))
        elif state == "lease":
            assert store.acquire_lease("work", "key")
        elif state == "tombstone":
            store.revoke_owner("owner")
        else:
            store._blob(store._encoded({"pending": True}))
        assert not store.is_empty()
        raise ValueError("rollback")
    assert store.is_empty()


def test_is_empty_fails_closed_when_transaction_expires(store, bridge):
    with pytest.raises(CloudflareStorageError), store.transaction():
        bridge.advance(61_000)
        store.is_empty()


def test_is_empty_requires_boolean_response(bridge, key):
    def handle(request):
        if json.loads(request.content)["op"] == "is_empty":
            return httpx.Response(200, json={"empty": "true"})
        return bridge.handle(request)
    store = CloudflareStore(key, transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(CloudflareStorageError):
            store.is_empty()
    finally:
        store.close()


def test_is_empty_and_initialization_are_atomic_between_clients(bridge, key):
    stores = [CloudflareStore(key, transport=httpx.MockTransport(bridge.handle)) for _ in range(2)]
    ready = threading.Barrier(2)

    def initialize(store):
        ready.wait(timeout=5)
        with store.transaction():
            if not store.is_empty():
                return False
            put(store, "owner")
            return True

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(initialize, stores)) == [False, True]
    finally:
        for store in stores:
            store.close()


def test_private_transport_encryption_and_restart(store, bridge, key):
    put(store, value={"token": "synthetic-super-secret"}, owner="a")
    assert store.get("test", "one") == {"token": "synthetic-super-secret"}
    assert "synthetic-super-secret" not in json.dumps(bridge.requests)
    assert b"synthetic-super-secret" not in bridge.path.read_bytes()
    bridge.close(); bridge.start()
    other = CloudflareStore(key, transport=httpx.MockTransport(bridge.handle))
    try:
        assert other.get("test", "one") == {"token": "synthetic-super-secret"}
    finally:
        other.close()


def test_outer_transaction_rolls_back_all_methods(store):
    with pytest.raises(ValueError):
        with store.transaction():
            put(store, "a")
            put(store, "b")
            raise ValueError("rollback")
    assert store.scan("test") == []


def test_nested_savepoint_preserves_earlier_write(store):
    with store.transaction():
        put(store, "a")
        with pytest.raises(ValueError):
            with store.transaction():
                put(store, "b")
                store.delete("test", "a")
                raise ValueError("nested rollback")
        put(store, "c")
    assert [row.value for row in store.scan("test")] == [{"value": "c"}, {"value": "a"}]
    assert store.get("test", "b") is None


def test_consume_and_aba_revision(store):
    put(store)
    first = store.record("test", "one")
    assert store.consume("test", "one") == {"value": "one"}
    assert store.consume("test", "one") is None
    put(store)
    assert not store.compare_and_swap("test", "one", first.version, {"wrong": True})
    current = store.record("test", "one")
    assert current.version > first.version
    assert store.compare_and_swap("test", "one", current.version, {"next": True})
    assert store.get("test", "one") == {"next": True}


def test_update_and_delete_if_version(store):
    assert store.update("test", "absent", lambda v: v) is None
    assert store.put_if_absent("test", "a", {"count": 1}, expires=time.time() + 10)
    assert not store.put_if_absent("test", "a", {"count": 9}, expires=time.time() + 10)
    assert store.update("test", "a", lambda v: {"count": v["count"] + 1}) == {"count": 2}
    row = store.record("test", "a")
    assert not store.delete_if_version("test", "a", row.version - 1)
    assert store.delete_if_version("test", "a", row.version)


def test_revoke_owner_deletes_records_and_leases_fences_stale_writer(store):
    put(store, owner="a")
    put(store, "other", owner="b")
    lease = store.acquire_lease("execution", "a", owner="a")
    store.revoke_owner("a")
    assert store.get("test", "one") is None
    assert store.get("test", "other") == {"value": "other"}
    assert not store.lease_valid(lease)
    assert not store.renew_lease(lease)
    assert store.acquire_lease("execution", "new", owner="a") is None
    with pytest.raises(OwnerRevokedError):
        put(store, "stale", owner="a")


def test_expiry_capacity_and_reserved_revocation(bridge, key):
    store = CloudflareStore(key, capacity=1, transport=httpx.MockTransport(bridge.handle))
    try:
        store.put("test", "old", {}, expires=time.time() - 1)
        put(store, "one", owner="a")
        with pytest.raises(CloudflareStorageError):
            put(store, "two")
        store.revoke_owner("a")
        put(store, "two")
        assert len(store.scan("test")) == 1
    finally:
        store.close()


def test_lease_fencing_and_rate_limits(store, bridge):
    first = store.acquire_lease("exec", "x", ttl=1)
    assert store.acquire_lease("exec", "x") is None
    assert store.renew_lease(first, ttl=2)
    bridge.advance(3_000)
    second = store.acquire_lease("exec", "x", ttl=30)
    assert second and second.holder != first.holder
    store.release_lease(first)
    assert store.lease_valid(second)
    store.release_lease(second)
    assert not store.lease_valid(second)
    assert store.increment_window("rate", 2, 60) == (True, 0)
    assert store.increment_window("rate", 2, 60) == (True, 0)
    allowed, wait = store.increment_window("rate", 2, 60)
    assert not allowed and wait > 0


def test_large_ciphertext_uses_bounded_chunks(store, bridge):
    content = "x" * (16 * 1024 * 1024)
    put(store, value={"content": content})
    assert store.get("test", "one") == {"content": content}
    chunks = [r for r in bridge.requests if r["op"] == "blob_chunk"]
    assert len(chunks) > 128
    assert max(len(r["data"]) for r in chunks) <= CHUNK_BYTES


def test_lost_commit_response_queries_status_without_replay(store, bridge):
    bridge.lose = "commit"
    put(store)
    assert store.get("test", "one") == {"value": "one"}
    writes = [r for r in bridge.requests if r["op"] == "write"]
    assert len(writes) == 1
    assert any(r["op"] == "status" for r in bridge.requests)


def test_lost_staged_write_response_never_commits(store, bridge):
    bridge.lose = "write"
    with pytest.raises(CloudflareStorageError):
        put(store)
    assert store.get("test", "one") is None


def test_lost_begin_response_aborts_only_that_transaction(store, bridge):
    bridge.lose = "begin"
    with pytest.raises(CloudflareStorageError):
        put(store)
    put(store, "fresh")
    assert store.get("test", "one") is None
    assert store.get("test", "fresh") == {"value": "fresh"}


def test_lost_commit_and_status_stays_uncertain_without_replaying(bridge, key):
    committed = False
    def request(r):
        nonlocal committed
        op = json.loads(r.content)["op"]
        result = bridge.handle(r)
        if op == "commit" and not committed:
            committed = True
            raise httpx.ReadError("synthetic lost commit", request=r)
        if op == "status":
            raise httpx.ReadError("synthetic unavailable status", request=r)
        return result
    store = CloudflareStore(key, transport=httpx.MockTransport(request))
    try:
        with pytest.raises(CloudflareStorageError, match="outcome"):
            put(store)
        assert store.get("test", "one") == {"value": "one"}
        assert len([r for r in bridge.requests if r["op"] == "write"]) == 1
    finally:
        store.close()


def test_lease_and_tombstone_survive_restart(store, bridge):
    first = store.acquire_lease("exec", "one", owner="a")
    store.revoke_owner("b")
    bridge.close(); bridge.start()
    assert store.lease_valid(first)
    assert store.acquire_lease("exec", "one", owner="a") is None
    assert store.is_owner_revoked("b")
    with pytest.raises(OwnerRevokedError):
        put(store, owner="b")


def test_expired_upload_transaction_is_never_reacquired(store, bridge):
    bridge.advance_on_chunk = True
    with pytest.raises(CloudflareStorageError):
        put(store, value={"content": "x" * (CHUNK_BYTES * 3)})
    assert store.get("test", "one") is None
    assert len([r for r in bridge.requests if r["op"] == "blob_start"]) == 1


def test_crashed_client_staging_discarded_by_next_process(store, bridge, key):
    store._begin()
    put(store)
    bridge.advance(61_000)
    other = CloudflareStore(key, transport=httpx.MockTransport(bridge.handle))
    try:
        assert other.get("test", "one") is None
        put(other, "two")
        with pytest.raises(CloudflareStorageError):
            store._commit()
    finally:
        store._tx = None
        other.close()


def test_two_clients_consume_once(bridge, key):
    def run(index):
        s = CloudflareStore(key, transport=httpx.MockTransport(bridge.handle))
        try:
            return s.consume("test", "one")
        finally:
            s.close()
    s = CloudflareStore(key, transport=httpx.MockTransport(bridge.handle))
    put(s); s.close()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(run, range(4)))
    assert values.count({"value": "one"}) == 1
    assert values.count(None) == 3


@pytest.mark.parametrize("url", ["cloudflare://evil", "cloudflare://store/path", "cloudflare:http://evil"])
def test_store_selector_has_no_arbitrary_url(url, key):
    with pytest.raises(ValueError):
        open_store(url, key)


def test_redirect_not_followed(key):
    calls = []
    def request(r):
        calls.append(str(r.url))
        return httpx.Response(307, headers={"location": "https://outside.invalid"})
    store = CloudflareStore(key, transport=httpx.MockTransport(request))
    with pytest.raises(CloudflareStorageError):
        put(store)
    assert set(calls) == {"http://canvas-store.internal/rpc"}
    store.close()
