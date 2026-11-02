"""Activity stream model — the Canvas notifications-feed equivalent.

Distilled from:
    - Canvas API:  https://canvas.instructure.com/doc/api/users.html#method.users.activity_stream
    - research:    docs/research/04-content-and-communication.md (cross-references §5 Inbox and §6 Announcements)

The activity stream is Canvas's unified notifications feed: new announcements,
discussion replies, inbox messages, submission events, conferences,
collaborations, and assessment requests all surface here. Each entry's
`type` field discriminates the payload, and different types carry different
fields — so we accept the common header fields explicitly and let the rest
pass through via `extra=allow` on the base model.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .base import CanvasModel


class ActivityStreamItem(CanvasModel):
    """One entry in `/api/v1/users/self/activity_stream`.

    Reference: Canvas Users API — activity_stream

    The `type` field is the discriminator. Known values include:
        Message, DiscussionTopic, Submission, Conversation, Announcement,
        Conference, Collaboration, AssessmentRequest.

    Type-specific fields (e.g. `discussion_topic_id` for DiscussionTopic,
    `submission_comments` for Submission) are absorbed by the base
    `extra="allow"` config and accessible via dict-style access or attribute
    lookup on the model instance.
    """
    id: int | None = None
    title: str | None = None
    message: str | None = None               # HTML body / preview
    type: str | None = None                  # see class docstring for known values
    created_at: datetime | None = None
    updated_at: datetime | None = None
    read_state: bool | None = None
    context_type: str | None = None          # "Course", "Group", "User"
    course_id: int | None = None
    html_url: str | None = None

    # Explicit passthrough for type-specific payload. Callers can also just
    # access extra fields directly on the instance (pydantic `extra="allow"`);
    # `extra` here gives a stable place to stash them when you want to
    # serialize the "everything else" bundle.
    extra: dict[str, Any] | None = None
