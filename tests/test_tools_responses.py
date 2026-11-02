import json
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from canvas_py._config import CanvasConfig
from canvas_py._errors import CanvasError
from mcp.server.fastmcp import FastMCP

from canvas_connector.responses import ResponseStore, envelope, normalize
from canvas_connector.tools import register_tools


def server():
    c = SimpleNamespace(config=CanvasConfig("https://school.example", "secret"), navigation=Mock(), inbox=Mock(), files=Mock())
    mcp = FastMCP("test")
    register_tools(mcp, lambda: c, lambda: None, owner=lambda: "alice")
    fns = {t.name: (lambda *args, fn=t.fn, **kwargs: asyncio.run(fn(*args, **kwargs))) for t in mcp._tool_manager.list_tools()}
    return c, mcp, fns


def test_html_normalization_preserves_status_and_removes_signed_links():
    links = []
    data = normalize({"description": '<p onclick="bad()">Read <a href="https://docs.google.com/document/d/abc?token=secret">document</a><img src="https://cdn.example/a?X-Amz-Signature=hidden" onerror="bad()"></p>',
                      "missing": False, "score": 0, "grade_matches_current_submission": None},
                     canvas_origin="https://school.example", include_html=True, links=links)
    assert data["missing"] is False and data["score"] == 0 and data["grade_matches_current_submission"] is None
    assert "secret" not in json.dumps(data) and "hidden" not in json.dumps(data)
    assert "onclick" not in data["description_html"] and "onerror" not in data["description_html"]
    assert links[0]["provider"] == "google_drive"
    assert "school Google account" in links[0]["next_action"]


def test_navigation_envelope_unwraps_data_preserves_context_and_incomplete_coverage():
    result = envelope({"status": "ok", "source": {"cache_hit": True},
        "data": {"items": [{"missing": None}], "next_offset": 10}, "course_id": 2})
    assert result["data"]["items"][0]["missing"] is None
    assert result["context"]["course_id"] == 2
    assert result["coverage"]["complete"] is False


def test_large_results_have_bound_owner_checked_complete_continuations():
    store = ResponseStore(inline_chars=200)
    original = envelope({"long": '"secret course words"' * 10000, "score": 0})
    first = store.compact(original, "alice")
    assert first["status"] == "partial" and not first["coverage"]["complete"]
    key = first["data"]["result_id"]
    with pytest.raises(ValueError):
        store.read("bob", key)
    chunks = [first["data"]["text"]]
    cursor = first["data"]["next_read"]
    while cursor:
        chunk = store.read("alice", key, cursor["offset"], cursor["max_chars"])
        assert len(chunk["data"]["text"]) <= 40000
        chunks.append(chunk["data"]["text"])
        cursor = chunk["data"]["next_read"]
    assert json.loads("".join(chunks)) == original
    assert store.compact(first, "alice") is first


def test_tool_annotations_and_typed_action_schema():
    _, mcp, _ = server()
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    assert len(tools) == 42
    assert tools["canvas_submit_assignment"].annotations.readOnlyHint is False
    assert tools["canvas_submit_assignment"].annotations.destructiveHint is True
    assert tools["canvas_prepare_action"].annotations.readOnlyHint is True
    assert tools["canvas_inbox_thread"].annotations.readOnlyHint is True
    assert "discriminator" in json.dumps(tools["canvas_prepare_action"].parameters)
    assert not any(t in tools for t in ("download_file", "canvas_sync_courses", "canvas_proxy", "canvas_execute_action"))


def test_external_google_link_returns_handoff_without_fetching():
    c, _, fns = server()
    result = fns["canvas_open_url"]("https://docs.google.com/document/d/test/edit")
    assert result["status"] == "external"
    assert result["data"]["provider"] == "google_drive"
    c.navigation.open_url.assert_not_called()


@pytest.mark.parametrize("state", ["invited", "invited_or_pending"])
def test_course_invitation_filter_uses_documented_canvas_query(state):
    c, _, fns = server()
    c.paginate = Mock(return_value=(item for item in []))
    result = fns["canvas_list_courses"](enrollment_state=state)
    assert result["status"] == "ok"
    request = c.paginate.call_args.args[0]
    assert ("enrollment_state", "invited_or_pending") in request.query_pairs()


def test_forbidden_is_not_empty_success_and_error_does_not_leak_body():
    c, _, fns = server()
    c.navigation.dashboard.side_effect = CanvasError("upstream secret body", status=403, body="private")
    result = fns["canvas_dashboard"]()
    assert result["status"] == "forbidden" and not result["coverage"]["complete"]
    assert "secret" not in json.dumps(result) and "private" not in json.dumps(result)


def test_inbox_read_never_marks_read():
    c, _, fns = server()
    c.inbox.get.return_value = {"id": 3, "workflow_state": "unread", "messages": []}
    result = fns["canvas_inbox_thread"](3)
    assert result["status"] == "ok"
    c.inbox.get.assert_called_once_with(3, auto_mark_as_read=False)


def test_file_metadata_never_exposes_download_credential():
    c, _, fns = server()
    c.files.get.return_value = {"id": 3, "display_name": "paper.pdf", "url": "https://cdn.example/token-in-path/secret"}
    result = fns["canvas_get_file"](3)
    assert "secret" not in json.dumps(result)
    assert result["data"]["read"]["tool"] == "canvas_read_file"


def test_permission_groups_cannot_execute_another_action_kind():
    c, _, fns = server()
    receipt = fns["canvas_prepare_action"]({"kind": "upload", "filename": "a.txt", "content_base64": "aGk="})["data"]
    assert receipt["execute"] == {"tool": "canvas_upload_file", "action_id": receipt["action_id"]}
    for name in ("canvas_submit_assignment", "canvas_send_message", "canvas_add_submission_comment",
                 "canvas_post_discussion", "canvas_update_inbox", "canvas_update_planner"):
        result = fns[name](receipt["action_id"])
        assert result["status"] == "invalid_request"
    c.files.upload.assert_not_called()
    assert fns["canvas_action_status"](receipt["action_id"])["data"]["state"] == "prepared"
