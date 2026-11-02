"""Measure local connector overhead with isolated synthetic accounts, never Canvas.

Starts loopback-only HTTP workers and mocks every upstream request. PostgreSQL is
optional via CONNECTOR_BENCHMARK_POSTGRES_URL; it uses a disposable random schema.
Results are a reproducible workload measurement, not a public capacity promise.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import math
import multiprocessing
import os
from pathlib import Path
import platform
import socket
import tempfile
import time
import uuid

import httpx
from cryptography.fernet import Fernet

from canvas_connector.auth import CanvasOAuthProvider, Institution
from canvas_connector.storage import Store

ORIGIN = "https://benchmark.example"


def provider(database, key, namespace):
    if namespace:
        from canvas_connector.postgres import PostgresStore
        storage = PostgresStore(database, key, namespace=namespace)
    else:
        storage = Store(database, key)
    return CanvasOAuthProvider(issuer_url=ORIGIN, store=storage,
        institutions=[Institution("https://synthetic.example", "synthetic-client", "synthetic-secret")])


def worker(database, key, namespace, port, delay_ms):
    import uvicorn
    from canvas_py import Canvas
    import canvas_connector.security as security
    from canvas_connector.server import create_http_app

    def synthetic_canvas(config):
        def upstream(request):
            if request.url.path != "/api/v1/users/self/profile":
                raise AssertionError("The benchmark permits only the synthetic profile endpoint")
            time.sleep(delay_ms / 1000)
            return httpx.Response(200, json={"id": int(config.token.removeprefix("synthetic-")),
                "name": "Synthetic benchmark account"})
        canvas = Canvas(config, http_client=httpx.Client(transport=httpx.MockTransport(upstream)))
        canvas._http._owns_client = True
        return canvas

    security.guarded_canvas = synthetic_canvas
    uvicorn.run(create_http_app(provider(database, key, namespace)), host="127.0.0.1", port=port,
        access_log=False, proxy_headers=False, log_level="error")


def free_port():
    with socket.socket() as socket_:
        socket_.bind(("127.0.0.1", 0))
        return socket_.getsockname()[1]


def percentile(values, quantile):
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(quantile * len(ordered)) - 1)] * 1000, 2) if ordered else None


async def measure(ports, tokens, count, concurrency):
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(timeout=30, trust_env=False, limits=limits) as client:
        async def call(index, method, params):
            token = tokens[index % len(tokens)]
            selected = (index + index // len(tokens)) % len(ports)
            return await client.post(f"http://127.0.0.1:{ports[selected]}/mcp",
                headers={"Host": "benchmark.example", "Authorization": "Bearer " + token,
                    "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"},
                json={"jsonrpc": "2.0", "id": index, "method": method, "params": params})

        for index in range(len(ports)):
            initialized = await call(index, "initialize", {"protocolVersion": "2025-11-25",
                "capabilities": {}, "clientInfo": {"name": "synthetic-load-check", "version": "1"}})
            initialized.raise_for_status()
            assert initialized.json()["result"]["protocolVersion"] == "2025-11-25"
        limiter = asyncio.Semaphore(concurrency)
        latencies, errors = [], Counter()
        successful = 0

        async def sample(index):
            nonlocal successful
            async with limiter:
                began = time.perf_counter()
                try:
                    response = await call(index, "tools/call", {"name": "canvas_connection_status", "arguments": {}})
                    if response.status_code != 200:
                        errors[f"http_{response.status_code}"] += 1
                        return
                    result = response.json().get("result", {}).get("structuredContent", {})
                    if result.get("status") != "ok":
                        errors["tool_error"] += 1
                    elif result.get("data", {}).get("user_id") != index % len(tokens) + 1:
                        errors["identity_mismatch"] += 1
                    else:
                        successful += 1
                except (httpx.HTTPError, ValueError, KeyError):
                    errors["transport_or_protocol"] += 1
                finally:
                    latencies.append(time.perf_counter() - began)

        started = time.perf_counter()
        await asyncio.gather(*(sample(index) for index in range(count)))
        elapsed = time.perf_counter() - started
        return {"attempted": count, "successful": successful, "errors": dict(errors),
            "elapsed_seconds": round(elapsed, 3), "successful_requests_per_second": round(successful / elapsed, 2),
            "request_latency_ms": {"p50": percentile(latencies, .5), "p95": percentile(latencies, .95),
                "p99": percentile(latencies, .99)}, "queue_timing": "includes server queueing; excludes client semaphore wait"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=2, choices=range(1, 9))
    parser.add_argument("--concurrency", type=int, default=32, choices=range(1, 129))
    parser.add_argument("--accounts", type=int, default=64, choices=range(1, 129))
    parser.add_argument("--requests", type=int, default=1000, choices=range(1, 10001))
    parser.add_argument("--upstream-delay-ms", type=int, default=20, choices=range(0, 1001))
    parser.add_argument("--database", choices=["sqlite", "postgres"], default="sqlite")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.requests / args.accounts >= 590:
        parser.error("Use more accounts or fewer requests to stay below per-grant rate limits")
    dsn = os.environ.get("CONNECTOR_BENCHMARK_POSTGRES_URL")
    if args.database == "postgres" and not dsn:
        parser.error("Set CONNECTOR_BENCHMARK_POSTGRES_URL to a disposable PostgreSQL database")
    namespace = "canvas_bench_" + uuid.uuid4().hex if args.database == "postgres" else None
    key = Fernet.generate_key().decode()
    context = multiprocessing.get_context("spawn")
    processes = []
    auth = None
    with tempfile.TemporaryDirectory(prefix="canvas-benchmark-") as directory:
        database = dsn if namespace else str(Path(directory) / "synthetic.db")
        try:
            auth = provider(database, key, namespace)
            tokens, expires = [], time.time() + 3600
            for index in range(args.accounts):
                grant = "synthetic-grant-" + str(index)
                auth.store.put("grant", grant, {"institution": "https://synthetic.example", "user_id": str(index + 1),
                    "client_id": "synthetic-client", "scopes": ["canvas:read"], "expires": expires,
                    "credential": {"access_token": f"synthetic-{index + 1}", "expires_at": expires}}, expires=expires, owner=grant)
                tokens.append(auth._issue(grant_id=grant, client_id="synthetic-client", scopes=["canvas:read"]).access_token)
            ports = []
            while len(ports) < args.workers:
                port = free_port()
                if port not in ports:
                    ports.append(port)
            for port in ports:
                process = context.Process(target=worker, args=(database, key, namespace, port, args.upstream_delay_ms))
                process.start()
                processes.append(process)
            deadline = time.monotonic() + 30
            for port in ports:
                while True:
                    try:
                        response = httpx.get(f"http://127.0.0.1:{port}/health", headers={"Host": "benchmark.example"}, timeout=.5, trust_env=False)
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Synthetic worker failed to become ready")
                    time.sleep(.1)
            result = {"schema_version": "1", "workload": "synthetic authenticated MCP profile reads",
                "database": args.database, "workers": args.workers, "concurrency": args.concurrency,
                "accounts": args.accounts, "upstream_delay_ms": args.upstream_delay_ms,
                "environment": {"os": platform.system(), "architecture": platform.machine(), "python": platform.python_version()},
                **asyncio.run(measure(ports, tokens, args.requests, args.concurrency))}
            encoded = json.dumps(result, indent=2) + "\n"
            print(encoded, end="")
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(encoded)
            if result["errors"]:
                raise SystemExit(1)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(5)
                if process.is_alive():
                    process.kill()
                    process.join(5)
            if auth:
                auth.store.close()
            if namespace:
                import psycopg
                from psycopg import sql
                with psycopg.connect(dsn, autocommit=True) as connection:
                    connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(namespace)))


if __name__ == "__main__":
    main()
