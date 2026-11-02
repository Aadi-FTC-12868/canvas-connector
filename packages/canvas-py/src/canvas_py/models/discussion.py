"""DiscussionTopic and DiscussionEntry models.

Distilled from:
    - research: docs/research/04-content-and-communication.md §1
    - api-docs:  docs/api-docs/discussions.md
    - iOS ref:   canvas-ios/Core/Core/Features/Discussions/

Canvas omits most fields unless requested via `include[]`, so every field is
optional. Extra fields pass through via the `extra=allow` config on the base.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .base import CanvasModel


class DiscussionEntry(CanvasModel):
    """A single post inside a discussion (either a top-level entry or a reply).

    The `/view` endpoint returns a tree built from these; iOS treats replies
    as nested children under `recent_replies`. `parent_id` points at the
    entry this one replies to (null for top-level entries).
    """
    id: int
    user_id: int | None = None
    user_name: str | None = None
    parent_id: int | None = None
    message: str | None = None              # raw HTML
    read_state: str | None = None           # "read" | "unread"
    forced_read_state: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    attachment: dict[str, Any] | None = None
    attachments: list[dict[str, Any]] | None = None
    recent_replies: list["DiscussionEntry"] | None = None
    has_more_replies: bool | None = None
    rating_count: int | None = None
    rating_sum: int | None = None
    # Canvas uses `anonymous_post_user_id` instead of `user_id` for anon topics.
    anonymous_post_user_id: str | None = None
    # Present on entries returned inside the `/view` tree — child entries live here.
    replies: list["DiscussionEntry"] | None = None
    # Soft-delete marker Canvas sets on removed entries in the tree.
    deleted: bool | None = None


# Allow the recursive self-reference above.
DiscussionEntry.model_rebuild()


class DiscussionTopic(CanvasModel):
    """Canvas Discussion Topic (also the object announcements are returned as).

    The same object type represents regular discussions and announcements;
    announcements are distinguished by the `only_announcements=1` list filter
    or by `subscription_hold`/`announcement` flags Canvas sets server-side.
    """
    id: int
    title: str | None = None
    message: str | None = None              # raw HTML
    html_url: str | None = None
    posted_at: datetime | None = None
    last_reply_at: datetime | None = None
    delayed_post_at: datetime | None = None
    lock_at: datetime | None = None
    discussion_type: str | None = None      # side_comment|threaded|not_threaded
    position: int | None = None
    podcast_has_student_posts: bool | None = None
    require_initial_post: bool | None = None
    user_can_see_posts: bool | None = None
    discussion_subentry_count: int | None = None
    read_state: str | None = None           # "read" | "unread"
    unread_count: int | None = None
    subscribed: bool | None = None
    subscription_hold: str | None = None
    assignment_id: int | None = None
    published: bool | None = None
    can_unpublish: bool | None = None
    locked: bool | None = None
    can_lock: bool | None = None
    locked_for_user: bool | None = None
    lock_info: dict[str, Any] | None = None
    lock_explanation: str | None = None
    pinned: bool | None = None
    author: dict[str, Any] | None = None
    user_name: str | None = None
    topic_children: list[int] | None = None
    group_topic_children: list[dict[str, Any]] | None = None
    root_topic_id: int | None = None
    podcast_url: str | None = None
    attachments: list[dict[str, Any]] | None = None
    permissions: dict[str, Any] | None = None
    allow_rating: bool | None = None
    only_graders_can_rate: bool | None = None
    sort_by_rating: bool | None = None
    is_announcement: bool | None = None
    anonymous_state: str | None = None
