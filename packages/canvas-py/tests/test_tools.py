"""Unit tests for intent-bundle helper logic.

The full bundles (whats_new, catch_me_up_on_course, missing_work_audit)
compose many live calls and are validated via live integration rather
than per-call mocking — mocking every sub-surface would be more fragile
than the function it's testing. Here we test the pure helpers that carry
the correctness-critical classification logic.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from canvas_py.tools._digests import (
    _WORK_PLANNABLE_TYPES,
    _extract_upcoming_from_planner,
    _grade_from_enrollment,
    _partition_upcoming,
)
from canvas_py.tools._models import UpcomingItem


# --- _partition_upcoming -----------------------------------------------

def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def test_partition_urgent_vs_upcoming():
    now = _now()
    items = [
        UpcomingItem(title="now+6h", due_at=now + timedelta(hours=6), plannable_type="assignment"),
        UpcomingItem(title="now+40h", due_at=now + timedelta(hours=40), plannable_type="assignment"),
        UpcomingItem(title="now+120h", due_at=now + timedelta(hours=120), plannable_type="assignment"),
    ]
    urgent, upcoming, overdue = _partition_upcoming(items)
    assert [x.title for x in urgent] == ["now+6h", "now+40h"]
    assert [x.title for x in upcoming] == ["now+120h"]
    assert overdue == []


def test_partition_overdue_vs_submitted():
    now = _now()
    items = [
        UpcomingItem(title="past-missing", due_at=now - timedelta(days=2), plannable_type="assignment", status="missing"),
        UpcomingItem(title="past-submitted", due_at=now - timedelta(days=2), plannable_type="assignment", status="submitted"),
        UpcomingItem(title="past-graded", due_at=now - timedelta(days=2), plannable_type="assignment", status="graded"),
    ]
    _urgent, _upcoming, overdue = _partition_upcoming(items)
    assert [x.title for x in overdue] == ["past-missing"]


def test_partition_excludes_announcements_and_calendar_events():
    """Announcements and calendar events in the past are NOT 'overdue' work."""
    now = _now()
    items = [
        UpcomingItem(title="old announcement", due_at=now - timedelta(days=5), plannable_type="announcement"),
        UpcomingItem(title="old calendar event", due_at=now - timedelta(days=5), plannable_type="calendar_event"),
        UpcomingItem(title="old planner note", due_at=now - timedelta(days=5), plannable_type="planner_note"),
        UpcomingItem(title="real missing hw", due_at=now - timedelta(days=5), plannable_type="assignment", status="missing"),
    ]
    urgent, upcoming, overdue = _partition_upcoming(items)
    # Only the assignment should end up overdue.
    assert [x.title for x in overdue] == ["real missing hw"]
    assert urgent == []
    assert upcoming == []


def test_partition_items_without_due_date_go_to_upcoming():
    items = [
        UpcomingItem(title="no-date", due_at=None, plannable_type="assignment"),
    ]
    urgent, upcoming, overdue = _partition_upcoming(items)
    assert [x.title for x in upcoming] == ["no-date"]


def test_work_plannable_types_set():
    assert "assignment" in _WORK_PLANNABLE_TYPES
    assert "quiz" in _WORK_PLANNABLE_TYPES
    assert "discussion_topic" in _WORK_PLANNABLE_TYPES
    assert "announcement" not in _WORK_PLANNABLE_TYPES
    assert "calendar_event" not in _WORK_PLANNABLE_TYPES


# --- _grade_from_enrollment --------------------------------------------

def test_grade_from_enrollment_prefers_current_period():
    course = SimpleNamespace(
        enrollments=[{
            "current_period_computed_current_grade": "A-",
            "current_period_computed_current_score": 91.5,
            "computed_current_grade": "B+",
            "computed_current_score": 87.0,
        }]
    )
    grade, score, label = _grade_from_enrollment(course)  # type: ignore[arg-type]
    assert grade == "A-"
    assert score == 91.5
    assert label == "current_period_current"


def test_grade_from_enrollment_falls_back_to_overall_current():
    course = SimpleNamespace(
        enrollments=[{
            "computed_current_grade": "B+",
            "computed_current_score": 87.0,
        }]
    )
    grade, score, label = _grade_from_enrollment(course)  # type: ignore[arg-type]
    assert grade == "B+"
    assert score == 87.0
    assert label == "computed_current"


def test_grade_from_enrollment_handles_missing_enrollments():
    course = SimpleNamespace(enrollments=None)
    grade, score, label = _grade_from_enrollment(course)  # type: ignore[arg-type]
    assert grade is None
    assert score is None
    assert label == "none"


def test_grade_from_enrollment_handles_empty_enrollments():
    course = SimpleNamespace(enrollments=[])
    grade, score, label = _grade_from_enrollment(course)  # type: ignore[arg-type]
    assert grade is None
    assert score is None


# --- _extract_upcoming_from_planner ------------------------------------

def test_extract_upcoming_from_planner_with_submissions_dict():
    """Planner items with submission state should get status set."""
    now = _now()
    planner = [
        SimpleNamespace(
            course_id=2966,
            context_name="Bio",
            plannable_id=5000,
            plannable_type="assignment",
            plannable_date=now + timedelta(hours=3),
            plannable={"title": "Lab writeup", "points_possible": 50},
            submissions={"missing": True, "submitted": False, "graded": False, "late": False},
            html_url="https://x/y/z",
        )
    ]
    items = _extract_upcoming_from_planner(planner)  # type: ignore[arg-type]
    assert len(items) == 1
    it = items[0]
    assert it.course_name == "Bio"
    assert it.title == "Lab writeup"
    assert it.status == "missing"
    assert it.points_possible == 50


def test_extract_upcoming_handles_submissions_false():
    """Planner items with submissions=False are non-submittable (announcements).
    Status should be None, type passed through."""
    now = _now()
    planner = [
        SimpleNamespace(
            course_id=None,
            context_name="Something",
            plannable_id=1,
            plannable_type="announcement",
            plannable_date=now,
            plannable={"title": "Hello"},
            submissions=False,
            html_url=None,
        )
    ]
    items = _extract_upcoming_from_planner(planner)  # type: ignore[arg-type]
    assert items[0].status is None
    assert items[0].plannable_type == "announcement"


def test_extract_course_name_override_from_map():
    """When a course_names map is provided, it takes precedence over context_name."""
    now = _now()
    planner = [
        SimpleNamespace(
            course_id=42,
            context_name="short",
            plannable_id=1,
            plannable_type="assignment",
            plannable_date=now,
            plannable={"title": "X"},
            submissions=None,
            html_url=None,
        )
    ]
    items = _extract_upcoming_from_planner(planner, course_names={42: "Long Full Course Name"})  # type: ignore[arg-type]
    assert items[0].course_name == "Long Full Course Name"
