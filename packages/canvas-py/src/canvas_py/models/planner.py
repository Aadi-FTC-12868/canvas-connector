"""Planner models — PlannerItem and PlannerNote.

Distilled from:
    - api-docs:  docs/api-docs/planner.md
    - research:  docs/research/04-content-and-communication.md §7 (Planner/Calendar)

PlannerItem is the unified "what's on my plate" envelope: Canvas normalizes
assignments, quizzes, discussions, announcements, calendar events, wiki pages,
and user-created notes into this single shape. The `plannable_type` field
discriminates what the nested `plannable` dict actually contains.

PlannerNote is the user-created reminder object — the only plannable type
that the client owns write access to.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .base import CanvasModel


class PlannerItem(CanvasModel):
    """One entry in the Canvas planner feed.

    Reference: ~/Projects/canvas-py/docs/api-docs/planner.md

    The `plannable` field carries the full nested object of type
    `plannable_type` — e.g. when `plannable_type="assignment"`, `plannable`
    is the full Assignment JSON. Callers usually don't need a separate fetch.

    The `submissions` field is lenient: Canvas returns a dict of computed
    submission state (`submitted`, `graded`, `late`, `missing`, `excused`,
    `needs_grading`, `has_feedback`) when the plannable is gradable, and the
    literal `false` when it's a non-assignment item (a planner_note, say).
    We type it as `dict | bool | None` to accept both.
    """
    context_type: str | None = None          # "Course", "Group", "User", "Account"
    context_name: str | None = None          # human-readable course/group name
    course_id: int | None = None
    plannable_id: int | None = None
    plannable_type: str | None = None        # assignment|quiz|discussion_topic|
                                             # announcement|wiki_page|calendar_event|
                                             # planner_note|assessment_request
    plannable_date: datetime | None = None   # due/posted time for the item
    plannable: dict[str, Any] | None = None  # nested object of `plannable_type`
    planner_override: dict[str, Any] | None = None  # user's private state layer
    submissions: dict[str, Any] | bool | None = None  # dict when gradable, False otherwise
    html_url: str | None = None


class PlannerNote(CanvasModel):
    """A user-created personal reminder.

    Reference: ~/Projects/canvas-py/docs/api-docs/planner.md — "Planner notes"

    `linked_object_type` + `linked_object_id` optionally tie the note to a
    specific Canvas object (assignment, page, etc.) so the UI can show it
    alongside that object.
    """
    id: int
    title: str | None = None
    details: str | None = None               # free-form note body
    todo_date: datetime | None = None        # scheduled date; may be null (unscheduled)
    course_id: int | None = None             # optional course context
    workflow_state: str | None = None        # "active" | "deleted"
    user_id: int | None = None
    linked_object_type: str | None = None    # e.g. "assignment", "wiki_page"
    linked_object_id: int | None = None
