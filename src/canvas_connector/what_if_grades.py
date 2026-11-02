"""Current-student hypothetical scores, never instructor grade updates.

Contract: https://developerdocs.instructure.com/services/canvas/resources/what_if_grades
The official WhatIfGradesApiController accepts null to clear one score, and
SubmissionsHelper rounds numeric scores to two decimals. Its service changes
only student_entered_score; course reset also touches submission updated_at.

GET submissions with student_entered_score is the read contract. Never request
read_state (the serializer can mark submissions read). exclude_response_fields
and course include[]=restrict_quantitative_data are supported by the official
submission/course serializers. Reads do not invoke the expensive grade calculator.
Canvas returns hypothetical aggregate grades only from update/reset, not from a
documented read-only calculator. We preserve Canvas's totals, never sum points.

Course reset affects all own submissions, but student list reads omit hidden or
unpublished assignments. Independent reset verification is therefore explicitly
limited to Canvas-visible rows, even after exhausting pagination.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, localcontext
from itertools import islice
import math
from typing import Annotated, Any, Literal, Union

from canvas_py import Canvas
from canvas_py._request import CanvasRequest, QueryPair
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from .responses import dump, envelope, window

Id = Annotated[int, Field(gt=0, le=2**63 - 1, strict=True)]
_ID = TypeAdapter(Id)
_MAX_DISCOVERY = 1000
_ACTUAL_FIELDS = ("score", "grade", "entered_score", "entered_grade", "excused",
                  "points_deducted", "workflow_state", "graded_at", "posted_at",
                  "grader_id", "grade_matches_current_submission", "attempt",
                  "submitted_at", "late_policy_status", "grading_period_id")
_READ_QUERY: list[QueryPair] = [("include[]", "student_entered_score"),
    ("include[]", "visibility"), ("exclude_response_fields[]", "attachments"),
    ("exclude_response_fields[]", "body"), ("exclude_response_fields[]", "preview_url")]
_RESET_CAVEAT = ("Canvas resets every saved what-if score for this user in this course. "
    "Independent readback covers Canvas-visible submissions only; hidden or unpublished "
    "submissions cannot be independently confirmed.")
_AGGREGATE_BOUNDARY = ("Saved what-if scores are readable without changes. Canvas supplies "
    "calculated hypothetical course totals only in an update/reset response; this read "
    "does not recalculate or infer weighted totals.")


class WhatIfTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    course_id: Id


class WhatIfAssignment(WhatIfTarget):
    assignment_id: Id


class WhatIfScoreSet(WhatIfAssignment):
    kind: Literal["what_if_score_set"]
    score: Annotated[float, Field(strict=True, allow_inf_nan=False)]

    @field_validator("score")
    @classmethod
    def canvas_precision(cls, value: float) -> float:
        # Ruby Float#round uses half away from zero. Do not cap extra-credit or
        # negative scores; Canvas's dedicated endpoint accepts finite numbers.
        with localcontext() as context:
            context.prec = 340
            return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class WhatIfScoreReset(WhatIfAssignment):
    kind: Literal["what_if_score_reset"]


class WhatIfCourseReset(WhatIfTarget):
    kind: Literal["what_if_course_reset"]


WhatIfActionTypes = Union[WhatIfScoreSet, WhatIfScoreReset, WhatIfCourseReset]
KINDS = ["what_if_score_set", "what_if_score_reset", "what_if_course_reset"]
ROUTES = {kind: "canvas_update_what_if_grades" for kind in KINDS}
_ADAPTER = TypeAdapter(Annotated[WhatIfActionTypes, Field(discriminator="kind")])


class _Request(CanvasRequest[Any]):
    def __init__(self, path: str, *, method: str = "GET", body: dict | None = None,
                 query: list[QueryPair] | None = None):
        self._path, self.method, self.body, self.query = path, method, body, query or []

    @property
    def path(self) -> str:
        return self._path

    def json_body(self) -> dict | None:
        return self.body

    def query_pairs(self) -> list[QueryPair]:
        return self.query


def _object(canvas: Canvas, request: CanvasRequest) -> dict:
    result = dump(canvas.send(request))
    if not isinstance(result, dict):
        raise ValueError("Canvas did not return the requested what-if object")
    return result


def _identity(value: dict, key: str, expected: int) -> None:
    if type(value.get(key)) is not int or value[key] != expected:
        raise ValueError(f"Canvas returned a different or unknown {key}")


def _items(canvas: Canvas, request: CanvasRequest, offset: int, limit: int) -> tuple[list[dict], bool]:
    iterator = canvas.paginate(request)
    try:
        values = [dump(v) for v in islice(iterator, offset, offset + limit + 1)]
    except TypeError as error:
        raise ValueError("Canvas did not return a what-if object list") from error
    finally:
        iterator.close()
    if any(not isinstance(v, dict) for v in values):
        raise ValueError("Canvas did not return a what-if object list")
    return values[:limit], len(values) > limit


def _all(canvas: Canvas, request: CanvasRequest) -> list[dict]:
    values, more = _items(canvas, request, 0, _MAX_DISCOVERY)
    if more:
        raise ValueError("What-if verification exceeded its safe discovery limit; open Canvas")
    return values


def _context(canvas: Canvas, course_id: int) -> dict:
    profile = _object(canvas, _Request("users/self/profile"))
    actor = _ID.validate_python(profile.get("id"))
    course = _object(canvas, _Request(f"courses/{course_id}", query=[
        ("include[]", "restrict_quantitative_data")]))
    _identity(course, "id", course_id)
    enrollments = _all(canvas, _Request(f"courses/{course_id}/enrollments", query=[
        ("user_id", actor), ("type[]", "StudentEnrollment"),
        ("state[]", "active"), ("state[]", "completed")]))
    if not enrollments:
        raise ValueError("Canvas did not confirm the current student's enrollment")
    for enrollment in enrollments:
        _identity(enrollment, "user_id", actor)
        _identity(enrollment, "course_id", course_id)
        _ID.validate_python(enrollment.get("id"))
        if enrollment.get("type") != "StudentEnrollment" or enrollment.get("enrollment_state") not in {"active", "completed"}:
            raise ValueError("Canvas did not confirm the current student's enrollment")
    permissions = _object(canvas, _Request(f"courses/{course_id}/permissions", query=[
        ("permissions[]", "read_grades"), ("permissions[]", "reset_what_if_grades")]))
    return {"user_id": actor, "course_id": course_id,
        "course": {k: course[k] for k in ("id", "name", "workflow_state", "hide_final_grades",
            "restrict_quantitative_data", "access_restricted_by_date", "start_at", "end_at") if k in course},
        "enrollments": sorted([{k: e[k] for k in ("id", "course_id", "user_id", "type",
            "enrollment_state", "start_at", "end_at") if k in e} for e in enrollments], key=lambda e: e["id"]),
        "permissions": {k: permissions.get(k) for k in ("read_grades", "reset_what_if_grades")}}


def _availability(context: dict) -> dict:
    course, permissions = context["course"], context["permissions"]
    reasons = []
    if course.get("workflow_state") != "available":
        reasons.append("Course is not available for what-if changes.")
    if not any(e["enrollment_state"] == "active" for e in context["enrollments"]):
        reasons.append("No active student enrollment was confirmed.")
    if "access_restricted_by_date" in course and course["access_restricted_by_date"] is not False:
        reasons.append("Course access is restricted or unknown.")
    if permissions.get("reset_what_if_grades") is not True:
        reasons.append("Canvas did not grant the student's what-if permission.")
    can_read = (permissions.get("read_grades") is True
        and course.get("restrict_quantitative_data") is False
        and course.get("workflow_state") in {"available", "completed"}
        and ("access_restricted_by_date" not in course or course["access_restricted_by_date"] is False))
    if not can_read:
        reasons.append("Canvas did not confirm access to numeric grades.")
    return {"can_read_scores": can_read, "can_change": not reasons, "reasons": reasons}


def _number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("Canvas returned an unknown what-if score")
    try:
        number = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError("Canvas returned an unknown what-if score") from error
    if not math.isfinite(number):
        raise ValueError("Canvas returned a non-finite what-if score")
    return number


def _row(submission: dict, assignment: dict, actor: int, course_id: int, assignment_id: int) -> dict:
    _identity(submission, "user_id", actor)
    _identity(submission, "assignment_id", assignment_id)
    _identity(assignment, "id", assignment_id)
    _identity(assignment, "course_id", course_id)
    submission_id = _ID.validate_python(submission.get("id"))
    if "student_entered_score" not in submission:
        raise ValueError("Canvas did not provide student_entered_score; its absence does not mean reset")
    if not {"score", "grade"}.issubset(submission):
        raise ValueError("Canvas did not provide the actual grade needed for comparison")
    actual = {k: submission[k] for k in _ACTUAL_FIELDS if k in submission}
    if any(isinstance(v, (dict, list)) for v in actual.values()):
        raise ValueError("Canvas returned an unknown actual grade state")
    permissions = submission.get("permissions")
    if permissions is not None and not isinstance(permissions, dict):
        raise ValueError("Canvas returned an unknown submission permission state")
    if permissions is not None and "read" in permissions and permissions["read"] is not True:
        raise ValueError("Canvas did not permit reading this submission's what-if state")
    return {"submission_id": submission_id, "user_id": actor, "assignment_id": assignment_id,
        "assignment": {k: assignment[k] for k in ("id", "course_id", "name", "points_possible",
            "grading_type", "published", "omit_from_final_grade", "locked_for_user") if k in assignment},
        "assignment_visible": submission.get("assignment_visible"),
        "submit_permission": permissions.get("submit") if permissions is not None and "submit" in permissions else "not_returned",
        "student_entered_score": _number(submission["student_entered_score"]), "actual_grade": actual}


def _single(canvas: Canvas, context: dict, assignment_id: int) -> dict:
    course_id = context["course_id"]
    assignment = _object(canvas, _Request(f"courses/{course_id}/assignments/{assignment_id}"))
    submission = _object(canvas, _Request(
        f"courses/{course_id}/assignments/{assignment_id}/submissions/self", query=_READ_QUERY))
    return _row(submission, assignment, context["user_id"], course_id, assignment_id)


def _list_request(course_id: int) -> _Request:
    return _Request(f"courses/{course_id}/students/submissions", query=[*_READ_QUERY,
        ("student_ids[]", "self"), ("include[]", "assignment"), ("per_page", 100)])


def _rows(values: list[dict], context: dict) -> list[dict]:
    rows = []
    seen_submissions, seen_assignments = set(), set()
    for value in values:
        assignment_id = _ID.validate_python(value.get("assignment_id"))
        assignment = value.get("assignment")
        if not isinstance(assignment, dict):
            raise ValueError("Canvas did not provide the submission's assignment identity")
        row = _row(value, assignment, context["user_id"], context["course_id"], assignment_id)
        if row["submission_id"] in seen_submissions or assignment_id in seen_assignments:
            raise ValueError("Canvas returned duplicate what-if submissions")
        seen_submissions.add(row["submission_id"])
        seen_assignments.add(assignment_id)
        rows.append(row)
    return rows


def _require_row(row: dict) -> None:
    if row["assignment_visible"] is not True or row["assignment"].get("published") is not True:
        raise ValueError("Canvas did not confirm a visible published assignment")
    if row["submit_permission"] is not True and row["submit_permission"] != "not_returned":
        raise ValueError("Canvas does not permit a what-if change for this submission")
    if not isinstance(row["assignment"].get("grading_type"), str) or row["assignment"]["grading_type"] == "not_graded":
        raise ValueError("This assignment does not have a grade to test")
    # Assignment due/lock dates and can_submit govern coursework submission,
    # not this dedicated hypothetical-score API. Never submit coursework here.


def read_what_if_grades(canvas: Canvas, course_id: int, assignment_id: int | None = None,
                        offset: int = 0, limit: int = 30) -> dict:
    """Read saved scores for self, without calculating or writing any score."""
    course_id = _ID.validate_python(course_id)
    window([], offset, limit)
    if assignment_id is not None:
        assignment_id = _ID.validate_python(assignment_id)
        if offset:
            raise ValueError("offset is only supported for a course what-if list")
    context = _context(canvas, course_id)
    availability = _availability(context)
    data = {**context, "availability": availability, "aggregate_grades": {
        "status": "not_calculated", "reason": _AGGREGATE_BOUNDARY}}
    if not availability["can_read_scores"]:
        return envelope(data, origin=canvas.config.base_url, status="unavailable",
                        coverage={"scope": "numeric_what_if_scores", "complete": False})
    if assignment_id is not None:
        row = _single(canvas, context, assignment_id)
        try:
            _require_row(row)
            row["available_for_what_if"] = availability["can_change"]
        except ValueError as error:
            row.update(available_for_what_if=False, availability_reason=str(error))
        data["submissions"] = [row]
        return envelope(data, origin=canvas.config.base_url,
                        coverage={"scope": "requested_own_submission", "complete": True})
    values, more = _items(canvas, _list_request(course_id), offset, limit)
    data["submissions"] = _rows(values, context)
    for row in data["submissions"]:
        try:
            _require_row(row)
            row["available_for_what_if"] = availability["can_change"]
        except ValueError as error:
            row.update(available_for_what_if=False, availability_reason=str(error))
    return envelope(data, origin=canvas.config.base_url, coverage={
        "scope": "canvas_visible_own_submissions", "complete": offset == 0 and not more,
        "offset": offset, "returned": len(values), "next_offset": offset + limit if more else None,
        "excludes": ["hidden_assignments", "unpublished_assignments"],
        "pagination_note": "Offset windows may shift if Canvas changes between reads."})


def snapshot(canvas: Canvas, p: dict) -> dict:
    """Prepare exact own score state and policies; re-read before dispatch."""
    action = _ADAPTER.validate_python(p)
    context = _context(canvas, action.course_id)
    availability = _availability(context)
    if not availability["can_change"]:
        raise ValueError(" ".join(availability["reasons"]))
    result = {**context, "effect": "Change only the current student's saved hypothetical scores; recorded grades are not edited."}
    if isinstance(action, WhatIfCourseReset):
        rows = _rows(_all(canvas, _list_request(action.course_id)), context)
        if any(r["assignment_visible"] is not True or r["assignment"].get("published") is not True for r in rows):
            raise ValueError("Canvas did not confirm visibility of the course submission list")
        # Reset may include ungraded assignments too. Every visible row's
        # current/actual grade must nevertheless be available for readback.
        result.update({"submissions": sorted(rows, key=lambda r: r["submission_id"]),
                       "verification_note": _RESET_CAVEAT})
    else:
        row = _single(canvas, context, action.assignment_id)
        _require_row(row)
        result.update({"assignment_id": action.assignment_id, "submission": row})
    return result


def _calculation(response: dict, context: dict) -> dict:
    if context["course"].get("hide_final_grades") is not False:
        return {"status": "hidden", "reason": "Canvas course totals are hidden or their visibility was not confirmed."}
    grades = response.get("grades")
    if not isinstance(grades, list) or len(grades) != 1 or not isinstance(grades[0], dict):
        return {"status": "unavailable", "reason": "Canvas did not return one current-student grade calculation."}
    # These totals are Canvas's calculation (including weighting/drop rules),
    # not an independently recalculated or persisted actual course grade.
    selected = {}
    for key in ("current", "final"):
        grade = grades[0].get(key)
        if not isinstance(grade, dict) or not {"grade", "total", "possible"}.issubset(grade):
            return {"status": "unavailable", "reason": "Canvas returned incomplete aggregate grades."}
        selected[key] = {k: _number(grade[k]) for k in ("grade", "total", "possible")}
    return {"status": "returned", "source": "canvas_mutation_response", "grades": selected,
            "independently_recalculated": False, "assignment_group_details_included": False}


def perform(canvas: Canvas, p: dict, target: dict) -> dict:
    """Send once to a dedicated what-if endpoint, then independently read back."""
    action = _ADAPTER.validate_python(p)
    _identity(target, "course_id", action.course_id)
    actor = _ID.validate_python(target.get("user_id"))
    if not _availability(target)["can_change"]:
        raise ValueError("Prepared what-if action is not available")
    course_reset = isinstance(action, WhatIfCourseReset)
    if course_reset:
        before = target.get("submissions")
        if not isinstance(before, list) or len(before) > _MAX_DISCOVERY:
            raise ValueError("Prepared course reset has no bounded submission snapshot")
        response = _object(canvas, _Request(f"courses/{action.course_id}/what_if_grades/reset", method="PUT"))
        after = sorted(_rows(_all(canvas, _list_request(action.course_id)), target), key=lambda r: r["submission_id"])
        expected = [{**row, "student_entered_score": None} for row in before]
        if after != expected:
            raise ValueError("Visible what-if reset and unchanged actual grades were not confirmed")
        coverage = {"complete": False, "scope": "canvas_visible_own_submissions",
                    "visible_submissions_verified": len(after), "visible_submissions_complete": True,
                    "unverified": ["hidden_submissions", "unpublished_submissions"]}
        result = {"visible_submissions_verified": len(after), "verification_note": _RESET_CAVEAT}
    else:
        _identity(target, "assignment_id", action.assignment_id)
        before = target.get("submission")
        if not isinstance(before, dict):
            raise ValueError("Prepared action has no exact submission snapshot")
        _identity(before, "assignment_id", action.assignment_id)
        _identity(before, "user_id", actor)
        _require_row(before)
        submission_id = _ID.validate_python(before.get("submission_id"))
        score = action.score if isinstance(action, WhatIfScoreSet) else None
        response = _object(canvas, _Request(f"submissions/{submission_id}/what_if_grades", method="PUT",
                                           body={"student_entered_score": score}))
        after = _single(canvas, target, action.assignment_id)
        if after != {**before, "student_entered_score": score}:
            raise ValueError("Exact what-if score and unchanged actual grade were not confirmed")
        coverage = {"complete": True, "scope": "requested_own_submission"}
        result = {"assignment_id": action.assignment_id, "submission_id": submission_id,
                  "student_entered_score": score, "actual_grade": after["actual_grade"]}
    return {"verified": True, "user_id": actor, "course_id": action.course_id, **result,
            "actual_grades_unchanged": True, "coverage": coverage,
            "aggregate_grades": _calculation(response, target)}
