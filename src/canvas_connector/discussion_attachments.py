"""Reviewed student discussion entries with one immutable multipart attachment.

Contracts: https://canvas.instructure.com/doc/api/discussion_topics.html.
The official DiscussionTopicsApiController saves entry attachments in the
current user's storage and uses the submissions folder for graded topics.
Its serializer exposes topic attach/reply permissions and entry attachment
metadata. Attachment#handle_duplicates(:rename) may change display_name.
Existing attached-entry edits remain outside this contract: the update
controller clears attachment_id when the attachment parameter is absent.
"""
from __future__ import annotations

import base64
import hashlib
import re
import tempfile
from pathlib import Path
from typing import Annotated, Any, Literal

from canvas_py import Canvas
from canvas_py._request import CanvasRequest
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from . import group_content
from .responses import dump

MAX_BYTES = 8 * 1024 * 1024
Id = Annotated[int, Field(gt=0, le=2**63 - 1, strict=True)]
_ID = TypeAdapter(Id)
_MIME = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+\Z")


def _filename(value: str) -> str:
    if (value != value.strip() or value in {".", ".."} or
            any(c in "/\\" or ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError("filename must be a plain filename without paths or control characters")
    return value


class _Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    context: group_content.ContentContext
    topic_id: Id
    message: Annotated[str, Field(min_length=1, max_length=100000, strict=True)]
    filename: Annotated[str, Field(min_length=1, max_length=240, strict=True)]
    content_type: Annotated[str, Field(min_length=3, max_length=127, strict=True)] = "application/octet-stream"
    content_base64: Annotated[str, Field(min_length=1, max_length=11184812, strict=True)]

    @field_validator("message")
    @classmethod
    def visible_message(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("A discussion message must contain visible text and no NUL characters")
        return value

    @field_validator("filename")
    @classmethod
    def plain_filename(cls, value: str) -> str:
        return _filename(value)

    @field_validator("content_type")
    @classmethod
    def mime_type(cls, value: str) -> str:
        if not _MIME.fullmatch(value):
            raise ValueError("content_type must be a MIME type without parameters or control characters")
        return value

    @field_validator("content_base64")
    @classmethod
    def bounded_bytes(cls, value: str) -> str:
        raw = base64.b64decode(value, validate=True)
        if not raw or len(raw) > MAX_BYTES:
            raise ValueError("Attachment must contain 1 byte to 8 MiB")
        if base64.b64encode(raw).decode("ascii") != value:
            raise ValueError("Attachment must use canonical base64 encoding")
        return value


class DiscussionAttachmentPost(_Attachment):
    kind: Literal["discussion_attachment_post"]


class DiscussionAttachmentReply(_Attachment):
    kind: Literal["discussion_attachment_reply"]
    entry_id: Id


DiscussionAttachmentActionTypes = DiscussionAttachmentPost | DiscussionAttachmentReply
KINDS = ["discussion_attachment_post", "discussion_attachment_reply"]
ROUTES = {kind: "canvas_post_discussion_attachment" for kind in KINDS}
_ACTION = TypeAdapter(Annotated[DiscussionAttachmentActionTypes, Field(discriminator="kind")])


class _Request(CanvasRequest[Any]):
    def __init__(self, path: str, query: list | None = None):
        self._path, self._query = path, query or []

    @property
    def path(self) -> str:
        return self._path

    def query_pairs(self) -> list:
        return self._query


class _Multipart(_Request):
    method = "POST"
    allow_retries = False
    allow_redirects = False

    def __init__(self, path: str, action: DiscussionAttachmentActionTypes):
        super().__init__(path)
        self._message = action.message
        self._part = (action.filename, base64.b64decode(action.content_base64, validate=True), action.content_type)

    def form_body(self) -> dict:
        return {"message": self._message}

    def multipart_files(self) -> dict[str, tuple[str, bytes, str]]:
        return {"attachment": self._part}


def _object(canvas: Canvas, path: str, query: list | None = None) -> dict:
    result = dump(canvas.send(_Request(path, query)))
    if not isinstance(result, dict):
        raise ValueError("Canvas did not return the requested discussion object")  # noqa: TRY004 - invalid upstream shape
    return result


def _identity(value: dict, key: str, expected: int) -> None:
    if type(value.get(key)) is not int or value[key] != expected:
        raise ValueError(f"Canvas did not confirm the expected discussion {key}")


def _actor(canvas: Canvas) -> int:
    return _ID.validate_python(_object(canvas, "users/self/profile").get("id"))


def _base(action: DiscussionAttachmentActionTypes) -> str:
    return f"{group_content.context_path(action.context)}/discussion_topics/{action.topic_id}"


def _entry(canvas: Canvas, action: DiscussionAttachmentActionTypes, entry_id: int) -> dict:
    result = dump(canvas.send(_Request(_base(action) + "/entry_list", [("ids[]", entry_id)])))
    if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict):
        raise ValueError("Canvas did not return exactly the requested discussion entry")
    entry = result[0]
    _identity(entry, "id", entry_id)
    if entry.get("deleted"):
        raise ValueError("The discussion entry is deleted")
    if "discussion_topic_id" in entry:
        _identity(entry, "discussion_topic_id", action.topic_id)
    if "parent_id" not in entry or (entry["parent_id"] is not None and
            (type(entry["parent_id"]) is not int or not 0 < entry["parent_id"] <= 2**63 - 1)):
        raise ValueError("Canvas did not confirm the discussion entry's parent")
    return entry


def _access(canvas: Canvas, action: DiscussionAttachmentActionTypes, actor: int) -> tuple[dict, Any]:
    context = action.context
    if isinstance(context, group_content.GroupContext):
        access = group_content.get_group(canvas, context.group_id)
        _identity(access, "user_id", actor)
        group, membership = access["group"], access["membership"]
        return ({k: group.get(k) for k in ("id", "name", "context_type", "course_id", "account_id", "is_public")},
                {k: membership.get(k) for k in ("id", "group_id", "user_id", "workflow_state")})
    course = _object(canvas, f"courses/{context.course_id}")
    _identity(course, "id", context.course_id)
    if course.get("workflow_state") != "available" or course.get("access_restricted_by_date") is True:
        raise ValueError("The course is not currently available for student participation")
    enrollments = course.get("enrollments")
    own = [item for item in enrollments if isinstance(item, dict) and type(item.get("user_id")) is int
           and item["user_id"] == actor and item.get("type") == "student" and item.get("enrollment_state") == "active"] if isinstance(enrollments, list) else []
    if not own:
        raise ValueError("An active student enrollment for the current user is required")
    membership = sorted(({k: item.get(k) for k in ("user_id", "type", "role", "role_id", "enrollment_state")} for item in own),
                        key=lambda row: (str(row.get("role_id")), str(row.get("role"))))
    return ({k: course.get(k) for k in ("id", "name", "course_code", "workflow_state", "is_public", "is_public_to_auth_users")}, membership)


def snapshot(canvas: Canvas, p: dict) -> dict:
    action = _ACTION.validate_python(p)
    actor = _actor(canvas)
    audience, membership = _access(canvas, action, actor)
    topic = _object(canvas, _base(action), [("include[]", "sections")])
    _identity(topic, "id", action.topic_id)
    key = "group_id" if isinstance(action.context, group_content.GroupContext) else "course_id"
    for field in (key, "context_id"):
        if field in topic:
            _identity(topic, field, getattr(action.context, key))
    if "context_type" in topic and topic["context_type"] not in {action.context.context_type, action.context.context_type.title()}:
        raise ValueError("Canvas returned a topic outside the reviewed context")
    if topic.get("locked") is True or topic.get("locked_for_user") is True or topic.get("published") is False:
        raise ValueError("This discussion is locked, closed or unpublished")
    permissions = topic.get("permissions")
    if not isinstance(permissions, dict) or any(permissions.get(key) is not True for key in ("reply", "attach")):
        raise ValueError("Canvas must explicitly permit replying and attaching to this topic")
    if isinstance(action.context, group_content.CourseContext) and topic.get("group_category_id") is not None:
        raise ValueError("Choose the exact group and child discussion topic for a group discussion")
    sections = topic.get("sections")
    if topic.get("is_section_specific") is True and (not isinstance(sections, list) or not sections):
        raise ValueError("Canvas did not identify this discussion's section audience")
    if sections is not None and (not isinstance(sections, list) or len(sections) > 1000 or any(not isinstance(s, dict) for s in sections)):
        raise ValueError("Canvas returned an invalid or oversized section audience")
    result = {"user_id": actor, "context": action.context.model_dump(), "audience": audience, "membership": membership,
              "topic": {key: topic.get(key) for key in ("id", "title", "assignment_id", "root_topic_id", "group_category_id",
                  "published", "locked", "locked_for_user", "permissions", "require_initial_post", "user_can_see_posts",
                  "subscription_hold", "is_announcement", "anonymous_state", "is_section_specific")},
              "sections": [{k: s.get(k) for k in ("id", "name", "course_id")} for s in sections or []]}
    if isinstance(action, DiscussionAttachmentReply):
        if topic.get("user_can_see_posts") is False or topic.get("subscription_hold") == "initial_post_required":
            raise ValueError("An initial post is required before replying; no automatic unlocking post is permitted")
        entry = _entry(canvas, action, action.entry_id)
        permissions = entry.get("permissions")
        if permissions is not None and (not isinstance(permissions, dict) or permissions.get("reply", True) is not True):
            raise ValueError("Canvas does not permit replying to this entry")
        result["parent_entry"] = {key: entry.get(key) for key in ("id", "user_id", "parent_id", "message", "updated_at", "permissions")}
    raw = base64.b64decode(action.content_base64, validate=True)
    result["attachment"] = {"filename": action.filename, "content_type": action.content_type,
                            "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    result["effect"] = "Publish this message and attachment to the reviewed discussion audience. Canvas may rename the display name on a file-name collision; graded discussions may record participation."
    return result


def _attachment(entry: dict) -> dict:
    one, many = entry.get("attachment"), entry.get("attachments")
    if many is not None and (not isinstance(many, list) or len(many) != 1 or not isinstance(many[0], dict)):
        raise ValueError("Canvas returned ambiguous discussion attachments")
    if one is not None and not isinstance(one, dict):
        raise ValueError("Canvas returned an invalid discussion attachment")
    attachment = one if one is not None else many[0] if many else None
    if not attachment:
        raise ValueError("Canvas did not save the discussion attachment")
    if one is not None and many is not None and any(type(one.get(k)) is not type(many[0].get(k)) or
            one.get(k) != many[0].get(k) for k in ("id", "filename", "display_name", "size")):
        raise ValueError("Canvas returned conflicting discussion attachment identities")
    _ID.validate_python(attachment.get("id"))
    return attachment


def _names(attachment: dict, action: DiscussionAttachmentActionTypes, size: int) -> dict:
    filename = attachment.get("filename")
    if not isinstance(filename, str) or not 1 <= len(filename) <= 1024:
        raise ValueError("Canvas did not identify the attachment's stored filename")
    _filename(filename)
    display = attachment.get("display_name")
    if not isinstance(display, str) or not 1 <= len(display) <= 1024:
        raise ValueError("Canvas did not identify the attachment's saved display name")
    _filename(display)
    # Canvas can CGI-escape/truncate its storage filename or inherit one from
    # identical stored content. The display name preserves the requested name,
    # except for the native number/UUID collision suffixes in make_unique_filename.
    suffix = re.search(r"(?:\.[A-Za-z][A-Za-z0-9]*)*(?:\.[A-Za-z0-9]*)$", action.filename)
    extension = suffix.group() if suffix else ""
    stem = action.filename[:-len(extension)] if extension else action.filename
    uuid = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    collision = re.fullmatch(re.escape(stem) + r"-(?:[1-9][0-9]*|" + uuid + r")" + re.escape(extension), display)
    fallback = re.fullmatch(re.escape(action.filename) + uuid, display)
    if display != action.filename and not collision and not fallback:
        raise ValueError("The attachment display name does not match the requested name or a Canvas collision rename")
    if type(attachment.get("size")) is not int or attachment["size"] != size:
        raise ValueError("The saved attachment size differs from the reviewed bytes")
    return {"id": _ID.validate_python(attachment.get("id")), "filename": filename,
            "display_name": display, "size": size}


def perform(canvas: Canvas, p: dict, target: dict) -> dict:
    action = _ACTION.validate_python(p)
    if snapshot(canvas, p) != target:
        raise ValueError("Discussion audience, permissions, parent or attachment changed after preparation")
    actor, expected = target["user_id"], target["attachment"]
    path = _base(action) + (f"/entries/{action.entry_id}/replies" if isinstance(action, DiscussionAttachmentReply) else "/entries")
    posted = dump(canvas.send(_Multipart(path, action)))
    if not isinstance(posted, dict):
        raise ValueError("Canvas did not identify the created discussion entry")  # noqa: TRY004 - uncertain mutation response
    entry_id = _ID.validate_python(posted.get("id"))
    posted_attachment = _names(_attachment(posted), action, expected["bytes"])
    if _actor(canvas) != actor:
        raise ValueError("The current user changed while posting the attachment")
    saved = _entry(canvas, action, entry_id)
    parent_id = action.entry_id if isinstance(action, DiscussionAttachmentReply) else None
    for entry in (posted, saved):
        _identity(entry, "user_id", actor)
        if entry.get("message") != action.message or "parent_id" not in entry or entry["parent_id"] != parent_id or entry.get("deleted"):
            raise ValueError("The exact discussion message and reply parent were not confirmed")
    if _names(_attachment(saved), action, expected["bytes"]) != posted_attachment:
        raise ValueError("The discussion attachment changed before independent readback")
    file_id = posted_attachment["id"]
    metadata = _object(canvas, f"users/self/files/{file_id}")
    if _names(metadata, action, expected["bytes"]) != posted_attachment:
        raise ValueError("Personal file metadata does not match the discussion attachment")
    if metadata.get("locked_for_user") is True or metadata.get("hidden_for_user") is True:
        raise ValueError("The saved discussion attachment cannot be independently read")
    if "user_id" in metadata:
        _identity(metadata, "user_id", actor)
    if "context_type" in metadata and metadata["context_type"] != "User":
        raise ValueError("The discussion attachment is outside the current user's storage")
    if "context_id" in metadata:
        _identity(metadata, "context_id", actor)
    url = metadata.get("url")
    if not isinstance(url, str) or not url:
        raise ValueError("Canvas did not provide a readable saved attachment")
    with tempfile.TemporaryDirectory(prefix="canvas-discussion-readback-") as directory:
        destination = Path(directory) / "attachment.bin"
        canvas.files.download(url, destination, max_bytes=MAX_BYTES)
        raw = destination.read_bytes()
    if len(raw) != expected["bytes"] or hashlib.sha256(raw).hexdigest() != expected["sha256"]:
        raise ValueError("Downloaded discussion attachment bytes differ from the prepared content")
    # A file/entry can change during the bounded download. Confirm the exact
    # association and visible filename again after verifying the bytes.
    final = _entry(canvas, action, entry_id)
    if _actor(canvas) != actor:
        raise ValueError("The current user changed during attachment verification")
    _identity(final, "user_id", actor)
    if (final.get("message") != action.message or "parent_id" not in final or final["parent_id"] != parent_id or
            _names(_attachment(final), action, expected["bytes"]) != posted_attachment):
        raise ValueError("The saved discussion changed during attachment verification")
    return {"verified": True, "user_id": actor, "context": action.context.model_dump(), "topic_id": action.topic_id,
            "entry_id": entry_id, "parent_id": parent_id, "message": action.message,
            "attachment": {**posted_attachment, "requested_filename": action.filename,
                           "display_name_changed": posted_attachment["display_name"] != action.filename,
                           "sha256": expected["sha256"], "html_url": f"{canvas.config.base_url}/files/{file_id}",
                           "read": {"tool": "canvas_read_file", "file_id": file_id}},
            "html_url": f"{canvas.config.base_url}/{group_content.context_path(action.context)}/discussion_topics/{action.topic_id}"}
