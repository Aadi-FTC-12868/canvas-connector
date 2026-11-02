"""Prepared edits that preserve one existing direct discussion attachment.

The native DiscussionEntriesController#update preserves attachment_id when
top-level JSON attachment is an empty object: it is non-nil, while can_attach?
finds no uploaded_data. This is a source-backed compatibility contract, not an
arbitrary attachment setter. Missing/null attachment would remove the file.
https://github.com/instructure/canvas-lms/blob/master/app/controllers/discussion_entries_controller.rb
https://developerdocs.instructure.com/services/canvas/resources/discussion_topics

Ordinary entry JSON omits update permissions and revision counters. Own-author,
current membership/topic access and student-edit settings are checked, explicit
permission denials are honored, and Canvas still enforces native :update.
"""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Annotated, Any, Literal

from canvas_py import Canvas
from canvas_py._request import CanvasRequest
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from . import discussion_attachments, group_content
from .responses import dump

MAX_BYTES = 8 * 1024 * 1024
Id = Annotated[int, Field(gt=0, le=2**63 - 1, strict=True)]
_ID = TypeAdapter(Id)


class DiscussionAttachmentEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["discussion_attachment_edit"]
    context: group_content.ContentContext
    topic_id: Id
    entry_id: Id
    message: Annotated[str, Field(min_length=1, max_length=100000, strict=True)]

    @field_validator("message")
    @classmethod
    def visible_message(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("A discussion message must contain visible text and no NUL characters")
        return value


DiscussionEditActionTypes = DiscussionAttachmentEdit
KINDS = ["discussion_attachment_edit"]
ROUTES = {"discussion_attachment_edit": "canvas_edit_discussion_attachment"}
_ACTION = TypeAdapter(DiscussionAttachmentEdit)

_TOPIC_FIELDS = ("id", "title", "assignment_id", "root_topic_id", "group_category_id", "published",
                 "locked", "locked_for_user", "permissions", "require_initial_post", "user_can_see_posts",
                 "subscription_hold", "is_announcement", "anonymous_state", "is_section_specific")
_FILE_FIELDS = ("id", "folder_id", "filename", "display_name", "size", "content-type", "created_at",
                "updated_at", "modified_at", "locked", "hidden", "locked_for_user", "hidden_for_user",
                "lock_at", "unlock_at", "category", "context_type", "context_id")
_ASSOCIATION_FIELDS = ("id", "filename", "display_name", "size", "content-type", "folder_id")
_EFFECT = (
    "Edit your existing message for the reviewed discussion audience while preserving its direct attachment. "
    "Canvas records the editor and edit history, refreshes the discussion and may process module participation. "
    "New mentions may notify people; eligible graded discussions may notify LTI asset processors. "
    "Attachment preservation does not suppress these normal edit effects."
)


class _Read(CanvasRequest[Any]):
    allow_redirects = False

    def __init__(self, path: str, query: list | None = None):
        self._path, self._query = path, query or []

    @property
    def path(self) -> str:
        return self._path

    def query_pairs(self) -> list:
        return self._query


class _Edit(_Read):
    method = "PUT"
    allow_retries = False
    allow_redirects = False

    def __init__(self, action: DiscussionAttachmentEdit):
        super().__init__(_base(action) + f"/entries/{action.entry_id}")
        self._message = action.message

    def json_body(self) -> dict:
        # Fixed top-level JSON sentinel. Form encoding, omission, null, or
        # nesting this under discussion_entry does not preserve the attachment.
        return {"message": self._message, "attachment": {}}


def _base(action: DiscussionAttachmentEdit) -> str:
    return f"{group_content.context_path(action.context)}/discussion_topics/{action.topic_id}"


def _object(canvas: Canvas, path: str, query: list | None = None) -> dict:
    value = dump(canvas.send(_Read(path, query)))
    if not isinstance(value, dict):
        raise ValueError("Canvas did not return the requested discussion object")  # noqa: TRY004 - invalid upstream shape
    return value


def _identity(value: dict, field: str, expected: int) -> None:
    if type(value.get(field)) is not int or value[field] != expected:
        raise ValueError("Canvas returned a different discussion, entry, file, or current user")


def _file_identity(value: dict) -> dict:
    result = {"id": _ID.validate_python(value.get("id"))}
    for field in ("filename", "display_name"):
        name = value.get(field)
        if not isinstance(name, str) or not 1 <= len(name) <= 1024:
            raise ValueError("Canvas did not identify the existing attachment filename")
        result[field] = name
    size = value.get("size")
    if type(size) is not int or not 0 <= size <= MAX_BYTES:
        raise ValueError("Existing discussion attachment must have a known size of at most 8 MiB")
    result["size"] = size
    for key in ("content-type", "folder_id"):
        if key in value:
            result[key] = value[key]
    return result


def _attachment(entry: dict) -> dict:
    one, many = entry.get("attachment"), entry.get("attachments")
    if one is not None and not isinstance(one, dict):
        raise ValueError("Canvas returned an invalid direct discussion attachment")
    if many is not None and (not isinstance(many, list) or len(many) != 1 or not isinstance(many[0], dict)):
        raise ValueError("Canvas returned ambiguous direct discussion attachments")
    if one is None and many is None:
        raise ValueError("This action requires an existing direct discussion attachment")
    identity = _file_identity(one if one is not None else many[0])
    if one is not None and many is not None and _file_identity(many[0]) != identity:
        raise ValueError("Canvas returned conflicting direct discussion attachment identities")
    return identity


def _entry_state(entry: dict, action: DiscussionAttachmentEdit, actor: int) -> dict:
    _identity(entry, "id", action.entry_id)
    _identity(entry, "user_id", actor)
    if "discussion_topic_id" in entry:
        _identity(entry, "discussion_topic_id", action.topic_id)
    if entry.get("deleted") is not None and entry["deleted"] is not False:
        raise ValueError("The discussion entry is deleted or its state is unknown")
    if "parent_id" not in entry:
        raise ValueError("Canvas did not identify the discussion entry's parent")
    if entry["parent_id"] is not None:
        _ID.validate_python(entry["parent_id"])
    if "message" not in entry or (entry["message"] is not None and
            (not isinstance(entry["message"], str) or len(entry["message"]) > 100000)):
        raise ValueError("Canvas did not return a bounded current discussion message")
    for field in ("created_at", "updated_at"):
        if not isinstance(entry.get(field), str) or not 1 <= len(entry[field]) <= 128:
            raise ValueError("Canvas did not identify the current discussion entry timestamps")
    permissions = entry.get("permissions")
    if "permissions" in entry and (not isinstance(permissions, dict) or
            ("update" in permissions and permissions["update"] is not True)):
        raise ValueError("Canvas does not permit this discussion entry edit")
    result = {key: entry[key] for key in ("id", "user_id", "parent_id", "message", "created_at", "updated_at")}
    if "editor_id" in entry:
        if entry["editor_id"] is not None:
            _ID.validate_python(entry["editor_id"])
        result["editor_id"] = entry["editor_id"]
    result["update_permission"] = permissions.get("update") if isinstance(permissions, dict) else None
    result["attachment"] = _attachment(entry)
    return result


def _entry(canvas: Canvas, action: DiscussionAttachmentEdit, actor: int) -> dict:
    rows = dump(canvas.send(_Read(_base(action) + "/entry_list", [("ids[]", action.entry_id)])))
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError("Canvas did not return exactly the requested discussion entry")
    return _entry_state(rows[0], action, actor)


def _read_state(canvas: Canvas, action: DiscussionAttachmentEdit) -> tuple[dict, str]:
    actor = _ID.validate_python(_object(canvas, "users/self/profile").get("id"))
    # Reuse the existing current-student/accepted-member audience checks, without
    # invoking the attachment-post snapshot's new-upload permission requirements.
    audience, membership = discussion_attachments._access(canvas, action, actor)
    course_id = action.context.course_id if isinstance(action.context, group_content.CourseContext) else None
    if isinstance(action.context, group_content.GroupContext):
        if audience.get("context_type") == "Course":
            course_id = _ID.validate_python(audience.get("course_id"))
        elif audience.get("context_type") != "Account":
            raise ValueError("Canvas did not identify the group's parent context")
    settings = None
    if course_id is not None:
        settings = _object(canvas, f"courses/{course_id}/settings")
        if settings.get("allow_student_discussion_editing") is not True:
            raise ValueError("This student edit requires the course's own-discussion-editing setting")
        settings = {"course_id": course_id, "allow_student_discussion_editing": True}
    topic = _object(canvas, _base(action), [("include[]", "sections")])
    _identity(topic, "id", action.topic_id)
    context_key = "group_id" if isinstance(action.context, group_content.GroupContext) else "course_id"
    for key in (context_key, "context_id"):
        if key in topic:
            _identity(topic, key, getattr(action.context, context_key))
    if "context_type" in topic and topic["context_type"] not in {
            action.context.context_type, action.context.context_type.title()}:
        raise ValueError("Canvas returned a discussion outside the reviewed context")
    if (topic.get("locked") is not False or topic.get("locked_for_user") is not False or
            topic.get("published") is not True or topic.get("lock_info") is not None):
        raise ValueError("The discussion is unavailable, closed, unpublished or locked")
    if topic.get("user_can_see_posts") is not True or topic.get("subscription_hold") == "initial_post_required":
        raise ValueError("Discussion entries are unavailable or require an initial post; never auto-post to unlock them")
    # Topic :reply additionally checks comments_disabled?/post_to_forum. Those
    # are not native own-entry :update predicates: an author can still edit an
    # unlocked announcement entry after replies are disabled. Canvas enforces
    # the remaining entry-specific :update policy on the single PUT.
    if isinstance(action.context, group_content.CourseContext) and topic.get("group_category_id") is not None:
        raise ValueError("Choose the exact group and child discussion for this group topic")
    sections = topic.get("sections")
    if topic.get("is_section_specific") is True and (not isinstance(sections, list) or not sections):
        raise ValueError("Canvas did not identify the discussion's section audience")
    if sections is not None and (not isinstance(sections, list) or len(sections) > 1000 or
            any(not isinstance(section, dict) for section in sections)):
        raise ValueError("Canvas returned an invalid or oversized section audience")
    entry = _entry(canvas, action, actor)
    file = _object(canvas, f"files/{entry['attachment']['id']}")
    identity = _file_identity(file)
    if any(key not in identity or type(identity[key]) is not type(value) or identity[key] != value
           for key, value in entry["attachment"].items()):
        raise ValueError("The existing attachment association differs from its independent file metadata")
    if file.get("locked_for_user") is not False or file.get("hidden_for_user") is not False:
        raise ValueError("The existing attachment cannot be independently read by the current user")
    if file.get("file_state") == "deleted" or file.get("workflow_state") == "deleted":
        raise ValueError("The existing attachment is deleted")
    url = file.get("url")
    if not isinstance(url, str) or not url:
        raise ValueError("Canvas did not provide a readable existing attachment")
    return ({"user_id": actor, "context": action.context.model_dump(), "audience": audience,
             "membership": membership, "student_edit_setting": settings,
             "topic": {key: topic.get(key) for key in _TOPIC_FIELDS},
             "sections": [{key: section.get(key) for key in ("id", "name", "course_id")} for section in sections or []],
             "entry": entry, "file": {key: file[key] for key in _FILE_FIELDS if key in file}}, url)


def snapshot(canvas: Canvas, p: dict) -> dict:
    action = _ACTION.validate_python(p)
    before, url = _read_state(canvas, action)
    with tempfile.TemporaryDirectory(prefix="canvas-discussion-preserve-") as directory:
        destination = Path(directory) / "attachment.bin"
        canvas.files.download(url, destination, max_bytes=MAX_BYTES)
        raw = destination.read_bytes()
    if len(raw) != before["file"]["size"]:
        raise ValueError("Existing attachment download differs from its declared size")
    after, _ = _read_state(canvas, action)
    if after != before:
        raise ValueError("Discussion, audience or attachment changed while verifying the existing bytes")
    return {**before, "attachment_sha256": hashlib.sha256(raw).hexdigest(), "effect": _EFFECT}


def perform(canvas: Canvas, p: dict, target: dict) -> dict:
    action = _ACTION.validate_python(p)
    if snapshot(canvas, p) != target:
        raise ValueError("Discussion message, audience, permissions or attachment changed after preparation")
    if target["entry"]["message"] == action.message:
        return _result(canvas, action, target, no_op=True)
    response = dump(canvas.send(_Edit(action)))
    if not isinstance(response, dict):
        raise ValueError("Canvas did not identify the edited discussion entry")  # noqa: TRY004 - uncertain mutation response
    acknowledgment = _entry_state(response, action, target["user_id"])
    saved = snapshot(canvas, p)
    for entry in (acknowledgment, saved["entry"]):
        if (entry["message"] != action.message or entry.get("editor_id") != target["user_id"] or
                entry["parent_id"] != target["entry"]["parent_id"] or
                entry["created_at"] != target["entry"]["created_at"] or
                entry["attachment"] != target["entry"]["attachment"]):
            raise ValueError("The exact edit and preserved attachment were not independently confirmed")
    for key in ("user_id", "context", "audience", "membership", "student_edit_setting", "topic", "sections", "file", "attachment_sha256"):
        if saved[key] != target[key]:
            raise ValueError("Discussion access or existing attachment changed during the edit")
    return _result(canvas, action, saved, no_op=False)


def _result(canvas: Canvas, action: DiscussionAttachmentEdit, saved: dict, *, no_op: bool) -> dict:
    file = saved["file"]
    return {"verified": True, "user_id": saved["user_id"], "context": saved["context"],
            "topic_id": action.topic_id, "entry_id": action.entry_id,
            "parent_id": saved["entry"]["parent_id"], "message": action.message,
            "editor_id": saved["entry"].get("editor_id"), "updated_at": saved["entry"]["updated_at"],
            "attachment_preserved": True, "no_op": no_op,
            "attachment": {key: file[key] for key in _ASSOCIATION_FIELDS if key in file} | {
                "sha256": saved["attachment_sha256"], "read": {"tool": "canvas_read_file", "file_id": file["id"]},
                "html_url": f"{canvas.config.base_url}/files/{file['id']}"},
            "html_url": f"{canvas.config.base_url}/{group_content.context_path(action.context)}/discussion_topics/{action.topic_id}"}
