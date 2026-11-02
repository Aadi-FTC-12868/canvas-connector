"""Cross-process runtime properties, using synthetic course data only."""
from __future__ import annotations

import json
import multiprocessing
import time

import pytest
from cryptography.fernet import Fernet
from starlette.testclient import TestClient

from canvas_connector.auth import CanvasOAuthProvider, Institution
from canvas_connector.responses import ResponseStore, envelope
from canvas_connector.server import create_http_app
from canvas_connector.storage import Store


def _read_continuation(database, key, result_id, output):
    with_store = Store(database, key)
    try:
        replies = ResponseStore(persistence=with_store)
        parts = []
        cursor = {"offset": 0, "max_chars": 40000}
        while cursor:
            result = replies.read("student-a", result_id, cursor["offset"], cursor["max_chars"])
            parts.append(result["data"]["text"])
            cursor = result["data"]["next_read"]
        output.put(json.loads("".join(parts)))
    finally:
        with_store.close()


def test_large_result_can_continue_in_another_process(tmp_path):
    key = Fernet.generate_key()
    store = Store(tmp_path / "shared.db", key)
    value = envelope({"instructions": "Synthetic course text. " * 9000})
    first = ResponseStore(persistence=store).compact(value, "student-a")
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    process = context.Process(target=_read_continuation,
        args=(str(tmp_path / "shared.db"), key, first["data"]["result_id"], queue))
    process.start()
    try:
        assert queue.get(timeout=15) == value
        process.join(10)
        assert process.exitcode == 0
        assert b"Synthetic course text" not in (tmp_path / "shared.db").read_bytes()
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        store.close()


def test_shared_continuation_budget_and_revocation(tmp_path):
    key = Fernet.generate_key()
    left = Store(tmp_path / "shared.db", key)
    right = Store(tmp_path / "shared.db", key)
    try:
        original = envelope({"data": "X" * 800})
        first = ResponseStore(persistence=left, budget=1400, inline_chars=100).compact(original, "a")
        assert first["data"]["encoding"] == "json_text"
        second = ResponseStore(persistence=right, budget=1400, inline_chars=100).compact(original, "b")
        assert second["status"] == "result_too_large"
        with pytest.raises(ValueError):
            ResponseStore(persistence=right).read("b", first["data"]["result_id"])
        left.revoke_owner("a")
        with pytest.raises(ValueError):
            ResponseStore(persistence=right).read("a", first["data"]["result_id"])
        assert ResponseStore(persistence=right, budget=1400, inline_chars=100).compact(original, "b")["status"] == "ok"
    finally:
        left.close()
        right.close()


def test_rate_budget_is_shared_by_independent_apps(tmp_path):
    key = Fernet.generate_key().decode()
    providers = [CanvasOAuthProvider(issuer_url="https://connector.example", database_path=tmp_path / "shared.db",
        encryption_key=key, institutions=[Institution("https://school.example", "client", "synthetic")]) for _ in range(2)]
    try:
        with TestClient(create_http_app(providers[0]), base_url="https://connector.example") as first, \
                TestClient(create_http_app(providers[1]), base_url="https://connector.example") as second:
            for _ in range(60):
                assert first.get("/health").status_code == 200
                assert second.get("/health").status_code == 200
            assert first.get("/health").status_code == 429
            assert second.get("/health").status_code == 429
    finally:
        for provider in providers:
            provider.store.close()


def test_inflight_principal_rechecks_revocation_before_dispatch(tmp_path):
    from canvas_connector.server import Principal, _authorize_write, _principal
    key = Fernet.generate_key().decode()
    provider = CanvasOAuthProvider(issuer_url="https://connector.example", database_path=tmp_path / "shared.db",
        encryption_key=key, institutions=[Institution("https://school.example", "client", "synthetic")])
    other_worker = Store(tmp_path / "shared.db", key)
    expires = time.time() + 3600
    provider.store.put("grant", "student", {"institution": "https://school.example", "user_id": "11",
        "client_id": "fixture", "scopes": ["canvas:read", "canvas:write"], "expires": expires,
        "credential": {"access_token": "synthetic", "expires_at": expires}}, expires=expires, owner="student")
    token = provider._issue(grant_id="student", client_id="fixture", scopes=["canvas:read", "canvas:write"])
    context = _principal.set(Principal(None, "student", ["canvas:read", "canvas:write"],
        validate_access=lambda: provider.validate_access(token.access_token)))
    try:
        _authorize_write()
        other_worker.revoke_owner("student")
        with pytest.raises(PermissionError, match="revoked"):
            _authorize_write()
    finally:
        _principal.reset(context)
        provider.store.close()
        other_worker.close()
