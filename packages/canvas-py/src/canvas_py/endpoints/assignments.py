"""Assignments endpoints.

Distilled from:
    - research: docs/research/03-assignments-submissions-grades.md
    - api-docs:  docs/api-docs/assignments.md
    - iOS ref:   canvas-ios/Core/Core/Features/Assignments/
"""
from __future__ import annotations

from typing import Any, ClassVar

from .._request import CanvasRequest, QueryPair
from ..models.assignment import Assignment


# Student-useful default includes. Without these you don't see the user's
# submission state, overrides, or the real per-student due date.
_STUDENT_DEFAULT_INCLUDES = ["submission", "all_dates", "overrides"]


class ListAssignments(CanvasRequest[list[Assignment]]):
    """`GET /api/v1/courses/:course_id/assignments`.

    Args:
        course_id: Canvas course ID.
        include: include[] fields. Defaults to submission+all_dates+overrides.
        bucket: filter (past|overdue|undated|ungraded|unsubmitted|upcoming|future).
        per_page: page size.
    """

    method: ClassVar[str] = "GET"
    response_is_paginated: ClassVar[bool] = True

    def __init__(
        self,
        course_id: int,
        *,
        include: list[str] | None = None,
        bucket: str | None = None,
        search_term: str | None = None,
        order_by: str | None = None,
        per_page: int = 50,
    ):
        self.course_id = course_id
        self.include = include if include is not None else list(_STUDENT_DEFAULT_INCLUDES)
        self.bucket = bucket
        self.search_term = search_term
        self.order_by = order_by
        self.per_page = per_page

    @property
    def path(self) -> str:
        return f"courses/{self.course_id}/assignments"

    def query_pairs(self) -> list[QueryPair]:
        pairs: list[QueryPair] = [("include[]", inc) for inc in self.include]
        if self.bucket:
            pairs.append(("bucket", self.bucket))
        if self.search_term:
            pairs.append(("search_term", self.search_term))
        if self.order_by:
            pairs.append(("order_by", self.order_by))
        pairs.append(("per_page", self.per_page))
        return pairs

    def decode(self, data: Any) -> list[Assignment]:
        if not isinstance(data, list):
            raise TypeError(f"expected list from /assignments, got {type(data).__name__}")
        return [Assignment.model_validate(item) for item in data]


class GetAssignment(CanvasRequest[Assignment]):
    """`GET /api/v1/courses/:course_id/assignments/:id`."""

    method: ClassVar[str] = "GET"

    def __init__(
        self,
        course_id: int,
        assignment_id: int,
        *,
        include: list[str] | None = None,
    ):
        self.course_id = course_id
        self.assignment_id = assignment_id
        self.include = include if include is not None else list(_STUDENT_DEFAULT_INCLUDES)

    @property
    def path(self) -> str:
        return f"courses/{self.course_id}/assignments/{self.assignment_id}"

    def query_pairs(self) -> list[QueryPair]:
        return [("include[]", inc) for inc in self.include]

    def decode(self, data: Any) -> Assignment:
        return Assignment.model_validate(data)
