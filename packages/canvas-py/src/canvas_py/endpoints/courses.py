"""Courses endpoints.

Distilled from:
    - research: docs/research/01-api-layer.md
    - api-docs:  docs/api-docs/courses.md
    - iOS ref:   canvas-ios/Core/Core/Features/Courses/
"""
from __future__ import annotations

from typing import Any, ClassVar

from .._request import CanvasRequest, QueryPair
from ..models.course import Course


# Default include[] set for student-facing course listings. Without these,
# the response lacks grade, term, and progress info — see api-docs/courses.md.
_STUDENT_DEFAULT_INCLUDES = [
    "term",
    "course_progress",
    "total_scores",
    "current_grading_period_scores",
    "grading_periods",
    "teachers",
    "concluded",
]


class ListCourses(CanvasRequest[list[Course]]):
    """`GET /api/v1/courses` — list courses for the current user.

    Args:
        enrollment_state: filter by state (default "active" — current classes).
        enrollment_type: filter by role (default None — all).
        states: multi-state filter via Canvas's `state[]` param
            (e.g. ["available", "completed"] to include archived courses).
            Mutually exclusive with `enrollment_state` in practice — Canvas
            accepts both but typically one is enough.
        include: optional include[] fields. Defaults to a student-useful set.
        per_page: page size (Canvas caps at 100 usually).
    """

    method: ClassVar[str] = "GET"
    response_is_paginated: ClassVar[bool] = True

    def __init__(
        self,
        *,
        enrollment_state: str | None = "active",
        enrollment_type: str | None = None,
        states: list[str] | None = None,
        include: list[str] | None = None,
        per_page: int = 50,
    ):
        self.enrollment_state = enrollment_state
        self.enrollment_type = enrollment_type
        self.states = states or []
        self.include = include if include is not None else list(_STUDENT_DEFAULT_INCLUDES)
        self.per_page = per_page

    @property
    def path(self) -> str:
        return "courses"

    def query_pairs(self) -> list[QueryPair]:
        pairs: list[QueryPair] = []
        if self.enrollment_state:
            pairs.append(("enrollment_state", self.enrollment_state))
        if self.enrollment_type:
            pairs.append(("enrollment_type", self.enrollment_type))
        for s in self.states:
            pairs.append(("state[]", s))
        for inc in self.include:
            pairs.append(("include[]", inc))
        pairs.append(("per_page", self.per_page))
        return pairs

    def decode(self, data: Any) -> list[Course]:
        if not isinstance(data, list):
            raise TypeError(f"expected list from /courses, got {type(data).__name__}")
        return [Course.model_validate(item) for item in data]


class GetCourse(CanvasRequest[Course]):
    """`GET /api/v1/courses/:id` — single course with student-useful includes."""

    method: ClassVar[str] = "GET"

    def __init__(self, course_id: int, *, include: list[str] | None = None):
        self.course_id = course_id
        self.include = include if include is not None else list(_STUDENT_DEFAULT_INCLUDES)

    @property
    def path(self) -> str:
        return f"courses/{self.course_id}"

    def query_pairs(self) -> list[QueryPair]:
        return [("include[]", inc) for inc in self.include]

    def decode(self, data: Any) -> Course:
        return Course.model_validate(data)
