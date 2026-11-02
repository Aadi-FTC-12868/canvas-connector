"""Explicit student-owned interactions behind prepared action review.

Contracts checked against official Instructure API documentation:
https://canvas.instructure.com/doc/api/discussion_topics.html
https://canvas.instructure.com/doc/api/submission_comments.html
https://canvas.instructure.com/doc/api/calendar_events.html
https://canvas.instructure.com/doc/api/users.html

This module has no generic proxy or target-user argument. Identity is read from
Canvas; editing/deleting another user's content is rejected even if an elevated
token would allow it. Canvas remains authoritative for institution policy.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from canvas_py import Canvas
from canvas_py._errors import CanvasError
from canvas_py._request import CanvasRequest

from .responses import dump

Id = Annotated[int, Field(gt=0, strict=True)]
Text = Annotated[str, Field(min_length=1, max_length=100000)]
OptionalText = Annotated[str, Field(max_length=100000)]
Title = Annotated[str, Field(min_length=1, max_length=255)]
Location = Annotated[str, Field(max_length=1000)]


class StudentAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DiscussionTarget(StudentAction):
    course_id: Id
    topic_id: Id


class DiscussionEdit(DiscussionTarget):
    kind: Literal["discussion_edit"]
    entry_id: Id
    message: Text


class DiscussionDelete(DiscussionTarget):
    kind: Literal["discussion_delete"]
    entry_id: Id


class DiscussionSubscribe(DiscussionTarget):
    kind: Literal["discussion_subscribe"]


class DiscussionUnsubscribe(DiscussionTarget):
    kind: Literal["discussion_unsubscribe"]


class DiscussionRead(DiscussionTarget):
    kind: Literal["discussion_read"]
    entry_id: Id | None = None
    read: Annotated[bool, Field(strict=True)] = True


class DiscussionRate(DiscussionTarget):
    kind: Literal["discussion_rate"]
    entry_id: Id
    rating: Annotated[int, Field(ge=0, le=1, strict=True)]


class CommentTarget(StudentAction):
    course_id: Id
    assignment_id: Id
    comment_id: Id


class CommentEdit(CommentTarget):
    kind: Literal["submission_comment_edit"]
    comment: Text


class CommentDelete(CommentTarget):
    kind: Literal["submission_comment_delete"]


def _date(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Calendar dates require an ISO datetime with timezone")
    return result


class CalendarFields(StudentAction):
    @field_validator("start_at", "end_at", check_fields=False)
    @classmethod
    def valid_date(cls, value: str | None) -> str | None:
        if value is not None:
            _date(value)
        return value

    @field_validator("time_zone_edited", check_fields=False)
    @classmethod
    def valid_zone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as error:
                raise ValueError("time_zone_edited must be an IANA timezone") from error
        return value

    @model_validator(mode="after")
    def date_order(self):
        if self.start_at is not None and self.end_at is not None and _date(self.end_at) < _date(self.start_at):
            raise ValueError("Calendar end_at cannot precede start_at")
        return self


class CalendarCreate(CalendarFields):
    kind: Literal["calendar_create"]
    title: Title
    start_at: str
    end_at: str
    description: OptionalText = ""
    location_name: Location = ""
    location_address: Location = ""
    time_zone_edited: Annotated[str, Field(max_length=100)] | None = None


class CalendarUpdate(CalendarFields):
    kind: Literal["calendar_update"]
    event_id: Id
    title: Title | None = None
    start_at: str | None = None
    end_at: str | None = None
    description: OptionalText | None = None
    location_name: Location | None = None
    location_address: Location | None = None
    time_zone_edited: Annotated[str, Field(max_length=100)] | None = None

    @model_validator(mode="after")
    def nonempty_patch(self):
        if not any(getattr(self, key) is not None for key in _CALENDAR_WRITABLE):
            raise ValueError("Provide at least one calendar field to update")
        return self


class CalendarDelete(StudentAction):
    kind: Literal["calendar_delete"]
    event_id: Id
    cancel_reason: Annotated[str, Field(max_length=1000)] = ""


StudentActionTypes = Union[DiscussionEdit, DiscussionDelete, DiscussionSubscribe, DiscussionUnsubscribe,
    DiscussionRead, DiscussionRate, CommentEdit, CommentDelete, CalendarCreate, CalendarUpdate, CalendarDelete]
KINDS = ["discussion_edit", "discussion_delete", "discussion_subscribe", "discussion_unsubscribe",
         "discussion_read", "discussion_rate", "submission_comment_edit", "submission_comment_delete",
         "calendar_create", "calendar_update", "calendar_delete"]
ROUTES = {**{k: "canvas_manage_discussion" for k in KINDS if k.startswith("discussion_")},
          **{k: "canvas_manage_submission_comments" for k in KINDS if k.startswith("submission_comment_")},
          **{k: "canvas_manage_calendar" for k in KINDS if k.startswith("calendar_")}}
_CALENDAR_WRITABLE = ("title", "description", "start_at", "end_at", "location_name", "location_address", "time_zone_edited")
_CALENDAR_SNAPSHOT = ("id", "context_code", "title", "description", "start_at", "end_at", "location_name",
                      "location_address", "all_day", "workflow_state", "updated_at", "rrule", "series_uuid",
                      "parent_event_id", "appointment_group_id", "child_events_count")


class StudentRequest(CanvasRequest[Any]):
    """Internal request assembled exclusively from the typed handlers below."""
    def __init__(self, path: str, *, method: str = "GET", body: dict | None = None, query: list | None = None):
        self._path, self.method, self._body, self._query = path, method, body, query or []

    @property
    def path(self) -> str:
        return self._path

    def json_body(self) -> dict | None:
        return self._body

    def query_pairs(self) -> list:
        return self._query


def _send(canvas: Canvas, path: str, *, method: str = "GET", body: dict | None = None, query: list | None = None):
    return dump(canvas.send(StudentRequest(path, method=method, body=body, query=query)))


def _self_id(canvas: Canvas) -> int:
    profile = _send(canvas, "users/self/profile")
    value = profile.get("id")
    if type(value) is not int or value < 1:
        raise ValueError("Canvas did not confirm the current user's identity")
    return value


def _discussion_base(p: dict) -> str:
    return f"courses/{p['course_id']}/discussion_topics/{p['topic_id']}"


def _entry(canvas: Canvas, p: dict, *, allow_deleted: bool = False) -> dict:
    rows = _send(canvas, _discussion_base(p) + "/entry_list", query=[("ids[]", p["entry_id"])])
    if not isinstance(rows, list):
        raise ValueError("Canvas did not return a discussion entry list")
    found = [r for r in rows if isinstance(r, dict) and r.get("id") == p["entry_id"]]
    if len(found) != 1 or (found[0].get("deleted") and not allow_deleted):
        raise ValueError("The specified discussion entry is unavailable")
    return found[0]


def _comment_submission(canvas: Canvas, p: dict, actor: int) -> dict:
    submission = dump(canvas.submissions.get(p["course_id"], p["assignment_id"]))
    if submission.get("user_id") != actor or submission.get("assignment_id") != p["assignment_id"]:
        raise ValueError("Canvas returned a different user's submission")
    if not isinstance(submission.get("submission_comments"), list):
        raise ValueError("Canvas did not provide submission comments for verification")
    return submission


def _own_calendar(event: dict, actor: int, *, updating: bool = False) -> None:
    if event.get("context_code") != f"user_{actor}":
        raise ValueError("Only events on your own personal Canvas calendar can be changed")
    if event.get("workflow_state") != "active":
        raise ValueError("Calendar event is not active")
    if any(event.get(key) for key in ("appointment_group_id", "parent_event_id", "child_events_count", "rrule", "series_uuid")):
        raise ValueError("Appointments, shared child events and recurring series require the Canvas calendar")
    if updating and event.get("all_day") is not False:
        raise ValueError("Only confirmed timed events can be edited; all-day events require the Canvas calendar")
    permission = (event.get("permissions") or {}).get("update" if updating else "delete")
    if permission is False:
        raise ValueError("Canvas does not permit this calendar change")


def snapshot(canvas: Canvas, p: dict) -> dict:
    """Capture only relevant fields; repeat immediately before dispatch."""
    kind = p["kind"]
    actor = _self_id(canvas)
    if kind.startswith("discussion_"):
        topic = dump(canvas.discussions.get(p["course_id"], p["topic_id"]))
        if topic.get("id") != p["topic_id"]:
            raise ValueError("Canvas returned a different discussion topic")
        result = {"user_id": actor, "course_id": p["course_id"], "topic_id": p["topic_id"],
                  "title": topic.get("title"), "locked": topic.get("locked"), "locked_for_user": topic.get("locked_for_user")}
        if kind in {"discussion_edit", "discussion_delete", "discussion_rate"} and (topic.get("locked") or topic.get("locked_for_user")):
            raise ValueError("Discussion is locked")
        if p.get("entry_id") is not None:
            entry = _entry(canvas, p)
            result["entry"] = {k: entry.get(k) for k in ("id", "user_id", "parent_id", "message", "updated_at", "attachment", "attachments", "permissions")}
            if kind in {"discussion_edit", "discussion_delete"}:
                if entry.get("user_id") != actor:
                    raise ValueError("Only your own identifiable discussion entries can be edited or deleted")
                permission = (entry.get("permissions") or {}).get("update" if kind == "discussion_edit" else "delete")
                if permission is False:
                    raise ValueError("Canvas does not permit this discussion entry change")
                # The official update controller may clear attachment_id when
                # no multipart attachment is supplied. Never lose it silently.
                if kind == "discussion_edit" and (entry.get("attachment") or entry.get("attachments")):
                    raise ValueError("Edit attached discussion entries in Canvas to preserve the attachment")
        if kind in {"discussion_subscribe", "discussion_unsubscribe"}:
            if topic.get("subscription_hold") and kind == "discussion_subscribe":
                raise ValueError("Canvas currently prevents subscribing to this topic")
            result["subscribed"] = topic.get("subscribed")
        elif kind == "discussion_read":
            result["read_state"] = entry.get("read_state") if p.get("entry_id") is not None else topic.get("read_state")
        elif kind == "discussion_rate":
            if topic.get("allow_rating") is not True or topic.get("only_graders_can_rate"):
                raise ValueError("This discussion does not allow student ratings")
            view = _send(canvas, _discussion_base(p) + "/view", query=[("include_new_entries", True)])
            if not isinstance(view.get("entry_ratings"), dict):
                raise ValueError("Canvas did not provide your discussion ratings")
            result["rating"] = view["entry_ratings"].get(str(p["entry_id"]), view["entry_ratings"].get(p["entry_id"], 0))
        return result
    if kind.startswith("submission_comment_"):
        submission = _comment_submission(canvas, p, actor)
        found = [c for c in submission["submission_comments"] if c.get("id") == p["comment_id"]]
        if len(found) != 1 or found[0].get("author_id") != actor:
            raise ValueError("Only your own submission comments can be edited or deleted")
        comment = found[0]
        permission = (comment.get("permissions") or {}).get("update" if kind == "submission_comment_edit" else "delete")
        if permission is False:
            raise ValueError("Canvas does not permit this comment change")
        return {"user_id": actor, "course_id": p["course_id"], "assignment_id": p["assignment_id"],
                "comment": {k: comment.get(k) for k in ("id", "author_id", "comment", "edited_at", "created_at", "attachments", "attempt")}}
    if kind == "calendar_create":
        return {"user_id": actor, "context_code": f"user_{actor}", "calendar": "Your personal Canvas calendar"}
    if kind in {"calendar_update", "calendar_delete"}:
        event = _send(canvas, f"calendar_events/{p['event_id']}")
        if event.get("id") != p["event_id"]:
            raise ValueError("Canvas returned a different calendar event")
        _own_calendar(event, actor, updating=kind == "calendar_update")
        if kind == "calendar_update":
            start, end = p.get("start_at") or event.get("start_at"), p.get("end_at") or event.get("end_at")
            if start is None or end is None or _date(end) < _date(start):
                raise ValueError("Updated calendar dates are incomplete or reversed")
        return {"user_id": actor, "event": {k: event.get(k) for k in _CALENDAR_SNAPSHOT}}
    raise ValueError("Unsupported student action")


def perform(canvas: Canvas, p: dict, target: dict) -> dict:
    kind = p["kind"]
    actor = target["user_id"]
    if kind.startswith("discussion_"):
        base = _discussion_base(p)
        if kind in {"discussion_edit", "discussion_delete"}:
            posted = _send(canvas, base + f"/entries/{p['entry_id']}",
                           method="PUT" if kind == "discussion_edit" else "DELETE",
                           body={"message": p["message"]} if kind == "discussion_edit" else None)
            readback = _entry(canvas, p, allow_deleted=True)
            if kind == "discussion_delete":
                if readback.get("deleted") is not True:
                    raise ValueError("Discussion deletion was not confirmed")
            elif readback.get("user_id") != actor or readback.get("message") != p["message"] or readback.get("deleted"):
                raise ValueError("Exact discussion edit was not confirmed")
            if isinstance(posted, dict) and posted.get("id") not in {None, p["entry_id"]}:
                raise ValueError("Canvas returned a different edited entry")
            return {"verified": True, "entry": readback}
        if kind in {"discussion_subscribe", "discussion_unsubscribe"}:
            desired = kind == "discussion_subscribe"
            _send(canvas, base + "/subscribed", method="PUT" if desired else "DELETE")
            readback = dump(canvas.discussions.get(p["course_id"], p["topic_id"]))
            if readback.get("id") != p["topic_id"] or readback.get("subscribed") is not desired:
                raise ValueError("Discussion subscription was not confirmed")
            return {"verified": True, "topic_id": p["topic_id"], "subscribed": desired}
        if kind == "discussion_read":
            entry_id = p.get("entry_id")
            path = base + (f"/entries/{entry_id}" if entry_id is not None else "") + "/read"
            _send(canvas, path, method="PUT" if p["read"] else "DELETE")
            readback = _entry(canvas, p) if entry_id is not None else dump(canvas.discussions.get(p["course_id"], p["topic_id"]))
            desired = "read" if p["read"] else "unread"
            if readback.get("read_state") != desired or readback.get("id") != (entry_id or p["topic_id"]):
                raise ValueError("Discussion read state was not confirmed")
            return {"verified": True, "topic_id": p["topic_id"], "entry_id": entry_id, "read_state": desired}
        if kind == "discussion_rate":
            _send(canvas, base + f"/entries/{p['entry_id']}/rating", method="POST", body={"rating": p["rating"]})
            view = _send(canvas, base + "/view", query=[("include_new_entries", True)])
            if not isinstance(view.get("entry_ratings"), dict):
                raise ValueError("Canvas did not return current user ratings")
            rating = view["entry_ratings"].get(str(p["entry_id"]), view["entry_ratings"].get(p["entry_id"], 0))
            if rating != p["rating"]:
                raise ValueError("Discussion rating was not confirmed")
            return {"verified": True, "topic_id": p["topic_id"], "entry_id": p["entry_id"], "rating": rating}
    if kind.startswith("submission_comment_"):
        path = f"courses/{p['course_id']}/assignments/{p['assignment_id']}/submissions/{actor}/comments/{p['comment_id']}"
        editing = kind == "submission_comment_edit"
        posted = _send(canvas, path, method="PUT" if editing else "DELETE", body={"comment": p["comment"]} if editing else None)
        if not isinstance(posted, dict) or posted.get("id") != p["comment_id"]:
            raise ValueError("Canvas returned a different submission comment")
        submission = _comment_submission(canvas, p, actor)
        matches = [c for c in submission["submission_comments"] if c.get("id") == p["comment_id"]]
        if editing:
            if len(matches) != 1 or matches[0].get("author_id") != actor or matches[0].get("comment") != p["comment"]:
                raise ValueError("Exact submission comment edit was not confirmed")
            return {"verified": True, "comment": matches[0]}
        if matches:
            raise ValueError("Submission comment deletion was not confirmed")
        return {"verified": True, "deleted_comment_id": p["comment_id"]}
    if kind == "calendar_delete":
        posted = _send(canvas, f"calendar_events/{p['event_id']}", method="DELETE", body={"cancel_reason": p["cancel_reason"], "which": "one"})
        if not isinstance(posted, dict) or posted.get("id") != p["event_id"]:
            raise ValueError("Canvas returned a different calendar event")
        try:
            readback = _send(canvas, f"calendar_events/{p['event_id']}")
        except CanvasError as error:
            if error.status != 404:
                raise
        else:
            if readback.get("id") != p["event_id"] or readback.get("context_code") != f"user_{actor}" or readback.get("workflow_state") != "deleted":
                raise ValueError("Calendar deletion was not confirmed")
        return {"verified": True, "deleted_event_id": p["event_id"]}
    if kind in {"calendar_create", "calendar_update"}:
        fields = {key: p[key] for key in _CALENDAR_WRITABLE if p.get(key) is not None}
        creating = kind == "calendar_create"
        if creating:
            fields.update(context_code=f"user_{actor}", all_day=False)
        posted = _send(canvas, "calendar_events" if creating else f"calendar_events/{p['event_id']}",
                       method="POST" if creating else "PUT", body={"calendar_event": fields, **({} if creating else {"which": "one"})})
        event_id = posted.get("id") if isinstance(posted, dict) else None
        if type(event_id) is not int or event_id < 1 or (not creating and event_id != p["event_id"]):
            raise ValueError("Canvas did not return the expected calendar event identity")
        readback = _send(canvas, f"calendar_events/{event_id}")
        _own_calendar(readback, actor, updating=True)
        if readback.get("id") != event_id:
            raise ValueError("Canvas returned a different calendar event on readback")
        expected = {**({} if creating else target["event"]), **fields}
        for key in ("title", "description", "location_name", "location_address"):
            if (readback.get(key) or "") != (expected.get(key) or ""):
                raise ValueError("Exact calendar fields were not confirmed")
        for key in ("start_at", "end_at"):
            if not readback.get(key) or _date(readback[key]) != _date(expected[key]):
                raise ValueError("Calendar time was not confirmed")
        return {"verified": True, "event": readback}
    raise ValueError("Unsupported student action")
