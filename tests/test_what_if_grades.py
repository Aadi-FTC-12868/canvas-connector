"""Dedicated learner what-if APIs through the real client and synthetic HTTP."""
import copy
import json
from typing import Annotated

import httpx
import pytest
from canvas_py import Canvas
from canvas_py._config import CanvasConfig
from canvas_py._errors import CanvasError
from pydantic import Field, TypeAdapter, ValidationError

from canvas_connector import what_if_grades as wi

ADAPTER = TypeAdapter(Annotated[wi.WhatIfActionTypes, Field(discriminator="kind")])
SET = {"kind": "what_if_score_set", "course_id": 1, "assignment_id": 2, "score": 19.75}
RESET = {"kind": "what_if_score_reset", "course_id": 1, "assignment_id": 2}
COURSE_RESET = {"kind": "what_if_course_reset", "course_id": 1}


class WhatIfHTTP:
    def __init__(self):
        self.profile = {"id": 7}
        self.course = {"id": 1, "name": "Synthetic Science", "workflow_state": "available",
                       "restrict_quantitative_data": False, "hide_final_grades": False}
        self.enrollments = [{"id": 11, "user_id": 7, "course_id": 1,
                             "type": "StudentEnrollment", "enrollment_state": "active"}]
        self.permissions = {"read_grades": True, "reset_what_if_grades": True}
        self.assignments = {2: {"id": 2, "course_id": 1, "name": "Synthetic lab",
            "points_possible": 20.0, "grading_type": "points", "published": True,
            "omit_from_final_grade": False, "locked_for_user": False}}
        self.submissions = {2: {"id": 32, "assignment_id": 2, "user_id": 7,
            "student_entered_score": 12.5, "score": 10.0, "grade": "10",
            "entered_score": 10.0, "entered_grade": "10", "excused": False,
            "workflow_state": "graded", "grade_matches_current_submission": True,
            "graded_at": "2026-01-02T10:00:00Z", "posted_at": "2026-01-02T10:00:00Z",
            "grader_id": 9, "attempt": 1, "assignment_visible": True}}
        self.grades = [{"current": {"grade": 91.75, "total": 30, "possible": 40, "dropped": []},
                        "final": {"grade": 68.25, "total": 30, "possible": 80, "dropped": []},
                        "current_groups": {"4": {"weight": 70}}}]
        self.requests, self.writes = [], []
        self.page_size = 100
        self.ignore_write = False
        self.after_write = None
        self.readback_status = None
        self.write_status = None
        self.list_override = None
        self.canvas = Canvas(CanvasConfig("https://school.example", "synthetic-token"),
                             http_client=httpx.Client(transport=httpx.MockTransport(self.handle)))

    def add_assignment(self, assignment_id=3):
        self.assignments[assignment_id] = {**self.assignments[2], "id": assignment_id, "name": "Another synthetic lab"}
        self.submissions[assignment_id] = {**self.submissions[2], "id": 30 + assignment_id, "assignment_id": assignment_id,
                                           "student_entered_score": 8.0}

    def handle(self, request):
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/v1/")
        if request.method != "GET":
            self.writes.append(request)
            if self.write_status:
                return httpx.Response(self.write_status, json={"error": "synthetic denial"})
            assert request.method == "PUT"
            if path == "courses/1/what_if_grades/reset":
                assert not request.content
                if not self.ignore_write:
                    for submission in self.submissions.values():
                        submission["student_entered_score"] = None
                        submission["updated_at"] = "2026-01-04T10:00:00Z"
                response = {"grades": self.grades}
            else:
                assert path == "submissions/32/what_if_grades"
                body = json.loads(request.content)
                assert set(body) == {"student_entered_score"}
                if not self.ignore_write:
                    self.submissions[2]["student_entered_score"] = body["student_entered_score"]
                # Echo claims success even in ignore_write case; only GET proves it.
                response = {"grades": self.grades, "submission": {"id": 32, **body}}
            if self.after_write:
                self.after_write(self)
            return httpx.Response(200, json=response)
        assert not request.content
        if self.writes and self.readback_status:
            return httpx.Response(self.readback_status, json={"error": "synthetic denial"})
        if path == "users/self/profile":
            result = self.profile
        elif path == "courses/1":
            assert "restrict_quantitative_data" in request.url.params.get_list("include[]")
            result = self.course
        elif path == "courses/1/enrollments":
            assert request.url.params["user_id"] == str(self.profile["id"])
            assert request.url.params.get_list("type[]") == ["StudentEnrollment"]
            result = self.enrollments
        elif path == "courses/1/permissions":
            assert set(request.url.params.get_list("permissions[]")) == {"read_grades", "reset_what_if_grades"}
            result = self.permissions
        elif path == "courses/1/students/submissions":
            if request.url.params.get("page") is None:
                assert request.url.params["student_ids[]"] == "self"
                assert "assignment" in request.url.params.get_list("include[]")
                self.assert_score_query(request)
            rows = [{**sub, "assignment": self.assignments[key]} for key, sub in self.submissions.items()]
            if self.list_override is not None:
                rows = self.list_override
            if not isinstance(rows, list):
                return httpx.Response(200, json=rows)
            page = int(request.url.params.get("page", 1))
            start = (page - 1) * self.page_size
            more = start + self.page_size < len(rows)
            headers = {"Link": f'<https://school.example/api/v1/courses/1/students/submissions?page={page+1}>; rel="next"'} if more else {}
            return httpx.Response(200, json=rows[start:start + self.page_size], headers=headers)
        elif path == "courses/1/assignments/2":
            result = self.assignments[2]
        elif path == "courses/1/assignments/2/submissions/self":
            self.assert_score_query(request)
            result = self.submissions[2]
        else:
            raise AssertionError((request.method, path))
        return httpx.Response(200, json=result)

    @staticmethod
    def assert_score_query(request):
        assert {"student_entered_score", "visibility"}.issubset(request.url.params.get_list("include[]"))
        assert "read_state" not in request.url.params.get_list("include[]")
        assert "read_status" not in request.url.params.get_list("include[]")
        assert "attachments" in request.url.params.get_list("exclude_response_fields[]")


@pytest.mark.parametrize("payload,expected,path", [
    (SET, 19.75, "submissions/32/what_if_grades"),
    (RESET, None, "submissions/32/what_if_grades"),
    (COURSE_RESET, None, "courses/1/what_if_grades/reset"),
])
def test_exact_dedicated_write_and_independent_readback(payload, expected, path):
    state = WhatIfHTTP()
    state.add_assignment()
    before = copy.deepcopy(state.submissions)
    snapshot = wi.snapshot(state.canvas, payload)
    assert not state.writes
    result = wi.perform(state.canvas, payload, snapshot)
    assert len(state.writes) == 1
    assert state.writes[0].url.path == "/api/v1/" + path
    assert state.requests[-1].method == "GET"
    assert state.submissions[2]["student_entered_score"] == expected
    assert state.submissions[2]["score"] == before[2]["score"]
    assert result["verified"] and result["actual_grades_unchanged"]
    assert result["aggregate_grades"]["grades"]["current"]["grade"] == 91.75
    assert result["aggregate_grades"]["source"] == "canvas_mutation_response"
    assert result["aggregate_grades"]["independently_recalculated"] is False
    assert wi.ROUTES[payload["kind"]] == "canvas_update_what_if_grades"
    if payload == COURSE_RESET:
        assert state.submissions[3]["student_entered_score"] is None
        assert result["coverage"]["complete"] is False
        assert result["coverage"]["visible_submissions_verified"] == 2
        assert "hidden" in snapshot["verification_note"]
        assert result["coverage"]["scope"] == "canvas_visible_own_submissions"
    else:
        assert state.submissions[3] == before[3]
        assert result["coverage"]["complete"] is True


@pytest.mark.parametrize("value", [True, False, "20", "20%", "EX", None, float("nan"), float("inf"), -float("inf")])
def test_score_must_be_finite_json_number(value):
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({**SET, "score": value})


@pytest.mark.parametrize("value,expected", [(0, 0.0), (-5.125, -5.13), (20.125, 20.13), (200, 200.0), (1e100, 1e100)])
def test_canvas_score_precision_without_invented_points_cap(value, expected):
    state = WhatIfHTTP()
    payload = {**SET, "score": value}
    assert ADAPTER.validate_python(payload).score == expected
    wi.perform(state.canvas, payload, wi.snapshot(state.canvas, payload))
    assert json.loads(state.writes[0].content)["student_entered_score"] == expected


@pytest.mark.parametrize("field", ["course_id", "assignment_id"])
@pytest.mark.parametrize("value", [True, 0, -1, "1", 1.0, 2**63, None])
def test_strict_ids(field, value):
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({**SET, field: value})


@pytest.mark.parametrize("extra", [{"user_id": 8}, {"submission_id": 44}, {"url": "https://evil.example"},
    {"posted_grade": 100}, {"submission": {"score": 100}}, {"as_user_id": 8}])
def test_model_cannot_select_user_submission_url_or_actual_grade(extra):
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({**SET, **extra})


@pytest.mark.parametrize("target,key,value", [
    ("profile", "id", True), ("profile", "id", "7"),
    ("course", "id", 9), ("course", "workflow_state", "completed"),
    ("course", "restrict_quantitative_data", True), ("course", "restrict_quantitative_data", None),
    ("course", "access_restricted_by_date", True),
    ("enrollment", "user_id", 8), ("enrollment", "course_id", 9),
    ("enrollment", "type", "TeacherEnrollment"), ("enrollment", "enrollment_state", "completed"),
    ("permissions", "read_grades", False), ("permissions", "reset_what_if_grades", False),
    ("permissions", "reset_what_if_grades", None),
    ("assignment", "id", 9), ("assignment", "course_id", 9),
    ("assignment", "published", False), ("assignment", "grading_type", "not_graded"),
    ("submission", "user_id", 8), ("submission", "assignment_id", 9),
    ("submission", "id", None), ("submission", "assignment_visible", False),
    ("submission", "permissions", {"submit": False}), ("submission", "permissions", {"submit": 1}),
    ("submission", "permissions", {"read": False}),
    ("submission", "student_entered_score", "NaN"),
])
def test_identity_policy_and_availability_fail_before_write(target, key, value):
    state = WhatIfHTTP()
    objects = {"profile": state.profile, "course": state.course, "enrollment": state.enrollments[0],
               "permissions": state.permissions, "assignment": state.assignments[2], "submission": state.submissions[2]}
    objects[target][key] = value
    with pytest.raises(ValueError):
        wi.snapshot(state.canvas, SET)
    assert not state.writes


@pytest.mark.parametrize("field", ["student_entered_score", "score", "grade", "assignment_visible"])
def test_omitted_score_does_not_mean_null_or_reset(field):
    state = WhatIfHTTP()
    state.submissions[2].pop(field)
    with pytest.raises(ValueError):
        wi.snapshot(state.canvas, SET)
    assert not state.writes


def test_coursework_lock_does_not_block_own_hypothetical_score():
    state = WhatIfHTTP()
    state.assignments[2].update(locked_for_user=True, can_submit=False)
    result = wi.perform(state.canvas, SET, wi.snapshot(state.canvas, SET))
    assert result["verified"]
    assert len(state.writes) == 1


@pytest.mark.parametrize("payload", [SET, RESET, COURSE_RESET])
def test_success_echo_without_saved_score_is_not_verification(payload):
    state = WhatIfHTTP()
    before = wi.snapshot(state.canvas, payload)
    state.ignore_write = True
    with pytest.raises(ValueError, match="not confirmed"):
        wi.perform(state.canvas, payload, before)
    assert len(state.writes) == 1


@pytest.mark.parametrize("field,value", [("score", 99), ("grade", "99"), ("entered_score", 99),
    ("excused", True), ("grade_matches_current_submission", False), ("posted_at", None)])
@pytest.mark.parametrize("payload", [SET, COURSE_RESET])
def test_actual_grade_change_prevents_verified_success(field, value, payload):
    state = WhatIfHTTP()
    before = wi.snapshot(state.canvas, payload)
    state.after_write = lambda state: state.submissions[2].update({field: value})
    with pytest.raises(ValueError, match="not confirmed"):
        wi.perform(state.canvas, payload, before)
    assert len(state.writes) == 1


def test_readback_submission_identity_mismatch_is_not_success():
    state = WhatIfHTTP()
    before = wi.snapshot(state.canvas, SET)
    state.after_write = lambda state: state.submissions[2].update(id=999)
    with pytest.raises(ValueError, match="not confirmed"):
        wi.perform(state.canvas, SET, before)


@pytest.mark.parametrize("payload", [SET, COURSE_RESET])
def test_readback_denied_is_not_empty_or_success(payload):
    state = WhatIfHTTP()
    before = wi.snapshot(state.canvas, payload)
    state.readback_status = 403
    with pytest.raises(CanvasError):
        wi.perform(state.canvas, payload, before)
    assert len(state.writes) == 1


def test_api_write_denial_does_not_retry():
    state = WhatIfHTTP()
    before = wi.snapshot(state.canvas, SET)
    state.write_status = 403
    with pytest.raises(CanvasError):
        wi.perform(state.canvas, SET, before)
    assert len(state.writes) == 1


@pytest.mark.parametrize("assignment_id", [None, 2])
def test_read_is_get_only_and_does_not_fabricate_calculated_total(assignment_id):
    state = WhatIfHTTP()
    before = copy.deepcopy(state.submissions)
    result = wi.read_what_if_grades(state.canvas, 1, assignment_id)
    assert result["status"] == "ok"
    assert result["data"]["submissions"][0]["student_entered_score"] == 12.5
    assert result["data"]["submissions"][0]["actual_grade"]["score"] == 10
    assert result["data"]["submissions"][0]["available_for_what_if"] is True
    assert result["data"]["aggregate_grades"]["status"] == "not_calculated"
    assert result["source"]["checked_at"]
    assert not state.writes and state.submissions == before
    assert all(r.method == "GET" for r in state.requests)


@pytest.mark.parametrize("restricted", [True, None])
def test_restricted_or_unknown_numeric_visibility_is_discovery_only(restricted):
    state = WhatIfHTTP()
    state.course["restrict_quantitative_data"] = restricted
    result = wi.read_what_if_grades(state.canvas, 1)
    assert result["status"] == "unavailable"
    assert result["coverage"]["complete"] is False
    assert "submissions" not in result["data"]
    assert result["data"]["availability"]["can_change"] is False
    assert not state.writes


def test_completed_enrollment_can_read_but_cannot_change():
    state = WhatIfHTTP()
    state.enrollments[0]["enrollment_state"] = "completed"
    state.permissions["reset_what_if_grades"] = False
    result = wi.read_what_if_grades(state.canvas, 1, 2)
    assert result["status"] == "ok"
    assert result["data"]["submissions"][0]["available_for_what_if"] is False


def test_canvas_decimal_string_score_is_read_as_numeric():
    state = WhatIfHTTP()
    state.submissions[2]["student_entered_score"] = "12.50"
    result = wi.read_what_if_grades(state.canvas, 1, 2)
    assert result["data"]["submissions"][0]["student_entered_score"] == 12.5


def test_explicit_course_access_restriction_is_discovery_only():
    state = WhatIfHTTP()
    state.course["access_restricted_by_date"] = True
    result = wi.read_what_if_grades(state.canvas, 1)
    assert result["status"] == "unavailable"
    assert "submissions" not in result["data"]


@pytest.mark.parametrize("hide", [True, None])
def test_hidden_or_unknown_course_totals_are_not_disclosed(hide):
    state = WhatIfHTTP()
    state.course["hide_final_grades"] = hide
    result = wi.perform(state.canvas, SET, wi.snapshot(state.canvas, SET))
    assert result["verified"]
    assert result["aggregate_grades"]["status"] == "hidden"
    assert "grades" not in result["aggregate_grades"]


def test_read_follows_pages_and_has_bounded_window():
    state = WhatIfHTTP()
    state.add_assignment()
    state.page_size = 1
    result = wi.read_what_if_grades(state.canvas, 1, limit=1)
    assert len(result["data"]["submissions"]) == 1
    assert result["coverage"]["next_offset"] == 1
    assert result["coverage"]["complete"] is False
    assert any(r.url.params.get("page") == "2" for r in state.requests)
    result = wi.read_what_if_grades(state.canvas, 1, offset=1, limit=1)
    assert result["data"]["submissions"][0]["assignment_id"] == 3
    assert result["coverage"]["next_offset"] is None
    assert result["coverage"]["complete"] is False


def test_course_reset_exhausts_pages_and_refuses_overflow(monkeypatch):
    state = WhatIfHTTP()
    state.add_assignment()
    state.page_size = 1
    before = wi.snapshot(state.canvas, COURSE_RESET)
    assert len(before["submissions"]) == 2
    monkeypatch.setattr(wi, "_MAX_DISCOVERY", 1)
    with pytest.raises(ValueError, match="safe discovery limit"):
        wi.snapshot(state.canvas, COURSE_RESET)
    assert not state.writes


@pytest.mark.parametrize("mode", ["duplicate", "other_user", "wrong_course", "no_assignment", "hidden", "object"])
def test_course_reset_requires_exact_complete_visible_own_rows(mode):
    state = WhatIfHTTP()
    row = {**state.submissions[2], "assignment": copy.deepcopy(state.assignments[2])}
    if mode == "duplicate":
        state.list_override = [row, row]
    elif mode == "other_user":
        row["user_id"] = 8
    elif mode == "wrong_course":
        row["assignment"]["course_id"] = 9
    elif mode == "no_assignment":
        row.pop("assignment")
    elif mode == "hidden":
        row["assignment_visible"] = False
    else:
        state.list_override = {"error": "not an empty list"}
    if state.list_override is None:
        state.list_override = [row]
    with pytest.raises(ValueError):
        wi.snapshot(state.canvas, COURSE_RESET)
    assert not state.writes


def test_course_reset_disappearing_row_is_not_verified():
    state = WhatIfHTTP()
    state.add_assignment()
    before = wi.snapshot(state.canvas, COURSE_RESET)
    state.after_write = lambda state: state.submissions.pop(3)
    with pytest.raises(ValueError, match="not confirmed"):
        wi.perform(state.canvas, COURSE_RESET, before)
    assert len(state.writes) == 1


@pytest.mark.parametrize("args", [{"offset": -1}, {"limit": 0}, {"limit": 101},
    {"assignment_id": 2, "offset": 1}, {"course_id": True}, {"assignment_id": "2"}])
def test_read_argument_validation_precedes_http(args):
    state = WhatIfHTTP()
    with pytest.raises(ValueError):
        wi.read_what_if_grades(state.canvas, **{"course_id": 1, **args})
    assert not state.requests
