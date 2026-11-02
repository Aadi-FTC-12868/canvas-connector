"""Verified uploads to the current user's native conversation attachment folder.

Canvas selects the first active folder with the exact conversation-attachment
name anywhere in the user's folders. A personal file elsewhere is not eligible.
Discovery never calls folder-creating GET routes. Upload may initialize the
native root/folder through the documented parent_folder_path upload option.
Sources: Canvas User#conversation_attachments_folder, #initialize_default_folder,
ConversationMessage#attachment_ids=, and the Files upload API.
"""
from __future__ import annotations

import base64
import hashlib
import re
import tempfile
from itertools import islice
from pathlib import Path
from typing import Annotated, Any, Literal

from canvas_py import Canvas
from canvas_py._request import CanvasRequest
from canvas_py.endpoints.file_uploads import UploadContext
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from .discussion_attachments import _filename, _names
from .responses import dump

MAX_BYTES = 8 * 1024 * 1024
FOLDER_NAME = "conversation attachments"
Id = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
_ID = TypeAdapter(Id)
_FOLDER_FIELDS = ("id", "name", "parent_folder_id", "context_type", "context_id", "can_upload",
                  "locked_for_user", "hidden_for_user", "for_submissions", "updated_at")


class ConversationAttachmentUpload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["conversation_attachment_upload"]
    filename: Annotated[str, Field(strict=True, min_length=1, max_length=240)]
    content_type: Annotated[str, Field(strict=True, min_length=3, max_length=127)] = "application/octet-stream"
    content_base64: Annotated[str, Field(strict=True, min_length=1, max_length=11184812)]

    @field_validator("filename")
    @classmethod
    def plain_filename(cls, value: str) -> str:
        return _filename(value)

    @field_validator("content_type")
    @classmethod
    def mime_type(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", value):
            raise ValueError("content_type must be a MIME type without parameters")
        return value

    @field_validator("content_base64")
    @classmethod
    def immutable_bytes(cls, value: str) -> str:
        raw = base64.b64decode(value, validate=True)
        if not 0 < len(raw) <= MAX_BYTES or base64.b64encode(raw).decode() != value:
            raise ValueError("Use canonical base64 for 1 byte to 8 MiB of attachment data")
        return value


InboxAttachmentActionTypes = ConversationAttachmentUpload
KINDS = ["conversation_attachment_upload"]
ROUTES = {"conversation_attachment_upload": "canvas_upload_inbox_attachment"}


class _Request(CanvasRequest[Any]):
    allow_redirects = False
    pin_pagination_path = True

    def __init__(self, path: str, query: list | None = None):
        self._path, self._query = path, query or []

    @property
    def path(self) -> str:
        return self._path

    def query_pairs(self) -> list:
        return self._query

    def validate_pagination_query(self, pairs: list[tuple[str, str]], *, page_number: int) -> None:
        expected = [("per_page", "100")]
        if page_number > 1:
            expected.append(("page", str(page_number)))
        if sorted(pairs) != sorted(expected):
            raise ValueError("Canvas changed the complete personal-folder discovery query")


def _object(canvas: Canvas, path: str) -> dict:
    value = dump(canvas.send(_Request(path)))
    if not isinstance(value, dict):
        raise ValueError("Canvas did not return the requested attachment object")
    return value


def _identity(row: dict, key: str, expected: int) -> None:
    if type(row.get(key)) is not int or row[key] != expected:
        raise ValueError("Canvas returned a different attachment, folder or owner")


def _actor(canvas: Canvas) -> int:
    return _ID.validate_python(_object(canvas, "users/self/profile").get("id"))


def _folder_state(row: dict, actor: int, *, accessible: bool = False) -> dict:
    _ID.validate_python(row.get("id"))
    _identity(row, "context_id", actor)
    if row.get("context_type") != "User":
        raise ValueError("Conversation attachments must belong to the current user's folders")
    if (not isinstance(row.get("name"), str) or not 0 < len(row["name"]) <= 1024
            or "parent_folder_id" not in row):
        raise ValueError("Canvas did not identify the attachment folder and its parent")
    if row["parent_folder_id"] is not None:
        _ID.validate_python(row["parent_folder_id"])
    if row.get("workflow_state") == "deleted":
        raise ValueError("Canvas returned a deleted attachment folder")
    if accessible and (row.get("locked_for_user") is not False or row.get("hidden_for_user") is not False):
        raise ValueError("The attachment folder's access is locked, hidden or unknown")
    return {key: row[key] for key in _FOLDER_FIELDS if key in row}


def _destination(canvas: Canvas, actor: int, *, writable: bool) -> dict:
    iterator = canvas.paginate(_Request("users/self/folders", [("per_page", 100)]))
    try:
        raw = [dump(row) for row in islice(iterator, 1001)]
    finally:
        iterator.close()
    if len(raw) > 1000 or any(not isinstance(row, dict) for row in raw):
        raise ValueError("Complete attachment-folder discovery exceeds the supported bound")
    rows = [_folder_state(row, actor) for row in raw]
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("Canvas returned duplicate personal-folder identities")
    roots = [row for row in rows if row["parent_folder_id"] is None]
    if len(roots) > 1 or (roots and roots[0]["name"] != "my files"):
        raise ValueError("Canvas did not identify one native personal root folder")
    candidates = [row for row in rows if row["name"] == FOLDER_NAME]
    if len(candidates) > 1:
        raise ValueError("Multiple native conversation attachment folders are ambiguous")
    root = roots[0] if roots else None
    if not candidates:
        if root:
            current = _folder_state(_object(canvas, f"users/self/folders/{root['id']}"), actor, accessible=True)
            if current != root or (writable and current.get("can_upload") is not True):
                raise ValueError("The native personal root changed or cannot accept attachments")
        return {"actor_id": actor, "folder_id": None, "folder": None, "ancestors": [root] if root else [],
                "root_folder_id": root["id"] if root else None, "creates_folder": True,
                "creates_root": root is None}
    if not root:
        raise ValueError("The native attachment folder does not have a personal root")
    by_id = {row["id"]: row for row in rows}
    chain, seen = [], set()
    current = candidates[0]
    for _ in range(32):
        if current["id"] in seen:
            raise ValueError("Attachment folder ancestry contains a cycle")
        seen.add(current["id"])
        fresh = _folder_state(_object(canvas, f"users/self/folders/{current['id']}"), actor, accessible=True)
        if fresh != current:
            raise ValueError("Attachment folder changed during discovery")
        chain.append(fresh)
        parent = fresh["parent_folder_id"]
        if parent is None:
            if fresh["id"] != root["id"]:
                raise ValueError("Attachment folder does not descend from the personal root")
            if writable and chain[0].get("can_upload") is not True:
                raise ValueError("Canvas did not permit upload to the native attachment folder")
            return {"actor_id": actor, "folder_id": chain[0]["id"], "folder": chain[0],
                    "ancestors": list(reversed(chain[1:])), "root_folder_id": root["id"],
                    "creates_folder": False, "creates_root": False}
        if parent not in by_id:
            raise ValueError("Attachment folder ancestry is incomplete")
        current = by_id[parent]
    raise ValueError("Attachment folder ancestry exceeds the supported bound")


def snapshot(canvas: Canvas, p: dict) -> dict:
    action = ConversationAttachmentUpload.model_validate(p)
    actor = _actor(canvas)
    result = _destination(canvas, actor, writable=True)
    raw = base64.b64decode(action.content_base64, validate=True)
    result["attachment"] = {"filename": action.filename, "content_type": action.content_type,
                            "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    result["effect"] = ("Upload only to your native conversation attachment folder; this does not send a message. "
                        "Canvas may create the missing personal root/attachment folder and rename a duplicate filename.")
    return result


def _metadata(canvas: Canvas, expected: dict) -> tuple[dict, str]:
    row = _object(canvas, f"users/self/files/{expected['id']}")
    _identity(row, "id", expected["id"])
    _identity(row, "folder_id", expected["folder_id"])
    for key in ("user_id", "context_id"):
        if key in row:
            _identity(row, key, expected["actor_id"])
    if "context_type" in row and row["context_type"] != "User":
        raise ValueError("The attachment is outside the current user's storage")
    if row.get("hidden_for_user") is not False or row.get("locked_for_user") is not False:
        raise ValueError("The saved attachment cannot be independently read")
    if (row.get("display_name") != expected["filename"] or row.get("filename") != expected["stored_filename"]
            or type(row.get("size")) is not int or row["size"] != expected["bytes"]):
        raise ValueError("The saved attachment name or size changed")
    if not isinstance(row.get("url"), str) or not row["url"]:
        raise ValueError("Canvas did not provide readable attachment bytes")
    return {key: row.get(key) for key in ("id", "folder_id", "filename", "display_name", "size",
            "updated_at", "modified_at", "locked_for_user", "hidden_for_user", "user_id", "context_id", "context_type")}, row["url"]


def verify_attachments(canvas: Canvas, files: list[dict]) -> list[dict]:
    """Revalidate owner-bound receipt files; return only stable, non-secret data."""
    if not isinstance(files, list) or not 1 <= len(files) <= 20:
        raise ValueError("Use 1 to 20 distinct verified conversation attachments")
    actor = _actor(canvas)
    destination = _destination(canvas, actor, writable=False)
    if destination["creates_folder"]:
        raise ValueError("The native conversation attachment folder is missing")
    result, seen = [], set()
    keys = {"id", "actor_id", "folder_id", "filename", "stored_filename", "bytes", "sha256", "destination"}
    for file in files:
        if not isinstance(file, dict) or set(file) != keys:
            raise ValueError("A complete verified conversation upload receipt is required")
        file_id = _ID.validate_python(file["id"])
        if file_id in seen:
            raise ValueError("Conversation attachment files must be distinct")
        seen.add(file_id)
        if (file["destination"] != "conversation_attachment" or type(file["actor_id"]) is not int
                or file["actor_id"] != actor or type(file["folder_id"]) is not int
                or file["folder_id"] != destination["folder_id"] or type(file["bytes"]) is not int
                or not 0 < file["bytes"] <= MAX_BYTES or not isinstance(file["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", file["sha256"])):
            raise ValueError("Conversation attachment receipt has another owner, destination or invalid bytes")
        for key in ("filename", "stored_filename"):
            if not isinstance(file[key], str) or not 0 < len(file[key]) <= 1024:
                raise ValueError("Conversation attachment receipt lacks a bounded filename")
            _filename(file[key])
        before, url = _metadata(canvas, file)
        with tempfile.TemporaryDirectory(prefix="canvas-inbox-readback-") as directory:
            path = Path(directory) / "attachment.bin"
            canvas.files.download(url, path, max_bytes=MAX_BYTES)
            raw = path.read_bytes()
        if len(raw) != file["bytes"] or hashlib.sha256(raw).hexdigest() != file["sha256"]:
            raise ValueError("Conversation attachment bytes differ from the reviewed upload")
        after, _ = _metadata(canvas, file)
        if before != after:
            raise ValueError("Conversation attachment changed during byte verification")
        result.append(dict(file))
    if _actor(canvas) != actor or _destination(canvas, actor, writable=False) != destination:
        raise ValueError("The attachment owner or native folder changed during verification")
    return result


def perform(canvas: Canvas, p: dict, target: dict) -> dict:
    action = ConversationAttachmentUpload.model_validate(p)
    if snapshot(canvas, p) != target:
        raise ValueError("The native attachment destination changed after preparation")
    raw = base64.b64decode(action.content_base64, validate=True)
    options = ({"parent_folder_id": target["folder_id"]} if target["folder_id"] is not None
               else {"parent_folder_path": FOLDER_NAME})
    with tempfile.TemporaryDirectory(prefix="canvas-inbox-upload-") as directory:
        path = Path(directory) / action.filename
        path.write_bytes(raw)
        uploaded = dump(canvas.files.upload(path, context=UploadContext.user(), name=action.filename,
            content_type=action.content_type, on_duplicate="rename", expected_size=len(raw),
            expected_sha256=target["attachment"]["sha256"], verify=False, **options))
    if not isinstance(uploaded, dict):
        raise ValueError("Canvas did not identify the uploaded conversation attachment")
    names = _names(uploaded, action, len(raw))
    actor = target["actor_id"]
    if _actor(canvas) != actor:
        raise ValueError("The current user changed during the attachment upload")
    destination = _destination(canvas, actor, writable=False)
    folder_id = destination["folder_id"]
    if folder_id is None or (target["folder_id"] is not None and folder_id != target["folder_id"]):
        raise ValueError("Canvas did not retain the reviewed native attachment folder")
    _identity(uploaded, "folder_id", folder_id)
    expected = {"id": names["id"], "actor_id": actor, "folder_id": folder_id,
                "filename": names["display_name"], "stored_filename": names["filename"],
                "bytes": len(raw), "sha256": target["attachment"]["sha256"],
                "destination": "conversation_attachment"}
    verified = verify_attachments(canvas, [expected])[0]
    return {"verified": True, **verified, "requested_filename": action.filename,
            "display_name_changed": names["display_name"] != action.filename,
            "message_sent": False, "submitted": False,
            "folder_was_absent": target["creates_folder"], "root_was_absent": target["creates_root"],
            "html_url": f"{canvas.config.base_url}/files/{names['id']}"}
