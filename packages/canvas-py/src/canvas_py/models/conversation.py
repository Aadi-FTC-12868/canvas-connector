"""Inbox / Conversations models.

Reference:
    - api-docs: docs/api-docs/conversations.md
    - research: docs/research/04-content-and-communication.md §5
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import computed_field

from .base import CanvasModel


class ConversationMessage(CanvasModel):
    """A single message inside a conversation thread."""
    id: int | None = None
    created_at: datetime | None = None
    body: str | None = None
    author_id: int | None = None
    generated: bool | None = None
    media_comment: dict[str, Any] | None = None
    attachments: list[dict[str, Any]] | None = None
    forwarded_messages: list[dict[str, Any]] | None = None


class ConversationParticipant(CanvasModel):
    """A user in a conversation."""
    id: int
    name: str | None = None
    full_name: str | None = None
    avatar_url: str | None = None
    common_courses: dict[str, Any] | None = None
    common_groups: dict[str, Any] | None = None


class Conversation(CanvasModel):
    """A conversation thread (aka Inbox entry).

    Canvas returns conversations as thread-level summaries for list calls;
    only a single-thread fetch includes the `messages` array.
    """
    id: int
    subject: str | None = None
    workflow_state: str | None = None   # read|unread|archived
    last_message: str | None = None
    last_message_at: datetime | None = None
    last_authored_message: str | None = None
    last_authored_message_at: datetime | None = None
    message_count: int | None = None
    subscribed: bool | None = None
    private: bool | None = None
    starred: bool | None = None
    properties: list[str] | None = None
    audience: list[int] | None = None
    audience_contexts: dict[str, Any] | None = None
    avatar_url: str | None = None
    participants: list[ConversationParticipant] | None = None
    visible: bool | None = None
    context_name: str | None = None
    context_code: str | None = None
    # include only in single-thread get
    messages: list[ConversationMessage] | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_unread(self) -> bool:
        return self.workflow_state == "unread"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def from_whom(self) -> str:
        """Best-effort string for 'who sent this' — first non-me participant name."""
        if not self.participants:
            return self.context_name or "(unknown)"
        names = [p.name or p.full_name or f"user_{p.id}" for p in self.participants]
        return ", ".join(n for n in names if n)
