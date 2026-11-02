"""Announcements endpoints.

Distilled from:
    - research: docs/research/04-content-and-communication.md §6 (Announcements)
    - api-docs: docs/api-docs/announcements.md

Key fact: announcements and discussion topics share the same underlying
Canvas object. The ``/api/v1/announcements`` endpoint is a specialized filter
that returns DiscussionTopic objects where ``is_announcement=true``, plus a
``context_code`` field so callers can tell which course each belongs to.
"""
from __future__ import annotations

from typing import Any, ClassVar

from .._request import CanvasRequest, QueryPair
from ..models.discussion import DiscussionTopic


class ListAnnouncements(CanvasRequest[list[DiscussionTopic]]):
    """`GET /api/v1/announcements` — announcements across one or more contexts.

    Args:
        context_codes: required list of Canvas context codes (e.g.
            ``["course_2966", "course_2995"]``). Empty list is rejected —
            Canvas would 400 anyway; we fail fast.
        start_date: ISO 8601 or ``yyyy-mm-dd``. Default 14 days back.
        end_date: ISO 8601 or ``yyyy-mm-dd``. Default 28 days forward.
        active_only: limit to non-deleted announcements. Canvas defaults
            true for students, false for admins.
        per_page: page size.
    """

    method: ClassVar[str] = "GET"
    response_is_paginated: ClassVar[bool] = True

    def __init__(
        self,
        context_codes: list[str],
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        active_only: bool | None = None,
        per_page: int = 50,
    ):
        if not context_codes:
            # Docs say /announcements without context_codes returns 400.
            # Fail fast with a useful message rather than letting the server
            # reject it and surface a generic error.
            raise ValueError(
                "ListAnnouncements requires at least one context_code "
                "(e.g. 'course_2966'). Canvas rejects the endpoint otherwise."
            )
        self.context_codes = list(context_codes)
        self.start_date = start_date
        self.end_date = end_date
        self.active_only = active_only
        self.per_page = per_page

    @property
    def path(self) -> str:
        return "announcements"

    def query_pairs(self) -> list[QueryPair]:
        pairs: list[QueryPair] = []
        for code in self.context_codes:
            pairs.append(("context_codes[]", code))
        if self.start_date:
            pairs.append(("start_date", self.start_date))
        if self.end_date:
            pairs.append(("end_date", self.end_date))
        if self.active_only is not None:
            pairs.append(("active_only", self.active_only))
        pairs.append(("per_page", self.per_page))
        return pairs

    def decode(self, data: Any) -> list[DiscussionTopic]:
        if not isinstance(data, list):
            raise TypeError(
                f"expected list from /announcements, got {type(data).__name__}"
            )
        # Announcements are DiscussionTopic objects with an added
        # `context_code` field. The extra field passes through via the
        # `extra="allow"` config on CanvasModel.
        return [DiscussionTopic.model_validate(item) for item in data]
