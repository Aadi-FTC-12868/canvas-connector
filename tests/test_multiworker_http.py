"""Actual independent HTTP processes with a shared encrypted database."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import socket
import time

import httpx
from cryptography.fernet import Fernet

from canvas_connector.auth import CanvasOAuthProvider, Institution

BASE = "https://connector.example"


def _provider(database, key):
    return CanvasOAuthProvider(issuer_url=BASE, database_path=database, encryption_key=key,
        institutions=[Institution("https://school.example", "fixture-client", "fixture-secret")])


def _worker(database, key, port):
    import uvicorn
    from canvas_py import Canvas
    from canvas_connector.server import create_http_app
    import canvas_connector.security as security

    def factory(config):
        def upstream(request):
            if request.url.path == "/api/v1/users/self/profile":
                return httpx.Response(200, json={"id": 11 if config.token == "student-one" else 22, "name": "Synthetic student"})
            if request.url.path == "/api/v1/courses/1/assignments/2":
                return httpx.Response(200, json={"id": 2, "name": "Synthetic assignment",
                    "description": "Long synthetic material. " * 6000})
            raise AssertionError("Unexpected synthetic endpoint")
        canvas = Canvas(config, http_client=httpx.Client(transport=httpx.MockTransport(upstream)))
        canvas._http._owns_client = True
        return canvas

    security.guarded_canvas = factory
    provider = _provider(database, key)
    uvicorn.run(create_http_app(provider), host="127.0.0.1", port=port,
                access_log=False, log_level="error", proxy_headers=False)


def _port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_real_http_workers_share_continuations_actions_and_identity(tmp_path):
    database = str(tmp_path / "shared.db")
    key = Fernet.generate_key().decode()
    provider = _provider(database, key)
    tokens = []
    for index, token in enumerate(("student-one", "student-two")):
        grant = f"synthetic-grant-{index}"
        expires = time.time() + 3600
        provider.store.put("grant", grant, {"institution": "https://school.example", "user_id": str(index),
            "client_id": "fixture", "scopes": ["canvas:read", "canvas:write"], "expires": expires,
            "credential": {"access_token": token, "refresh_token": "unused", "expires_at": expires}},
            expires=expires, owner=grant)
        tokens.append(provider._issue(grant_id=grant, client_id="fixture", scopes=["canvas:read", "canvas:write"]).access_token)
    context = multiprocessing.get_context("spawn")
    ports = [_port(), _port()]
    workers = [context.Process(target=_worker, args=(database, key, port)) for port in ports]
    for worker in workers:
        worker.start()
    try:
        def call(worker, token, method, params=None):
            with httpx.Client(base_url=f"http://127.0.0.1:{ports[worker]}", trust_env=False, timeout=15) as client:
                result = client.post("/mcp", headers={"Host": "connector.example", "Authorization": "Bearer " + token,
                    "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"},
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
                assert result.status_code == 200, result.text
                return result.json()["result"]

        deadline = time.monotonic() + 20
        for port in ports:
            while True:
                try:
                    response = httpx.get(f"http://127.0.0.1:{port}/health", headers={"Host": "connector.example"}, timeout=0.5, trust_env=False)
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                assert time.monotonic() < deadline, "HTTP worker did not start"
                time.sleep(0.05)
        for index in range(2):
            initialized = call(index, tokens[0], "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "synthetic-multiworker-test", "version": "1"}})
            assert initialized["protocolVersion"] == "2025-11-25"

        def profile(index):
            token_index = index % 2
            result = call((index // 2) % 2, tokens[token_index], "tools/call", {"name": "canvas_connection_status"})
            assert result["structuredContent"]["data"]["user_id"] == (11 if token_index == 0 else 22)

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(profile, range(20)))

        large = call(0, tokens[0], "tools/call", {"name": "canvas_get_assignment", "arguments": {"course_id": 1, "assignment_id": 2}})["structuredContent"]
        assert large["status"] == "partial"
        cursor = large["data"]["next_read"]
        continued = call(1, tokens[0], "tools/call", {"name": "canvas_read_result", "arguments": {k: v for k, v in cursor.items() if k != "tool"}})
        assert not continued.get("isError")
        denied = call(1, tokens[1], "tools/call", {"name": "canvas_read_result", "arguments": {k: v for k, v in cursor.items() if k != "tool"}})
        assert denied["structuredContent"]["status"] == "invalid_request"

        prepared = call(0, tokens[0], "tools/call", {"name": "canvas_prepare_action", "arguments": {"action": {
            "kind": "inbox_send", "recipients": [9], "subject": "Fixture", "body": "This test must never send."}}})["structuredContent"]["data"]
        receipt = call(1, tokens[0], "tools/call", {"name": "canvas_action_status", "arguments": {"action_id": prepared["action_id"]}})
        assert receipt["structuredContent"]["data"]["state"] == "prepared"
        provider.store.revoke_owner("synthetic-grant-0")
        assert asyncio.run(provider.load_access_token(tokens[0])) is None
    finally:
        for worker in workers:
            worker.terminate()
            worker.join(5)
        provider.store.close()
