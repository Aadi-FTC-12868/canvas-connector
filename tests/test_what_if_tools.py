"""What-if MCP envelopes and execution boundaries against synthetic Canvas HTTP."""
import asyncio
import json
from unittest.mock import Mock

import pytest
from mcp.server.fastmcp import FastMCP

from canvas_connector.tools import register_tools
from test_what_if_grades import COURSE_RESET, RESET, SET, WhatIfHTTP


@pytest.fixture
def boundary():
    state, authorize = WhatIfHTTP(), Mock()
    mcp = FastMCP("what-if-boundary")
    register_tools(mcp, lambda: state.canvas, authorize, owner=lambda: "alice")
    registered = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    call = {name: (lambda *args, fn=tool.fn, **kwargs: asyncio.run(fn(*args, **kwargs)))
            for name, tool in registered.items()}
    yield state, registered, call, authorize
    state.canvas.close()


def test_read_and_mutation_annotations_and_no_teacher_grade_fields(boundary):
    _, registered, _, _ = boundary
    assert registered["canvas_what_if_grades"].annotations.readOnlyHint is True
    mutation = registered["canvas_update_what_if_grades"]
    assert mutation.annotations.readOnlyHint is False
    assert mutation.annotations.destructiveHint is True
    assert set(mutation.parameters["properties"]) == {"action_id"}
    schema = registered["canvas_prepare_action"].parameters
    for name in ("WhatIfScoreSet", "WhatIfScoreReset", "WhatIfCourseReset"):
        model = schema["$defs"][name]
        assert model["additionalProperties"] is False
        assert not {"user_id", "submission_id", "posted_grade", "grade"} & set(model["properties"])


def test_read_tool_is_get_only_with_saved_scores_and_boundary(boundary):
    state, _, call, authorize = boundary
    result = call["canvas_what_if_grades"](1, assignment_id=2)
    assert result["status"] == "ok"
    assert result["data"]["submissions"][0]["student_entered_score"] == 12.5
    assert result["data"]["aggregate_grades"]["status"] == "not_calculated"
    assert result["source"]["canvas_origin"] == "https://school.example"
    assert result["coverage"]["complete"] is True
    assert not state.writes
    authorize.assert_not_called()


@pytest.mark.parametrize("payload", [SET, RESET, COURSE_RESET])
def test_execution_group_isolation_and_readback_coverage_survive_mcp(boundary, payload):
    state, _, call, authorize = boundary
    prepared = call["canvas_prepare_action"](payload)["data"]
    action_id = prepared["action_id"]
    assert prepared["execute"] == {"tool": "canvas_update_what_if_grades", "action_id": action_id}
    assert not state.writes
    authorize.assert_not_called()
    assert call["canvas_submit_assignment"](action_id)["status"] == "invalid_request"
    assert call["canvas_update_preferences"](action_id)["status"] == "invalid_request"
    assert call["canvas_action_status"](action_id)["data"]["state"] == "prepared"
    result = call["canvas_update_what_if_grades"](action_id)
    assert result["status"] == "succeeded"
    assert result["coverage"]["verification"] is True
    assert result["data"]["result"]["actual_grades_unchanged"] is True
    assert state.submissions[2]["score"] == 10
    assert state.submissions[2]["grade"] == "10"
    if payload == COURSE_RESET:
        assert result["coverage"]["complete"] is False
        assert result["coverage"]["scope"] == "canvas_visible_own_submissions"
        assert result["coverage"]["visible_submissions_complete"] is True
        assert "hidden" in prepared["review"]["target"]["verification_note"]
    else:
        assert result["coverage"]["complete"] is True
        assert result["coverage"]["scope"] == "requested_own_submission"
    assert call["canvas_update_what_if_grades"](action_id)["status"] == "succeeded"
    assert len(state.writes) == 1


@pytest.mark.parametrize("field,value", [("student_entered_score", 5), ("score", 8), ("user_id", 99)])
def test_stale_target_rejected_before_any_write(boundary, field, value):
    state, _, call, _ = boundary
    action_id = call["canvas_prepare_action"](SET)["data"]["action_id"]
    state.submissions[2][field] = value
    result = call["canvas_update_what_if_grades"](action_id)
    assert result["status"] == "rejected"
    assert result["data"]["result"]["upstream_write_attempted"] is False
    assert not state.writes


@pytest.mark.parametrize("failure", ["ignore_write", "actual_grade_changed", "readback_denied"])
def test_uncertain_readback_cannot_be_replayed(boundary, failure):
    state, _, call, _ = boundary
    action_id = call["canvas_prepare_action"](SET)["data"]["action_id"]
    if failure == "ignore_write":
        state.ignore_write = True
    elif failure == "actual_grade_changed":
        state.after_write = lambda state: state.submissions[2].update(score=99)
    else:
        state.readback_status = 403
    result = call["canvas_update_what_if_grades"](action_id)
    assert result["status"] == "uncertain"
    assert result["coverage"]["verification"] is False
    assert result["data"]["result"]["upstream_write_attempted"] is True
    assert call["canvas_update_what_if_grades"](action_id)["status"] == "uncertain"
    assert len(state.writes) == 1


def test_authorization_denial_preserves_prepared_receipt(boundary):
    state, _, call, authorize = boundary
    action_id = call["canvas_prepare_action"](SET)["data"]["action_id"]
    authorize.side_effect = ValueError("Write access is disabled")
    result = call["canvas_update_what_if_grades"](action_id)
    assert result["status"] == "invalid_request"
    assert call["canvas_action_status"](action_id)["data"]["state"] == "prepared"
    assert not state.writes


def test_read_failure_is_incomplete_not_empty(boundary):
    state, _, call, _ = boundary
    state.submissions[2].pop("student_entered_score")
    result = call["canvas_what_if_grades"](1, assignment_id=2)
    assert result["status"] == "invalid_request"
    assert result["coverage"]["complete"] is False
    assert result["data"] is None
    assert "absence does not mean reset" in json.dumps(result)
    assert not state.writes
