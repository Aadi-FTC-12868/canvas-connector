"""Conversations (Inbox) endpoints — reads.

Distilled from:
    - api-docs: docs/api-docs/conversations.md
    - research: docs/research/04-content-and-communication.md §5
    - iOS ref:  canvas-ios/Core/Core/Features/Inbox/

Writes (compose, reply) are not yet implemented — safety concern: sending
messages impersonating the account holder without explicit authorization is
a write we don't want on autopilot.
"""
from __future__ import annotations

from typing import Any, ClassVar

from .._request import CanvasRequest, QueryPair
from ..models.conversation import Conversation


class ListConversations(CanvasRequest[list[Conversation]]):
    """`GET /api/v1/conversations` — list inbox threads.

    Args:
        scope: `unread`, `starred`, `archived`, `sent`, or None (all).
        filter_contexts: list of context codes like `course_2966` to filter
            to threads involving those courses/groups.
        include_participant_avatars: if True, adds avatar URLs.
    """

    method: ClassVar[str] = "GET"
    response_is_paginated: ClassVar[bool] = True

    def __init__(
        self,
        *,
        scope: str | None = None,
        filter_contexts: list[str] | None = None,
        include_participant_avatars: bool = False,
        per_page: int = 50,
    ):
        self.scope = scope
        self.filter_contexts = filter_contexts or []
        self.include_participant_avatars = include_participant_avatars
        self.per_page = per_page

    @property
    def path(self) -> str:
        return "conversations"

    def query_pairs(self) -> list[QueryPair]:
        pairs: list[QueryPair] = []
        if self.scope:
            pairs.append(("scope", self.scope))
        for ctx in self.filter_contexts:
            pairs.append(("filter[]", ctx))
        if self.include_participant_avatars:
            pairs.append(("include[]", "participant_avatars"))
        pairs.append(("per_page", self.per_page))
        return pairs

    def decode(self, data: Any) -> list[Conversation]:
        if not isinstance(data, list):
            raise TypeError(f"expected list from /conversations, got {type(data).__name__}")
        return [Conversation.model_validate(item) for item in data]


class GetConversation(CanvasRequest[Conversation]):
    """`GET /api/v1/conversations/:id` — single thread with all messages."""

    method: ClassVar[str] = "GET"

    def __init__(
        self,
        conversation_id: int,
        *,
        auto_mark_as_read: bool = False,
    ):
        self.conversation_id = conversation_id
        self.auto_mark_as_read = auto_mark_as_read

    @property
    def path(self) -> str:
        return f"conversations/{self.conversation_id}"

    def query_pairs(self) -> list[QueryPair]:
        # Default True server-side; pass False to preserve unread state
        # during programmatic browsing.
        return [("auto_mark_as_read", self.auto_mark_as_read)]

    def decode(self, data: Any) -> Conversation:
        return Conversation.model_validate(data)


class MarkConversation(CanvasRequest[Conversation]):
    """`PUT /api/v1/conversations/:id` — update workflow_state, subscribed,
    starred. Useful for marking threads read/archived/starred."""

    method: ClassVar[str] = "PUT"

    def __init__(
        self,
        conversation_id: int,
        *,
        workflow_state: str | None = None,  # read|unread|archived
        subscribed: bool | None = None,
        starred: bool | None = None,
    ):
        self.conversation_id = conversation_id
        self.workflow_state = workflow_state
        self.subscribed = subscribed
        self.starred = starred

    @property
    def path(self) -> str:
        return f"conversations/{self.conversation_id}"

    def json_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {}
        convo: dict[str, Any] = {}
        if self.workflow_state is not None:
            convo["workflow_state"] = self.workflow_state
        if self.subscribed is not None:
            convo["subscribed"] = self.subscribed
        if self.starred is not None:
            convo["starred"] = self.starred
        if convo:
            body["conversation"] = convo
        return body

    def decode(self, data: Any) -> Conversation:
        return Conversation.model_validate(data)
