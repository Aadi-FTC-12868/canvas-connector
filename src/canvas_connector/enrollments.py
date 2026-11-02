"""Own student course invitations, including invitations for future courses.

Contracts: https://developerdocs.instructure.com/services/canvas/resources/enrollments
State filters use the documented current_future_and_restricted synthetic state
so future/restricted invitations are not silently missed. No enrollment creation,
roster administration, other-user target, or administrative deletion is exposed.
"""
from __future__ import annotations

from itertools import islice
from typing import Annotated, Any, Literal

from canvas_py import Canvas
from canvas_py._request import CanvasRequest
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .responses import dump, envelope, window

Id = Annotated[int, Field(gt=0, le=2**63 - 1, strict=True)]
_ID = TypeAdapter(Id)
_MAX_DISCOVERY = 1000
_STATES = ("current_future_and_restricted", "rejected", "completed")
_FIELDS = ("id", "user_id", "course_id", "course_section_id", "root_account_id", "type",
           "enrollment_state", "role", "role_id", "start_at", "end_at", "updated_at",
           "associated_user_id", "temporary_enrollment_source_user_id", "temporary_enrollment_pairing_id",
           "self_enrolled", "permissions")


class InvitationTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    course_id: Id
    enrollment_id: Id


class InvitationAccept(InvitationTarget):
    kind: Literal["course_invitation_accept"]


class InvitationDecline(InvitationTarget):
    kind: Literal["course_invitation_decline"]


InvitationActionTypes = InvitationAccept | InvitationDecline
KINDS = ["course_invitation_accept", "course_invitation_decline"]
ROUTES = {kind: "canvas_respond_to_invitation" for kind in KINDS}
_ADAPTER = TypeAdapter(Annotated[InvitationActionTypes, Field(discriminator="kind")])


class _Request(CanvasRequest[Any]):
    def __init__(self, path: str, *, method: str = "GET", query: list | None = None):
        self._path, self.method, self._query = path, method, query or []

    @property
    def path(self) -> str:
        return self._path

    def query_pairs(self) -> list:
        return self._query


class _List(_Request):
    response_is_paginated = True

    def decode(self, data: Any) -> list[dict]:
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise ValueError("Canvas did not return the current user's enrollment list")
        return data


def _object(canvas: Canvas, path: str, *, method: str = "GET") -> dict:
    result = dump(canvas.send(_Request(path, method=method)))
    if not isinstance(result, dict):
        raise ValueError("Canvas did not return the requested enrollment object")
    return result


def _actor(canvas: Canvas) -> int:
    return _ID.validate_python(_object(canvas, "users/self/profile").get("id"))


def _own_enrollments(canvas: Canvas, actor: int) -> list[dict]:
    query = [("type[]", "StudentEnrollment"), ("per_page", 100)]
    query.extend(("state[]", state) for state in _STATES)
    iterator = canvas.paginate(_List("users/self/enrollments", query=query))
    try:
        rows = list(islice(iterator, _MAX_DISCOVERY + 1))
    finally:
        iterator.close()
    if len(rows) > _MAX_DISCOVERY:
        raise ValueError("Enrollment discovery exceeds 1000 records; use Canvas to inspect the invitation")
    seen = set()
    for row in rows:
        identity = _ID.validate_python(row.get("id"))
        if identity in seen:
            raise ValueError("Canvas returned duplicate enrollment identities")
        seen.add(identity)
        if type(row.get("user_id")) is not int or row["user_id"] != actor:
            raise ValueError("Canvas returned an enrollment for a different user")
        _ID.validate_python(row.get("course_id"))
        if row.get("type") != "StudentEnrollment":
            raise ValueError("Canvas returned a nonstudent enrollment")
        if not isinstance(row.get("enrollment_state"), str):
            raise ValueError("Canvas did not identify the enrollment state")
    return rows


def list_invitations(canvas: Canvas, state: Literal["invited", "rejected", "creation_pending"] = "invited",
                     offset: int = 0, limit: int = 30) -> dict:
    """Read own invitations; creation_pending is visible but cannot yet be accepted."""
    window([], offset, limit)
    if state not in {"invited", "rejected", "creation_pending"}:
        raise ValueError("Choose invited, rejected or creation_pending")
    actor = _actor(canvas)
    rows = [{**{k: row[k] for k in _FIELDS if k in row},
             "html_url": f"{canvas.config.base_url}/courses/{row['course_id']}"}
            for row in _own_enrollments(canvas, actor) if row["enrollment_state"] == state]
    selected, coverage = window(rows, offset, limit)
    coverage.update(scope="own_student_course_invitations", discovery_limit=_MAX_DISCOVERY,
                    pagination_note="Offset windows may shift when Canvas changes between reads.")
    return envelope({"user_id": actor, "state": state, "invitations": selected},
                    origin=canvas.config.base_url, coverage=coverage)


def _enrollment(canvas: Canvas, actor: int, action: InvitationActionTypes) -> dict:
    matches = [row for row in _own_enrollments(canvas, actor) if row["id"] == action.enrollment_id]
    if len(matches) != 1 or matches[0]["course_id"] != action.course_id:
        raise ValueError("The exact student enrollment and course were not confirmed")
    return {k: matches[0].get(k) for k in _FIELDS}


def snapshot(canvas: Canvas, p: dict) -> dict:
    action = _ADAPTER.validate_python(p)
    actor = _actor(canvas)
    enrollment = _enrollment(canvas, actor, action)
    states = {"invited", "rejected"} if isinstance(action, InvitationAccept) else {"invited"}
    if enrollment["enrollment_state"] not in states:
        raise ValueError("This action requires an existing invitation; pending creation and active enrollments cannot be changed")
    if enrollment.get("self_enrolled") is True:
        raise ValueError("Canvas does not permit this invitation action on a self-enrollment")
    if any(enrollment.get(key) is not None for key in ("associated_user_id", "temporary_enrollment_source_user_id", "temporary_enrollment_pairing_id")):
        raise ValueError("Delegated or temporary enrollments require their separate Canvas workflow")
    permission = "accept" if isinstance(action, InvitationAccept) else "reject"
    permissions = enrollment.get("permissions")
    if permissions is not None and (not isinstance(permissions, dict) or
            (permission in permissions and permissions[permission] is not True)):
        raise ValueError("Canvas does not permit this invitation response")
    effect = ("Accept this invitation, including a previously declined invitation. Course dates and restrictions still apply."
              if isinstance(action, InvitationAccept) else "Decline this invitation. This does not remove an active course enrollment.")
    return {"user_id": actor, "course_id": action.course_id, "enrollment_id": action.enrollment_id,
            "enrollment": enrollment, "effect": effect}


def perform(canvas: Canvas, p: dict, target: dict) -> dict:
    action = _ADAPTER.validate_python(p)
    actor = _ID.validate_python(target.get("user_id"))
    if target.get("course_id") != action.course_id or target.get("enrollment_id") != action.enrollment_id:
        raise ValueError("Prepared invitation target does not match")
    before = target.get("enrollment")
    if not isinstance(before, dict) or before.get("user_id") != actor:
        raise ValueError("Prepared invitation ownership was not confirmed")
    method = "accept" if isinstance(action, InvitationAccept) else "reject"
    response = _object(canvas, f"courses/{action.course_id}/enrollments/{action.enrollment_id}/{method}", method="POST")
    if response.get("success") is not True:
        raise ValueError("Canvas did not confirm the invitation response")
    if _actor(canvas) != actor:
        raise ValueError("Current user changed during invitation response")
    saved = _enrollment(canvas, actor, action)
    expected = "active" if isinstance(action, InvitationAccept) else "rejected"
    if saved["enrollment_state"] != expected:
        raise ValueError("The exact invitation response was not confirmed by independent readback")
    for key in ("id", "user_id", "course_id", "course_section_id", "root_account_id", "type", "role", "role_id",
                "associated_user_id", "temporary_enrollment_source_user_id", "temporary_enrollment_pairing_id"):
        if saved.get(key) != before.get(key):
            raise ValueError("Enrollment identity or role changed during invitation response")
    return {"verified": True, "user_id": actor, "course_id": action.course_id,
            "enrollment_id": action.enrollment_id, "enrollment_state": expected,
            "course_access": "Course dates and restrictions still apply", "submitted": False,
            "html_url": f"{canvas.config.base_url}/courses/{action.course_id}"}
