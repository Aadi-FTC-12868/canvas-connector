"""Behavioral regressions for false all-clear digests and missing-work audits."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from canvas_py._errors import CanvasAuthError, CanvasPermissionError, CanvasServerError
from canvas_py.models.assignment import Assignment
from canvas_py.tools._audits import missing_work_audit
from canvas_py.tools._digests import catch_me_up_on_course, whats_new, _extract_upcoming_from_planner, _partition_upcoming


def fail(error):
    def call(*args, **kwargs):
        raise error
    return call


def canvas_fixture(count=1):
    courses = [NS(id=i, display_name=f"Course {i}", enrollments=[], term=None, course_progress=None) for i in range(1, count+1)]
    return NS(
        courses=NS(list_active=lambda: courses, get=lambda cid: courses[cid-1]),
        assignments=NS(list=lambda *args, **kwargs: []),
        activity=NS(summary=lambda: []), inbox=NS(unread=lambda: []),
        planner=NS(items=lambda **kwargs: []),
        announcements=NS(list_across=lambda *args, **kwargs: []),
        discussions=NS(list=lambda *args, **kwargs: []),
    )


@pytest.mark.parametrize("bundle", [whats_new, missing_work_audit, lambda c: catch_me_up_on_course(c, 1)])
def test_auth_failure_is_never_converted_to_successful_empty_result(bundle):
    canvas = canvas_fixture()
    canvas.assignments.list = fail(CanvasAuthError("expired", status=401))
    with pytest.raises(CanvasAuthError):
        bundle(canvas)


@pytest.mark.parametrize("bundle", [whats_new, missing_work_audit, lambda c: catch_me_up_on_course(c, 1)])
def test_course_auth_failure_retains_type(bundle):
    canvas = canvas_fixture()
    canvas.courses.list_active = canvas.courses.get = fail(CanvasAuthError("expired", status=401))
    with pytest.raises(CanvasAuthError):
        bundle(canvas)


@pytest.mark.parametrize("bundle", [whats_new, missing_work_audit, lambda c: catch_me_up_on_course(c, 1)])
def test_assignment_permission_failure_is_structured_incomplete_result(bundle):
    canvas = canvas_fixture()
    canvas.assignments.list = fail(CanvasPermissionError("not available", status=403))
    result = bundle(canvas)
    assert result.coverage.complete is False
    assert result.coverage.status == "partial"
    assert result.warnings
    failed = [s for s in result.coverage.surfaces if s.status == "unavailable"]
    assert len(failed) == 1
    assert failed[0].course_id == 1 and failed[0].http_status == 403
    assert "Incomplete results" in result.summary
    assert "nothing urgent" not in result.summary and "quiet" not in result.summary


def test_failed_announcements_cannot_be_silently_empty():
    canvas = canvas_fixture()
    canvas.announcements.list_across = fail(CanvasPermissionError("forbidden", status=403))
    result = whats_new(canvas)
    assert result.coverage.status == "partial"
    assert any(s.surface == "announcements" and s.status == "unavailable" for s in result.coverage.surfaces)


def test_feedback_scans_every_course_instead_of_silently_capping_at_eight():
    canvas = canvas_fixture(10)
    seen = []
    canvas.assignments.list = lambda cid: seen.append(cid) or []
    result = whats_new(canvas)
    assert seen == list(range(1, 11))
    assert result.coverage.complete


def test_failed_course_discovery_marks_dependent_checks_skipped():
    canvas = canvas_fixture()
    canvas.courses.list_active = fail(CanvasServerError("server down", status=500))
    result = whats_new(canvas)
    assert result.coverage.status == "partial"
    assert {s.surface for s in result.coverage.surfaces if s.status == "skipped"} == {"announcements", "recent_feedback"}


def test_audit_uses_all_assignments_and_keeps_submitted_late_work():
    canvas = canvas_fixture()
    def assignments(cid, **kwargs):
        assert kwargs.get("bucket") is None
        assert "submission" in kwargs["include"]
        return [Assignment(id=1, name="Submitted late", submission={"late": True, "missing": False, "workflow_state": "submitted"})]
    canvas.assignments.list = assignments
    result = missing_work_audit(canvas)
    assert result.total_late == 1
    assert result.total_missing == 0
    assert result.coverage.complete


@pytest.mark.parametrize("types", [["none"], ["not_graded"], ["on_paper"], ["online_upload"]])
def test_audit_does_not_invent_missing_flag_from_past_due_date(types):
    canvas = canvas_fixture()
    canvas.assignments.list = lambda *args, **kwargs: [Assignment(id=1, name="Old work", due_at=datetime.now(timezone.utc)-timedelta(days=3), submission_types=types)]
    result = missing_work_audit(canvas)
    assert result.total_missing == result.total_late == 0
    assert "Canvas-flagged" in result.summary


def test_explicit_missing_flag_is_kept_for_paper_work_unless_excused():
    canvas = canvas_fixture()
    canvas.assignments.list = lambda *args, **kwargs: [
        Assignment(id=1, name="Paper work", submission_types=["on_paper"], submission={"missing": True}),
        Assignment(id=2, name="Excused", submission_types=["on_paper"], submission={"missing": True, "excused": True}),
    ]
    result = missing_work_audit(canvas)
    assert result.total_missing == 1
    assert result.by_course[0].items[0].plannable_id == 1


@pytest.mark.parametrize("submissions,types", [
    (None, None), ({"submitted": False}, ["on_paper"]), ({"submitted": False}, ["none"]),
    ({"submitted": True, "late": True}, ["online_upload"]), ({"missing": True, "excused": True}, ["online_upload"]),
])
def test_planner_does_not_classify_unknown_paper_late_or_excused_as_overdue(submissions, types):
    planner = NS(course_id=1, context_name="Course", plannable_id=1, plannable_type="assignment", plannable_date=datetime.now(timezone.utc)-timedelta(days=1), plannable={"title": "Work", "submission_types": types}, submissions=submissions, html_url=None)
    assert _partition_upcoming(_extract_upcoming_from_planner([planner]))[2] == []


def test_course_catchup_retains_urgent_items_in_structured_result():
    canvas = canvas_fixture()
    canvas.planner.items = lambda **kwargs: [NS(course_id=1, context_name="Course", plannable_id=1, plannable_type="assignment", plannable_date=datetime.now(timezone.utc)+timedelta(hours=2), plannable={"title": "Due soon"}, submissions={"submitted": False}, html_url=None)]
    result = catch_me_up_on_course(canvas, 1)
    assert result.urgent[0].title == "Due soon"
