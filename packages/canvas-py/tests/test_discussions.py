"""Tests for the Discussions endpoints and thread-tree flattener.

These lock in request shape (path, method, query, body) for the write
endpoints without hitting Canvas. `flatten_thread_tree` gets hand-built
tree fixtures to verify depth-first ordering and depth annotations.
"""
from __future__ import annotations

import pytest

from canvas_py.endpoints.discussions import (
    ListDiscussionTopics,
    MarkEntryRead,
    PostDiscussionEntry,
    PostDiscussionReply,
    flatten_thread_tree,
)


# --- ListDiscussionTopics ----------------------------------------------------


def test_list_discussion_topics_path_and_defaults():
    req = ListDiscussionTopics(2995)
    assert req.method == "GET"
    assert req.path == "courses/2995/discussion_topics"
    assert req.response_is_paginated is True
    # Default: no announcement filter, no order_by, no search_term, per_page=50.
    assert req.query_pairs() == [("per_page", 50)]


def test_list_discussion_topics_with_all_query_params():
    req = ListDiscussionTopics(
        2995,
        only_announcements=True,
        order_by="recent_activity",
        search_term="midterm",
        per_page=25,
    )
    pairs = req.query_pairs()
    assert ("only_announcements", 1) in pairs
    assert ("order_by", "recent_activity") in pairs
    assert ("search_term", "midterm") in pairs
    assert ("per_page", 25) in pairs
    # only_announcements omitted when False.
    default = ListDiscussionTopics(2995).query_pairs()
    assert not any(k == "only_announcements" for k, _ in default)


# --- PostDiscussionEntry -----------------------------------------------------


def test_post_discussion_entry_plain_message_body():
    req = PostDiscussionEntry(2995, 101, "hello world")
    assert req.method == "POST"
    assert req.path == "courses/2995/discussion_topics/101/entries"
    # No query params on a bare POST.
    assert req.query_pairs() == []
    # JSON body is {message: "..."} — no attachment key when none provided.
    assert req.json_body() == {"message": "hello world"}
    assert req.form_body() is None


def test_post_discussion_entry_with_attachment_not_implemented():
    """Attachments require multipart form — explicitly deferred for now."""
    req = PostDiscussionEntry(2995, 101, "hi", attachment_file_ids=[42])
    with pytest.raises(NotImplementedError):
        req.json_body()


def test_post_discussion_reply_path_and_body():
    req = PostDiscussionReply(2995, 101, 555, "agreed")
    assert req.method == "POST"
    assert req.path == "courses/2995/discussion_topics/101/entries/555/replies"
    assert req.json_body() == {"message": "agreed"}


# --- MarkEntryRead -----------------------------------------------------------


def test_mark_entry_read_is_put_with_no_body():
    req = MarkEntryRead(2995, 101, 555)
    assert req.method == "PUT"
    assert req.path == "courses/2995/discussion_topics/101/entries/555/read"
    assert req.json_body() is None
    assert req.form_body() is None
    assert req.query_pairs() == []
    # 204 No Content from Canvas — decode is None-returning.
    assert req.decode(None) is None


# --- flatten_thread_tree -----------------------------------------------------


def _sample_view() -> dict:
    """Hand-built tree approximating a /view response.

    Shape:
        root A
          reply A.1
            reply A.1.a
          reply A.2
        root B
          reply B.1
    """
    return {
        "participants": [],
        "unread_entries": [],
        "entry_ratings": {},
        "view": [
            {
                "id": 1,
                "message": "A",
                "replies": [
                    {
                        "id": 2,
                        "message": "A.1",
                        "replies": [
                            {"id": 3, "message": "A.1.a", "replies": []},
                        ],
                    },
                    {"id": 4, "message": "A.2", "replies": []},
                ],
            },
            {
                "id": 5,
                "message": "B",
                "replies": [
                    {"id": 6, "message": "B.1", "replies": []},
                ],
            },
        ],
    }


def test_flatten_thread_tree_depth_first_order():
    flat = flatten_thread_tree(_sample_view())
    assert [e["id"] for e in flat] == [1, 2, 3, 4, 5, 6]


def test_flatten_thread_tree_assigns_depth():
    flat = flatten_thread_tree(_sample_view())
    depths = {e["id"]: e["depth"] for e in flat}
    assert depths == {
        1: 0,
        2: 1,
        3: 2,
        4: 1,
        5: 0,
        6: 1,
    }


def test_flatten_thread_tree_empty_inputs():
    assert flatten_thread_tree({"view": []}) == []
    assert flatten_thread_tree({}) == []
    assert flatten_thread_tree([]) == []


def test_flatten_thread_tree_accepts_bare_list():
    """Tolerate callers who pre-extract the `view` key."""
    roots = _sample_view()["view"]
    flat = flatten_thread_tree(roots)
    assert [e["id"] for e in flat] == [1, 2, 3, 4, 5, 6]


def test_flatten_thread_tree_falls_back_to_recent_replies():
    """On bare DiscussionEntry payloads the children live under
    `recent_replies` rather than `replies`."""
    view = {
        "view": [
            {
                "id": 10,
                "recent_replies": [
                    {"id": 11, "recent_replies": []},
                ],
            },
        ]
    }
    flat = flatten_thread_tree(view)
    assert [e["id"] for e in flat] == [10, 11]
    assert [e["depth"] for e in flat] == [0, 1]
