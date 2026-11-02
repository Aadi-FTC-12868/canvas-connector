"""Real SDK processes, both wire eras, and real core action receipts."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest

from canvas_tunnel.bridge import child_environment
from canvas_tunnel.transport import MAX_MESSAGE_BYTES

FIXTURES = Path(__file__).parent / "fixtures"
ACTION = {"kind": "dashboard_reorder", "course_ids": [3, 1, 2]}
META = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "synthetic-audit", "version": "1"}}


class Peer:
    def __init__(self, process, modern, folder):
        self.process, self.modern, self.folder = process, modern, folder
        self.counter = 100

    async def send(self, method, params=None, request_id=None):
        self.counter += 1
        request_id = self.counter if request_id is None else request_id
        params = dict(params or {})
        if self.modern:
            params["_meta"] = META
        raw = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        self.process.stdin.write(json.dumps(raw, ensure_ascii=False).encode() + b"\n")
        await self.process.stdin.drain()
        return request_id

    async def read(self, timeout=12):
        line = await asyncio.wait_for(self.process.stdout.readline(), timeout)
        if not line:
            error = await self.process.stderr.read()
            raise AssertionError(f"Facade ended without a response: {error.decode()}")
        assert len(line) <= MAX_MESSAGE_BYTES + 1
        return json.loads(line)

    async def request(self, method, params=None, request_id=None):
        sent = await self.send(method, params, request_id)
        response = await self.read()
        assert response["id"] == sent
        return response

    async def call(self, name, arguments=None):
        response = await self.request("tools/call", {"name": name, "arguments": arguments or {}})
        assert "result" in response, response
        return response["result"]

    async def handshake(self):
        if self.modern:
            return None
        response = await self.request("initialize", {"protocolVersion": "2025-11-25",
            "capabilities": {}, "clientInfo": {"name": "synthetic-audit", "version": "1"}})
        assert response["result"]["protocolVersion"] == "2025-11-25"
        self.process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        await self.process.stdin.drain()
        return response["result"]

    def controls(self, **values):
        (self.folder / "controls.json").write_text(json.dumps(values))

    def requests(self):
        path = self.folder / "requests.jsonl"
        return [json.loads(row) for row in path.read_text().splitlines()] if path.exists() else []

    def writes(self):
        return [row for row in self.requests() if row["method"] != "GET"]


@asynccontextmanager
async def peer(core_python, folder, *, modern=True, mode="writes", timeout=105, direct=False):
    folder.mkdir(parents=True, exist_ok=True)
    env = child_environment()
    # Synthetic sentinels must be removed before the connector is spawned.
    env.update(OPENAI_API_KEY="synthetic-openai-secret", MCP_TUNNEL_API_KEY="synthetic-control-secret",
               CONNECTOR_ENCRYPTION_KEY="synthetic-hosted-secret")
    command = ([core_python, str(FIXTURES / "legacy_child.py"), str(folder), mode] if direct else
               [sys.executable, str(FIXTURES / "run_facade.py"), core_python, str(folder), mode, str(timeout)])
    process = await asyncio.create_subprocess_exec(*command, env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, limit=MAX_MESSAGE_BYTES + 65536, start_new_session=True)
    current = Peer(process, modern, folder)
    try:
        yield current
    finally:
        if process.returncode is None:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()


async def wait_for(path, timeout=10):
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, str(path)
        await asyncio.sleep(.02)


@pytest.mark.parametrize("modern", [True, False])
async def test_exact_catalog_and_structured_capabilities_both_eras(core_python, tmp_path, modern):
    async with peer(core_python, tmp_path / "direct", direct=True, modern=False) as direct:
        initial = await direct.handshake()
        expected = (await direct.request("tools/list"))["result"]
        expected_caps = (await direct.call("canvas_capabilities"))["structuredContent"]["data"]
    async with peer(core_python, tmp_path / "facade", modern=modern) as bridge:
        advertised = await bridge.handshake()
        if modern:
            advertised = (await bridge.request("server/discover"))["result"]
            assert advertised["supportedVersions"] == ["2026-07-28"]
        listing = (await bridge.request("tools/list"))["result"]
        assert listing["tools"] == expected["tools"]
        assert advertised["capabilities"] == initial["capabilities"]
        tools = {t["name"]: t for t in listing["tools"]}
        assert tools["canvas_dashboard_cards"]["annotations"]["readOnlyHint"] is True
        assert tools["canvas_reorder_dashboard"]["annotations"]["readOnlyHint"] is False
        result = await bridge.call("canvas_capabilities")
        assert result["structuredContent"]["data"] == expected_caps
        assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
        assert bridge.requests() == []
        child_env = json.loads((bridge.folder / "environment.json").read_text())
        assert not any(k.startswith(("OPENAI_", "MCP_", "CONNECTOR_")) for k in child_env)


async def test_modern_self_contained_call_needs_no_discovery_or_initialize(core_python, tmp_path):
    async with peer(core_python, tmp_path) as bridge:
        response = await bridge.request("tools/call", {"name": "canvas_capabilities", "arguments": {}},
                                        request_id="exact-caller-id")
        assert response["id"] == "exact-caller-id"
        assert response["result"]["structuredContent"]["status"] == "ok"
        assert bridge.requests() == []


@pytest.mark.parametrize("modern", [True, False])
@pytest.mark.parametrize("mode", ["readonly", "writes"])
async def test_prepared_action_write_gate_exact_saved_receipt_and_terminal_replay(core_python, tmp_path, modern, mode):
    async with peer(core_python, tmp_path, modern=modern, mode=mode) as bridge:
        await bridge.handshake()
        read = (await bridge.call("canvas_dashboard_cards"))["structuredContent"]
        assert read["data"]["course_ids"] == [2, 1, 3]
        prepared = (await bridge.call("canvas_prepare_action", {"action": ACTION}))["structuredContent"]
        action_id = prepared["data"]["action_id"]
        assert not bridge.writes()
        outcome = await bridge.call("canvas_reorder_dashboard", {"action_id": action_id})
        if mode == "readonly":
            assert outcome["isError"] is True and "Writes are disabled" in str(outcome)
            assert not bridge.writes()
        else:
            assert outcome["structuredContent"]["status"] == "succeeded"
            assert outcome["structuredContent"]["data"]["result"]["verified"] is True
            replay = await bridge.call("canvas_reorder_dashboard", {"action_id": action_id})
            assert replay["structuredContent"]["data"] == outcome["structuredContent"]["data"]
            assert len(bridge.writes()) == 1


async def test_concurrent_request_ids_do_not_cross_correlate(core_python, tmp_path):
    async with peer(core_python, tmp_path) as bridge:
        await bridge.send("tools/call", {"name": "canvas_capabilities"}, "catalog")
        await bridge.send("tools/call", {"name": "canvas_dashboard_cards"}, 987)
        responses = {r["id"]: r for r in [await bridge.read(), await bridge.read()]}
        assert "actions" in responses["catalog"]["result"]["structuredContent"]["data"]
        assert responses[987]["result"]["structuredContent"]["data"]["course_ids"] == [2, 1, 3]


async def test_denied_read_and_large_result_continuation_remain_honest(core_python, tmp_path):
    async with peer(core_python, tmp_path) as bridge:
        bridge.controls(denied=True)
        denied = (await bridge.call("canvas_dashboard_cards"))["structuredContent"]
        assert denied["status"] == "forbidden" and denied["coverage"]["complete"] is False
        assert "synthetic-upstream-private-secret" not in str(denied)
        bridge.controls(large_name=True)
        large = (await bridge.call("canvas_connection_status"))["structuredContent"]
        assert large["coverage"]["complete"] is False
        tail, chunks = large, [large["data"]["text"]]
        while tail["data"]["next_read"]:
            next_read = tail["data"]["next_read"]
            tail = (await bridge.call(next_read["tool"],
                {k: v for k, v in next_read.items() if k != "tool"}))["structuredContent"]
            chunks.append(tail["data"]["text"])
            assert len(chunks) < 10
        assert hashlib.sha256("".join(chunks).encode()).hexdigest() == large["data"]["sha256"]
        assert not bridge.writes()


async def test_cancellation_after_apply_does_not_claim_rollback_or_retry(core_python, tmp_path):
    async with peer(core_python, tmp_path) as bridge:
        prepared = (await bridge.call("canvas_prepare_action", {"action": ACTION}))["structuredContent"]
        action_id = prepared["data"]["action_id"]
        bridge.controls(pause_after_write=1)
        await bridge.send("tools/call", {"name": "canvas_reorder_dashboard", "arguments": {"action_id": action_id}}, "cancel-write")
        await wait_for(tmp_path / "mutation_applied")
        bridge.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
            "params": {"requestId": "cancel-write", "reason": "Synthetic caller cancellation", "_meta": META}}).encode() + b"\n")
        await bridge.process.stdin.drain()
        await asyncio.sleep(1.3)
        status = (await bridge.call("canvas_action_status", {"action_id": action_id}))["structuredContent"]
        assert status["data"]["state"] == "succeeded"
        assert len(bridge.writes()) == 1
        assert json.loads((tmp_path / "positions.json").read_text())["course_3"] == 1
        # Reading status received its own ID, not a fabricated response to the cancelled ID.


async def test_timeout_after_apply_is_uncertain_and_never_replayed(core_python, tmp_path):
    async with peer(core_python, tmp_path, timeout=1) as bridge:
        prepared = (await bridge.call("canvas_prepare_action", {"action": ACTION}))["structuredContent"]
        action_id = prepared["data"]["action_id"]
        bridge.controls(pause_after_write=2)
        response = await bridge.request("tools/call", {"name": "canvas_reorder_dashboard", "arguments": {"action_id": action_id}})
        assert response["error"]["code"] == -32001
        assert "may have completed" in response["error"]["message"]
        assert len(bridge.writes()) == 1
        await asyncio.sleep(1.3)
        status = (await bridge.call("canvas_action_status", {"action_id": action_id}))["structuredContent"]
        assert status["data"]["state"] == "succeeded"
        assert len(bridge.writes()) == 1


async def test_child_exit_ends_facade_and_restart_loses_old_receipts_without_replay(core_python, tmp_path):
    async with peer(core_python, tmp_path) as bridge:
        prepared = (await bridge.call("canvas_prepare_action", {"action": ACTION}))["structuredContent"]
        action_id = prepared["data"]["action_id"]
        bridge.controls(pause_after_write=30)
        await bridge.send("tools/call", {"name": "canvas_reorder_dashboard", "arguments": {"action_id": action_id}})
        await wait_for(tmp_path / "mutation_applied")
        os.kill(int((tmp_path / "child.pid").read_text()), signal.SIGKILL)
        await asyncio.wait_for(bridge.process.wait(), 8)
        assert bridge.process.returncode != 0
        assert len(bridge.writes()) == 1
    (tmp_path / "controls.json").write_text("{}")
    async with peer(core_python, tmp_path) as restarted:
        receipt = (await restarted.call("canvas_action_status", {"action_id": action_id}))["structuredContent"]
        assert receipt["status"] == "invalid_request"
        read = (await restarted.call("canvas_dashboard_cards"))["structuredContent"]
        assert read["data"]["course_ids"] == ACTION["course_ids"]
        assert len(restarted.writes()) == 1


async def test_oversized_frame_is_rejected_before_any_canvas_dispatch(core_python, tmp_path):
    async with peer(core_python, tmp_path) as bridge:
        assert (await bridge.call("canvas_capabilities"))["structuredContent"]["status"] == "ok"
        request = {"jsonrpc": "2.0", "id": "oversize", "method": "tools/call",
            "params": {"name": "canvas_prepare_action", "arguments": {"action": {"kind": "upload_file",
                "content_base64": "A" * MAX_MESSAGE_BYTES}}, "_meta": META}}
        bridge.process.stdin.write(json.dumps(request).encode() + b"\n")
        try:
            await bridge.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        await asyncio.wait_for(bridge.process.wait(), 8)
        assert bridge.process.returncode != 0 and bridge.requests() == []


async def test_child_death_exits_when_caller_stops_consuming_stdout(core_python, tmp_path):
    process = subprocess.Popen(  # noqa: ASYNC220 - a non-draining raw pipe is this regression's subject
        [sys.executable, str(FIXTURES / "run_facade.py"), core_python, str(tmp_path), "readonly", "105"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=child_environment(), start_new_session=True,
    )
    try:
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": "stalled-catalog",
            "method": "tools/list", "params": {"_meta": META}}).encode() + b"\n")
        process.stdin.flush()
        await wait_for(tmp_path / "child.pid")
        await asyncio.sleep(.4)  # Catalog exceeds pipe capacity; deliberately never read it.
        os.kill(int((tmp_path / "child.pid").read_text()), signal.SIGKILL)
        with anyio.fail_after(8):
            while process.poll() is None:
                await asyncio.sleep(.02)
        assert process.returncode != 0
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()
