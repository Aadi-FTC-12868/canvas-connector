"""Unit tests for inbox / conversations endpoint shapes."""
from __future__ import annotations

from canvas_py.endpoints.conversations import (
    GetConversation,
    ListConversations,
    MarkConversation,
)


def test_list_conversations_scope():
    req = ListConversations(scope="unread")
    pairs = dict(req.query_pairs())
    assert pairs["scope"] == "unread"


def test_list_conversations_filter_contexts_repeats():
    req = ListConversations(filter_contexts=["course_1", "course_2"])
    pairs = req.query_pairs()
    filters = [v for k, v in pairs if k == "filter[]"]
    assert filters == ["course_1", "course_2"]


def test_get_conversation_default_auto_mark_false():
    """Default is False so programmatic browsing preserves unread state."""
    req = GetConversation(42)
    pairs = dict(req.query_pairs())
    assert pairs["auto_mark_as_read"] is False


def test_mark_conversation_sets_read():
    req = MarkConversation(42, workflow_state="read")
    body = req.json_body()
    assert body["conversation"]["workflow_state"] == "read"


def test_mark_conversation_star():
    req = MarkConversation(42, starred=True)
    body = req.json_body()
    assert body["conversation"]["starred"] is True
    assert "workflow_state" not in body["conversation"]


def test_mark_conversation_empty_body_when_nothing_set():
    req = MarkConversation(42)
    body = req.json_body()
    assert body == {}
