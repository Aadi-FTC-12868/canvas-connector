"""Own submission feedback markers with side-effect-free readback.

REST include[]=read_state/read_status can mark feedback read. Instead this
adapter uses one fixed GraphQL query for readState, after REST has established
own submission identity. There is no caller-supplied query or mutation. GraphQL
requires the broad upstream url:POST|/api/graphql scope; operators may omit it.
Rubric and document-annotation markers have dedicated read-only REST endpoints.

Contracts: https://developerdocs.instructure.com/services/canvas/resources/submissions
https://canvas.instructure.com/doc/api/graph_ql.html
https://github.com/instructure/canvas-lms/blob/master/app/graphql/types/submission_type.rb
https://github.com/instructure/canvas-lms/blob/master/app/graphql/interfaces/submission_interface.rb
"""
from __future__ import annotations

import hashlib
import json
from itertools import islice
from typing import Annotated, Any, Literal

from canvas_py import Canvas
from canvas_py._request import CanvasRequest
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .responses import dump, envelope

Id = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
_ID = TypeAdapter(Id)
_QUERY = "query CanvasConnectorFeedback($id: ID!) { submission(id: $id) { _id userId assignmentId readState } }"
_SUBMISSION_FIELDS = ("id", "user_id", "assignment_id", "attempt", "updated_at", "submitted_at",
    "workflow_state", "score", "grade", "graded_at", "posted_at", "grade_matches_current_submission")


class _Target(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    course_id: Id
    assignment_id: Id


class SubmissionFeedbackRead(_Target):
    kind: Literal["submission_feedback_read"]
    read: Annotated[bool, Field(strict=True)]


class RubricFeedbackRead(_Target):
    kind: Literal["rubric_feedback_read"]


class AnnotationFeedbackRead(_Target):
    kind: Literal["annotation_feedback_read"]


FeedbackActionTypes = SubmissionFeedbackRead | RubricFeedbackRead | AnnotationFeedbackRead
KINDS = ["submission_feedback_read", "rubric_feedback_read", "annotation_feedback_read"]
ROUTES = {kind: "canvas_update_feedback_read_state" for kind in KINDS}
_ADAPTER = TypeAdapter(Annotated[FeedbackActionTypes, Field(discriminator="kind")])
_PART = {"submission_feedback_read": "submission", "rubric_feedback_read": "rubric", "annotation_feedback_read": "annotations"}
_PATH = {"rubric": "rubric_assessments/read", "annotations": "document_annotations/read"}


class _Request(CanvasRequest[Any]):
    def __init__(self, path: str, *, method: str = "GET", query: list | None = None, body: dict | None = None):
        self._path, self.method, self.query, self.body = path, method, query or [], body
        self.allow_retries = method == "GET"
        self.allow_redirects = method == "GET"

    @property
    def path(self) -> str:
        return self._path

    def query_pairs(self) -> list:
        return self.query

    def json_body(self) -> dict | None:
        return self.body


def _object(canvas: Canvas, request: CanvasRequest) -> dict:
    result = dump(canvas.send(request))
    if not isinstance(result, dict):
        raise ValueError("Canvas did not return the requested feedback object")
    return result


def _identity(row: dict, key: str, expected: int) -> None:
    if type(row.get(key)) is not int or row[key] != expected:
        raise ValueError(f"Canvas returned a different or unknown {key}")


def _context(canvas: Canvas, course_id: int, assignment_id: int) -> dict:
    actor = _ID.validate_python(_object(canvas, _Request("users/self/profile")).get("id"))
    iterator = canvas.paginate(_Request(f"courses/{course_id}/enrollments", query=[
        ("user_id", actor), ("type[]", "StudentEnrollment"),
        ("state[]", "active"), ("state[]", "completed"), ("per_page", 100)]))
    try:
        enrollments = [dump(row) for row in islice(iterator, 1001)]
    finally:
        iterator.close()
    if not enrollments or len(enrollments) > 1000:
        raise ValueError("Canvas did not confirm a bounded own student enrollment")
    seen = set()
    for row in enrollments:
        if not isinstance(row, dict):
            raise ValueError("Canvas returned an unknown enrollment")
        identity = _ID.validate_python(row.get("id"))
        if identity in seen:
            raise ValueError("Canvas returned duplicate enrollments")
        seen.add(identity)
        _identity(row, "user_id", actor)
        _identity(row, "course_id", course_id)
        if row.get("type") != "StudentEnrollment" or row.get("enrollment_state") not in {"active", "completed"}:
            raise ValueError("Canvas did not confirm own student enrollment")
    assignment = _object(canvas, _Request(f"courses/{course_id}/assignments/{assignment_id}"))
    _identity(assignment, "id", assignment_id)
    _identity(assignment, "course_id", course_id)
    path = f"courses/{course_id}/assignments/{assignment_id}/submissions/self"
    submission = _object(canvas, _Request(path, query=[("include[]", "submission_comments"),
        ("include[]", "visibility"), ("exclude_response_fields[]", "body"),
        ("exclude_response_fields[]", "preview_url"), ("exclude_response_fields[]", "attachments")]))
    _identity(submission, "user_id", actor)
    _identity(submission, "assignment_id", assignment_id)
    _ID.validate_python(submission.get("id"))
    if submission.get("assignment_visible") is not True:
        raise ValueError("Canvas did not confirm the assignment is visible to the current student")
    comments = submission.get("submission_comments")
    if not isinstance(comments, list) or len(comments) > 1000 or any(not isinstance(row, dict) for row in comments):
        raise ValueError("Canvas did not return bounded current feedback")
    comment_ids = [_ID.validate_python(row.get("id")) for row in comments]
    if len(comment_ids) != len(set(comment_ids)):
        raise ValueError("Canvas returned duplicate feedback comment identities")
    comment_versions = sorted([{k: row.get(k) for k in ("id", "author_id", "created_at", "edited_at", "comment")}
                               for row in comments], key=lambda row: row["id"])
    fingerprint = hashlib.sha256(json.dumps(comment_versions, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"user_id": actor, "course_id": course_id, "assignment_id": assignment_id,
        "assignment": {k: assignment.get(k) for k in ("id", "course_id", "published")},
        "enrollments": sorted([{k: row.get(k) for k in ("id", "user_id", "course_id", "type", "enrollment_state")}
                               for row in enrollments], key=lambda row: row["id"]),
        "submission": {k: submission.get(k) for k in _SUBMISSION_FIELDS},
        "feedback_version": fingerprint, "comment_count": len(comments)}


def _base(context: dict) -> str:
    return f"courses/{context['course_id']}/assignments/{context['assignment_id']}/submissions/self"


def _read_marker(canvas: Canvas, context: dict, part: str) -> str | bool:
    if part != "submission":
        row = _object(canvas, _Request(f"{_base(context)}/{_PATH[part]}"))
        if type(row.get("read")) is not bool:
            raise ValueError("Canvas did not provide the feedback read marker")
        return row["read"]
    expected = context["submission"]
    result = _object(canvas, _Request("/api/graphql", method="POST", body={
        "operationName": "CanvasConnectorFeedback", "query": _QUERY,
        "variables": {"id": str(expected["id"])}}))
    # GraphQL may return HTTP 200 with errors or partial data. Never interpret
    # null/forbidden/unknown schema as an unread marker or fall back to a GET
    # whose serializer changes the state being inspected.
    if result.get("errors"):
        raise ValueError("Canvas could not provide side-effect-free feedback state; GraphQL access/schema may be unavailable")
    row = (result.get("data") or {}).get("submission") if isinstance(result.get("data"), dict) else None
    if not isinstance(row, dict):
        raise ValueError("Canvas did not provide the current student's feedback state")
    for key, value in (("_id", expected["id"]), ("userId", context["user_id"]), ("assignmentId", context["assignment_id"])):
        if type(row.get(key)) not in {int, str} or str(row[key]) != str(value):
            raise ValueError("Canvas returned a different feedback identity")
    if row.get("readState") not in {"read", "unread"}:
        raise ValueError("Canvas did not provide a known feedback read state")
    return row["readState"]


def read_feedback_state(canvas: Canvas, course_id: int, assignment_id: int,
                        part: Literal["submission", "rubric", "annotations"] = "submission") -> dict:
    course_id, assignment_id = _ID.validate_python(course_id), _ID.validate_python(assignment_id)
    if part not in {"submission", "rubric", "annotations"}:
        raise ValueError("Choose submission, rubric or annotations")
    context = _context(canvas, course_id, assignment_id)
    return envelope({"user_id": context["user_id"], "course_id": course_id,
        "assignment_id": assignment_id, "submission_id": context["submission"]["id"],
        "part": part, "read_state": _read_marker(canvas, context, part),
        "html_url": f"{canvas.config.base_url}/courses/{course_id}/assignments/{assignment_id}/submissions/{context['user_id']}"},
        origin=canvas.config.base_url, coverage={"complete": True, "scope": "requested_own_feedback_marker"})


def snapshot(canvas: Canvas, p: dict) -> dict:
    action = _ADAPTER.validate_python(p)
    context = _context(canvas, action.course_id, action.assignment_id)
    part = _PART[action.kind]
    if isinstance(action, SubmissionFeedbackRead) and action.read:
        if context["assignment"]["published"] is not True:
            raise ValueError("Canvas must confirm a published assignment before marking feedback items read")
        effect = ("Mark grade, comment and rubric participation read through three non-atomic requests. "
                  "Comment marking includes all visible comments across attempts. Separate rubric-assessment "
                  "and annotation preference flags are independent. This does not attest understanding.")
    elif isinstance(action, SubmissionFeedbackRead):
        effect = "Mark the grade participation unread so aggregate submission feedback becomes unread. Other feedback markers are unchanged."
    else:
        effect = "Mark only the requested rubric-assessment or annotation preference flag read; the aggregate submission feedback marker is independent."
    operations = (["read/grade", "read/comment", "read/rubric"] if isinstance(action, SubmissionFeedbackRead) and action.read
                  else ["read"] if part == "submission" else [_PATH[part]])
    return {**context, "part": part, "read_state": _read_marker(canvas, context, part),
            "operations": operations, "effect": effect}



def perform(canvas: Canvas, p: dict, target: dict) -> dict:
    action = _ADAPTER.validate_python(p)
    _identity(target, "course_id", action.course_id)
    _identity(target, "assignment_id", action.assignment_id)
    part = _PART[action.kind]
    if target.get("part") != part:
        raise ValueError("Prepared feedback marker does not match the action")
    desired = ("read" if action.read else "unread") if isinstance(action, SubmissionFeedbackRead) else True
    operations = (["read/grade", "read/comment", "read/rubric"] if isinstance(action, SubmissionFeedbackRead) and action.read
                  else ["read"] if part == "submission" else [_PATH[part]])
    if target.get("operations") != operations:
        raise ValueError("Prepared feedback operations do not match the requested effect")
    method = "DELETE" if desired == "unread" else "PUT"
    for suffix in operations:
        result = dump(canvas.send(_Request(f"{_base(target)}/{suffix}", method=method)))
        if (part == "submission" and result is not None) or (part != "submission" and (not isinstance(result, dict) or result.get("read") is not True)):
            raise ValueError("Canvas did not acknowledge the requested feedback change")
    after = _context(canvas, action.course_id, action.assignment_id)
    # Ignore updated_at alone: changing a feedback marker may touch the row.
    expected, actual = dict(target["submission"]), dict(after["submission"])
    expected.pop("updated_at", None)
    actual.pop("updated_at", None)
    if after["user_id"] != target["user_id"] or expected != actual or after["feedback_version"] != target["feedback_version"]:
        raise ValueError("Feedback identity, attempt or grade changed during verification")
    state = _read_marker(canvas, after, part)
    if state != desired:
        raise ValueError("Canvas did not independently confirm the requested feedback marker")
    return {"verified": True, "user_id": after["user_id"], "course_id": action.course_id,
        "assignment_id": action.assignment_id, "submission_id": after["submission"]["id"],
        "part": part, "read_state": state, "operations_acknowledged": operations,
            "returned_grade_fields_unchanged": True, "submitted": False}
