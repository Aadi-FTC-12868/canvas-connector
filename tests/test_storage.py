"""Backend contract and real spawned-process concurrency checks (synthetic data)."""
from __future__ import annotations

import asyncio
import multiprocessing as mp
import time

from cryptography.fernet import Fernet
import pytest

from canvas_connector.auth import CanvasOAuthProvider, Institution
from canvas_connector.storage import OwnerRevokedError, Store


def make_store(tmp_path):
    return Store(tmp_path / "shared.db", Fernet.generate_key())


def test_nested_transactions_rollback_and_versioned_updates(tmp_path):
    store = make_store(tmp_path)
    try:
        with store.transaction():
            store.put("row", "key", {"number": 1}, expires=time.time() + 60, owner="owner")
            with pytest.raises(ValueError):
                with store.transaction():
                    store.put("row", "key", {"number": 2}, expires=time.time() + 60, owner="owner")
                    raise ValueError("rollback savepoint")
            assert store.get("row", "key") == {"number": 1}
        record = store.record("row", "key")
        assert record.version == 1
        assert store.compare_and_swap("row", "key", record.version, {"number": 3})
        assert not store.compare_and_swap("row", "key", record.version, {"number": 4})
        assert store.update("row", "key", lambda value: {"number": value["number"] + 1}) == {"number": 4}
        assert not store.put_if_absent("row", "key", {}, expires=time.time() + 60)
        rows = store.scan("row", owner="owner")
        assert rows[0].value == {"number": 4}
        assert rows[0].owner == "owner" and rows[0].key_hash != "key"
        assert not store.delete_if_version("row", "key", record.version)
        assert store.delete_if_version("row", "key", rows[0].version)
        with pytest.raises(RuntimeError):
            with store.transaction():
                store.put("row", "rollback", {}, expires=time.time() + 60)
                raise RuntimeError("rollback whole transaction")
        assert store.get("row", "rollback") is None
    finally:
        store.close()


def _counter_worker(path, key, ready, start, result):
    store = Store(path, key)
    ready.put(True)
    start.wait(10)
    admitted = 0
    for _ in range(40):
        store.update("counter", "shared", lambda row: {"count": row["count"] + 1})
        allowed, _ = store.increment_window("shared-limit", 31, 60)
        admitted += int(allowed)
    result.put(admitted)
    store.close()


def _recreate_worker(path, key, ready, start, result):
    store = Store(path, key)
    stale = store.get("grant", "grant")
    ready.put(True)
    start.wait(10)
    try:
        store.put("grant", "grant", stale, expires=time.time() + 60, owner="owner")
    except OwnerRevokedError:
        result.put("revoked")
    else:
        result.put("resurrected")
    store.close()


def _lease_worker(path, key, result):
    store = Store(path, key)
    lease = store.acquire_lease("work", "item", ttl=0.25, owner="owner")
    result.put(lease)
    # Intentionally exit without releasing: simulates a dead worker.
    store.close()


def _join(processes):
    for process in processes:
        process.join(15)
        if process.is_alive():
            process.terminate()
            process.join(3)
            pytest.fail("spawned worker did not exit")
        assert process.exitcode == 0


def test_real_processes_share_atomic_updates_and_rate_windows(tmp_path):
    context = mp.get_context("spawn")
    path, key = str(tmp_path / "shared.db"), Fernet.generate_key().decode()
    store = Store(path, key)
    store.put("counter", "shared", {"count": 0}, expires=time.time() + 60)
    ready, result, start = context.Queue(), context.Queue(), context.Event()
    processes = [context.Process(target=_counter_worker, args=(path, key, ready, start, result)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        for _ in processes:
            assert ready.get(timeout=10)
        start.set()
        admitted = [result.get(timeout=15) for _ in processes]
        _join(processes)
        assert store.get("counter", "shared") == {"count": 80}
        assert sum(admitted) == 31
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(3)
        store.close()


def test_revocation_blocks_stale_process_recreation(tmp_path):
    context = mp.get_context("spawn")
    path, key = str(tmp_path / "shared.db"), Fernet.generate_key().decode()
    store = Store(path, key)
    store.put("grant", "grant", {"synthetic": True}, expires=time.time() + 60, owner="owner")
    ready, result, start = context.Queue(), context.Queue(), context.Event()
    process = context.Process(target=_recreate_worker, args=(path, key, ready, start, result))
    try:
        process.start()
        assert ready.get(timeout=10)
        store.revoke_owner("owner")
        start.set()
        assert result.get(timeout=10) == "revoked"
        _join([process])
        assert store.get("grant", "grant") is None
        assert store.is_owner_revoked("owner")
    finally:
        if process.is_alive():
            process.terminate()
            process.join(3)
        store.close()


def test_dead_worker_lease_expires_and_stale_holder_cannot_mutate_it(tmp_path):
    context = mp.get_context("spawn")
    path, key = str(tmp_path / "shared.db"), Fernet.generate_key().decode()
    store = Store(path, key)
    result = context.Queue()
    process = context.Process(target=_lease_worker, args=(path, key, result))
    process.start()
    old = result.get(timeout=10)
    _join([process])
    # The short lease may already expire during interpreter teardown.
    time.sleep(max(0, old.expires - time.time()) + 0.02)
    new = store.acquire_lease("work", "item", ttl=60, owner="owner")
    assert new and new.holder != old.holder
    assert not store.renew_lease(old)
    store.release_lease(old)
    assert store.lease_valid(new)
    store.revoke_owner("owner")
    assert not store.lease_valid(new)
    assert not store.renew_lease(new)
    assert store.acquire_lease("work", "item", owner="owner") is None
    store.close()


def _provider(path, key):
    return CanvasOAuthProvider(issuer_url="https://connector.example", database_path=path,
        encryption_key=key, institutions=[Institution("https://school.example", "client", "synthetic-secret")])


def _seed_grant(provider):
    grant_id = "synthetic-grant"
    expires = time.time() + 3600
    provider.store.put("grant", grant_id, {"institution": "https://school.example",
        "credential": {"access_token": "expired-canvas-token", "refresh_token": "canvas-refresh", "expires_at": time.time() - 1},
        "user_id": "1", "client_id": "mcp-client", "scopes": ["canvas:read", "canvas:write"], "expires": expires}, expires=expires, owner=grant_id)
    return grant_id, provider._issue(grant_id=grant_id, client_id="mcp-client", scopes=["canvas:read", "canvas:write"])


def _credential_worker(path, key, token, ready, start, result, refresh_count, entered, release):
    provider = _provider(path, key)
    def refresh(*args):
        with refresh_count.get_lock():
            refresh_count.value += 1
        entered.set()
        if not release.wait(10):
            raise TimeoutError("test release missing")
        return {"access_token": "fresh-process-token", "refresh_token": "new-canvas-refresh", "expires_at": time.time() + 3600}
    provider._canvas_token = refresh
    ready.put(True)
    start.wait(10)
    try:
        value = asyncio.run(provider.get_credentials(token))
        result.put(("ok", value.token))
    except PermissionError:
        result.put(("revoked", None))
    finally:
        provider.store.close()


def test_independent_provider_processes_refresh_canvas_token_once(tmp_path):
    context = mp.get_context("spawn")
    path, key = str(tmp_path / "auth.db"), Fernet.generate_key().decode()
    parent = _provider(path, key)
    grant, tokens = _seed_grant(parent)
    ready, result = context.Queue(), context.Queue()
    start, entered, release = context.Event(), context.Event(), context.Event()
    count = context.Value("i", 0)
    processes = [context.Process(target=_credential_worker,
        args=(path, key, tokens.access_token, ready, start, result, count, entered, release)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        for _ in processes:
            assert ready.get(timeout=10)
        start.set()
        assert entered.wait(10)
        time.sleep(0.15)  # Ensure the competing process waits on the durable lease.
        release.set()
        answers = [result.get(timeout=15) for _ in processes]
        _join(processes)
        assert answers == [("ok", "fresh-process-token"), ("ok", "fresh-process-token")]
        assert count.value == 1
        assert parent.store.get("grant", grant)["credential"]["refresh_token"] == "new-canvas-refresh"
    finally:
        release.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(3)
        parent.store.close()


def test_process_refresh_cannot_resurrect_revoked_grant(tmp_path):
    context = mp.get_context("spawn")
    path, key = str(tmp_path / "auth.db"), Fernet.generate_key().decode()
    parent = _provider(path, key)
    grant, tokens = _seed_grant(parent)
    ready, result = context.Queue(), context.Queue()
    start, entered, release = context.Event(), context.Event(), context.Event()
    count = context.Value("i", 0)
    process = context.Process(target=_credential_worker,
        args=(path, key, tokens.access_token, ready, start, result, count, entered, release))
    try:
        process.start()
        assert ready.get(timeout=10)
        start.set()
        assert entered.wait(10)
        parent.store.revoke_owner(grant)
        release.set()
        assert result.get(timeout=15) == ("revoked", None)
        _join([process])
        assert parent.store.get("grant", grant) is None
        assert parent.validate_access(tokens.access_token) is None
        assert count.value == 1
    finally:
        release.set()
        if process.is_alive():
            process.terminate()
            process.join(3)
        parent.store.close()


def _exchange_worker(path, key, refresh, ready, start, result):
    from mcp.shared.auth import OAuthClientInformationFull
    from mcp.server.auth.provider import TokenError
    provider = _provider(path, key)
    client = OAuthClientInformationFull(client_id="mcp-client", redirect_uris=["https://client.example/callback"], token_endpoint_auth_method="none")
    loaded = asyncio.run(provider.load_refresh_token(client, refresh))
    ready.put(loaded is not None)
    start.wait(10)
    try:
        token = asyncio.run(provider.exchange_refresh_token(client, loaded, ["canvas:read"]))
        result.put(("ok", token.access_token))
    except TokenError:
        result.put(("replay", None))
    finally:
        provider.store.close()


def test_concurrent_process_refresh_exchange_detects_replay_and_revokes_winner(tmp_path):
    context = mp.get_context("spawn")
    path, key = str(tmp_path / "auth.db"), Fernet.generate_key().decode()
    parent = _provider(path, key)
    grant, tokens = _seed_grant(parent)
    ready, result, start = context.Queue(), context.Queue(), context.Event()
    processes = [context.Process(target=_exchange_worker, args=(path, key, tokens.refresh_token, ready, start, result)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        for _ in processes:
            assert ready.get(timeout=10)
        start.set()
        answers = [result.get(timeout=15) for _ in processes]
        _join(processes)
        assert sorted(answer[0] for answer in answers) == ["ok", "replay"]
        winner = next(answer[1] for answer in answers if answer[0] == "ok")
        assert parent.validate_access(winner) is None
        assert parent.store.get("grant", grant) is None
        assert parent.store.is_owner_revoked(grant)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(3)
        parent.store.close()


def test_cas_does_not_accept_version_from_deleted_record(tmp_path):
    store = make_store(tmp_path)
    store.put("row", "reused-key", {"old": True}, expires=time.time() + 60)
    old = store.record("row", "reused-key")
    store.delete("row", "reused-key")
    store.put("row", "reused-key", {"new": True}, expires=time.time() + 60)
    assert store.record("row", "reused-key").version != old.version
    assert not store.compare_and_swap("row", "reused-key", old.version, {"stale": True})
    store.close()
