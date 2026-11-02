"""Reviewed immutable uploads into shared Canvas group storage.

Native upload uses the group's default unfiled folder when parent_folder_id is
omitted. Root/default initialization happens only inside the authorized upload,
never in passive discovery. Unknown parent usage-rights policy is explicitly
reviewed; actual saved locking is observed without inventing a prediction.
Sources: Canvas GroupsController#create_file, Api::V1::Attachment#create_new_attachment,
Attachment#default_values/#set_publish_state_for_usage_rights, Folder.unfiled_folder.
https://canvas.instructure.com/doc/api/file.file_uploads.html
"""
from __future__ import annotations

import base64
import hashlib
import re
import tempfile
from pathlib import Path
from typing import Annotated, Literal

from canvas_py import Canvas
from canvas_py.endpoints.file_uploads import UploadContext
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from . import group_files
from .discussion_attachments import _filename, _names
from .responses import dump

MAX_BYTES = 8 * 1024 * 1024
Id = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
_ID = TypeAdapter(Id)
_RIGHT = "manage_files_add"
_FOLDER_TOUCH_FIELDS = {"updated_at", "modified_at", "files_count", "folders_count"}


class GroupFileUpload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["group_file_upload"]
    group_id: Id
    parent_folder_id: Id | None = None
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
            raise ValueError("content_type must be a MIME type without parameters or control characters")
        return value

    @field_validator("content_base64")
    @classmethod
    def immutable_bytes(cls, value: str) -> str:
        raw = base64.b64decode(value, validate=True)
        if not 0 < len(raw) <= MAX_BYTES or base64.b64encode(raw).decode("ascii") != value:
            raise ValueError("Use canonical base64 containing 1 byte to 8 MiB")
        return value


GroupFileUploadActionTypes = GroupFileUpload
KINDS = ["group_file_upload"]
ROUTES = {"group_file_upload": "canvas_upload_group_file"}


def _identity(row: dict, key: str, expected: int) -> None:
    if type(row.get(key)) is not int or row[key] != expected:
        raise ValueError("Canvas did not confirm the reviewed group upload identity or destination")


def _publication(canvas: Canvas, access: dict) -> dict:
    policy = group_files.publication_policy(canvas, access)
    if type(policy.get("known")) is not bool:
        raise ValueError("Canvas did not identify the group publication policy")
    known = policy["known"]
    required = policy.get("usage_rights_required")
    if (known and type(required) is not bool) or (not known and required is not None):
        raise ValueError("Canvas returned conflicting group publication policy evidence")
    return {"policy": policy, "expected_locked": required if known else None,
            "expected_usage_rights": None,
            "effect": ("Canvas is expected to lock the new file until usage rights are provided." if required is True
                       else "Canvas is expected to leave the new file unlocked under the current usage-rights policy." if known
                       else "The prior usage-rights policy is unknown. This review authorizes storage in this shared group "
                            "under its native visibility policy, whether the new file is locked or unlocked. "
                            "The actual saved lock and current-user access will be verified afterward.")}


def _audience(access: dict) -> dict:
    group = access["group"]
    if type(group.get("is_public")) is not bool:
        raise ValueError("Canvas must identify whether this shared group is public before upload")
    if not isinstance(group.get("name"), str) or not 0 < len(group["name"]) <= 4096:
        raise ValueError("Canvas did not identify the shared group audience")
    return {"group_id": group["id"], "group_name": group["name"], "is_public": group["is_public"],
            "scope": "shared_group_native_visibility", "recipient_roster_verified": False,
            "description": ("This group is public; access to its stored files can extend beyond its members, subject to Canvas visibility rules."
                            if group["is_public"] else "Storage is shared with group members and others authorized by Canvas; it is not personal storage.")}


def _destination(canvas: Canvas, action: GroupFileUpload) -> dict:
    if action.parent_folder_id is None:
        return {"selection": "native_default_unfiled", **group_files.native_default_target(canvas, action.group_id)}
    return {"selection": "explicit_folder",
            **group_files.folder_target(canvas, action.group_id, action.parent_folder_id, write=True)}


def snapshot(canvas: Canvas, p: dict) -> dict:
    action = GroupFileUpload.model_validate(p)
    access = group_files.group_access(canvas, action.group_id, _RIGHT)
    audience = _audience(access)
    destination = _destination(canvas, action)
    raw = base64.b64decode(action.content_base64, validate=True)
    publication = _publication(canvas, access)
    if group_files.group_access(canvas, action.group_id, _RIGHT) != access:
        raise ValueError("Group membership or upload permissions changed during preparation")
    return {"access": access, "group_id": action.group_id, "audience": audience,
            "destination": destination, "publication": publication,
            "file": {"filename": action.filename, "content_type": action.content_type,
                     "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()},
            "effect": "Upload these bytes into the reviewed shared group under its native visibility policy. "
                      "Canvas may rename a duplicate filename and send ordinary new-file notifications. "
                      + ("Canvas may initialize its missing group root and native unfiled folder. "
                         if action.parent_folder_id is None else "")
                      + "This does not submit coursework, overwrite a file, or verify notification delivery. "
                      "Concurrent group changes can make verification uncertain; no automatic retry or rollback is performed."}


def _folder_structure(row: dict) -> dict:
    return {key: value for key, value in row.items() if key not in _FOLDER_TOUCH_FIELDS}


def _after_destination(canvas: Canvas, action: GroupFileUpload, before: dict, folder_id: int) -> dict:
    if action.parent_folder_id is not None:
        _identity({"id": folder_id}, "id", action.parent_folder_id)
        after = group_files.folder_target(canvas, action.group_id, folder_id, write=True)
        if (after["root_folder_id"] != before["root_folder_id"]
                or _folder_structure(after["folder"]) != _folder_structure(before["folder"])
                or [_folder_structure(row) for row in after["ancestors"]] !=
                   [_folder_structure(row) for row in before["ancestors"]]):
            raise ValueError("The reviewed group upload folder or its ancestry changed")
        return {"selection": "explicit_folder", **after}
    observed = group_files.observed_native_default(canvas, action.group_id, folder_id, before)
    default = {"group_id": action.group_id, "root_folder_id": observed["root_folder_id"],
               "folder_id": folder_id, "root": observed["ancestors"][-1], "folder": observed["folder"],
               "root_was_absent": False, "default_was_absent": False}
    for key in ("root", "folder"):
        if before.get(key) is not None and _folder_structure(before[key]) != _folder_structure(default[key]):
            raise ValueError("The reviewed native group default folder changed")
    # Verify the actual native choice, not a second selection. Hidden inherited
    # from a root masks a new unfiled folder's own visible workflow state.
    # New rows may have been created concurrently; never claim exclusive creation.
    return {"selection": "native_default_unfiled", **default}


def _saved(canvas: Canvas, action: GroupFileUpload, file_id: int, folder_id: int,
           names: dict, publication: dict, actor: int) -> tuple[dict, str]:
    raw = group_files.read_group_file(canvas, action.group_id, file_id, include_user=True)
    _identity(raw, "folder_id", folder_id)
    if not isinstance(raw.get("user"), dict):
        raise ValueError("Canvas did not independently identify the new group file's uploader")
    _identity(raw["user"], "id", actor)
    actual_names = _names(raw, action, names["size"])
    if actual_names != names:
        raise ValueError("The independently read group file differs from the finalized upload")
    metadata = group_files.file_metadata(raw)
    if "usage_rights" not in metadata or metadata["usage_rights"] is not None:
        raise ValueError("Canvas did not confirm the new file's expected empty usage-rights association")
    if type(metadata.get("locked")) is not bool:
        raise ValueError("Canvas did not confirm the saved group file lock state")
    expected = publication["expected_locked"]
    if expected is not None and metadata["locked"] is not expected:
        raise ValueError("The saved group file lock differs from the reviewed native-policy prediction")
    url = raw.get("url")
    if not isinstance(url, str) or not url:
        raise ValueError("Canvas did not provide readable bytes for independent group upload verification")
    return metadata, url


def perform(canvas: Canvas, p: dict, target: dict) -> dict:
    action = GroupFileUpload.model_validate(p)
    if snapshot(canvas, p) != target:
        raise ValueError("The reviewed group upload audience, policy, folder or bytes changed")
    raw = base64.b64decode(action.content_base64, validate=True)
    options = {"parent_folder_id": action.parent_folder_id} if action.parent_folder_id is not None else {}
    with tempfile.TemporaryDirectory(prefix="canvas-group-upload-") as directory:
        path = Path(directory) / "upload.bin"
        path.write_bytes(raw)
        uploaded = dump(canvas.files.upload(path, context=UploadContext.group(action.group_id),
            name=action.filename, content_type=action.content_type, on_duplicate="rename",
            expected_size=len(raw), expected_sha256=target["file"]["sha256"], verify=False, **options))
    if not isinstance(uploaded, dict):
        raise ValueError("Canvas did not identify the finalized group upload")
    names = _names(uploaded, action, len(raw))
    folder_id = _ID.validate_python(uploaded.get("folder_id"))
    destination = _after_destination(canvas, action, target["destination"], folder_id)
    before, url = _saved(canvas, action, names["id"], folder_id, names, target["publication"], target["access"]["user_id"])
    with tempfile.TemporaryDirectory(prefix="canvas-group-upload-readback-") as directory:
        path = Path(directory) / "verification.bin"
        canvas.files.download(url, path, max_bytes=MAX_BYTES)
        saved = path.read_bytes()
    if len(saved) != len(raw) or hashlib.sha256(saved).hexdigest() != target["file"]["sha256"]:
        raise ValueError("Saved group file bytes differ from the reviewed upload")
    after, _ = _saved(canvas, action, names["id"], folder_id, names, target["publication"], target["access"]["user_id"])
    final_destination = _after_destination(canvas, action, target["destination"], folder_id)
    if before != after or destination != final_destination:
        raise ValueError("The saved group file or destination changed during byte verification")
    access = group_files.group_access(canvas, action.group_id, _RIGHT)
    if access != target["access"] or _publication(canvas, access) != target["publication"]:
        raise ValueError("Group audience, membership, upload permissions or publication policy changed during verification")
    publication = {"policy_known_before_upload": target["publication"]["policy"]["known"],
                   "expected_locked": target["publication"]["expected_locked"],
                   "observed_locked": after["locked"], "actual_access_verified": True,
                   "prediction_verified": target["publication"]["policy"]["known"],
                   **{key: after[key] for key in ("hidden", "locked_for_user", "hidden_for_user", "lock_at", "unlock_at", "usage_rights", "visibility_level") if key in after}}
    return {"verified": True, "group_id": action.group_id, "user_id": access["user_id"],
            "id": names["id"], "folder_id": folder_id, "root_folder_id": destination["root_folder_id"],
            "requested_filename": action.filename, "filename": names["display_name"], "stored_filename": names["filename"],
            "display_name_changed": names["display_name"] != action.filename,
            "requested_content_type": action.content_type, "content_type": after.get("content-type"),
            "bytes": len(raw), "sha256": target["file"]["sha256"], "publication": publication,
            "audience": target["audience"], "destination_selection": destination["selection"],
            "root_was_absent_before_upload": target["destination"].get("root_was_absent", False),
            "default_was_absent_before_upload": target["destination"].get("default_was_absent"),
            "native_destination_observed_after_upload": True, "notification_delivery_verified": False,
            "submitted": False, "overwritten": False,
            "html_url": f"{canvas.config.base_url}/groups/{action.group_id}/files/{names['id']}",
            "read": {"tool": "canvas_read_file", "file_id": names["id"]}}
