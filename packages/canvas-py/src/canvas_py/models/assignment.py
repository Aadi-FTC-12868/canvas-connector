"""Assignment model — see api-docs/assignments.md for field semantics."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .base import CanvasModel


class Assignment(CanvasModel):
    """Canvas Assignment object.

    Reference: ~/Projects/canvas-py/docs/api-docs/assignments.md
    Covers the student-facing subset; many fields are instructor-only and
    preserved via `extra=allow` on the base model.
    """
    id: int
    name: str | None = None
    description: str | None = None       # HTML
    due_at: datetime | None = None
    lock_at: datetime | None = None
    unlock_at: datetime | None = None
    points_possible: float | None = None
    grading_type: str | None = None
    submission_types: list[str] | None = None
    allowed_extensions: list[str] | None = None
    allowed_attempts: int | None = None
    course_id: int | None = None
    html_url: str | None = None
    url: str | None = None
    published: bool | None = None
    locked_for_user: bool | None = None
    lock_info: dict[str, Any] | None = None
    lock_explanation: str | None = None
    has_overrides: bool | None = None
    all_dates: list[dict[str, Any]] | None = None
    quiz_id: int | None = None
    discussion_topic: dict[str, Any] | None = None
    peer_reviews: bool | None = None
    group_category_id: int | None = None
    position: int | None = None
    assignment_group_id: int | None = None
    omit_from_final_grade: bool | None = None
    rubric: list[dict[str, Any]] | None = None
    rubric_settings: dict[str, Any] | None = None
    use_rubric_for_grading: bool | None = None
    workflow_state: str | None = None
    annotatable_attachment_id: int | None = None
    submission: dict[str, Any] | None = None  # included via include[]=submission

    @property
    def accepts_file_upload(self) -> bool:
        return bool(self.submission_types and "online_upload" in self.submission_types)

    @property
    def is_quiz(self) -> bool:
        return self.quiz_id is not None or bool(
            self.submission_types and "online_quiz" in self.submission_types
        )

    @property
    def is_discussion(self) -> bool:
        return self.discussion_topic is not None or bool(
            self.submission_types and "discussion_topic" in self.submission_types
        )
