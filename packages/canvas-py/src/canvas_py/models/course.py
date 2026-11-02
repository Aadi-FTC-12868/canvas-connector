"""Course model — see api-docs/courses.md for field semantics."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, computed_field

from .base import CanvasModel


class Course(CanvasModel):
    """Canvas Course object (student-facing subset).

    Reference: ~/Projects/canvas-py/docs/api-docs/courses.md
    Every field is optional because Canvas omits defaults and because
    `include[]` controls which fields are present.
    """
    id: int
    name: str | None = None
    original_name: str | None = None
    course_code: str | None = None
    workflow_state: str | None = None
    account_id: int | None = None
    enrollment_term_id: int | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    created_at: datetime | None = None
    default_view: str | None = None
    syllabus_body: str | None = None
    is_public: bool | None = None
    public_description: str | None = None
    time_zone: str | None = None
    total_students: int | None = None
    nickname: str | None = None
    concluded: bool | None = None
    access_restricted_by_date: bool | None = None
    calendar: dict[str, Any] | None = None

    # include[]-dependent fields
    term: dict[str, Any] | None = None
    course_progress: dict[str, Any] | None = None
    teachers: list[dict[str, Any]] | None = None
    enrollments: list[dict[str, Any]] | None = Field(default=None)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def display_name(self) -> str:
        """Best human-readable name — nickname > name > course_code > id."""
        return self.nickname or self.name or self.course_code or f"course_{self.id}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_current(self) -> bool:
        """Heuristic: course is usable (not completed/deleted/restricted)."""
        if self.workflow_state in ("completed", "deleted"):
            return False
        if self.access_restricted_by_date:
            return False
        return True
