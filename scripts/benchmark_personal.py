"""Measure a personal MCP server with one synthetic owner and five app grants.

Runs one loopback-only worker with temporary encrypted SQLite. Every Canvas
request uses a synthetic HTTP transport, and the worker rejects non-loopback
socket connections. Results describe this machine and this workload, not a VPS
capacity promise. Credentials, course data, and production configuration are
never loaded. OAuth tokens are created through provider APIs before measurement;
the benchmark does not measure a browser login or a real assistant connection.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import logging
import math
import multiprocessing
from pathlib import Path
import platform
import resource
import socket
import tempfile
import threading
import time

import httpx
from cryptography.fernet import Fernet

ORIGIN = "https://personal-benchmark.example"
CANVAS = "https://synthetic-canvas.example"
PAT = "synthetic-personal-benchmark-canvas-token"
EMAIL = "owner@example.test"
PASSWORD = "Synthetic benchmark owner password!"
USER_ID = 41
APP_COUNT = 5
CONCURRENCIES = (1, 4, 8, 16)
LARGE_BODY = "Synthetic assignment instructions. " * 2400


class Upstream:
    def __init__(self, delay_ms):
        self.delay = delay_ms / 1000
        self.lock = threading.Lock()
        self.active = 0
        self.reset()

    def reset(self):
        with self.lock:
            if self.active:
                raise RuntimeError("Cannot reset metrics during an upstream request")
            self.calls = Counter()
            self.maximum = 0
            self.throttled = 0
            self.throttle_remaining = 0

    def snapshot(self):
        with self.lock:
            return {"requests": sum(self.calls.values()), "by_endpoint": dict(self.calls),
                    "maximum_concurrent": self.maximum, "throttled_responses": self.throttled,
                    "non_get_requests": sum(v for k, v in self.calls.items() if not k.startswith("GET "))}

    def __call__(self, request):
        if request.url.host != "synthetic-canvas.example" or request.headers.get("authorization") != "Bearer " + PAT:
            raise AssertionError("Unexpected synthetic upstream identity")
        if request.method != "GET":
            raise AssertionError("Benchmark must never dispatch a Canvas mutation")
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.calls[request.method + " " + request.url.path] += 1
            throttle = self.throttle_remaining > 0
            if throttle:
                self.throttle_remaining -= 1
                self.throttled += 1
        try:
            time.sleep(self.delay)
            if throttle:
                return httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "Rate Limit Exceeded"})
            if request.url.path == "/api/v1/users/self/profile":
                return httpx.Response(200, json={"id": USER_ID, "name": "Synthetic owner"})
            if request.url.path == "/api/v1/courses/1/assignments":
                page = int(request.url.params.get("page", "1"))
                if page not in (1, 2, 3):
                    raise AssertionError("Unexpected synthetic pagination")
                items = [{"id": index, "course_id": 1, "name": f"Synthetic assignment {index}",
                          "submission_types": ["online_text_entry"], "published": True}
                         for index in range((page - 1) * 10 + 1, page * 10 + 1)]
                headers = {"Link": f'<{CANVAS}/api/v1/courses/1/assignments?page={page + 1}>; rel="next"'} if page < 3 else {}
                return httpx.Response(200, json=items, headers=headers)
            if request.url.path == "/api/v1/courses/1/assignments/1":
                return httpx.Response(200, json={"id": 1, "course_id": 1, "name": "Synthetic long assignment",
                                                "description": LARGE_BODY, "submission_types": ["online_text_entry"]})
            raise AssertionError("Unexpected synthetic upstream endpoint")
        finally:
            with self.lock:
                self.active -= 1


def synthetic_client(upstream, *, base_url="", timeout=30, request_gate=None):
    transport = httpx.MockTransport(upstream)
    if request_gate is not None:
        transport = request_gate.wrap(transport)
    return httpx.Client(base_url=base_url, transport=transport, timeout=timeout,
                        follow_redirects=False, trust_env=False)


def make_provider(database, key, upstream):
    from canvas_connector.personal import PersonalProvider
    return PersonalProvider(issuer_url=ORIGIN, database_path=database, encryption_key=key,
                            http_client_factory=lambda: synthetic_client(upstream))


def prevent_external_sockets():
    original = socket.socket.connect

    def connect(sock, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise RuntimeError("External networking is disabled in this synthetic worker")
        return original(sock, address)

    socket.socket.connect = connect


def worker(database, key, port, delay_ms):
    import uvicorn
    from starlette.responses import JSONResponse
    import canvas_connector.security as security
    from canvas_connector.server import create_http_app

    prevent_external_sockets()
    upstream = Upstream(delay_ms)
    security.secure_http_client = lambda **kwargs: synthetic_client(upstream, **kwargs)
    provider = make_provider(database, key, upstream)
    app = create_http_app(provider)
    for name in ("mcp.server.streamable_http", "mcp.server.streamable_http_manager"):
        logging.getLogger(name).setLevel(logging.WARNING)

    async def measured_app(scope, receive, send):
        # These synthetic controls exist only in this loopback benchmark wrapper,
        # never in the production application or its routes.
        if scope["type"] == "http" and scope["path"].startswith("/_benchmark/"):
            if scope["path"] == "/_benchmark/reset" and scope["method"] == "POST":
                upstream.reset()
            elif scope["path"] == "/_benchmark/throttle" and scope["method"] == "POST":
                with upstream.lock:
                    upstream.throttle_remaining = 1
            elif scope["path"] != "/_benchmark/metrics" or scope["method"] != "GET":
                await JSONResponse({"error": "not_found"}, 404)(scope, receive, send)
                return
            metrics = upstream.snapshot()
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            metrics["process_peak_rss_mib"] = round(rss / (1024 * 1024 if platform.system() == "Darwin" else 1024), 2)
            await JSONResponse(metrics)(scope, receive, send)
            return
        await app(scope, receive, send)

    try:
        uvicorn.run(measured_app, host="127.0.0.1", port=port, access_log=False,
                    proxy_headers=False, log_level="critical")
    finally:
        provider.store.close()


def seed(provider, owner_id):
    from mcp.shared.auth import OAuthClientInformationFull

    session = provider.accounts.login(EMAIL, PASSWORD, "synthetic-operator")
    provider.save_connection(session, CANVAS, PAT, provider._connection_version(owner_id))
    connection = provider._connection(owner_id)
    tokens, grant_ids = [], []
    for index in range(APP_COUNT):
        client_id = f"synthetic-app-{index}"
        redirect = f"https://assistant-{index}.example/callback"
        client = OAuthClientInformationFull(client_id=client_id, client_name=f"Synthetic assistant {index + 1}",
            redirect_uris=[redirect], token_endpoint_auth_method="none", grant_types=["authorization_code", "refresh_token"],
            response_types=["code"], scope="canvas:read canvas:write")
        asyncio.run(provider.register_client(client))
        pending = {"client_id": client_id, "client_name": client.client_name,
                   "params": {"redirect_uri": redirect, "redirect_uri_provided_explicitly": True,
                              "code_challenge": "A" * 43, "scopes": ["canvas:read", "canvas:write"]}}
        code = provider._create_account_grant(pending, connection, ["canvas:read"])
        grant_id = provider.store.get("code", code)["grant_id"]
        provider.store.delete("code", code)
        tokens.append(provider._issue(grant_id=grant_id, client_id=client_id, scopes=["canvas:read"]).access_token)
        grant_ids.append(grant_id)
    return session, tokens, grant_ids


def percentile(values, quantile):
    values = sorted(values)
    return round(values[max(0, math.ceil(quantile * len(values)) - 1)] * 1000, 2) if values else None


class Runner:
    def __init__(self, client, tokens):
        self.client, self.tokens = client, tokens
        self.request_id = 0
        self.continuations = []

    async def rpc(self, app, method, params):
        self.request_id += 1
        return await self.client.post("/mcp", headers={"Authorization": "Bearer " + self.tokens[app],
            "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"},
            json={"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params})

    async def tool(self, app, name, arguments):
        return await self.rpc(app, "tools/call", {"name": name, "arguments": arguments})

    @staticmethod
    def result(response):
        response.raise_for_status()
        result = response.json().get("result", {})
        if result.get("isError"):
            raise ValueError("MCP tool returned an error")
        return result["structuredContent"]

    async def prepare(self):
        for app in range(APP_COUNT):
            response = await self.rpc(app, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "synthetic-personal-load", "version": "1"}})
            response.raise_for_status()
            assert response.json()["result"]["protocolVersion"] == "2025-11-25"
            first = self.result(await self.tool(app, "canvas_get_assignment", {"course_id": 1, "assignment_id": 1}))
            assert first["status"] == "partial" and first["data"]["encoding"] == "json_text"
            windows, text, current = [], "", first
            while True:
                data = current["data"]
                windows.append({"result_id": data["result_id"], "offset": current["coverage"]["offset"],
                                "max_chars": 40000, "text": data["text"], "sha256": data["sha256"]})
                text += data["text"]
                cursor = data["next_read"]
                if not cursor:
                    break
                current = self.result(await self.tool(app, cursor["tool"], {k: v for k, v in cursor.items() if k != "tool"}))
            assert hashlib.sha256(text.encode()).hexdigest() == first["data"]["sha256"]
            assert json.loads(text)["data"]["description"] == LARGE_BODY
            self.continuations.append(windows)

    async def phase(self, count, concurrency):
        await self.client.post("/_benchmark/reset")
        semaphore = asyncio.Semaphore(concurrency)
        errors, workload = Counter(), Counter()
        durations, successful = [], 0

        async def sample(index):
            nonlocal successful
            app = index % APP_COUNT
            kind = ("profile", "profile", "paginated_navigation", "continuation")[index % 4]
            workload[kind] += 1
            async with semaphore:
                started = time.perf_counter()
                try:
                    if kind == "profile":
                        result = self.result(await self.tool(app, "canvas_connection_status", {}))
                        assert result["status"] == "ok" and result["data"]["user_id"] == USER_ID
                    elif kind == "paginated_navigation":
                        result = self.result(await self.tool(app, "canvas_browse_course", {
                            "course_id": 1, "section": "assignments", "limit": 30, "refresh": True}))
                        data = result["data"]
                        assert result["status"] == "ok"
                        assert data["total"] == 30 and len(data["items"]) == 30
                        assert data["next_offset"] is None and result["coverage"]["complete"] is True
                    else:
                        expected = self.continuations[app][(index // 4) % len(self.continuations[app])]
                        arguments = {key: expected[key] for key in ("result_id", "offset", "max_chars")}
                        result = self.result(await self.tool(app, "canvas_read_result", arguments))
                        assert result["status"] in {"ok", "partial"}
                        assert result["data"]["text"] == expected["text"] and result["data"]["sha256"] == expected["sha256"]
                    successful += 1
                except httpx.HTTPStatusError as error:
                    errors[f"http_{error.response.status_code}"] += 1
                except (AssertionError, ValueError, KeyError, httpx.HTTPError):
                    errors["identity_content_or_protocol"] += 1
                finally:
                    durations.append(time.perf_counter() - started)

        began = time.perf_counter()
        await asyncio.gather(*(sample(index) for index in range(count)))
        elapsed = time.perf_counter() - began
        metrics = (await self.client.get("/_benchmark/metrics")).json()
        expected_upstream = workload["profile"] + workload["paginated_navigation"] * 3
        if metrics["requests"] != expected_upstream:
            errors["unexpected_upstream_amplification"] += 1
        if metrics["non_get_requests"]:
            errors["unexpected_mutation"] += 1
        if metrics["maximum_concurrent"] > 4:
            errors["shared_upstream_limit_exceeded"] += 1
        return {"concurrency": concurrency, "attempted_mcp_calls": count, "successful_mcp_calls": successful,
                "workload": dict(workload), "errors": dict(errors), "elapsed_seconds": round(elapsed, 3),
                "mcp_calls_per_second": round(successful / elapsed, 2),
                "latency_ms": {"p50": percentile(durations, .5), "p95": percentile(durations, .95),
                               "p99": percentile(durations, .99)}, "upstream": metrics,
                "expected_upstream_requests": expected_upstream,
                "latency_scope": "Includes server queueing, excludes client semaphore wait"}

    async def safety_checks(self, provider, session, grant_ids):
        before = (await self.client.get("/_benchmark/metrics")).json()["requests"]
        denied = await self.tool(0, "canvas_submit_assignment", {"action_id": "synthetic-unknown-action"})
        assert denied.status_code == 200 and denied.json()["result"]["isError"] is True
        assert "read-only" in json.dumps(denied.json())
        foreign = self.continuations[0][0]
        rejected = self.result(await self.tool(1, "canvas_read_result", {
            key: foreign[key] for key in ("result_id", "offset", "max_chars")}))
        assert rejected["status"] not in {"ok", "partial"}
        assert (await self.client.get("/_benchmark/metrics")).json()["requests"] == before
        await self.client.post("/_benchmark/reset")
        await self.client.post("/_benchmark/throttle")
        retried = self.result(await self.tool(1, "canvas_connection_status", {}))
        assert retried["data"]["user_id"] == USER_ID
        retry_metrics = (await self.client.get("/_benchmark/metrics")).json()
        assert retry_metrics["requests"] == 2 and retry_metrics["throttled_responses"] == 1
        provider.revoke_connection(session, grant_ids[0])
        revoked = await self.tool(0, "canvas_connection_status", {})
        assert revoked.status_code == 401
        surviving = self.result(await self.tool(1, "canvas_connection_status", {}))
        assert surviving["data"]["user_id"] == USER_ID
        return {"default_write_denied_without_upstream_request": True,
                "continuation_isolated_between_app_grants": True, "one_read_throttle_retried_once": True,
                "revoked_app_rejected_401": True, "other_app_still_authorized": True}


async def run_measurements(port, tokens, count, provider, session, grant_ids):
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers={"Host": "personal-benchmark.example"},
            timeout=60, trust_env=False, limits=httpx.Limits(max_connections=16, max_keepalive_connections=16)) as client:
        runner = Runner(client, tokens)
        await runner.prepare()
        phases = []
        for index, concurrency in enumerate(CONCURRENCIES):
            size = count // len(CONCURRENCIES) + (index < count % len(CONCURRENCIES))
            phases.append(await runner.phase(size, concurrency))
        checks = await runner.safety_checks(provider, session, grant_ids)
        return phases, checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=1000, choices=range(16, 2001),
                        help="Total measured MCP calls split over concurrency 1/4/8/16; preparation/check calls are extra")
    parser.add_argument("--upstream-delay-ms", type=int, default=20, choices=range(0, 1001))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    from canvas_connector.personal_admin import initialize_owner
    from canvas_connector.storage import Store

    key = Fernet.generate_key().decode()
    context = multiprocessing.get_context("spawn")
    process, provider = None, None
    with tempfile.TemporaryDirectory(prefix="canvas-personal-benchmark-") as directory:
        database = str(Path(directory) / "synthetic.sqlite3")
        try:
            storage = Store(database, key)
            owner_id = initialize_owner(storage, EMAIL, PASSWORD)
            storage.close()
            provider = make_provider(database, key, Upstream(0))
            session, tokens, grants = seed(provider, owner_id)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            process = context.Process(target=worker, args=(database, key, port, args.upstream_delay_ms))
            process.start()
            deadline = time.monotonic() + 30
            while True:
                try:
                    response = httpx.get(f"http://127.0.0.1:{port}/health", headers={"Host": "personal-benchmark.example"},
                                         timeout=.5, trust_env=False)
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if not process.is_alive() or time.monotonic() >= deadline:
                    raise RuntimeError("Synthetic personal worker did not become ready")
                time.sleep(.1)
            phases, checks = asyncio.run(run_measurements(port, tokens, args.requests, provider, session, grants))
            result = {"schema_version": "1", "workload": "One synthetic owner, five read-only app grants, real loopback MCP HTTP",
                "database": "temporary encrypted SQLite", "workers": 1, "owners": 1, "app_grants": APP_COUNT,
                "upstream_delay_ms": args.upstream_delay_ms, "measured_mcp_calls": args.requests,
                "preparation": "Provider APIs create owner/connection/grants; five oversized assignments are reconstructed before measurement",
                "continuations": "Measured reads revisit exact validated stored windows; they do not repeatedly create large results",
                "environment": {"os": platform.system(), "architecture": platform.machine(), "python": platform.python_version()},
                "phases": phases, "checks": checks, "external_network": "Worker socket connections restricted to loopback",
                "limits": "Synthetic local overhead only; no TLS/DNS latency, real Canvas quota, document extraction, VPS CPU/memory limit, or host-app connection measured"}
            encoded = json.dumps(result, indent=2) + "\n"
            print(encoded, end="")
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(encoded)
            if any(phase["errors"] for phase in phases):
                raise SystemExit(1)
        finally:
            if process:
                if process.is_alive():
                    process.terminate()
                process.join(5)
                if process.is_alive():
                    process.kill()
                    process.join(5)
            if provider:
                provider.store.close()


if __name__ == "__main__":
    main()
