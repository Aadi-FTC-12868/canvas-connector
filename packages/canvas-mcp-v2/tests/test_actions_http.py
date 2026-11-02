"""Prepared actions work over authenticated HTTP without elicitation."""

import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP

from canvas_mcp_v2.transport import create_http_app

KEY = "http-test-key-not-a-secret-0123456789abcdef"


def test_stateless_actions_use_json_without_confirmation_session(monkeypatch):
    monkeypatch.delenv("CANVAS_ENV_PATH", raising=False)
    monkeypatch.setenv("CANVAS_BASE_URL", "https://school.instructure.com")
    monkeypatch.setenv("CANVAS_API_TOKEN", "dummy-canvas-token")
    monkeypatch.setenv("CANVAS_MCP_API_KEY", KEY)
    monkeypatch.setenv("CANVAS_MCP_ENABLE_ACTIONS", "true")
    monkeypatch.setenv("CANVAS_MCP_STATELESS_HTTP", "true")
    mcp = FastMCP("test")
    create_http_app(mcp)
    assert mcp.settings.stateless_http and mcp.settings.json_response


@pytest.mark.asyncio
@pytest.mark.parametrize("stateless", [True, False])
async def test_http_actions_need_no_form_with_either_session_mode(tmp_path, stateless):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    script = tmp_path / "mock_http_server.py"
    script.write_text(
        "from test_actions import FakeCanvas\n"
        "from canvas_mcp_v2.actions import register_action_tools\n"
        "from canvas_mcp_v2.transport import create_http_app\n"
        "from mcp.server.fastmcp import FastMCP\nimport uvicorn\n"
        "mcp=FastMCP('mock HTTP')\nc=FakeCanvas()\nregister_action_tools(mcp,lambda:c)\n"
        f"uvicorn.run(create_http_app(mcp),host='127.0.0.1',port={port},access_log=False,log_level='error')\n"
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("CANVAS_")}
    env.update(
        PYTHONPATH=str(Path(__file__).parent),
        CANVAS_BASE_URL="https://school.instructure.com",
        CANVAS_API_TOKEN="dummy-canvas-token",
        CANVAS_MCP_API_KEY=KEY,
        CANVAS_MCP_TRANSPORT="http",
        CANVAS_MCP_ENABLE_ACTIONS="true",
        CANVAS_MCP_STATELESS_HTTP=str(stateless).lower(),
    )
    process = subprocess.Popen(
        [sys.executable, str(script)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    confirmations = []

    async def confirm(ctx, params):
        confirmations.append(params.message)
        raise RuntimeError("No connector confirmation form should be requested")

    def payload(result):
        assert not result.isError
        return result.structuredContent or json.loads(result.content[0].text)

    try:
        async with httpx.AsyncClient(timeout=1) as probe:
            for _ in range(100):
                try:
                    if (await probe.get(f"http://127.0.0.1:{port}/health")).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                assert process.poll() is None, "Mock HTTP server exited"
                await asyncio.sleep(0.05)
            else:
                pytest.fail("Mock HTTP server did not become ready")
            assert (await probe.post(f"http://127.0.0.1:{port}/mcp")).status_code == 401
        async with httpx.AsyncClient(  # noqa: SIM117
            headers={"Authorization": f"Bearer {KEY}"}, timeout=15
        ) as client:
            async with streamable_http_client(
                f"http://127.0.0.1:{port}/mcp", http_client=client
            ) as streams:
                async with ClientSession(
                    streams[0], streams[1], elicitation_callback=confirm
                ) as session:
                    await session.initialize()
                    assert (streams[2]() is None) == stateless
                    prepared = payload(
                        await session.call_tool(
                            "canvas_prepare_upload",
                            {"filename": "note.txt", "content_base64": "aGVsbG8=", "destination":"assignment", "course_id":10, "assignment_id":20},
                        )
                    )
                    completed = payload(
                        await session.call_tool(
                            "canvas_execute_action", {"action_id": prepared["action_id"]}
                        )
                    )
                    assert completed["state"] == "succeeded"
                    assert completed["result"]["verified"] and not completed["result"]["submitted"]
                    repeated = payload(
                        await session.call_tool(
                            "canvas_execute_action", {"action_id": prepared["action_id"]}
                        )
                    )
                    assert repeated["state"] == "succeeded"
                    review = payload(await session.call_tool("canvas_prepare_submission", {"course_id":10, "assignment_id":20, "upload_action_ids":[prepared["action_id"]]}))
                    submitted = payload(await session.call_tool("canvas_execute_action", {"action_id":review["action_id"]}))
                    assert submitted["result"]["submitted"] and submitted["result"]["verified"]
        assert confirmations == []
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
