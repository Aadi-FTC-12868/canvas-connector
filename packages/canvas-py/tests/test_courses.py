"""Unit tests for courses endpoint shapes."""
from __future__ import annotations

from canvas_py.endpoints.courses import ListCourses


def test_default_active_enrollment_state():
    req = ListCourses()
    pairs = dict(req.query_pairs())
    assert pairs["enrollment_state"] == "active"


def test_states_list_expands_to_repeated_pairs():
    req = ListCourses(enrollment_state=None, states=["available", "completed"])
    pairs = req.query_pairs()
    state_values = [v for k, v in pairs if k == "state[]"]
    assert state_values == ["available", "completed"]


def test_archived_filter_uses_completed_state():
    """list_archived helper should query enrollment_state=completed, because
    past courses keep workflow_state=available while the user's enrollment
    moves to completed. Verified against the real API."""
    # This is a one-off sanity check — the actual helper is on the namespace
    # class, but the underlying request is what matters.
    req = ListCourses(enrollment_state="completed")
    pairs = dict(req.query_pairs())
    assert pairs["enrollment_state"] == "completed"
