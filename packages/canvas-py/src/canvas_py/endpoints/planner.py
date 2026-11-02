"""Planner endpoints.

Distilled from:
    - research: docs/research/04-content-and-communication.md §7 (Planner/Calendar/Todos)
    - api-docs: docs/api-docs/planner.md

Planner is the single best "what do I need to do" surface in Canvas — one
call returns due assignments, upcoming quizzes, announcements, user notes,
and more, each with submission state pre-computed. The CRUD endpoints here
are for planner_notes (user-created reminders) and planner_overrides
(mark-complete / dismiss state layer).
"""
from __future__ import annotations

from typing import Any, ClassVar

from .._request import CanvasRequest, QueryPair
from ..models.planner import PlannerItem, PlannerNote


class ListPlannerItems(CanvasRequest[list[PlannerItem]]):
    """`GET /api/v1/planner/items` — the unified "on my plate" feed.

    Args:
        start_date: ISO 8601 or `yyyy-mm-dd` lower bound.
        end_date: ISO 8601 or `yyyy-mm-dd` upper bound.
        context_codes: scope to specific contexts, e.g. ``["course_2966"]``.
        filter: ``"new_activity"`` for items with new updates since last check.
        per_page: page size.
    """

    method: ClassVar[str] = "GET"
    response_is_paginated: ClassVar[bool] = True

    def __init__(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        context_codes: list[str] | None = None,
        filter: str | None = None,
        per_page: int = 50,
    ):
        self.start_date = start_date
        self.end_date = end_date
        self.context_codes = list(context_codes) if context_codes else []
        self.filter = filter
        self.per_page = per_page

    @property
    def path(self) -> str:
        return "planner/items"

    def query_pairs(self) -> list[QueryPair]:
        pairs: list[QueryPair] = []
        if self.start_date:
            pairs.append(("start_date", self.start_date))
        if self.end_date:
            pairs.append(("end_date", self.end_date))
        # context_codes[] is Canvas's repeated-key idiom — one pair per code.
        for code in self.context_codes:
            pairs.append(("context_codes[]", code))
        if self.filter:
            pairs.append(("filter", self.filter))
        pairs.append(("per_page", self.per_page))
        return pairs

    def decode(self, data: Any) -> list[PlannerItem]:
        if not isinstance(data, list):
            raise TypeError(f"expected list from /planner/items, got {type(data).__name__}")
        return [PlannerItem.model_validate(item) for item in data]


class ListPlannerNotes(CanvasRequest[list[PlannerNote]]):
    """`GET /api/v1/planner_notes` — user-created reminders."""

    method: ClassVar[str] = "GET"
    response_is_paginated: ClassVar[bool] = True

    def __init__(self, *, per_page: int = 50):
        self.per_page = per_page

    @property
    def path(self) -> str:
        return "planner_notes"

    def query_pairs(self) -> list[QueryPair]:
        return [("per_page", self.per_page)]

    def decode(self, data: Any) -> list[PlannerNote]:
        if not isinstance(data, list):
            raise TypeError(f"expected list from /planner_notes, got {type(data).__name__}")
        return [PlannerNote.model_validate(item) for item in data]


class CreatePlannerNote(CanvasRequest[PlannerNote]):
    """`POST /api/v1/planner_notes` — create a personal reminder.

    Args:
        title: required display title.
        details: optional free-form body.
        todo_date: ISO 8601 scheduled date; pass ``None`` for unscheduled.
        course_id: optional course context so the note appears alongside
            that course's planner items.
        linked_object_type: tie note to a Canvas object (e.g. ``"assignment"``).
        linked_object_id: the linked object's ID.
    """

    method: ClassVar[str] = "POST"

    def __init__(
        self,
        title: str,
        *,
        details: str | None = None,
        todo_date: str | None = None,
        course_id: int | None = None,
        linked_object_type: str | None = None,
        linked_object_id: int | None = None,
    ):
        self.title = title
        self.details = details
        self.todo_date = todo_date
        self.course_id = course_id
        self.linked_object_type = linked_object_type
        self.linked_object_id = linked_object_id

    @property
    def path(self) -> str:
        return "planner_notes"

    def json_body(self) -> dict[str, Any]:
        # Only include keys that were explicitly provided — Canvas rejects
        # nulls for some fields.
        body: dict[str, Any] = {"title": self.title}
        if self.details is not None:
            body["details"] = self.details
        if self.todo_date is not None:
            body["todo_date"] = self.todo_date
        if self.course_id is not None:
            body["course_id"] = self.course_id
        if self.linked_object_type is not None:
            body["linked_object_type"] = self.linked_object_type
        if self.linked_object_id is not None:
            body["linked_object_id"] = self.linked_object_id
        return body

    def decode(self, data: Any) -> PlannerNote:
        return PlannerNote.model_validate(data)


class UpdatePlannerNote(CanvasRequest[PlannerNote]):
    """`PUT /api/v1/planner_notes/:id` — update any subset of fields.

    Any keyword passed here is sent as-is in the JSON body. Canvas accepts
    ``title``, ``details``, ``todo_date``, ``course_id``,
    ``linked_object_type``, ``linked_object_id``.
    """

    method: ClassVar[str] = "PUT"

    def __init__(self, note_id: int, **fields: Any):
        self.note_id = note_id
        self.fields = fields

    @property
    def path(self) -> str:
        return f"planner_notes/{self.note_id}"

    def json_body(self) -> dict[str, Any]:
        return dict(self.fields)

    def decode(self, data: Any) -> PlannerNote:
        return PlannerNote.model_validate(data)


class DeletePlannerNote(CanvasRequest[PlannerNote]):
    """`DELETE /api/v1/planner_notes/:id` — soft-delete (workflow_state=deleted)."""

    method: ClassVar[str] = "DELETE"

    def __init__(self, note_id: int):
        self.note_id = note_id

    @property
    def path(self) -> str:
        return f"planner_notes/{self.note_id}"

    def decode(self, data: Any) -> PlannerNote:
        return PlannerNote.model_validate(data)


class CreatePlannerOverride(CanvasRequest[dict[str, Any]]):
    """`POST /api/v1/planner/overrides` — flip marked_complete / dismissed state.

    Overrides are the user's private layer — dismissed items stop appearing
    in the planner, marked_complete items render as done regardless of what
    Canvas computes server-side.

    Args:
        plannable_type: the type of the plannable being overridden, e.g.
            ``"assignment"``, ``"discussion_topic"``, ``"announcement"``.
        plannable_id: the underlying object's ID.
        marked_complete: if True, the planner treats this item as done.
        dismissed: if True, the planner hides this item.
    """

    method: ClassVar[str] = "POST"

    def __init__(
        self,
        plannable_type: str,
        plannable_id: int,
        *,
        marked_complete: bool = False,
        dismissed: bool = False,
    ):
        self.plannable_type = plannable_type
        self.plannable_id = plannable_id
        self.marked_complete = marked_complete
        self.dismissed = dismissed

    @property
    def path(self) -> str:
        return "planner/overrides"

    def json_body(self) -> dict[str, Any]:
        return {
            "plannable_type": self.plannable_type,
            "plannable_id": self.plannable_id,
            "marked_complete": self.marked_complete,
            "dismissed": self.dismissed,
        }

    def decode(self, data: Any) -> dict[str, Any]:
        # Override objects are small and the response shape isn't worth its
        # own model yet — callers usually just need the returned id.
        return data if isinstance(data, dict) else {"raw": data}


class UpdatePlannerOverride(CanvasRequest[dict[str, Any]]):
    """`PUT /api/v1/planner/overrides/:id` — toggle marked_complete / dismissed.

    Pass ``None`` for either field to leave it unchanged.
    """

    method: ClassVar[str] = "PUT"

    def __init__(
        self,
        override_id: int,
        *,
        marked_complete: bool | None = None,
        dismissed: bool | None = None,
    ):
        self.override_id = override_id
        self.marked_complete = marked_complete
        self.dismissed = dismissed

    @property
    def path(self) -> str:
        return f"planner/overrides/{self.override_id}"

    def json_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if self.marked_complete is not None:
            body["marked_complete"] = self.marked_complete
        if self.dismissed is not None:
            body["dismissed"] = self.dismissed
        return body

    def decode(self, data: Any) -> dict[str, Any]:
        return data if isinstance(data, dict) else {"raw": data}
