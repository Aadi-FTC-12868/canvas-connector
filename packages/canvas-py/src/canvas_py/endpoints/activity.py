"""Activity stream endpoints.

Distilled from:
    - Canvas API: https://canvas.instructure.com/doc/api/users.html#method.users.activity_stream
    - research:   docs/research/04-content-and-communication.md (cross-references §5, §6)

The activity stream is Canvas's unified notifications feed. Two endpoints:

    * ``/users/self/activity_stream`` — the full feed as ActivityStreamItem list.
    * ``/users/self/activity_stream/summary`` — aggregated counts by type
      (useful for "you have 3 unread announcements" style badges).
"""
from __future__ import annotations

from typing import Any, ClassVar

from .._request import CanvasRequest, QueryPair
from ..models.activity import ActivityStreamItem


class ListActivityStream(CanvasRequest[list[ActivityStreamItem]]):
    """`GET /api/v1/users/self/activity_stream` — full notifications feed.

    Args:
        only_active_courses: if True, filter to items from courses the user
            is currently actively enrolled in (hides past / future courses).
        per_page: page size.
    """

    method: ClassVar[str] = "GET"
    response_is_paginated: ClassVar[bool] = True

    def __init__(self, *, only_active_courses: bool = False, per_page: int = 50):
        self.only_active_courses = only_active_courses
        self.per_page = per_page

    @property
    def path(self) -> str:
        return "users/self/activity_stream"

    def query_pairs(self) -> list[QueryPair]:
        pairs: list[QueryPair] = []
        if self.only_active_courses:
            pairs.append(("only_active_courses", self.only_active_courses))
        pairs.append(("per_page", self.per_page))
        return pairs

    def decode(self, data: Any) -> list[ActivityStreamItem]:
        if not isinstance(data, list):
            raise TypeError(
                f"expected list from /activity_stream, got {type(data).__name__}"
            )
        return [ActivityStreamItem.model_validate(item) for item in data]


class GetActivityStreamSummary(CanvasRequest[list[dict[str, Any]]]):
    """`GET /api/v1/users/self/activity_stream/summary`.

    Returns a flat list of entries like::

        {"type": "DiscussionTopic", "unread_count": 2, "count": 7,
         "notification_category": null}

    Useful for building badge counts without fetching the full stream.
    """

    method: ClassVar[str] = "GET"

    def __init__(self) -> None:
        pass

    @property
    def path(self) -> str:
        return "users/self/activity_stream/summary"

    def decode(self, data: Any) -> list[dict[str, Any]]:
        if not isinstance(data, list):
            raise TypeError(
                f"expected list from /activity_stream/summary, got {type(data).__name__}"
            )
        # Summary entries are small flat dicts — no model needed.
        return list(data)
