"""Unit tests for activity stream endpoints — request shape only (no HTTP)."""
from __future__ import annotations

import pytest

from canvas_py.endpoints.activity import GetActivityStreamSummary, ListActivityStream
from canvas_py.models.activity import ActivityStreamItem


# --- ListActivityStream -----------------------------------------------------


def test_list_activity_stream_defaults():
    req = ListActivityStream()
    assert req.method == "GET"
    assert req.path == "users/self/activity_stream"
    assert req.response_is_paginated is True
    # only_active_courses=False → omitted; per_page always present.
    assert req.query_pairs() == [("per_page", 50)]


def test_list_activity_stream_with_only_active_courses():
    req = ListActivityStream(only_active_courses=True)
    assert req.query_pairs() == [
        ("only_active_courses", True),
        ("per_page", 50),
    ]


def test_list_activity_stream_decode_builds_models():
    req = ListActivityStream()
    payload = [
        {
            "id": 7001,
            "title": "New announcement: week 12",
            "message": "Reminder...",
            "type": "Announcement",
            "created_at": "2026-04-20T01:00:00Z",
            "updated_at": "2026-04-20T01:00:00Z",
            "read_state": False,
            "context_type": "Course",
            "course_id": 2966,
            "html_url": "https://imsa.instructure.com/courses/2966/discussion_topics/501",
        },
        {
            "id": 7002,
            "title": "Assignment graded",
            "type": "Submission",
            "course_id": 2995,
            "read_state": True,
        },
    ]
    result = req.decode(payload)
    assert len(result) == 2
    assert all(isinstance(x, ActivityStreamItem) for x in result)
    assert result[0].type == "Announcement"
    assert result[1].type == "Submission"


def test_list_activity_stream_decode_rejects_non_list():
    req = ListActivityStream()
    with pytest.raises(TypeError, match="expected list"):
        req.decode({"should": "be a list"})


# --- GetActivityStreamSummary -----------------------------------------------


def test_get_activity_stream_summary_path_and_method():
    req = GetActivityStreamSummary()
    assert req.method == "GET"
    assert req.path == "users/self/activity_stream/summary"
    assert req.query_pairs() == []


def test_get_activity_stream_summary_decode_returns_flat_list():
    req = GetActivityStreamSummary()
    payload = [
        {
            "type": "DiscussionTopic",
            "unread_count": 2,
            "count": 7,
            "notification_category": None,
        },
        {
            "type": "Announcement",
            "unread_count": 1,
            "count": 3,
            "notification_category": None,
        },
    ]
    result = req.decode(payload)
    assert result == payload
    assert result[0]["type"] == "DiscussionTopic"
    assert result[0]["unread_count"] == 2


def test_get_activity_stream_summary_decode_rejects_non_list():
    req = GetActivityStreamSummary()
    with pytest.raises(TypeError, match="expected list"):
        req.decode({"nope": True})
