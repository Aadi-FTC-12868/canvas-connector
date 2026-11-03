"""Opt-in PostgreSQL integration tests; creates and drops only random test schemas.

Run against a disposable fixture with CONNECTOR_TEST_POSTGRES_URL. No Canvas
network traffic is performed. Worker concurrency uses separate OS processes and
separate PostgreSQL connections.
"""
from __future__ import annotations

import asyncio
import multiprocessing as mp
import os
import time
import uuid

from cryptography.fernet import Fernet
import pytest

from canvas_connector.auth import CanvasOAuthProvider, Institution
from canvas_connector.storage import OwnerRevokedError, open_store

pytestmark = pytest.mark.skipif(not os.environ.get("CONNECTOR_TEST_POSTGRES_URL"), reason="PostgreSQL integration fixture is opt-in")


@pytest.fixture
def pg():
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql
    from canvas_connector.postgres import PostgresStore
    dsn = os.environ["CONNECTOR_TEST_POSTGRES_URL"]
    key = Fernet.generate_key().decode()
    namespace = "test_" + uuid.uuid4().hex
    store = PostgresStore(dsn, key, namespace=namespace)
    try:
        yield store, dsn, key, namespace
    finally:
        store.close()
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(namespace)))


@pytest.mark.parametrize("state", ["record", "expired_record", "lease", "tombstone"])
def test_postgres_is_empty_counts_raw_application_state(pg, state):
    store, _, _, _ = pg
    assert store.is_empty()
    with store.transaction():
        assert store.is_empty()
        if state in {"record", "expired_record"}:
            store.put("unrecognized_application_kind", "key", {},
                      expires=store.now() + (-1 if state == "expired_record" else 60))
        elif state == "lease":
            assert store.acquire_lease("work", "key")
        else:
            store.revoke_owner("owner")
        assert not store.is_empty()
    assert not store.is_empty()


def test_postgres_is_empty_observes_rollback_and_ignores_revisions(pg):
    store, _, _, _ = pg
    with pytest.raises(ValueError), store.transaction():
        store.put("row", "key", {}, expires=store.now() + 60)
        assert not store.is_empty()
        raise ValueError("rollback")
    assert store.is_empty()
    store.put("row", "key", {}, expires=store.now() + 60)
    store.delete("row", "key")
    assert store.is_empty()


def test_postgres_is_empty_uses_only_its_namespace(pg):
    import psycopg
    from psycopg import sql
    from canvas_connector.postgres import PostgresStore
    store, dsn, key, _ = pg
    namespace = "test_" + uuid.uuid4().hex
    other = PostgresStore(dsn, key, namespace=namespace)
    try:
        other.put("owner", "singleton", {}, expires=other.now() + 60)
        assert not other.is_empty()
        assert store.is_empty()
    finally:
        other.close()
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(namespace)))


def test_postgres_contract_encryption_cas_rollback_and_revocation(pg):
    from mcp.server.auth.provider import TokenError
    store, dsn, key, namespace = pg
    expiry = store.now() + 60
    with store.transaction():
        store.put("secret", "opaque-secret-key", {"token": "synthetic-private-token"}, expires=expiry, owner="owner")
        with pytest.raises(TokenError):
            with store.transaction():
                store.put("secret", "opaque-secret-key", {"token": "wrong"}, expires=expiry, owner="owner")
                raise TokenError("invalid_grant", "synthetic rollback")
    previous = store.record("secret", "opaque-secret-key")
    assert previous.value == {"token": "synthetic-private-token"}
    assert store.compare_and_swap("secret", "opaque-secret-key", previous.version, {"token": "new"})
    assert not store.compare_and_swap("secret", "opaque-secret-key", previous.version, {"token": "stale"})
    current = store.record("secret", "opaque-secret-key")
    store.delete("secret", "opaque-secret-key")
    store.put("secret", "opaque-secret-key", {"token": "recreated"}, expires=expiry, owner="owner")
    assert not store.compare_and_swap("secret", "opaque-secret-key", current.version, {"token": "ABA"})
    assert store.update("secret", "opaque-secret-key", lambda row: {**row, "versioned": True})["versioned"]
    assert len(store.scan("secret", owner="owner")) == 1
    raw = store.connection.execute("SELECT key,payload FROM records WHERE kind='secret'").fetchone()
    assert raw[0] != "opaque-secret-key"
    assert b"recreated" not in bytes(raw[1])
    duplicate = open_store(dsn, key, namespace=namespace)
    try:
        assert duplicate.get("secret", "opaque-secret-key")["token"] == "recreated"
        duplicate.revoke_owner("owner")
        assert store.get("secret", "opaque-secret-key") is None
        with pytest.raises(OwnerRevokedError):
            store.put("secret", "opaque-secret-key", previous.value, expires=expiry, owner="owner")
    finally:
        duplicate.close()


def test_postgres_leases_use_database_clock_and_fence_stale_holders(pg):
    from canvas_connector.postgres import PostgresStore
    store, dsn, key, namespace = pg
    second = PostgresStore(dsn, key, namespace=namespace)
    try:
        old = store.acquire_lease("refresh", "grant", ttl=0.15, owner="owner")
        assert second.acquire_lease("refresh", "grant", ttl=60, owner="owner") is None
        time.sleep(0.2)
        new = second.acquire_lease("refresh", "grant", ttl=60, owner="owner")
        assert new.holder != old.holder
        assert not store.renew_lease(old)
        store.release_lease(old)
        assert second.lease_valid(new)
        store.revoke_owner("owner")
        assert not second.lease_valid(new)
        assert second.acquire_lease("refresh", "grant", owner="owner") is None
    finally:
        second.close()


def _pg_counter(dsn, key, namespace, ready, start, result):
    store = open_store(dsn, key, namespace=namespace)
    ready.put(True)
    start.wait(10)
    admitted = 0
    for _ in range(25):
        store.update("counter", "shared", lambda value: {"count": value["count"] + 1})
        allowed, _ = store.increment_window("rate", 27, 60)
        admitted += int(allowed)
    result.put(admitted)
    store.close()


def _join(processes):
    for process in processes:
        process.join(20)
        assert not process.is_alive(), "PostgreSQL process did not exit"
        assert process.exitcode == 0


def test_postgres_processes_share_atomic_counter_and_request_limit(pg):
    store, dsn, key, namespace = pg
    store.put("counter", "shared", {"count": 0}, expires=store.now() + 60)
    context = mp.get_context("spawn")
    ready, result, start = context.Queue(), context.Queue(), context.Event()
    workers = [context.Process(target=_pg_counter, args=(dsn, key, namespace, ready, start, result)) for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        for _ in workers:
            assert ready.get(timeout=10)
        start.set()
        admissions = [result.get(timeout=20) for _ in workers]
        _join(workers)
        assert store.get("counter", "shared") == {"count": 50}
        assert sum(admissions) == 27
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(3)


def _pg_provider(dsn, key, namespace):
    return CanvasOAuthProvider(issuer_url="https://connector.example",
        store=open_store(dsn, key, namespace=namespace),
        institutions=[Institution("https://school.example", "client", "synthetic-secret")])


def _seed(provider):
    grant = "synthetic-grant"
    expiry = time.time() + 3600
    provider.store.put("grant", grant, {"institution": "https://school.example",
        "credential": {"access_token": "expired", "refresh_token": "refresh", "expires_at": time.time() - 1},
        "user_id": "1", "client_id": "mcp-client", "scopes": ["canvas:read", "canvas:write"], "expires": expiry}, expires=expiry, owner=grant)
    return grant, provider._issue(grant_id=grant, client_id="mcp-client", scopes=["canvas:read", "canvas:write"])


def _pg_credentials(dsn, key, namespace, token, ready, start, entered, release, count, result):
    provider = _pg_provider(dsn, key, namespace)
    def refresh(*args):
        with count.get_lock():
            count.value += 1
        entered.set()
        if not release.wait(10):
            raise TimeoutError("fixture release missing")
        return {"access_token": "fresh-postgres-token", "refresh_token": "fresh-refresh", "expires_at": time.time() + 3600}
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


@pytest.mark.parametrize("revoke", [False, True])
def test_postgres_provider_processes_refresh_once_and_cannot_resurrect_revocation(pg, revoke):
    store, dsn, key, namespace = pg
    provider = _pg_provider(dsn, key, namespace)
    grant, tokens = _seed(provider)
    context = mp.get_context("spawn")
    ready, result = context.Queue(), context.Queue()
    start, entered, release = context.Event(), context.Event(), context.Event()
    count = context.Value("i", 0)
    workers = [context.Process(target=_pg_credentials,
        args=(dsn, key, namespace, tokens.access_token, ready, start, entered, release, count, result)) for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        for _ in workers:
            assert ready.get(timeout=10)
        start.set()
        assert entered.wait(10)
        time.sleep(0.15)
        if revoke:
            store.revoke_owner(grant)
        release.set()
        outcomes = [result.get(timeout=20) for _ in workers]
        _join(workers)
        assert count.value == 1
        if revoke:
            assert outcomes == [("revoked", None), ("revoked", None)]
            assert store.get("grant", grant) is None
        else:
            assert outcomes == [("ok", "fresh-postgres-token"), ("ok", "fresh-postgres-token")]
            assert store.get("grant", grant)["credential"]["access_token"] == "fresh-postgres-token"
    finally:
        release.set()
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(3)
        provider.store.close()


def _pg_exchange(dsn, key, namespace, refresh, ready, start, result):
    from mcp.server.auth.provider import TokenError
    from mcp.shared.auth import OAuthClientInformationFull
    provider = _pg_provider(dsn, key, namespace)
    client = OAuthClientInformationFull(client_id="mcp-client", redirect_uris=["https://client.example/callback"], token_endpoint_auth_method="none")
    loaded = asyncio.run(provider.load_refresh_token(client, refresh))
    ready.put(loaded is not None)
    start.wait(10)
    try:
        tokens = asyncio.run(provider.exchange_refresh_token(client, loaded, ["canvas:read"]))
        result.put(("ok", tokens.access_token))
    except TokenError:
        result.put(("replay", None))
    finally:
        provider.store.close()


def test_postgres_concurrent_mcp_refresh_rotation_revokes_replay_family(pg):
    store, dsn, key, namespace = pg
    provider = _pg_provider(dsn, key, namespace)
    grant, tokens = _seed(provider)
    context = mp.get_context("spawn")
    ready, result, start = context.Queue(), context.Queue(), context.Event()
    workers = [context.Process(target=_pg_exchange, args=(dsn, key, namespace, tokens.refresh_token, ready, start, result)) for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        for _ in workers:
            assert ready.get(timeout=10)
        start.set()
        outcomes = [result.get(timeout=20) for _ in workers]
        _join(workers)
        assert sorted(row[0] for row in outcomes) == ["ok", "replay"]
        winner = next(row[1] for row in outcomes if row[0] == "ok")
        assert provider.validate_access(winner) is None
        assert store.get("grant", grant) is None
        assert store.is_owner_revoked(grant)
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(3)
        provider.store.close()


def test_provider_from_environment_selects_postgres_without_treating_dsn_as_path(pg, tmp_path, monkeypatch):
    import json
    from canvas_connector.__main__ import provider_from_env
    from canvas_connector.postgres import PostgresStore
    _, dsn, key, _ = pg
    institutions = tmp_path / "institutions.json"
    institutions.write_text(json.dumps([{"origin": "https://school.example", "client_id": "synthetic-client", "client_secret": "synthetic-secret"}]))
    monkeypatch.setenv("CONNECTOR_PUBLIC_URL", "https://connector.example")
    monkeypatch.setenv("CONNECTOR_DATABASE", dsn)
    monkeypatch.setenv("CONNECTOR_ENCRYPTION_KEY", key)
    monkeypatch.setenv("CONNECTOR_INSTITUTIONS_FILE", str(institutions))
    provider = provider_from_env()
    try:
        assert isinstance(provider.store, PostgresStore)
        assert provider.resource_url == "https://connector.example/mcp"
    finally:
        provider.store.close()


def test_postgres_reconnects_next_operation_without_replaying_transaction(pg):
    store, *_ = pg
    store.put("reconnect", "row", {"value": 1}, expires=store.now() + 60)
    store.connection.close()
    assert store.get("reconnect", "row") == {"value": 1}
    with pytest.raises(Exception):
        with store.transaction():
            store.put("reconnect", "row", {"value": 2}, expires=store.now() + 60)
            store.connection.close()  # Server would roll back this lost transaction.
            store.get("reconnect", "row")
    assert store.get("reconnect", "row") == {"value": 1}


def test_response_continuation_crosses_postgres_instances_and_respects_owner_and_revoke(pg):
    import hashlib
    from canvas_connector.responses import ResponseStore
    store, dsn, key, namespace = pg
    other = open_store(dsn, key, namespace=namespace)
    first = ResponseStore(persistence=store, inline_chars=10, budget=150000)
    second = ResponseStore(persistence=other, inline_chars=10, budget=150000)
    try:
        first_window = first.compact({"data": "synthetic-course-text-" * 4000}, "grant")
        result_id = first_window["data"]["result_id"]
        joined = first_window["data"]["text"]
        next_read = first_window["data"]["next_read"]
        while next_read:
            window = second.read("grant", result_id, next_read["offset"], next_read["max_chars"])
            joined += window["data"]["text"]
            next_read = window["data"]["next_read"]
        assert hashlib.sha256(joined.encode()).hexdigest() == first_window["data"]["sha256"]
        with pytest.raises(ValueError, match="account"):
            second.read("other-grant", result_id)
        blocked = second.compact({"data": "x" * 100000}, "other-grant")
        assert blocked["status"] == "result_too_large"
        store.revoke_owner("grant")
        with pytest.raises(ValueError, match="account"):
            second.read("grant", result_id)
        assert second.compact({"data": "x" * 100000}, "other-grant")["status"] == "partial"
    finally:
        other.close()


def _fake_canvas():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from canvas_py._config import CanvasConfig
    return SimpleNamespace(config=CanvasConfig("https://school.example", "synthetic-token"), navigation=Mock())


def _pg_execute_action(dsn, key, namespace, action_id, entered, release, result, dispatches):
    from canvas_connector.actions import ActionStore
    persistence = open_store(dsn, key, namespace=namespace)
    actions = ActionStore(persistence=persistence, bind_credentials=False, execution_lease=5, heartbeat_interval=1, max_execution_seconds=20)
    def perform(*args):
        with dispatches.get_lock():
            dispatches.value += 1
        entered.set()
        if not release.wait(10):
            raise TimeoutError("test dispatch release missing")
        return {"verified": True}
    actions._perform = perform
    try:
        receipt = actions.execute(_fake_canvas(), "grant", action_id, lambda: None)
        result.put(receipt["state"])
    finally:
        persistence.close()


@pytest.mark.parametrize("interrupt", ["lease_expiry", "revoke"])
def test_postgres_action_process_claim_and_terminal_fencing(pg, interrupt):
    from canvas_connector.actions import ActionStore
    store, dsn, key, namespace = pg
    other = open_store(dsn, key, namespace=namespace)
    first = ActionStore(persistence=store, bind_credentials=False)
    second = ActionStore(persistence=other, bind_credentials=False)
    canvas = _fake_canvas()
    prepared = first.prepare(canvas, "grant", {"kind": "inbox_send", "recipients": [1], "subject": "Synthetic", "body": "Test only"})
    action_id = prepared["action_id"]
    assert second.status(canvas, "grant", action_id)["state"] == "prepared"
    context = mp.get_context("spawn")
    entered, release, result, dispatches = context.Event(), context.Event(), context.Queue(), context.Value("i", 0)
    worker = context.Process(target=_pg_execute_action, args=(dsn, key, namespace, action_id, entered, release, result, dispatches))
    try:
        worker.start()
        assert entered.wait(10)
        assert second.execute(canvas, "grant", action_id, lambda: None)["state"] == "executing"
        with pytest.raises(ValueError, match="executing"):
            second.cancel(canvas, "grant", action_id)
        with pytest.raises(ValueError, match="account"):
            second.status(canvas, "other-grant", action_id)
        if interrupt == "lease_expiry":
            other.update("canvas_action", action_id, lambda row: {**row, "lease_expires": 0})
            assert second.status(canvas, "grant", action_id)["state"] == "uncertain"
        else:
            other.revoke_owner("grant")
        release.set()
        assert result.get(timeout=15) == ("uncertain" if interrupt == "lease_expiry" else "revoked")
        _join([worker])
        assert dispatches.value == 1
        if interrupt == "lease_expiry":
            assert second.execute(canvas, "grant", action_id, lambda: None)["state"] == "uncertain"
        else:
            assert store.get("canvas_action", action_id) is None
    finally:
        release.set()
        if worker.is_alive():
            worker.terminate()
            worker.join(3)
        other.close()


@pytest.mark.parametrize("method", ["load_access_token", "get_credentials"])
def test_postgres_lock_wait_does_not_block_async_auth_event_loop(pg, method):
    import threading
    store, dsn, key, namespace = pg
    provider = _pg_provider(dsn, key, namespace)
    grant, tokens = _seed(provider)
    store.update("grant", grant, lambda row: {**row, "credential": {**row["credential"], "expires_at": time.time() + 3600}})
    entered, release = threading.Event(), threading.Event()
    def hold_database_lock():
        with store.transaction():
            entered.set()
            release.wait(2)
    holder = threading.Thread(target=hold_database_lock)
    holder.start()
    assert entered.wait(2)
    async def check():
        before = time.monotonic()
        task = asyncio.create_task(getattr(provider, method)(tokens.access_token))
        await asyncio.sleep(0.05)
        assert time.monotonic() - before < 0.5, "database lock blocked the HTTP event loop"
        assert not task.done()
        release.set()
        assert await task is not None
    try:
        asyncio.run(check())
    finally:
        release.set()
        holder.join(3)
        provider.store.close()
