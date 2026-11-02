"""Membership tools through the real MCP wrapper and Canvas MockTransport."""
import asyncio
import json
from unittest.mock import Mock

import pytest
from mcp.server.fastmcp import FastMCP
from test_group_memberships import MembershipHTTP, action

from canvas_connector.tools import register_tools


@pytest.fixture
def boundary():
    upstream, mcp, authorize = MembershipHTTP(), FastMCP("membership-boundary"), Mock()
    register_tools(mcp, lambda: upstream.canvas, authorize, owner=lambda: "alice")
    registered = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    calls = {name: (lambda *args, fn=tool.fn, **kwargs: asyncio.run(fn(*args, **kwargs)))
             for name, tool in registered.items()}
    yield upstream, registered, calls, authorize
    upstream.close()


def test_membership_read_and_write_are_separate_permissions(boundary):
    _, registered, _, _ = boundary
    read = registered["canvas_group_membership_options"]
    write = registered["canvas_manage_group_membership"]
    assert read.annotations.readOnlyHint is True
    assert write.annotations.readOnlyHint is False and write.annotations.destructiveHint is True
    assert set(write.parameters["properties"]) == {"action_id"}
    schema = json.dumps(registered["canvas_prepare_action"].parameters)
    assert "group_join" in schema and "group_cancel_join_request" in schema
    assert "group_invitation_accept" not in schema


def test_membership_access_failure_stays_partial_and_never_means_nonmember(boundary):
    upstream, _, call, authorize = boundary
    upstream.errors["GET", "groups/2/users/self"] = (403, {"message": "private-upstream-secret"})
    result = call["canvas_group_membership_options"](1)
    assert result["status"] == "partial" and result["coverage"]["complete"] is False
    option = result["data"]["groups"][0]
    assert option["self_membership"]["status"] == "unavailable" and not option["actions"]
    assert "private-upstream-secret" not in json.dumps(result)
    assert result["source"]["content_is_untrusted"] is True
    authorize.assert_not_called()
    assert not upstream.writes


def test_denied_candidate_catalog_is_incomplete_error_not_empty_groups(boundary):
    upstream, _, call, _ = boundary
    upstream.errors["GET", "courses/1/groups"] = (403, {"message": "private-catalog-secret"})
    result = call["canvas_group_membership_options"](1)
    assert result["status"] == "forbidden" and result["coverage"]["complete"] is False
    assert result["data"] is None and "private-catalog-secret" not in json.dumps(result)


def test_pending_join_routes_once_without_claiming_accepted_membership(boundary):
    upstream, _, call, authorize = boundary
    upstream.new_state = "requested"
    prepared = call["canvas_prepare_action"](action())["data"]
    action_id = prepared["action_id"]
    assert prepared["execute"] == {"tool": "canvas_manage_group_membership", "action_id": action_id}
    assert not upstream.writes
    authorize.assert_not_called()
    assert call["canvas_manage_discussion"](action_id)["status"] == "invalid_request"
    assert call["canvas_action_status"](action_id)["data"]["state"] == "prepared"
    result = call["canvas_manage_group_membership"](action_id)
    assert result["status"] == "succeeded" and result["coverage"]["verification"] is True
    saved = result["data"]["result"]
    assert saved["pending_approval"] is True and saved["accepted"] is False and saved["submitted"] is False
    assert call["canvas_manage_group_membership"](action_id)["status"] == "succeeded"
    assert len(upstream.writes) == 1


def test_preflight_membership_change_is_rejected_without_write(boundary):
    upstream, _, call, _ = boundary
    action_id = call["canvas_prepare_action"](action())["data"]["action_id"]
    upstream.add_member(state="invited")
    result = call["canvas_manage_group_membership"](action_id)
    assert result["status"] == "rejected" and result["coverage"]["complete"] is False
    assert result["data"]["result"]["upstream_write_attempted"] is False and not upstream.writes


def test_failed_exact_readback_is_uncertain_and_cannot_be_replayed(boundary):
    upstream, _, call, _ = boundary
    action_id = call["canvas_prepare_action"](action())["data"]["action_id"]
    upstream.after_write = lambda state: state.members[2].update(user_id=8)
    result = call["canvas_manage_group_membership"](action_id)
    assert result["status"] == "uncertain" and result["coverage"]["verification"] is False
    assert call["canvas_manage_group_membership"](action_id)["status"] == "uncertain"
    assert len(upstream.writes) == 1


def test_disabled_write_authorization_preserves_prepared_receipt(boundary):
    upstream, _, call, authorize = boundary
    action_id = call["canvas_prepare_action"](action())["data"]["action_id"]
    authorize.side_effect = ValueError("Write access is disabled")
    result = call["canvas_manage_group_membership"](action_id)
    assert result["status"] == "invalid_request" and not upstream.writes
    assert call["canvas_action_status"](action_id)["data"]["state"] == "prepared"


def test_leader_guard_remains_visible_in_options(boundary):
    upstream, _, call, _ = boundary
    upstream.add_member()
    upstream.group["leader"] = {"id": 7, "display_name": "Current student", "avatar_url": "https://cdn.example/opaque-secret"}
    result = call["canvas_group_membership_options"](1)
    option = result["data"]["groups"][0]
    assert not option["actions"] and "leadership" in " ".join(option["reasons"])
    assert "opaque-secret" not in json.dumps(result) and not upstream.writes


def test_invalid_window_is_rejected_before_network(boundary):
    upstream, _, call, _ = boundary
    result = call["canvas_group_membership_options"](1, offset=-1)
    assert result["status"] == "invalid_request" and not upstream.seen
