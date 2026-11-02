"""Missing student navigation reads, grounded in Canvas mobile app routes.

These requests deliberately accept resource identities, never arbitrary URLs.
"""
from __future__ import annotations

from typing import Any, ClassVar
from urllib.parse import quote

from .._request import CanvasRequest, QueryPair


class _ObjectRequest(CanvasRequest[dict[str, Any]]):
    def decode(self, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise TypeError("expected a Canvas resource object")
        return data


class _ListRequest(CanvasRequest[list[dict[str, Any]]]):
    response_is_paginated: ClassVar[bool] = True

    def query_pairs(self) -> list[QueryPair]:
        return [("per_page", 100)]

    def decode(self, data: Any) -> list[dict[str, Any]]:
        if not isinstance(data, list) or any(not isinstance(x, dict) for x in data):
            raise TypeError("expected a Canvas resource list")
        return data


class GetCourseTabs(_ListRequest):
    def __init__(self, course_id: int):
        self.course_id = course_id

    @property
    def path(self) -> str:
        return f"courses/{self.course_id}/tabs"


class ListPages(_ListRequest):
    def __init__(self, course_id: int):
        self.course_id = course_id

    @property
    def path(self) -> str:
        return f"courses/{self.course_id}/pages"

    def query_pairs(self) -> list[QueryPair]:
        return [*super().query_pairs(), ("sort", "title"), ("order", "asc")]


class GetPage(_ObjectRequest):
    def __init__(self, course_id: int, page_url: str):
        if not page_url or page_url in {".", ".."} or any(c in page_url for c in "/\\\x00"):
            raise ValueError("page_url must be one Canvas page slug or ID")
        self.course_id, self.page_url = course_id, page_url

    @property
    def path(self) -> str:
        return f"courses/{self.course_id}/pages/{quote(self.page_url, safe='')}"


class GetFrontPage(_ObjectRequest):
    def __init__(self, course_id: int):
        self.course_id = course_id

    @property
    def path(self) -> str:
        return f"courses/{self.course_id}/front_page"


class ListQuizzes(_ListRequest):
    def __init__(self, course_id: int):
        self.course_id = course_id

    @property
    def path(self) -> str:
        return f"courses/{self.course_id}/quizzes"


class GetQuiz(_ObjectRequest):
    def __init__(self, course_id: int, quiz_id: int):
        self.course_id, self.quiz_id = course_id, quiz_id

    @property
    def path(self) -> str:
        return f"courses/{self.course_id}/quizzes/{self.quiz_id}"
