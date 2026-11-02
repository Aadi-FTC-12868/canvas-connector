"""Conversation policy has no extra connector form and does not attest consent."""
import json
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp import FastMCP
from test_actions import FakeCanvas, fn, prepare

from canvas_mcp_v2.actions import confirmation_capabilities, register_action_tools


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.delenv("CANVAS_ENV_PATH", raising=False)
    monkeypatch.setenv("CANVAS_MCP_TRANSPORT", "stdio")
    monkeypatch.delenv("CANVAS_MCP_ENABLE_ACTIONS", raising=False)
    canvas = FakeCanvas()
    mcp = FastMCP("conversation policy")
    store = register_action_tools(mcp, lambda: canvas)
    yield canvas, mcp, store
    store.close()


def test_policy_does_not_require_form_or_claim_to_attest_consent(setup):
    canvas, _, store = setup
    result = confirmation_capabilities()
    assert result["confirmation_mode"] == "conversation"
    assert not result["connector_form_required"] and not result["elicitation_required"]
    assert result["user_authorization_required"] and not result["server_attests_user_consent"]
    assert canvas.writes == [] and store.records == {}


@pytest.mark.asyncio
async def test_cancelled_preparation_stays_cancelled_after_expiry(setup):
    canvas, mcp, store = setup
    prepared = prepare(store, canvas)
    result = fn(mcp, "canvas_cancel_action")(prepared["action_id"])
    assert result["state"] == "cancelled" and result["result"] is None
    record = store.get(canvas, prepared["action_id"])
    record["expires"] = -1
    store._prune()
    assert (await fn(mcp,"canvas_execute_action")(prepared["action_id"]))["state"] == "cancelled"
    assert canvas.writes == []


@pytest.mark.asyncio
async def test_review_and_execution_do_not_invent_a_confirmation_receipt(setup):
    canvas, mcp, store = setup
    prepared = prepare(store, canvas)
    assert '"note.txt"' in prepared["review_text"]
    assert "does not submit coursework" in prepared["review_text"]
    assert not prepared["requires_client_user_confirmation"]
    result = await fn(mcp,"canvas_execute_action")(prepared["action_id"])
    assert result["state"] == "succeeded"
    assert not result["authorization"]["server_attests_user_consent"]
    assert "confirmation" not in result
    assert result["result"]["sha256"] == prepared["review"]["sha256"]


@pytest.mark.asyncio
async def test_real_protocol_diagnostic_and_honest_side_effect_annotations(setup, tmp_path):
    script = tmp_path / "policy_server.py"
    script.write_text("from mcp.server.fastmcp import FastMCP\nfrom canvas_mcp_v2.actions import register_action_tools\n"
                      "mcp=FastMCP('policy')\ndef forbidden():\n raise RuntimeError('Canvas provider must not be called')\n"
                      "register_action_tools(mcp,forbidden)\nmcp.run()\n")
    async with stdio_client(StdioServerParameters(command=sys.executable,args=[str(script)],env=dict(os.environ))) as streams:  # noqa: SIM117
        async with ClientSession(*streams) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            execute = next(t for t in tools if t.name == "canvas_execute_action")
            assert set(execute.inputSchema["properties"]) == {"action_id"}
            assert execute.annotations.readOnlyHint is False
            assert execute.annotations.destructiveHint is True
            assert "explicit user authorization" in execute.description
            result = await session.call_tool("canvas_confirmation_capabilities", {})
            assert not result.isError
            data = result.structuredContent or json.loads(result.content[0].text)
            assert data["upstream_writes"] is False
            assert not data["connector_form_required"]
