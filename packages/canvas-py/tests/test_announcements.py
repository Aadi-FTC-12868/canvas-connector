"""Unit tests for announcements endpoint — request shape + fail-fast validation."""
from __future__ import annotations

import pytest

from canvas_py.endpoints.announcements import ListAnnouncements
from canvas_py.models.discussion import DiscussionTopic


def test_list_announcements_rejects_empty_context_codes():
    # The Canvas docs say /announcements without context_codes returns 400.
    # We fail fast with a useful message rather than waiting for the server.
    with pytest.raises(ValueError, match="context_code"):
        ListAnnouncements([])


def test_list_announcements_path_and_method():
    req = ListAnnouncements(["course_2966"])
    assert req.method == "GET"
    assert req.path == "announcements"
    assert req.response_is_paginated is True


def test_list_announcements_builds_expected_query():
    req = ListAnnouncements(
        ["course_2966", "course_2995"],
        start_date="2026-04-01",
        end_date="2026-04-30",
        active_only=True,
    )
    assert req.query_pairs() == [
        ("context_codes[]", "course_2966"),
        ("context_codes[]", "course_2995"),
        ("start_date", "2026-04-01"),
        ("end_date", "2026-04-30"),
        ("active_only", True),
        ("per_page", 50),
    ]


def test_list_announcements_minimal_query_only_has_context_and_per_page():
    req = ListAnnouncements(["course_2966"])
    assert req.query_pairs() == [
        ("context_codes[]", "course_2966"),
        ("per_page", 50),
    ]


def test_list_announcements_decode_returns_discussion_topics():
    # Announcements are DiscussionTopic objects with an extra context_code
    # field — which should pass through via `extra=allow` on CanvasModel.
    req = ListAnnouncements(["course_2966"])
    payload = [
        {
            "id": 501,
            "title": "Week 12 reminders",
            "message": "<p>Don't forget the quiz Friday.</p>",
            "is_announcement": True,
            "context_code": "course_2966",
            "posted_at": "2026-04-18T15:00:00Z",
            "read_state": "unread",
        }
    ]
    result = req.decode(payload)
    assert len(result) == 1
    topic = result[0]
    assert isinstance(topic, DiscussionTopic)
    assert topic.id == 501
    assert topic.is_announcement is True
    # context_code is an extra field → accessible via attribute passthrough
    # (pydantic `extra="allow"` promotes unknown keys to attributes).
    assert topic.context_code == "course_2966"  # type: ignore[attr-defined]


def test_list_announcements_decode_rejects_non_list():
    req = ListAnnouncements(["course_2966"])
    with pytest.raises(TypeError, match="expected list"):
        req.decode({"nope": "dict"})
