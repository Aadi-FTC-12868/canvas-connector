"""Group MCP boundary tests using the real Canvas client over MockTransport."""
import asyncio
import json
from unittest.mock import Mock

import pytest
from mcp.server.fastmcp import FastMCP
from test_group_content import ACTION, COURSE, GROUP, MockCanvas

from canvas_connector.tools import register_tools


@pytest.fixture
def boundary():
    upstream, mcp, authorize = MockCanvas(), FastMCP("group-boundary"), Mock()
    store = register_tools(mcp, lambda: upstream.canvas, authorize, owner=lambda: "alice")
    registered = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    call = {name: (lambda *args, fn=tool.fn, **kwargs: asyncio.run(fn(*args, **kwargs)))
            for name, tool in registered.items()}
    yield upstream, registered, call, store, authorize
    upstream.close()


def test_group_tools_keep_creation_in_a_separate_write_permission_group(boundary):
    _, registered, _, _, _ = boundary
    for name in ("canvas_group_home", "canvas_browse_group", "canvas_open_group_item", "canvas_group_discussion_entries"):
        assert registered[name].annotations.readOnlyHint is True
    creation = registered["canvas_create_discussion"]
    assert creation.annotations.readOnlyHint is False and creation.annotations.destructiveHint is True
    assert set(creation.parameters["properties"]) == {"action_id"}
    schema = json.dumps(registered["canvas_prepare_action"].parameters)
    assert "topic_create" in schema and "context_type" in schema


@pytest.mark.parametrize("item_type,item_id,resource", [
    ("page", "study-guide", "page"), ("discussion", "10", "topic"),
    ("file", "30", "file"), ("folder", "40", "folder"),
])
def test_locked_group_item_is_not_reported_as_complete(boundary, item_type, item_id, resource):
    upstream, _, call, _, authorize = boundary
    getattr(upstream, resource).update(locked_for_user=True, lock_explanation="Opens after class")
    result = call["canvas_open_group_item"](2, item_type, item_id)
    if item_type == "page":
        assert result["status"] == "unavailable" and result["coverage"]["complete"] is False
        assert "body" not in json.dumps(result)
        assert not upstream.writes
        return
    assert result["status"] == "locked" and result["coverage"]["complete"] is False
    assert result["data"]["locked_for_user"] is True
    assert result["data"]["lock_explanation"] == "Opens after class"
    assert result["source"]["content_is_untrusted"] is True
    authorize.assert_not_called()
    assert not upstream.writes


def test_group_file_open_omits_opaque_download_credentials(boundary):
    upstream, _, call, _, _ = boundary
    upstream.file["url"] = "https://cdn.example/opaque-file-secret/download?X-Amz-Signature=query-secret"
    result = call["canvas_open_group_item"](2, "file", "30")
    assert result["status"] == "ok"
    assert "url" not in result["data"]
    assert result["data"]["read"] == {"tool": "canvas_read_file", "file_id": 30}
    assert "secret" not in json.dumps(result)
    assert not upstream.writes


def test_group_page_html_and_links_are_sanitized_at_the_tool_boundary(boundary):
    upstream, _, call, _, _ = boundary
    upstream.page["body"] = ('<p onclick="script-secret">Read <a href="/groups/2/files/30?verifier=query-secret">'
                             'the file</a><img src="https://cdn.example/img?X-Amz-Signature=image-secret" '
                             'onerror="script-secret"></p>')
    result = call["canvas_open_group_item"](2, "page", "study-guide", include_html=True)
    assert result["status"] == "ok" and "body_html" in result["data"]
    assert "secret" not in json.dumps(result)
    assert "onclick" not in result["data"]["body_html"] and "onerror" not in result["data"]["body_html"]
    assert result["links"][0]["url"] == "https://school.example/groups/2/files/30"
    assert not upstream.writes


def test_group_browse_preserves_window_coverage_and_omits_download_credentials(boundary):
    upstream, _, call, _, authorize = boundary
    upstream.rows[0]["url"] = "https://cdn.example/opaque-download-secret/file.pdf?X-Amz-Signature=query-secret"
    result = call["canvas_browse_group"](2, "files", limit=2)
    assert [row["id"] for row in result["data"]] == [50, 51]
    assert result["coverage"]["next_offset"] == 2 and result["coverage"]["complete"] is False
    assert result["coverage"]["membership_state"] == "accepted"
    assert "url" not in result["data"][0]
    assert result["data"][0]["html_url"] == "https://school.example/files/50"
    assert result["data"][0]["read"] == {"tool": "canvas_read_file", "file_id": 50}
    assert "secret" not in json.dumps(result)
    authorize.assert_not_called()
    assert not upstream.writes


@pytest.mark.parametrize("file_id", [True, "50", 0, 2**63])
def test_group_file_listing_only_routes_confirmed_positive_integer_ids(boundary, file_id):
    upstream, _, call, _, _ = boundary
    upstream.rows = [{"id": file_id, "url": "https://cdn.example/opaque-secret/file.pdf"}]
    result = call["canvas_browse_group"](2, "files")
    assert "url" not in result["data"][0] and "secret" not in json.dumps(result)
    assert "html_url" not in result["data"][0] and "read" not in result["data"][0]


def test_initial_post_gate_survives_tool_envelope_without_fetch_or_write(boundary):
    upstream, _, call, _, authorize = boundary
    upstream.topic.update(require_initial_post=True, user_can_see_posts=False)
    result = call["canvas_group_discussion_entries"](2, 10)
    assert result["status"] == "forbidden" and result["coverage"]["complete"] is False
    assert result["data"]["access"] == "initial_post_required"
    assert not any("/entries" in request.url.path for request in upstream.seen)
    authorize.assert_not_called()
    assert not upstream.writes


def test_denied_group_member_read_is_an_incomplete_error_not_empty_data(boundary):
    upstream, _, call, _, _ = boundary
    upstream.errors["GET", "groups/2/users/self"] = (403, {"message": "private-upstream-secret"})
    result = call["canvas_group_home"](2)
    assert result["status"] == "forbidden" and result["coverage"]["complete"] is False
    assert result["data"] is None and result["errors"][0]["http_status"] == 403
    assert "secret" not in json.dumps(result)
    assert not upstream.writes


@pytest.mark.parametrize("context", [COURSE, GROUP])
def test_prepared_topic_routes_to_creation_and_verifies_only_once(boundary, context):
    upstream, _, call, _, authorize = boundary
    payload = {**ACTION, "context": context}
    prepared = call["canvas_prepare_action"](payload)["data"]
    action_id = prepared["action_id"]
    assert prepared["execute"] == {"tool": "canvas_create_discussion", "action_id": action_id}
    assert prepared["review"]["context"] == context
    assert prepared["review"]["target"]["user_id"] == 7
    assert not upstream.writes
    authorize.assert_not_called()
    wrong_route = call["canvas_post_discussion"](action_id)
    assert wrong_route["status"] == "invalid_request" and not upstream.writes
    assert call["canvas_action_status"](action_id)["data"]["state"] == "prepared"
    result = call["canvas_create_discussion"](action_id)
    assert result["status"] == "succeeded" and result["coverage"]["verification"] is True
    assert result["data"]["result"]["context"] == context
    assert result["data"]["result"]["topic_id"] == 10
    assert json.loads(upstream.writes[0].content) == {"title": payload["title"], "message": payload["message"]}
    assert call["canvas_create_discussion"](action_id)["status"] == "succeeded"
    assert len(upstream.writes) == 1


@pytest.mark.parametrize("change", ["audience", "membership", "permission", "group_id"])
def test_action_store_rejects_changed_group_before_any_dispatch(boundary, change):
    upstream, _, call, _, _ = boundary
    action_id = call["canvas_prepare_action"](ACTION)["data"]["action_id"]
    if change == "audience":
        upstream.group["name"] = "Changed audience"
    elif change == "membership":
        upstream.member["workflow_state"] = "requested"
    elif change == "permission":
        upstream.group["permissions"]["create_discussion_topic"] = False
    else:
        upstream.group["id"] = 9
    result = call["canvas_create_discussion"](action_id)
    assert result["status"] == "rejected" and result["coverage"]["complete"] is False
    assert result["data"]["result"]["upstream_write_attempted"] is False
    assert not upstream.writes


def test_unverified_topic_readback_is_uncertain_and_cannot_be_retried(boundary):
    upstream, _, call, _, _ = boundary
    action_id = call["canvas_prepare_action"](ACTION)["data"]["action_id"]
    upstream.after_write = lambda state: state.topic.update(author={"id": 9})
    result = call["canvas_create_discussion"](action_id)
    assert result["status"] == "uncertain" and result["coverage"]["verification"] is False
    assert result["data"]["result"]["upstream_write_attempted"] is True
    assert call["canvas_create_discussion"](action_id)["status"] == "uncertain"
    assert len(upstream.writes) == 1


def test_creation_write_authorization_failure_leaves_receipt_prepared(boundary):
    upstream, _, call, _, authorize = boundary
    action_id = call["canvas_prepare_action"](ACTION)["data"]["action_id"]
    authorize.side_effect = ValueError("Write access is disabled")
    result = call["canvas_create_discussion"](action_id)
    assert result["status"] == "invalid_request" and not upstream.writes
    assert call["canvas_action_status"](action_id)["data"]["state"] == "prepared"
