"""Explicit student Inbox audiences, native forwards and receipt-backed files.

Contracts: Canvas Search/Conversations APIs and instructure/canvas-lms
conversations_controller.rb, conversations_helper.rb, conversation.rb,
conversation_participant.rb and lib/api/v1/conversation.rb. SHOW explicitly
disables marking read. Its secondary reads can lag; uncertainty never retries a
mutation. participants contains forwarded authors, not just delivery recipients.
Native included_messages grants only selected same-thread history; this module
never invokes whole-history add_recipients. Sender-side participating_user_ids
also includes deleted recipient rows, so recipient-side visibility is not proven.
"""
from __future__ import annotations

import json
import re
from itertools import islice
from typing import Annotated, Any, Literal

from canvas_py import Canvas
from canvas_py._request import CanvasRequest
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from .responses import dump, envelope, window

Id = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
Ids = Annotated[list[Id], Field(min_length=1, max_length=20)]
Text = Annotated[str, Field(strict=True, min_length=1, max_length=100000)]
Receipt = Annotated[str, Field(strict=True, min_length=1, max_length=128)]
_ID = TypeAdapter(Id)
_MAX = 1000


class _Action(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @field_validator("recipients", "message_ids", "included_message_ids", "upload_action_ids", check_fields=False)
    @classmethod
    def distinct(cls, value: list) -> list:
        if len(value) != len(set(value)):
            raise ValueError("Inbox recipient, message and upload receipt IDs must be distinct")
        return value

    @field_validator("body", "subject", check_fields=False)
    @classmethod
    def visible(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("Inbox text must contain visible content without NUL characters")
        return value


class _Compose(_Action):
    recipients: Ids
    subject: Annotated[str, Field(strict=True, min_length=1, max_length=255)]
    body: Text
    group_conversation: Annotated[bool, Field(strict=True)] = False


class InboxForward(_Compose):
    kind: Literal["inbox_forward"]
    source_conversation_id: Id
    message_ids: Ids


class InboxSendAttachments(_Compose):
    kind: Literal["inbox_send_attachments"]
    upload_action_ids: Annotated[list[Receipt], Field(min_length=1, max_length=20)]


class InboxReplyAudience(_Action):
    kind: Literal["inbox_reply_audience"]
    conversation_id: Id
    recipients: Ids
    body: Text
    included_message_ids: Annotated[list[Id], Field(max_length=20)] = Field(default_factory=list)
    upload_action_ids: Annotated[list[Receipt], Field(max_length=20)] = Field(default_factory=list)


InboxMessageActionTypes = InboxForward | InboxSendAttachments | InboxReplyAudience
KINDS = ["inbox_forward", "inbox_send_attachments", "inbox_reply_audience"]
ROUTES = {kind: "canvas_send_message" for kind in KINDS}
_ADAPTER = TypeAdapter(Annotated[InboxMessageActionTypes, Field(discriminator="kind")])


class _Request(CanvasRequest[Any]):
    pin_pagination_path = True
    allow_redirects = False

    def __init__(self, path: str, *, method: str = "GET", query: list | None = None, body: dict | None = None):
        self._path, self.method, self._query, self._body = path, method, query or [], body
        self.allow_retries = method == "GET"

    @property
    def path(self) -> str:
        return self._path

    def query_pairs(self) -> list:
        return self._query

    def json_body(self) -> dict | None:
        return self._body

    def validate_pagination_query(self, pairs: list[tuple[str, str]], *, page_number: int) -> None:
        expected = sorted((k, str(v)) for k, v in self._query if k != "page")
        if sorted((k, v) for k, v in pairs if k != "page") != expected:
            raise ValueError("Canvas changed the recipient search scope during pagination")
        pages = [v for k, v in pairs if k == "page"]
        if page_number > 1 and (len(pages) != 1 or not (
                pages[0] == str(page_number) or pages[0].startswith("bookmark:") and 9 < len(pages[0]) <= 4096)):
            raise ValueError("Canvas skipped or changed the recipient search page sequence")


def _request(canvas: Canvas, path: str, **kwargs: Any) -> Any:
    return dump(canvas.send(_Request(path, **kwargs)))


def _object(canvas: Canvas, path: str, **kwargs: Any) -> dict:
    result = _request(canvas, path, **kwargs)
    if not isinstance(result, dict):
        raise ValueError("Canvas did not provide the requested Inbox object")  # noqa: TRY004 -- safe upstream contract failure
    return result


def _actor(canvas: Canvas) -> int:
    return _ID.validate_python(_object(canvas, "users/self/profile").get("id"))


def _ids(value: Any, *, maximum: int = _MAX, duplicates: bool = False) -> list[int]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError("Canvas did not provide bounded Inbox identities")
    ids = [_ID.validate_python(item) for item in value]
    if not duplicates and len(ids) != len(set(ids)):
        raise ValueError("Canvas returned duplicate Inbox identities")
    return sorted(set(ids))


def _text(value: Any, maximum: int = 256000, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError("Canvas Inbox text is missing or exceeds the review bound")
    return value


def _attachment(value: Any) -> dict:
    if not isinstance(value, dict) or type(value.get("size")) is not int or value["size"] < 0:
        raise ValueError("Canvas attachment metadata is incomplete")
    return {"id": _ID.validate_python(value.get("id")), "size": value["size"],
            "display_name": _text(value.get("display_name"), 1024),
            "filename": _text(value.get("filename"), 1024),
            "content_type": _text(value.get("content-type", value.get("content_type")), 255, nullable=True)}


def _media(value: Any) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("media_id"), str) or not value["media_id"]:
        raise ValueError("Canvas did not identify the forwarded media")
    return {key: _text(value.get(key), 4096, nullable=True)
            for key in ("media_id", "media_type", "display_name")}


def _message(value: Any, budget: list[int], depth: int = 0) -> dict:
    budget[0] += 1
    if depth > 5 or budget[0] > _MAX or not isinstance(value, dict):
        raise ValueError("Inbox message history exceeds the 1000-node, depth-5 review bound")
    if type(value.get("generated")) is not bool or "media_comment" not in value:
        raise ValueError("Canvas did not provide complete Inbox message content")
    attachments, forwarded = value.get("attachments"), value.get("forwarded_messages")
    if not isinstance(attachments, list) or len(attachments) > 100 or not isinstance(forwarded, list):
        raise ValueError("Canvas did not provide bounded message attachments and forwards")
    files = sorted((_attachment(item) for item in attachments), key=lambda item: item["id"])
    _ids([item["id"] for item in files])
    children = sorted((_message(item, budget, depth + 1) for item in forwarded), key=lambda item: item["id"])
    _ids([item["id"] for item in children])
    return {"id": _ID.validate_python(value.get("id")), "author_id": _ID.validate_python(value.get("author_id")),
            "body": _text(value.get("body")), "created_at": _text(value.get("created_at"), 64),
            "generated": value["generated"], "attachments": files, "forwarded_messages": children,
            "media_comment": _media(value["media_comment"]),
            # Canvas plucks every CMP row, including soft-deleted ones. Treat
            # this field as identities, never evidence of active recipient copies.
            "participating_user_ids": _ids(value.get("participating_user_ids"), duplicates=True)}


def _thread(canvas: Canvas, conversation_id: int, actor: int) -> dict:
    raw = _object(canvas, f"conversations/{conversation_id}", query=[("auto_mark_as_read", False)])
    if _ID.validate_python(raw.get("id")) != conversation_id or type(raw.get("private")) is not bool:
        raise ValueError("Canvas did not identify the exact Inbox conversation")
    audience = _ids(raw.get("audience"))
    people = raw.get("participants")
    if not isinstance(people, list) or any(not isinstance(person, dict) for person in people):
        raise ValueError("Canvas did not provide conversation participants")
    participants = _ids([person.get("id") for person in people], maximum=2000)
    if actor in audience or actor not in participants or not set(audience) <= set(participants):
        raise ValueError("Canvas did not confirm the current user's conversation audience")
    raw_messages = raw.get("messages")
    if not isinstance(raw_messages, list) or len(raw_messages) > _MAX:
        raise ValueError("Canvas did not provide complete bounded visible Inbox messages")
    budget = [0]
    messages = sorted((_message(item, budget) for item in raw_messages), key=lambda item: item["id"])
    _ids([item["id"] for item in messages])
    if (type(raw.get("message_count")) is not int
            or raw["message_count"] != sum(not item["generated"] for item in messages)):
        raise ValueError("Canvas visible message count does not match its complete thread read")
    if any(actor not in item["participating_user_ids"] for item in messages):
        raise ValueError("Canvas did not confirm own visible message copies")
    if "cannot_reply" in raw and type(raw["cannot_reply"]) is not bool:
        raise ValueError("Canvas returned an unknown reply permission")
    if "context_code" not in raw:
        raise ValueError("Canvas did not identify the conversation context")
    context = raw["context_code"]
    if context is not None and (not isinstance(context, str) or not re.fullmatch(r"(?:course|group|account)_[1-9][0-9]{0,18}", context)):
        raise ValueError("Canvas did not provide a supported exact conversation context")
    result = {"id": conversation_id, "actor_id": actor, "subject": _text(raw.get("subject"), 4096, nullable=True),
              "private": raw["private"], "audience": audience, "participants": participants,
              "messages": messages, "cannot_reply": raw.get("cannot_reply", False), "context_code": context}
    if len(json.dumps(result, ensure_ascii=False).encode()) > 2 * 1024 * 1024:
        raise ValueError("Inbox history exceeds the 2 MiB review bound")
    return result


def _recipient(value: Any) -> dict:
    if not isinstance(value, dict) or value.get("type") not in {None, "user"}:
        raise ValueError("Canvas returned a context instead of an explicit message recipient")
    return {"id": _ID.validate_python(value.get("id")), "name": _text(value.get("name"), 1024),
            "full_name": _text(value.get("full_name"), 1024, nullable=True)}


def _recipient_rows(canvas: Canvas, query: list) -> list[dict]:
    iterator = canvas.paginate(_Request("search/recipients", query=[*query, ("per_page", 100)]))
    try:
        rows = [_recipient(dump(row)) for row in islice(iterator, _MAX + 1)]
    finally:
        iterator.close()
    if len(rows) > _MAX:
        raise ValueError("Recipient discovery exceeds 1000 users; narrow the search")
    _ids([row["id"] for row in rows])
    return rows


def read_recipients(canvas: Canvas, search: str | None = None, context: str | None = None,
                    user_id: int | None = None, from_conversation_id: int | None = None,
                    offset: int = 0, limit: int = 30) -> dict:
    """Find explicit messageable users, never expand recipient context strings."""
    window([], offset, limit)
    if user_id is not None:
        _ID.validate_python(user_id)
        if search is not None or context is not None:
            raise ValueError("Exact recipient lookup cannot be combined with search or context")
    else:
        if from_conversation_id is not None:
            raise ValueError("A conversation context is supported only for an exact user lookup")
        if search is None and context is None:
            raise ValueError("Provide a recipient search, course/group context, or exact user ID")
    if search is not None and (not isinstance(search, str) or not search.strip() or len(search) > 255):
        raise ValueError("Recipient search must contain 1..255 characters")
    if context is not None and (not isinstance(context, str) or not re.fullmatch(r"(?:course|group)_[1-9][0-9]{0,18}", context)):
        raise ValueError("Recipient context must be an exact course_ID or group_ID")
    actor = _actor(canvas)
    if context is not None:
        kind, raw_id = context.split("_")
        context_id = _ID.validate_python(int(raw_id))
        # SearchController silently drops an unknown context. Resolve the
        # exact fixed resource first so such a search cannot appear scoped.
        resource = _object(canvas, f"{'courses' if kind == 'course' else 'groups'}/{context_id}")
        if _ID.validate_python(resource.get("id")) != context_id or resource.get("workflow_state") == "deleted":
            raise ValueError("Canvas did not confirm the exact recipient search context")
    if from_conversation_id is not None:
        _thread(canvas, _ID.validate_python(from_conversation_id), actor)
    query = [(key, value) for key, value in (("search", search), ("context", context),
             ("user_id", user_id), ("from_conversation_id", from_conversation_id)) if value is not None]
    rows = _recipient_rows(canvas, [*query, ("type", "user")])
    if user_id is not None and (len(rows) > 1 or rows and rows[0]["id"] != user_id):
        raise ValueError("Canvas returned a different exact recipient")
    if _actor(canvas) != actor:
        raise ValueError("Canvas current user changed during recipient discovery")
    selected, coverage = window(rows, offset, limit)
    return envelope({"actor_id": actor, "recipients": selected}, origin=canvas.config.base_url,
                    coverage={"scope": "current_messageable_users", **coverage})


def _messageable(canvas: Canvas, recipients: list[int], actor: int, conversation_id: int | None) -> list[dict]:
    if actor in recipients and len(recipients) > 1:
        raise ValueError("Canvas removes self from mixed recipients; select recipients without self")
    result = []
    for user_id in recipients:
        query = [("user_id", user_id)]
        if conversation_id is not None:
            query.append(("from_conversation_id", conversation_id))
        rows = _recipient_rows(canvas, query)
        if len(rows) != 1 or rows[0]["id"] != user_id:
            raise ValueError("Canvas did not confirm every exact recipient is currently messageable")
        result.extend(rows)
    return sorted(result, key=lambda item: item["id"])


def _files(canvas: Canvas, action: InboxMessageActionTypes, files: list[dict] | None) -> list[dict]:
    expected = getattr(action, "upload_action_ids", [])
    if len(files or []) != len(expected):
        raise ValueError("Every attachment requires its own verified conversation-upload receipt")
    if not expected:
        return []
    from .inbox_attachments import verify_attachments
    checked = verify_attachments(canvas, files)
    _ids([item["id"] for item in checked], maximum=20)
    return sorted(checked, key=lambda item: item["id"])


def _student_reply_context(canvas: Canvas, thread: dict, actor: int) -> dict | None:
    context = thread["context_code"]
    if context is not None and context.startswith("account_"):
        # conversation_json can mask an actual Course as account_ID for an
        # AccountUser. The instructor-only delivery branch then cannot be
        # excluded from this response; this is a current verification boundary,
        # not absence of a native account-conversation reply API.
        raise ValueError("Canvas account-context replies can mask a course instructor delivery policy; review this selective reply in Canvas until its exact underlying context can be verified")
    if context is None or not context.startswith("course_"):
        return None
    course_id = _ID.validate_python(int(context.removeprefix("course_")))
    course = _object(canvas, f"courses/{course_id}")
    if _ID.validate_python(course.get("id")) != course_id or course.get("workflow_state") != "available":
        raise ValueError("Course-context replies require your current available student course")
    # Canvas's restrict_student_access instructor branch ignores selective
    # audiences or splits into other threads. Inspect all current own enrollment
    # types, not just a student-filtered list that could hide a mixed role.
    iterator = canvas.paginate(_Request(f"courses/{course_id}/enrollments", query=[
        ("user_id", actor), ("state[]", "active"), ("per_page", 100)]))
    try:
        rows = [dump(row) for row in islice(iterator, 101)]
    finally:
        iterator.close()
    if len(rows) > 100 or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Canvas did not provide complete bounded own course enrollments")
    checked = []
    for row in rows:
        if (type(row.get("user_id")) is not int or row["user_id"] != actor
                or type(row.get("course_id")) is not int or row["course_id"] != course_id
                or row.get("enrollment_state") != "active"
                or row.get("type") not in {"StudentEnrollment", "ObserverEnrollment", "DesignerEnrollment"}):
            raise ValueError("Selective course replies require a current student role without instructor roles; instructor policy can broaden delivery")
        checked.append({"id": _ID.validate_python(row.get("id")), "type": row["type"], "state": row["enrollment_state"]})
    if not any(row["type"] == "StudentEnrollment" for row in checked):
        raise ValueError("Canvas did not confirm your active student enrollment for this reply")
    _ids([row["id"] for row in checked], maximum=100)
    return {"course_id": course_id, "enrollments": sorted(checked, key=lambda row: row["id"])}


def snapshot(canvas: Canvas, p: dict, *, files: list[dict] | None = None) -> dict:
    action = _ADAPTER.validate_python(p)
    actor = _actor(canvas)
    conversation_id = (action.conversation_id if isinstance(action, InboxReplyAudience)
                       else action.source_conversation_id if isinstance(action, InboxForward) else None)
    thread = _thread(canvas, conversation_id, actor) if conversation_id is not None else None
    if isinstance(action, InboxReplyAudience):
        if thread["cannot_reply"]:
            raise ValueError("Canvas reports that replies are unavailable for this conversation")
        if thread["private"] and not set(action.recipients) <= set(thread["audience"]) | {actor}:
            raise ValueError("A private conversation cannot gain participants; start a new message")
    student_context = _student_reply_context(canvas, thread, actor) if isinstance(action, InboxReplyAudience) else None
    selected_ids = (action.message_ids if isinstance(action, InboxForward)
                    else action.included_message_ids if isinstance(action, InboxReplyAudience) else [])
    selected = [message for message in thread["messages"] if message["id"] in selected_ids] if thread else []
    if len(selected) != len(selected_ids):
        raise ValueError("Every selected message must be currently visible in the exact source conversation")
    recipients = _messageable(canvas, action.recipients, actor, conversation_id)
    checked_files = _files(canvas, action, files)
    if checked_files and any(item["actor_id"] != actor for item in checked_files):
        raise ValueError("Conversation attachment belongs to another Canvas user")
    if _actor(canvas) != actor:
        raise ValueError("Canvas current user changed during Inbox review")
    return {"actor_id": actor, "recipients": recipients, "thread": thread, "selected_messages": selected,
            "files": checked_files, "student_context": student_context,
            "delivery": {"recipients": sorted(action.recipients), "body": action.body,
                "subject": getattr(action, "subject", thread["subject"] if thread else None),
                "new_group_participants": sorted(set(action.recipients) - set(thread["audience"]) - {actor})
                    if isinstance(action, InboxReplyAudience) else [],
                "included_message_ids": sorted(getattr(action, "included_message_ids", [])),
                "included_history_grants": [{"message_id": message["id"], "selected_recipients": sorted(action.recipients),
                    "original_author_id": message["author_id"],
                    "may_restore_original_author_copy": message["author_id"] in set(thread["audience"]) | set(action.recipients) | {actor}}
                    for message in selected] if isinstance(action, InboxReplyAudience) else [],
                "forwarded_message_ids": sorted(action.message_ids) if isinstance(action, InboxForward) else [],
                "effects": ("Selected recipients receive the new reply and selected included history. Native included-history grants may also restore each original author's copy when that author remains a participant, even if not selected for the new reply. Newly added users remain participants for future messages; other membership is not removed. Recipient-side active history copies and notifications cannot be independently observed."
                    if isinstance(action, InboxReplyAudience) else "Create new conversation(s), embedding selected forwards or verified attachments. Forwarded authors are not added as delivery recipients. Notifications may be queued.")}}


def _new_message(message: dict, action: InboxMessageActionTypes, target: dict, recipients: set[int]) -> None:
    actor = target["actor_id"]
    if (message["author_id"] != actor or message["body"] != action.body or message["generated"]
            or set(message["participating_user_ids"]) != recipients | {actor} or message["media_comment"] is not None):
        raise ValueError("Canvas did not independently confirm the exact authored message and delivery audience")
    forwarded = target["selected_messages"] if isinstance(action, InboxForward) else []
    if message["forwarded_messages"] != forwarded:
        raise ValueError("Canvas did not confirm the exact native forwarded content")
    expected = target["files"]
    actual = message["attachments"]
    if (len(actual) != len(expected) or any(
            row["id"] != file["id"] or row["size"] != file["bytes"] or row["display_name"] != file["filename"]
            or row["filename"] != file["stored_filename"]
            for row, file in zip(actual, expected, strict=True))):
        raise ValueError("Canvas did not confirm every exact reviewed conversation attachment")


def _ack_message(posted: dict, message: dict, *, allow_missing: bool, allow_placeholder: bool = False) -> None:
    if "messages" not in posted and allow_missing:
        return
    messages = posted.get("messages")
    if not isinstance(messages, list) or len(messages) != 1 or not isinstance(messages[0], dict):
        raise ValueError("Canvas message acknowledgement does not match independent readback")
    acknowledged = messages[0]
    if allow_placeholder and type(acknowledged.get("id")) is int and acknowledged["id"] == 0:
        # Native delayed add_message returns an unsaved message with ID zero.
        # Only the already-verified single new message after the exact baseline
        # establishes completion; the placeholder alone never does.
        if (acknowledged.get("author_id") == message["author_id"]
                and type(acknowledged.get("author_id")) is int
                and acknowledged.get("body") == message["body"] and acknowledged.get("generated") is False):
            return
        raise ValueError("Canvas asynchronous reply placeholder does not match the reviewed message")
    if _ID.validate_python(acknowledged.get("id")) != message["id"]:
        raise ValueError("Canvas message acknowledgement does not match independent readback")


def perform(canvas: Canvas, p: dict, target: dict) -> dict:
    action = _ADAPTER.validate_python(p)
    actor = target["actor_id"]
    if _actor(canvas) != actor or _files(canvas, action, target["files"]) != target["files"]:
        raise ValueError("Canvas actor or conversation attachments changed before dispatch")
    body = {"body": action.body, "recipients": [str(user_id) for user_id in action.recipients]}
    if target["files"]:
        body["attachment_ids"] = [file["id"] for file in target["files"]]
    if isinstance(action, InboxReplyAudience):
        body["included_messages"] = action.included_message_ids
        posted = _object(canvas, f"conversations/{action.conversation_id}/add_message", method="POST", body=body)
        after = _thread(canvas, action.conversation_id, actor)
        before = target["thread"]
        if (after["subject"] != before["subject"] or after["private"] != before["private"]
                or after["context_code"] != before["context_code"]):
            raise ValueError("Canvas changed the reply conversation identity")
        if set(after["audience"]) != (set(before["audience"]) | set(action.recipients)) - {actor}:
            raise ValueError("Canvas did not confirm the expected persistent conversation membership")
        old = {message["id"]: message for message in before["messages"]}
        found = {message["id"]: message for message in after["messages"]}
        for message_id, previous in old.items():
            expected = dict(previous)
            if message_id in action.included_message_ids:
                original_author = {previous["author_id"]} & (set(before["audience"]) | {actor})
                expected["participating_user_ids"] = sorted(set(previous["participating_user_ids"]) | set(action.recipients) | original_author)
            if found.get(message_id) != expected:
                raise ValueError("Canvas did not confirm exactly the reviewed visible history grants")
        authored = [message for message in after["messages"] if message["id"] not in old and message["author_id"] == actor]
        if len(authored) != 1:
            raise ValueError("Canvas did not identify exactly one new authored reply")
        _new_message(authored[0], action, target, set(action.recipients))
        if _ID.validate_python(posted.get("id")) != action.conversation_id:
            raise ValueError("Canvas replied in a different conversation")
        _ack_message(posted, authored[0], allow_missing=False, allow_placeholder=True)
        result = {"verified": True, "conversation_id": action.conversation_id, "message": authored[0],
                  "audience": after["audience"], "new_group_participants": target["delivery"]["new_group_participants"],
                  "included_message_ids": action.included_message_ids,
                  "included_history_grants": target["delivery"]["included_history_grants"]}
        if action.included_message_ids:
            result["coverage"] = {"complete": False, "scope": "sender_visible_reply_and_history_grants",
                "new_message_verified": True, "recipient_active_history_copies_verified": False,
                "reason": "Canvas sender readback includes deleted recipient message rows; recipients' active copies and notification delivery are not exposed."}
    else:
        body.update(subject=action.subject, group_conversation=action.group_conversation,
                    force_new=not action.group_conversation, mode="sync")
        if isinstance(action, InboxForward):
            body.update(forwarded_message_ids=action.message_ids, from_conversation_id=action.source_conversation_id)
        posted = _request(canvas, "conversations", method="POST", body=body)
        expected = ([set(action.recipients)] if action.group_conversation else [{user_id} for user_id in action.recipients])
        if not isinstance(posted, list) or len(posted) != len(expected) or any(not isinstance(row, dict) for row in posted):
            raise ValueError("Canvas did not acknowledge the entire requested conversation set")
        _ids([row.get("id") for row in posted], maximum=20)
        expected_audiences = {frozenset(users - {actor}) for users in expected}
        actual_audiences, conversations = set(), []
        for row in posted:
            after = _thread(canvas, _ID.validate_python(row.get("id")), actor)
            audience = frozenset(after["audience"])
            if audience not in expected_audiences or audience in actual_audiences or after["private"] or after["subject"] != action.subject:
                raise ValueError("Canvas did not confirm the requested new conversation and recipient mapping")
            actual_audiences.add(audience)
            authored = [message for message in after["messages"] if message["author_id"] == actor]
            if len(authored) != 1:
                raise ValueError("Canvas did not identify exactly one new authored message")
            _new_message(authored[0], action, target, set(audience))
            _ack_message(row, authored[0], allow_missing=not action.group_conversation)
            conversations.append({"id": after["id"], "audience": after["audience"], "message": authored[0]})
        result = {"verified": True, "conversations": conversations}
    if _actor(canvas) != actor:
        raise ValueError("Canvas current user changed during message verification")
    return result
