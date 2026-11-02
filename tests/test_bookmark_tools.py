"""Actual FastMCP argument/result boundaries backed by a mock Canvas transport."""

import asyncio
import socket
from unittest.mock import Mock

import httpx
import pytest
from canvas_py import Canvas
from canvas_py._config import CanvasConfig
from mcp.server.fastmcp import FastMCP
from test_bookmarks import BASE, CREATE, PATH, REORDER, BookmarkState, client

from canvas_connector.tools import register_tools


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected external network in bookmark MCP tests")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


@pytest.fixture
def surface():
    state = BookmarkState()
    with client(state) as canvas:
        context = {"owner": "alice", "canvas": canvas}
        authorize = Mock()
        mcp = FastMCP("bookmark-contract-test")
        register_tools(mcp, lambda: context["canvas"], authorize, owner=lambda: context["owner"])
        yield state, mcp, context, authorize


def call(mcp, name, **arguments):
    result = asyncio.run(mcp.call_tool(name, arguments))
    assert isinstance(result, tuple) and isinstance(result[1], dict)
    return result[1]


def test_read_tool_paginates_with_incomplete_coverage_without_following_bookmark_url(surface):
    state, mcp, _, authorize = surface
    state.bookmarks[1]["url"] = CREATE["url"]
    state.page_size = 1
    result = call(mcp, "canvas_bookmarks", limit=1)
    assert result["status"] == "ok"
    assert result["coverage"]["complete"] is False
    assert result["coverage"]["next_offset"] == 1
    assert len(result["data"]["bookmarks"]) == 1
    assert result["data"]["bookmarks"][0]["link"]["access"] == "external_account_required"
    detail = call(mcp, "canvas_bookmarks", bookmark_id=1)
    assert detail["coverage"]["complete"] is True
    assert detail["data"]["bookmark"]["url"] == CREATE["url"]
    assert not state.writes
    assert all(path == "/api/v1/users/self/profile" or path.startswith(PATH) for _, path, _, _ in state.requests)
    authorize.assert_not_called()


def test_detail_tool_rejects_pagination_before_canvas_read(surface):
    state, mcp, _, _ = surface
    result = call(mcp, "canvas_bookmarks", bookmark_id=1, offset=1)
    assert result["status"] == "invalid_request"
    assert not state.requests


def test_read_tool_preserves_denied_access_as_incomplete_not_empty(surface):
    state, mcp, _, _ = surface
    state.read_error = (PATH, 403)
    result = call(mcp, "canvas_bookmarks")
    assert result["status"] == "forbidden"
    assert result["coverage"]["complete"] is False
    assert result["data"] is None
    assert not state.writes


@pytest.mark.parametrize("payload,expected_order", [
    ({**CREATE, "position": 1}, [(20, 1), (1, 2), (2, 3), (3, 4)]),
    (REORDER, [(2, 0), (1, 2), (3, 3)]),
])
def test_prepared_tool_create_or_move_verifies_actual_order_and_is_not_replayed(surface, payload, expected_order):
    state, mcp, _, authorize = surface
    prepared = call(mcp, "canvas_prepare_action", action=payload)["data"]
    assert prepared["execute"]["tool"] == "canvas_manage_bookmarks"
    assert not state.writes
    result = call(mcp, "canvas_manage_bookmarks", action_id=prepared["action_id"])["data"]
    assert result["state"] == "succeeded"
    assert result["result"]["verified"] is True
    assert [(item["id"], item["position"]) for item in result["result"]["order"]] == expected_order
    assert [(item["id"], item["position"]) for item in state.ordered()] == expected_order
    assert call(mcp, "canvas_manage_bookmarks", action_id=prepared["action_id"])["data"]["state"] == "succeeded"
    assert len(state.writes) == 1
    assert authorize.called


def test_wrong_executor_cannot_consume_bookmark_receipt(surface):
    state, mcp, _, _ = surface
    prepared = call(mcp, "canvas_prepare_action", action=CREATE)["data"]
    result = call(mcp, "canvas_send_message", action_id=prepared["action_id"])
    assert result["status"] == "invalid_request"
    assert call(mcp, "canvas_action_status", action_id=prepared["action_id"])["data"]["state"] == "prepared"
    assert not state.writes


@pytest.mark.parametrize("change", ["owner", "credentials"])
def test_receipt_is_bound_to_account_and_authenticated_credentials(surface, change):
    state, mcp, context, _ = surface
    prepared = call(mcp, "canvas_prepare_action", action=CREATE)["data"]
    calls_before = len(state.requests)
    with (
        httpx.Client(transport=httpx.MockTransport(state.handler)) as http,
        Canvas(CanvasConfig(BASE, "different-token"), http_client=http) as other_canvas,
    ):
        if change == "owner":
            context["owner"] = "bob"
        else:
            context["canvas"] = other_canvas
        result = call(mcp, "canvas_manage_bookmarks", action_id=prepared["action_id"])
    assert result["status"] == "invalid_request"
    assert len(state.requests) == calls_before
    assert not state.writes


def test_saved_target_without_correct_neighbor_order_is_uncertain_through_mcp(surface):
    state, mcp, _, _ = surface
    prepared = call(mcp, "canvas_prepare_action", action=REORDER)["data"]
    state.after_write = lambda s: s.bookmarks[3].update(position=10)
    result = call(mcp, "canvas_manage_bookmarks", action_id=prepared["action_id"])["data"]
    assert result["state"] == "uncertain"
    assert result["result"]["verified"] is False
    assert call(mcp, "canvas_manage_bookmarks", action_id=prepared["action_id"])["data"]["state"] == "uncertain"
    assert len(state.writes) == 1


def test_bookmark_tools_advertise_read_and_mutation_annotations(surface):
    _, mcp, _, _ = surface
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    assert tools["canvas_bookmarks"].annotations.readOnlyHint is True
    assert tools["canvas_manage_bookmarks"].annotations.readOnlyHint is False
    assert tools["canvas_manage_bookmarks"].annotations.destructiveHint is True
    assert set(tools["canvas_bookmarks"].parameters["properties"]) == {"bookmark_id", "offset", "limit"}
