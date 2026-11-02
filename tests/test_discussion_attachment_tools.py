"""Prepared multipart writes through actual FastMCP schemas and results."""

import asyncio
import base64
import copy
import socket
from unittest.mock import Mock

import httpx
import pytest
from canvas_py import Canvas
from canvas_py._config import CanvasConfig
from mcp.server.fastmcp import FastMCP
from test_discussion_attachments import BASE, RAW, DiscussionState, client, payload

from canvas_connector.tools import register_tools


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No real network in attachment MCP tests")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def surface(canvas):
    context = {"owner": "alice", "canvas": canvas}
    authorize = Mock()
    mcp = FastMCP("discussion-attachment-contract-test")
    register_tools(mcp, lambda: context["canvas"], authorize, owner=lambda: context["owner"])
    return mcp, context, authorize


def call(mcp, name, **arguments):
    result = asyncio.run(mcp.call_tool(name, arguments))
    assert isinstance(result, tuple) and isinstance(result[1], dict)
    return result[1]


@pytest.mark.parametrize("context", ["course", "group"])
@pytest.mark.parametrize("reply", [False, True])
def test_review_excludes_bytes_execution_uses_immutable_preparation_and_never_replays(context, reply):
    state, p = DiscussionState(context), payload(context, reply)
    original = copy.deepcopy(p)
    with client(state) as canvas:
        mcp, _, authorize = surface(canvas)
        prepared = call(mcp, "canvas_prepare_action", action=p)["data"]
        assert prepared["execute"]["tool"] == "canvas_post_discussion_attachment"
        assert "content_base64" not in str(prepared)
        assert prepared["review"]["target"]["context"] == p["context"]
        assert prepared["review"]["target"]["attachment"]["bytes"] == len(RAW)
        assert not state.writes
        p.update(content_base64=base64.b64encode(b"changed after review").decode(), filename="changed.txt", message="changed")
        result = call(mcp, "canvas_post_discussion_attachment", action_id=prepared["action_id"])["data"]
        assert result["state"] == "succeeded" and result["result"]["verified"] is True
        assert result["result"]["attachment"]["requested_filename"] == original["filename"]
        assert state.writes[0]["raw"] == RAW and state.writes[0]["message"] == original["message"]
        assert call(mcp, "canvas_post_discussion_attachment", action_id=prepared["action_id"])["data"]["state"] == "succeeded"
        assert len(state.writes) == 1
        authorize.assert_called()


def test_wrong_executor_cannot_consume_attachment_receipt():
    state = DiscussionState()
    with client(state) as canvas:
        mcp, _, _ = surface(canvas)
        prepared = call(mcp, "canvas_prepare_action", action=payload())["data"]
        assert call(mcp, "canvas_post_discussion", action_id=prepared["action_id"])["status"] == "invalid_request"
        assert call(mcp, "canvas_action_status", action_id=prepared["action_id"])["data"]["state"] == "prepared"
    assert not state.writes


@pytest.mark.parametrize("changed", ["owner", "credentials"])
def test_attachment_receipt_bound_to_user_and_authenticated_credentials(changed):
    state = DiscussionState()
    with client(state) as canvas:
        mcp, context, _ = surface(canvas)
        prepared = call(mcp, "canvas_prepare_action", action=payload())["data"]
        before = len(state.requests)
        with (httpx.Client(transport=httpx.MockTransport(state.handler)) as http,
              Canvas(CanvasConfig(BASE, "other-synthetic-token"), http_client=http) as other):
            if changed == "owner":
                context["owner"] = "bob"
            else:
                context["canvas"] = other
            result = call(mcp, "canvas_post_discussion_attachment", action_id=prepared["action_id"])
        assert result["status"] == "invalid_request"
        assert len(state.requests) == before
    assert not state.writes


def test_changed_audience_or_permission_rejects_before_dispatch():
    state = DiscussionState()
    with client(state) as canvas:
        mcp, _, _ = surface(canvas)
        prepared = call(mcp, "canvas_prepare_action", action=payload())["data"]
        state.topic["permissions"]["attach"] = False
        result = call(mcp, "canvas_post_discussion_attachment", action_id=prepared["action_id"])
        assert result["status"] == "rejected"
        assert result["data"]["result"]["upstream_write_attempted"] is False
    assert not state.writes


@pytest.mark.parametrize("failure", ["hash", "missing_attachment", "read_denied"])
def test_unverified_saved_attachment_is_uncertain_and_cannot_be_auto_repeated(failure):
    state = DiscussionState()
    with client(state) as canvas:
        mcp, _, _ = surface(canvas)
        prepared = call(mcp, "canvas_prepare_action", action=payload())["data"]
        if failure == "hash":
            state.raw = b"not the reviewed bytes"
        elif failure == "missing_attachment":
            state.after_write = lambda s: s.entries[20].update(attachment=None, attachments=[])
        else:
            state.after_write = lambda s: setattr(s, "error", ("GET", "/api/v1/users/self/files/90", 403))
        result = call(mcp, "canvas_post_discussion_attachment", action_id=prepared["action_id"])["data"]
        assert result["state"] == "uncertain" and result["result"]["verified"] is False
        assert call(mcp, "canvas_post_discussion_attachment", action_id=prepared["action_id"])["data"]["state"] == "uncertain"
        again = call(mcp, "canvas_prepare_action", action=payload())
        assert again["data"]["action_id"] == prepared["action_id"]
        assert again["data"]["state"] == "uncertain"
    assert len(state.writes) == 1


def test_executor_schema_accepts_only_receipt_id_and_marks_external_mutation():
    state = DiscussionState()
    with client(state) as canvas:
        mcp, _, _ = surface(canvas)
        tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
        executor = tools["canvas_post_discussion_attachment"]
        assert set(executor.parameters["properties"]) == {"action_id"}
        assert executor.annotations.readOnlyHint is False
        assert executor.annotations.openWorldHint is True
        assert executor.annotations.idempotentHint is False
        capabilities = call(mcp, "canvas_capabilities")["data"]
        assert {"discussion_attachment_post", "discussion_attachment_reply"} <= set(capabilities["actions"])
        assert "canvas_post_discussion_attachment" in capabilities["permission_groups"]
    assert not state.requests
