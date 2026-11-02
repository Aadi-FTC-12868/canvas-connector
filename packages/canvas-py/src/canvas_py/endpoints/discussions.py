"""Discussions endpoints.

Distilled from:
    - research: docs/research/04-content-and-communication.md §1
    - api-docs:  docs/api-docs/discussions.md
    - iOS ref:   canvas-ios/Core/Core/Features/Discussions/

Covers reads (list/get/view) and writes (post/reply/mark-read/subscribe).
The `/view` tree is the cheap server-cached route for full-thread loads —
see research doc §1. Attachments on entry POST/reply are not yet wired;
real file uploads live on a separate track.
"""
from __future__ import annotations

from typing import Any, ClassVar

from .._request import CanvasRequest, QueryPair
from ..models.discussion import DiscussionEntry, DiscussionTopic


class ListDiscussionTopics(CanvasRequest[list[DiscussionTopic]]):
    """`GET /api/v1/courses/:course_id/discussion_topics`.

    Args:
        course_id: Canvas course ID.
        only_announcements: when True, Canvas returns only announcement-type
            topics. Same API underlies both discussions and announcements.
        order_by: `position` | `recent_activity` | `title`.
        search_term: partial title match.
        per_page: page size.
    """

    method: ClassVar[str] = "GET"
    response_is_paginated: ClassVar[bool] = True

    def __init__(
        self,
        course_id: int,
        *,
        only_announcements: bool = False,
        order_by: str | None = None,
        search_term: str | None = None,
        per_page: int = 50,
    ):
        self.course_id = course_id
        self.only_announcements = only_announcements
        self.order_by = order_by
        self.search_term = search_term
        self.per_page = per_page

    @property
    def path(self) -> str:
        return f"courses/{self.course_id}/discussion_topics"

    def query_pairs(self) -> list[QueryPair]:
        pairs: list[QueryPair] = []
        if self.only_announcements:
            pairs.append(("only_announcements", 1))
        if self.order_by:
            pairs.append(("order_by", self.order_by))
        if self.search_term:
            pairs.append(("search_term", self.search_term))
        pairs.append(("per_page", self.per_page))
        return pairs

    def decode(self, data: Any) -> list[DiscussionTopic]:
        if not isinstance(data, list):
            raise TypeError(
                f"expected list from /discussion_topics, got {type(data).__name__}"
            )
        return [DiscussionTopic.model_validate(item) for item in data]


class GetDiscussionTopic(CanvasRequest[DiscussionTopic]):
    """`GET /api/v1/courses/:course_id/discussion_topics/:topic_id`."""

    method: ClassVar[str] = "GET"

    def __init__(self, course_id: int, topic_id: int):
        self.course_id = course_id
        self.topic_id = topic_id

    @property
    def path(self) -> str:
        return f"courses/{self.course_id}/discussion_topics/{self.topic_id}"

    def decode(self, data: Any) -> DiscussionTopic:
        return DiscussionTopic.model_validate(data)


class GetDiscussionView(CanvasRequest[dict[str, Any]]):
    """`GET /api/v1/courses/:course_id/discussion_topics/:topic_id/view`.

    Returns the cached full thread as a dict with keys:
        - `participants`:   list of contributor stubs (id, display_name, avatar_url)
        - `unread_entries`: list of entry IDs still unread for the caller
        - `entry_ratings`:  dict mapping entry_id -> rating
        - `view`:           recursive entry tree (list of top-level entries, each
                            with nested `replies`)

    We don't tighten this into a model yet — the tree is walked directly by
    `flatten_thread_tree` and callers that need anything deeper can consume
    the raw dict. Note: Canvas returns 403 here if `require_initial_post` is
    set and the caller hasn't posted yet.
    """

    method: ClassVar[str] = "GET"

    def __init__(self, course_id: int, topic_id: int):
        self.course_id = course_id
        self.topic_id = topic_id

    @property
    def path(self) -> str:
        return f"courses/{self.course_id}/discussion_topics/{self.topic_id}/view"

    def decode(self, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise TypeError(f"expected dict from /view, got {type(data).__name__}")
        return data


class PostDiscussionEntry(CanvasRequest[DiscussionEntry]):
    """`POST /api/v1/courses/:course_id/discussion_topics/:topic_id/entries`.

    Creates a new top-level entry in the topic. `message` is HTML-ish text
    (Canvas stores it verbatim and renders it in a WebView).

    Args:
        attachment_file_ids: file IDs to attach. Accepted for API shape
            completeness but NOT YET IMPLEMENTED — attaching to a discussion
            entry uses multipart form data (not the standard 3-step file
            upload flow), and the real attachment pipeline lives on another
            track. Passing non-empty values here raises NotImplementedError.
    """

    method: ClassVar[str] = "POST"

    def __init__(
        self,
        course_id: int,
        topic_id: int,
        message: str,
        *,
        attachment_file_ids: list[int] | None = None,
    ):
        self.course_id = course_id
        self.topic_id = topic_id
        self.message = message
        self.attachment_file_ids = attachment_file_ids or []

    @property
    def path(self) -> str:
        return f"courses/{self.course_id}/discussion_topics/{self.topic_id}/entries"

    def json_body(self) -> dict[str, Any]:
        if self.attachment_file_ids:
            # Attachments on discussion entries require multipart form-data;
            # see research/04-content-and-communication.md §1 "Port Notes".
            raise NotImplementedError(
                "attachment upload for discussion entries is not implemented yet"
            )
        return {"message": self.message}

    def decode(self, data: Any) -> DiscussionEntry:
        return DiscussionEntry.model_validate(data)


class PostDiscussionReply(CanvasRequest[DiscussionEntry]):
    """`POST /api/v1/courses/:course_id/discussion_topics/:topic_id/entries/:entry_id/replies`.

    Posts a reply to an existing entry. Subject to `require_initial_post`.
    """

    method: ClassVar[str] = "POST"

    def __init__(
        self,
        course_id: int,
        topic_id: int,
        entry_id: int,
        message: str,
    ):
        self.course_id = course_id
        self.topic_id = topic_id
        self.entry_id = entry_id
        self.message = message

    @property
    def path(self) -> str:
        return (
            f"courses/{self.course_id}/discussion_topics/"
            f"{self.topic_id}/entries/{self.entry_id}/replies"
        )

    def json_body(self) -> dict[str, Any]:
        return {"message": self.message}

    def decode(self, data: Any) -> DiscussionEntry:
        return DiscussionEntry.model_validate(data)


class MarkEntryRead(CanvasRequest[None]):
    """`PUT /api/v1/courses/:course_id/discussion_topics/:topic_id/entries/:entry_id/read`.

    No request body. Canvas returns 204 No Content; we decode to None.
    """

    method: ClassVar[str] = "PUT"

    def __init__(self, course_id: int, topic_id: int, entry_id: int):
        self.course_id = course_id
        self.topic_id = topic_id
        self.entry_id = entry_id

    @property
    def path(self) -> str:
        return (
            f"courses/{self.course_id}/discussion_topics/"
            f"{self.topic_id}/entries/{self.entry_id}/read"
        )

    def decode(self, data: Any) -> None:
        return None


class MarkTopicRead(CanvasRequest[None]):
    """`PUT /api/v1/courses/:course_id/discussion_topics/:topic_id/read`.

    Marks the topic's intro post as read (not the replies — use
    MarkAllEntriesRead for that). Returns 204.
    """

    method: ClassVar[str] = "PUT"

    def __init__(self, course_id: int, topic_id: int):
        self.course_id = course_id
        self.topic_id = topic_id

    @property
    def path(self) -> str:
        return f"courses/{self.course_id}/discussion_topics/{self.topic_id}/read"

    def decode(self, data: Any) -> None:
        return None


class MarkAllEntriesRead(CanvasRequest[None]):
    """`PUT /api/v1/courses/:course_id/discussion_topics/:topic_id/read_all`.

    Marks every entry in the topic as read. Returns 204.
    """

    method: ClassVar[str] = "PUT"

    def __init__(self, course_id: int, topic_id: int):
        self.course_id = course_id
        self.topic_id = topic_id

    @property
    def path(self) -> str:
        return f"courses/{self.course_id}/discussion_topics/{self.topic_id}/read_all"

    def decode(self, data: Any) -> None:
        return None


class SubscribeToTopic(CanvasRequest[None]):
    """`PUT /api/v1/courses/:course_id/discussion_topics/:topic_id/subscribe`.

    Subscribes the caller to notifications for this topic.
    """

    method: ClassVar[str] = "PUT"

    def __init__(self, course_id: int, topic_id: int):
        self.course_id = course_id
        self.topic_id = topic_id

    @property
    def path(self) -> str:
        return f"courses/{self.course_id}/discussion_topics/{self.topic_id}/subscribe"

    def decode(self, data: Any) -> None:
        return None


class UnsubscribeFromTopic(CanvasRequest[None]):
    """`DELETE /api/v1/courses/:course_id/discussion_topics/:topic_id/subscribe`.

    Removes the caller's subscription from this topic.
    """

    method: ClassVar[str] = "DELETE"

    def __init__(self, course_id: int, topic_id: int):
        self.course_id = course_id
        self.topic_id = topic_id

    @property
    def path(self) -> str:
        return f"courses/{self.course_id}/discussion_topics/{self.topic_id}/subscribe"

    def decode(self, data: Any) -> None:
        return None


# --- helpers -----------------------------------------------------------------


def flatten_thread_tree(view: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk the `/view` tree depth-first into a flat list with `depth` added.

    Canvas's own mobile app collapses replies by default — Aadi's complaint
    is that this hides content he wants to read. Flattening into a single
    list lets a caller print/process a whole threaded discussion linearly
    without chasing nested `replies` arrays.

    Args:
        view: the dict returned by GetDiscussionView. Expected to have a
            top-level `view` key holding the list of root entries. If passed
            a list directly, we treat that as the roots (tolerant — some
            callers may pre-extract).

    Returns:
        List of dicts (references into the original tree, not copies), each
        with an added `depth` integer: 0 for root entries, 1 for direct
        replies, and so on.
    """
    if isinstance(view, list):
        roots = view
    else:
        roots = view.get("view") or []
        if not isinstance(roots, list):
            return []

    out: list[dict[str, Any]] = []

    def visit(entry: dict[str, Any], depth: int) -> None:
        entry["depth"] = depth
        out.append(entry)
        # Canvas uses `replies` inside the /view tree; `recent_replies` is the
        # truncated form on bare DiscussionEntry objects. Prefer whichever the
        # tree provides.
        children = entry.get("replies")
        if children is None:
            children = entry.get("recent_replies")
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    visit(child, depth + 1)

    for root in roots:
        if isinstance(root, dict):
            visit(root, 0)

    return out
