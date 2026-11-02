"""Reviewed personal Inbox markers and own-view deletion, never message recall.

SHOW defaults to marking messages read; every read here disables that behavior.
Native subscription changes can change read state and cached message timestamps.
Deleting the last human message also removes generated events from one's view.
Sources: https://developerdocs.instructure.com/services/canvas/resources/conversations
https://github.com/instructure/canvas-lms/blob/master/app/models/conversation_participant.rb
https://github.com/instructure/canvas-lms/blob/master/app/controllers/conversations_controller.rb
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from itertools import islice
from typing import Annotated, Any, Literal

from canvas_py import Canvas
from canvas_py._errors import CanvasNotFoundError
from canvas_py._request import CanvasRequest
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

Id = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
_ID = TypeAdapter(Id)
_MAX_MESSAGES = 1000
_MAX_BYTES = 1024 * 1024


class _Target(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    conversation_id: Id


class InboxStarSet(_Target):
    kind: Literal["inbox_star_set"]
    starred: Annotated[bool, Field(strict=True)]


class InboxSubscriptionSet(_Target):
    kind: Literal["inbox_subscription_set"]
    subscribed: Annotated[bool, Field(strict=True)]


class InboxMessagesDelete(_Target):
    kind: Literal["inbox_messages_delete"]
    message_ids: Annotated[list[Id], Field(strict=True, min_length=1, max_length=20)]

    @field_validator("message_ids")
    @classmethod
    def unique_ids(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("Choose unique message IDs")
        return value


class InboxConversationDelete(_Target):
    kind: Literal["inbox_conversation_delete"]


InboxStateActionTypes = InboxStarSet | InboxSubscriptionSet | InboxMessagesDelete | InboxConversationDelete
KINDS = ["inbox_star_set", "inbox_subscription_set", "inbox_messages_delete", "inbox_conversation_delete"]
ROUTES = {kind: "canvas_update_inbox" if kind in KINDS[:2] else "canvas_delete_inbox_content" for kind in KINDS}
_ADAPTER = TypeAdapter(Annotated[InboxStateActionTypes, Field(discriminator="kind")])


class _Request(CanvasRequest[Any]):
    allow_redirects = False

    def __init__(self, path: str, *, method: str = "GET", query: list | None = None, body: dict | None = None):
        self._path, self.method, self.query, self.body = path, method, query or [], body
        self.allow_retries = method == "GET"

    @property
    def path(self) -> str:
        return self._path

    def query_pairs(self) -> list:
        return self.query

    def json_body(self) -> dict | None:
        return self.body


class _ListRequest(_Request):
    response_is_paginated = True
    pin_pagination_path = True

    def __init__(self, scope: str):
        super().__init__("conversations", query=[("per_page", 100), ("scope", scope)])

    def validate_pagination_query(self, pairs: list[tuple[str, str]], *, page_number: int) -> None:
        query = dict(pairs)
        expected = {key: str(value) for key, value in self.query}
        if (len(query) != len(pairs) or set(query) - {*expected, "page"}
                or any(query.get(key) != value for key, value in expected.items())
                or query.get("page", "1") != str(page_number)):
            raise ValueError("Canvas changed the complete Inbox verification query")

    def decode(self, data: Any) -> list[dict]:
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise ValueError("Canvas did not provide a complete Inbox verification list")
        return data


def _object(canvas: Canvas, request: CanvasRequest) -> dict:
    result = canvas.send(request)
    if not isinstance(result, dict):
        raise ValueError("Canvas did not provide the requested Inbox object")
    return result


def _actor(canvas: Canvas) -> int:
    return _ID.validate_python(_object(canvas, _Request("users/self/profile")).get("id"))


def _ids(value: Any, *, allow_empty: bool = True) -> list[int]:
    if not isinstance(value, list) or len(value) > _MAX_MESSAGES or (not value and not allow_empty):
        raise ValueError("Canvas did not provide bounded Inbox identities")
    ids = [_ID.validate_python(item) for item in value]
    if len(ids) != len(set(ids)):
        raise ValueError("Canvas returned duplicate Inbox identities")
    return sorted(ids)


def _text(value: Any, *, nullable: bool = False, limit: int = 4096) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str or len(value) > limit:
        raise ValueError("Canvas returned unknown or unbounded Inbox text")
    return value


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    _text(value, limit=64)
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            raise ValueError
        return date.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError) as exc:
        raise ValueError("Canvas returned an unknown Inbox timestamp") from exc


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _summary(row: dict, conversation_id: int, actor: int) -> dict:
    if _ID.validate_python(row.get("id")) != conversation_id:
        raise ValueError("Canvas returned a different conversation")
    for field in ("starred", "subscribed", "private"):
        if type(row.get(field)) is not bool:
            raise ValueError("Canvas did not provide explicit Inbox state")
    if row.get("workflow_state") not in {"read", "unread", "archived"}:
        raise ValueError("Canvas returned an unknown Inbox workflow state")
    if type(row.get("message_count")) is not int or not 0 <= row["message_count"] <= _MAX_MESSAGES:
        raise ValueError("Canvas did not provide a bounded Inbox message count")
    participants = row.get("participants")
    if (not isinstance(participants, list) or len(participants) > _MAX_MESSAGES
            or any(not isinstance(item, dict) for item in participants)):
        raise ValueError("Canvas did not identify the current conversation participants")
    participant_ids = _ids([item.get("id") for item in participants], allow_empty=False)
    audience = _ids(row.get("audience"))
    if actor not in participant_ids or actor in audience or not set(audience) <= set(participant_ids):
        raise ValueError("Canvas did not confirm the current user's conversation audience")
    for key in ("subject", "last_message_at", "last_authored_message_at"):
        if key not in row:
            raise ValueError("Canvas omitted required Inbox metadata")
    return {"id": conversation_id, "subject": _text(row["subject"], nullable=True),
        **{key: row[key] for key in ("workflow_state", "starred", "subscribed", "private", "message_count")},
        **{key: _timestamp(row[key]) for key in ("last_message_at", "last_authored_message_at")}, "audience": audience}


def _message(row: Any, budget: dict, *, depth: int = 0) -> dict:
    if not isinstance(row, dict) or depth > 5 or budget["nodes"] >= _MAX_MESSAGES:
        raise ValueError("Canvas returned an unknown or unbounded message tree")
    budget["nodes"] += 1
    identity, author = _ID.validate_python(row.get("id")), _ID.validate_python(row.get("author_id"))
    if type(row.get("generated")) is not bool or row.get("created_at") is None:
        raise ValueError("Canvas did not identify the exact Inbox message")
    created = _timestamp(row["created_at"])
    body = _text(row.get("body"), limit=_MAX_BYTES)
    budget["bytes"] += len(body.encode())
    if budget["bytes"] > _MAX_BYTES:
        raise ValueError("The visible message content exceeds the bounded review limit")
    attachments = row.get("attachments")
    if not isinstance(attachments, list) or len(attachments) > 100:
        raise ValueError("Canvas did not return bounded message attachments")
    files = []
    for file in attachments:
        if not isinstance(file, dict) or type(file.get("size")) is not int or file["size"] < 0:
            raise ValueError("Canvas returned unknown message attachment metadata")
        files.append({"id": _ID.validate_python(file.get("id")), "size": file["size"],
            **{key: _text(file.get(key), nullable=True) for key in ("filename", "display_name", "content-type")}})
    if len({file["id"] for file in files}) != len(files):
        raise ValueError("Canvas returned duplicate message attachments")
    if "media_comment" not in row:
        raise ValueError("Canvas omitted message media metadata")
    media = row["media_comment"]
    if media is not None:
        if not isinstance(media, dict):
            raise ValueError("Canvas returned unknown message media metadata")
        media = {key: _text(media.get(key), nullable=key == "display_name")
                 for key in ("media_id", "media_type", "display_name")}
    budget["bytes"] += len(json.dumps([files, media], ensure_ascii=False).encode())
    if budget["bytes"] > _MAX_BYTES:
        raise ValueError("The visible message content exceeds the bounded review limit")
    forwarded = row.get("forwarded_messages")
    if not isinstance(forwarded, list) or len(forwarded) > _MAX_MESSAGES:
        raise ValueError("Canvas did not return bounded forwarded messages")
    forwards = [_message(item, budget, depth=depth + 1) for item in forwarded]
    if len({item["id"] for item in forwards}) != len(forwards):
        raise ValueError("Canvas returned duplicate forwarded messages")
    return {"id": identity, "author_id": author, "created_at": created, "generated": row["generated"],
        "participating_user_ids": _ids(row.get("participating_user_ids"), allow_empty=False),
        "content_version": _hash({"body": body, "attachments": sorted(files, key=lambda item: item["id"]),
                                  "media_comment": media, "forwarded_messages": forwards})}


def _thread(canvas: Canvas, conversation_id: int, actor: int) -> tuple[dict, list[dict], dict[int, str]]:
    row = _object(canvas, _Request(f"conversations/{conversation_id}", query=[("auto_mark_as_read", False)]))
    summary = _summary(row, conversation_id, actor)
    messages = row.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= _MAX_MESSAGES:
        raise ValueError("Canvas did not provide a complete bounded visible message list")
    budget = {"nodes": 0, "bytes": 0}
    versions = [_message(item, budget) for item in messages]
    if len({item["id"] for item in versions}) != len(versions):
        raise ValueError("Canvas returned duplicate visible message identities")
    if any(actor not in item["participating_user_ids"] for item in versions):
        raise ValueError("Canvas did not confirm the current user's message copies")
    if summary["message_count"] != sum(not item["generated"] for item in versions) or summary["message_count"] == 0:
        raise ValueError("Canvas's visible message list and human-message count disagree")
    return summary, sorted(versions, key=lambda item: item["id"]), {item["id"]: item["body"][:500] for item in messages}


def _latest(messages: list[dict]) -> list[dict]:
    return sorted(messages, key=lambda item: (item["created_at"], item["id"]), reverse=True)


def _expected(action: InboxStateActionTypes, before: dict, messages: list[dict], actor: int) -> tuple[dict, list[dict]]:
    after = dict(before)
    remaining = messages
    if isinstance(action, InboxStarSet):
        after["starred"] = action.starred
    elif isinstance(action, InboxSubscriptionSet):
        if before["private"]:
            raise ValueError("Canvas only supports subscription changes for group conversations")
        after["subscribed"] = action.subscribed
        if before["subscribed"] != action.subscribed:
            if not action.subscribed:
                if before["workflow_state"] == "unread":
                    after["workflow_state"] = "read"
            else:
                if before["last_message_at"] is None:
                    raise ValueError("Use Canvas to resubscribe to this sent-only conversation; its cached latest-message time is absent")
                latest = _latest([item for item in messages if not item["generated"]])[0]
                after["last_message_at"] = latest["created_at"]
                if latest["created_at"] > before["last_message_at"]:
                    after["workflow_state"] = "unread"
                own = _latest([item for item in messages if not item["generated"] and item["author_id"] == actor])
                after["last_authored_message_at"] = own[0]["created_at"] if own and (
                    before["last_authored_message_at"] is not None or latest["author_id"] == actor) else None
    else:
        selected = set(action.message_ids) if isinstance(action, InboxMessagesDelete) else {item["id"] for item in messages}
        if not selected <= {item["id"] for item in messages}:
            raise ValueError("Choose only currently visible messages from this exact conversation")
        remaining = [item for item in messages if item["id"] not in selected]
        human = _latest([item for item in remaining if not item["generated"]])
        if not human:
            remaining = []  # Canvas also removes generated events after the last human message.
            after.update(message_count=0, last_message_at=None, last_authored_message_at=None, starred=False)
            if before["workflow_state"] == "unread":
                after["workflow_state"] = "read"
        else:
            if before["last_message_at"] is None:
                raise ValueError("Use Canvas to delete selected messages from this sent-only conversation; its cached latest-message time is absent")
            after["message_count"] = len(human)
            if before["subscribed"]:
                after["last_message_at"] = human[0]["created_at"]
            else:
                # Match update_cached_data's Ruby reject! semantics, including
                # generated messages. Do not invent a timestamp-stability rule.
                times = [item["created_at"] for item in _latest(remaining)]
                newer = [value for value in times if value > before["last_message_at"]]
                changed = len(newer) != len(times)
                after["last_message_at"] = (newer[0] if changed and newer else newer[-1] if newer else None)
            own = _latest([item for item in human if item["author_id"] == actor])
            after["last_authored_message_at"] = own[0]["created_at"] if own and (
                before["last_authored_message_at"] is not None or human[0]["author_id"] == actor) else None
    return after, remaining


def snapshot(canvas: Canvas, p: dict) -> dict:
    action = _ADAPTER.validate_python(p)
    actor = _actor(canvas)
    before, messages, previews = _thread(canvas, action.conversation_id, actor)
    after, remaining = _expected(action, before, messages, actor)
    result = {"user_id": actor, "conversation": before, "messages": messages,
        "expected_after": after, "expected_remaining_message_ids": [item["id"] for item in remaining]}
    if isinstance(action, InboxStarSet):
        effect = "Change the star marker only in your Canvas Inbox."
    elif isinstance(action, InboxSubscriptionSet):
        effect = "Change your subscription to this group conversation. Unsubscribing marks unread as read; resubscribing can advance the latest-message time and mark unread or unarchive. External notification delivery is not verified."
    else:
        selected = set(action.message_ids) if isinstance(action, InboxMessagesDelete) else set(previews)
        result["selected_messages"] = [{**item, "body_preview": previews[item["id"]]} for item in messages if item["id"] in selected]
        result["generated_cleanup_ids"] = [item["id"] for item in messages if item["id"] not in selected and item["id"] not in result["expected_remaining_message_ids"]]
        effect = "Remove the selected messages from your own Inbox view, including others' messages. This does not recall messages, remove other participants' copies, or retract notifications. Removing the last human message also removes generated events."
        if isinstance(action, InboxConversationDelete):
            effect = "Remove this entire conversation from your own Inbox view, including messages arriving before Canvas processes deletion. This does not recall messages, remove other participants' copies, or retract notifications."
    return {**result, "effect": effect}


def _verify_absence(canvas: Canvas, conversation_id: int) -> None:
    # Default/archived cover received conversations; sent also covers sent-only
    # threads with no last_message_at. A failed/partial list is not absence.
    # infer_scope names its default branch "inbox". Send that name explicitly
    # so every Link must preserve an explicit, identical verification scope.
    for scope in ("inbox", "archived", "sent"):
        iterator = canvas.paginate(_ListRequest(scope))
        try:
            rows = list(islice(iterator, _MAX_MESSAGES + 1))
        finally:
            iterator.close()
        if len(rows) > _MAX_MESSAGES:
            raise ValueError("Inbox absence verification exceeded the bounded collection limit")
        ids = _ids([row.get("id") for row in rows])
        if conversation_id in ids:
            raise ValueError("The deleted conversation still appears in your Inbox lists")


def perform(canvas: Canvas, p: dict, target: dict) -> dict:
    action = _ADAPTER.validate_python(p)
    actor = _ID.validate_python(target.get("user_id"))
    before, messages = target["conversation"], target["messages"]
    if before.get("id") != action.conversation_id:
        raise ValueError("Prepared conversation does not match the action")
    expected, remaining = _expected(action, before, messages, actor)
    if expected != target.get("expected_after") or [item["id"] for item in remaining] != target.get("expected_remaining_message_ids"):
        raise ValueError("Prepared Inbox effects no longer match the action")
    path = f"conversations/{action.conversation_id}"
    if isinstance(action, InboxStarSet):
        request = _Request(path, method="PUT", body={"conversation": {"starred": action.starred}})
    elif isinstance(action, InboxSubscriptionSet):
        request = _Request(path, method="PUT", body={"conversation": {"subscribed": action.subscribed}})
    elif isinstance(action, InboxMessagesDelete):
        request = _Request(f"{path}/remove_messages", method="POST", body={"remove": action.message_ids})
    else:
        request = _Request(path, method="DELETE")
    ack = _object(canvas, request)
    if _summary(ack, action.conversation_id, actor) != expected:
        raise ValueError("Canvas did not acknowledge the exact reviewed Inbox effects")
    if _actor(canvas) != actor:
        raise ValueError("Canvas current-user identity changed during Inbox verification")
    if remaining:
        after, actual_messages, _ = _thread(canvas, action.conversation_id, actor)
        if after != expected or actual_messages != remaining:
            raise ValueError("Canvas did not independently confirm the exact Inbox state and remaining messages")
    else:
        try:
            _thread(canvas, action.conversation_id, actor)
        except CanvasNotFoundError:
            _verify_absence(canvas, action.conversation_id)
        else:
            raise ValueError("Canvas still returns the conversation after own-view deletion")
        if _actor(canvas) != actor:
            raise ValueError("Canvas current-user identity changed during deletion verification")
    return {"verified": True, "user_id": actor, "conversation_id": action.conversation_id,
        "own_view_only": True, "state": expected, "remaining_message_ids": [item["id"] for item in remaining],
        "submitted": False}
