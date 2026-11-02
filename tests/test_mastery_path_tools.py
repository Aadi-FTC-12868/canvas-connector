"""Real MCP Mastery Path contracts against an actual Canvas MockTransport client."""
import asyncio
import json
from unittest.mock import Mock

import pytest
from mcp.server.fastmcp import FastMCP

from canvas_connector.tools import register_tools
from test_mastery_paths import SELECT, MasteryHTTP


@pytest.fixture
def boundary():
    state, authorize = MasteryHTTP(), Mock()
    mcp = FastMCP("mastery-path-boundary")
    register_tools(mcp, lambda: state.canvas, authorize, owner=lambda: "alice")
    registered = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    call = {name: (lambda *args, fn=tool.fn, **kwargs: asyncio.run(fn(*args, **kwargs)))
            for name, tool in registered.items()}
    yield state, registered, call, authorize
    state.canvas.close()


def test_mastery_annotations_and_exact_action_schema(boundary):
    _, registered, _, _ = boundary
    assert registered["canvas_mastery_path"].annotations.readOnlyHint is True
    execute = registered["canvas_select_mastery_path"]
    assert execute.annotations.readOnlyHint is False
    assert execute.annotations.destructiveHint is True
    assert set(execute.parameters["properties"]) == {"action_id"}
    model = registered["canvas_prepare_action"].parameters["$defs"]["MasteryPathSelect"]
    assert model["additionalProperties"] is False
    assert set(model["properties"]) == {"kind", "course_id", "module_id", "item_id", "assignment_set_id"}


def test_mastery_read_window_is_only_gets_and_offered_ids(boundary):
    state, _, call, authorize = boundary
    result = call["canvas_mastery_path"](1, 2, 3, limit=1)
    assert result["status"] == "ok"
    assert result["data"]["assignment_sets"][0]["id"] == 40
    assert result["data"]["availability"]["can_select"] is True
    assert result["data"]["coursework_complete"] is None
    assert result["coverage"]["next_offset"] == 1
    assert result["coverage"]["complete"] is False
    assert result["source"]["canvas_origin"] == "https://school.example"
    assert all(r.method == "GET" for r in state.requests)
    authorize.assert_not_called()


@pytest.mark.parametrize("access_status", [None, 403])
def test_distinct_executor_and_partial_assignment_access_coverage(boundary, access_status):
    state, _, call, authorize = boundary
    prepared = call["canvas_prepare_action"](SELECT)["data"]
    action_id = prepared["action_id"]
    assert prepared["execute"] == {"tool": "canvas_select_mastery_path", "action_id": action_id}
    assert not state.writes
    authorize.assert_not_called()
    assert call["canvas_submit_assignment"](action_id)["status"] == "invalid_request"
    assert call["canvas_update_module_progress"](action_id)["status"] == "invalid_request"
    assert call["canvas_action_status"](action_id)["data"]["state"] == "prepared"
    if access_status:
        state.errors["courses/1/assignments/101"] = access_status
    result = call["canvas_select_mastery_path"](action_id)
    assert result["status"] == "succeeded"
    assert result["coverage"]["selection_verified"] is True
    assert result["coverage"]["complete"] is (access_status is None)
    assert result["data"]["result"]["selected_set_id"] == 40
    assert result["data"]["result"]["coursework_complete"] is None
    assert call["canvas_select_mastery_path"](action_id)["status"] == "succeeded"
    assert len(state.writes) == 1


@pytest.mark.parametrize("change", ["selected", "assignment", "permission"])
def test_stale_preflight_rejects_without_post(boundary, change):
    state, _, call, _ = boundary
    action_id = call["canvas_prepare_action"](SELECT)["data"]["action_id"]
    if change == "selected":
        state.path.update(selected_set_id=50, awaiting_choice=False)
    elif change == "assignment":
        state.sets[0]["assignment_set_associations"][0].update(assignment_id=102, model=state.assignments[102])
    else:
        state.permissions["participate_as_student"] = False
    result = call["canvas_select_mastery_path"](action_id)
    assert result["status"] == "rejected"
    assert result["data"]["result"]["upstream_write_attempted"] is False
    assert not state.writes


@pytest.mark.parametrize("failure", ["ignored", "cached", "denied", "redirect", "rate_limited"])
def test_uncertain_selection_is_not_replayed(boundary, failure):
    state, _, call, _ = boundary
    action_id = call["canvas_prepare_action"](SELECT)["data"]["action_id"]
    if failure == "ignored":
        state.ignore_write = True
    elif failure == "cached":
        state.cached_readback = True
    elif failure == "denied":
        state.after_write = lambda state: state.errors.update({"courses/1/module_item_sequence": 403})
    else:
        state.write_status = 307 if failure == "redirect" else 429
    result = call["canvas_select_mastery_path"](action_id)
    assert result["status"] == "uncertain"
    assert result["coverage"]["verification"] is False
    assert result["data"]["result"]["upstream_write_attempted"] is True
    assert call["canvas_select_mastery_path"](action_id)["status"] == "uncertain"
    assert len(state.writes) == 1


def test_authorization_failure_keeps_prepared_receipt(boundary):
    state, _, call, authorize = boundary
    action_id = call["canvas_prepare_action"](SELECT)["data"]["action_id"]
    authorize.side_effect = ValueError("Write access is disabled")
    assert call["canvas_select_mastery_path"](action_id)["status"] == "invalid_request"
    assert call["canvas_action_status"](action_id)["data"]["state"] == "prepared"
    assert not state.writes


def test_failed_read_is_incomplete_not_empty_or_unavailable_feature(boundary):
    state, _, call, _ = boundary
    state.errors["courses/1/module_item_sequence"] = 403
    result = call["canvas_mastery_path"](1, 2, 3)
    assert result["status"] == "forbidden"
    assert result["data"] is None
    assert result["coverage"]["complete"] is False
    assert "private-upstream-body" not in json.dumps(result)
    assert not state.writes
