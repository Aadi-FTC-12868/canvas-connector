"""Preference MCP boundaries with the real Canvas client and synthetic HTTP."""
import asyncio
import json
from unittest.mock import Mock

import httpx
import pytest
from canvas_py import Canvas
from mcp.server.fastmcp import FastMCP

from canvas_connector.preferences import _Request
from canvas_connector.tools import register_tools
from test_preferences import CASES, NOTIFY, PreferencesCanvas


@pytest.fixture
def boundary():
    state, authorize = PreferencesCanvas(), Mock()
    errors = {}

    def handler(request):
        path = request.url.path.removeprefix("/api/v1/")
        if (request.method, path) in errors:
            return httpx.Response(errors[request.method, path], json={"message": "private-upstream-secret"})
        query = [(k, int(v) if k == "user_id" else v) for k, v in request.url.params.multi_items()]
        body = json.loads(request.content) if request.content else None
        return httpx.Response(200, json=state.send(_Request(path, method=request.method, body=body, query=query)))

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with Canvas(state.config, http_client=http) as canvas:
            mcp = FastMCP("preference-boundary")
            register_tools(mcp, lambda: canvas, authorize, owner=lambda: "alice")
            registered = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
            call = {name: (lambda *args, fn=tool.fn, **kwargs: asyncio.run(fn(*args, **kwargs)))
                    for name, tool in registered.items()}
            yield state, registered, call, authorize, errors


def test_preference_read_and_mutation_annotations_and_schema(boundary):
    _, registered, _, _, _ = boundary
    for name in ("canvas_communication_channels", "canvas_notification_preferences", "canvas_preferences"):
        assert registered[name].annotations.readOnlyHint is True
    for name in ("canvas_update_notifications", "canvas_update_preferences"):
        assert registered[name].annotations.readOnlyHint is False
        assert registered[name].annotations.destructiveHint is True
        assert set(registered[name].parameters["properties"]) == {"action_id"}
    schema = json.dumps(registered["canvas_prepare_action"].parameters)
    assert "notification_preference_update" in schema and "course_favorite_remove" in schema
    assert "course_color_set" in schema


@pytest.mark.parametrize("name,args,key", [
    ("canvas_communication_channels", {}, "communication_channels"),
    ("canvas_notification_preferences", {"channel_id": 2}, "notification_preferences"),
    ("canvas_preferences", {"section": "favorite_courses"}, "favorites"),
    ("canvas_preferences", {"section": "favorite_groups"}, "favorites"),
    ("canvas_preferences", {"section": "nicknames"}, "course_nicknames"),
    ("canvas_preferences", {"section": "colors"}, "course_colors"),
])
def test_read_tools_preserve_account_and_bounded_envelope_without_writes(boundary, name, args, key):
    state, _, call, authorize, _ = boundary
    result = call[name](**args, limit=1)
    assert result["status"] == "ok" and result["data"]["user_id"] == 7
    assert len(result["data"][key]) == 1
    assert result["coverage"]["returned"] == 1
    assert result["source"]["canvas_origin"] == "https://school.example"
    assert result["source"]["content_is_untrusted"] is True
    assert not state.writes
    authorize.assert_not_called()


def test_denied_channel_read_is_incomplete_not_empty_and_hides_upstream_body(boundary):
    state, _, call, _, errors = boundary
    errors["GET", "users/self/communication_channels"] = 403
    result = call["canvas_communication_channels"]()
    assert result["status"] == "forbidden" and result["coverage"]["complete"] is False
    assert result["data"] is None and result["errors"][0]["http_status"] == 403
    assert "secret" not in json.dumps(result)
    assert not state.writes


@pytest.mark.parametrize("payload,route,wrong_route", [
    ({**NOTIFY, "frequency": "never"}, "canvas_update_notifications", "canvas_update_preferences"),
    (CASES[5][0], "canvas_update_preferences", "canvas_update_notifications"),
])
def test_prepared_preferences_require_correct_execution_group_and_run_once(boundary, payload, route, wrong_route):
    state, _, call, authorize, _ = boundary
    prepared = call["canvas_prepare_action"](payload)["data"]
    action_id = prepared["action_id"]
    assert prepared["execute"] == {"tool": route, "action_id": action_id}
    if payload["kind"] == "notification_preference_update":
        assert "disables this notification" in prepared["review"]["target"]["warning"]
    assert not state.writes
    authorize.assert_not_called()
    assert call[wrong_route](action_id)["status"] == "invalid_request"
    assert call["canvas_submit_assignment"](action_id)["status"] == "invalid_request"
    assert call["canvas_action_status"](action_id)["data"]["state"] == "prepared"
    result = call[route](action_id)
    assert result["status"] == "succeeded" and result["coverage"]["verification"] is True
    assert result["data"]["result"]["verified"] is True
    assert call[route](action_id)["status"] == "succeeded"
    assert len(state.writes) == 1


def test_preference_authorization_failure_preserves_prepared_receipt(boundary):
    state, _, call, authorize, _ = boundary
    action_id = call["canvas_prepare_action"](NOTIFY)["data"]["action_id"]
    authorize.side_effect = ValueError("Write access is disabled")
    assert call["canvas_update_notifications"](action_id)["status"] == "invalid_request"
    assert call["canvas_action_status"](action_id)["data"]["state"] == "prepared"
    assert not state.writes


def test_preference_failed_readback_remains_uncertain_on_repeated_execute(boundary):
    state, _, call, _, _ = boundary
    action_id = call["canvas_prepare_action"](NOTIFY)["data"]["action_id"]
    state.ignore_write = True
    result = call["canvas_update_notifications"](action_id)
    assert result["status"] == "uncertain" and result["coverage"]["verification"] is False
    assert result["data"]["result"]["upstream_write_attempted"] is True
    assert call["canvas_update_notifications"](action_id)["status"] == "uncertain"
    assert len(state.writes) == 1
