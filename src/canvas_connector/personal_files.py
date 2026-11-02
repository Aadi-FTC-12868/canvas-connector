"""Prepared operations on the authenticated user's ordinary personal files.

Contracts: https://canvas.instructure.com/doc/api/files.html and
https://canvas.instructure.com/doc/api/file.file_uploads.html.
Folder metadata (including can_upload/for_submissions) is emitted by
https://github.com/instructure/canvas-lms/blob/master/lib/api/v1/folders.rb.
The ordinary File representation does not supply edit/delete permissions.
Canvas enforces submission associations in Attachment policy and the mutation
endpoints; those denials must propagate, never trigger a fallback or retry.

There are no context/user selectors, path creation, replacement, recursive
deletion, URL following, downloads, or upload requests in this module.
"""
from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from canvas_py import Canvas
from canvas_py._errors import CanvasError
from canvas_py._request import CanvasRequest
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from .responses import dump

Id = Annotated[int, Field(gt=0, le=2**63 - 1, strict=True)]
Name = Annotated[str, Field(min_length=1, max_length=255, strict=True)]


class _Action(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Named(_Action):
    name: Name

    @field_validator("name")
    @classmethod
    def plain_name(cls, value: str) -> str:
        if value != value.strip() or value in {".", ".."} or any(
            c in "/\\" or ord(c) < 32 or ord(c) == 127 for c in value
        ):
            raise ValueError("name must be a plain name without paths, control characters, or surrounding whitespace")
        return value


class PersonalFolderCreate(_Named):
    kind: Literal["personal_folder_create"]
    parent_folder_id: Id


class PersonalFolderRename(_Named):
    kind: Literal["personal_folder_rename"]
    folder_id: Id


class PersonalFolderMove(_Action):
    kind: Literal["personal_folder_move"]
    folder_id: Id
    parent_folder_id: Id


class PersonalFolderDelete(_Action):
    kind: Literal["personal_folder_delete"]
    folder_id: Id


class PersonalFileRename(_Named):
    kind: Literal["personal_file_rename"]
    file_id: Id


class PersonalFileMove(_Action):
    kind: Literal["personal_file_move"]
    file_id: Id
    parent_folder_id: Id


class PersonalFileDelete(_Action):
    kind: Literal["personal_file_delete"]
    file_id: Id


PersonalFileActionTypes = (
    PersonalFolderCreate | PersonalFolderRename | PersonalFolderMove
    | PersonalFolderDelete | PersonalFileRename | PersonalFileMove | PersonalFileDelete
)
KINDS = ["personal_folder_create", "personal_folder_rename", "personal_folder_move",
         "personal_folder_delete", "personal_file_rename", "personal_file_move", "personal_file_delete"]
ROUTES = {kind: "canvas_manage_personal_files" for kind in KINDS}
_ADAPTER = TypeAdapter(Annotated[PersonalFileActionTypes, Field(discriminator="kind")])
_ID = TypeAdapter(Id)

# These are Canvas-managed personal storage locations, not generic name filters
# on files. Submission folders are additionally identified by their API flag,
# including localized names. Unfiled remains a usable file/upload destination.
_SYSTEM_NAMES = frozenset({"submissions", "conversation attachments", "profile pictures",
                           "student annotation documents", "uploaded media", "icon maker icons",
                           "flamegraphs", "n+1 detection"})
_FOLDER_FIELDS = ("id", "name", "full_name", "parent_folder_id", "context_type", "context_id",
                  "for_submissions", "can_upload", "locked_for_user", "hidden_for_user",
                  "locked", "hidden", "lock_at", "unlock_at", "category", "unique_type", "permissions")
_FILE_FIELDS = ("id", "folder_id", "display_name", "filename", "size", "updated_at", "modified_at",
                "locked_for_user", "hidden_for_user", "locked", "hidden", "lock_at", "unlock_at",
                "category", "permissions", "context_type", "context_id", "associated_with_submission")


class _Profile(CanvasRequest[dict[str, Any]]):
    @property
    def path(self) -> str:
        return "users/self/profile"


class _Folder(CanvasRequest[dict[str, Any]]):
    def __init__(self, folder_id: int | Literal["root"]):
        self.folder_id = folder_id

    @property
    def path(self) -> str:
        return f"users/self/folders/{self.folder_id}"


class _File(CanvasRequest[dict[str, Any]]):
    def __init__(self, file_id: int):
        self.file_id = file_id

    @property
    def path(self) -> str:
        return f"users/self/files/{self.file_id}"


class _Mutation(CanvasRequest[dict[str, Any]]):
    # An unconfirmed mutation may already have reached Canvas. Never replay it
    # after a throttle response, or forward its body through a redirect.
    allow_retries = False
    allow_redirects = False

    def __init__(self, action: PersonalFileActionTypes):
        self.action = action

    @property
    def path(self) -> str:
        if isinstance(self.action, PersonalFolderCreate):
            return "users/self/folders"
        if hasattr(self.action, "file_id"):
            return f"files/{self.action.file_id}"
        return f"folders/{self.action.folder_id}"

    @property
    def method(self) -> str:
        if isinstance(self.action, PersonalFolderCreate):
            return "POST"
        if isinstance(self.action, (PersonalFolderDelete, PersonalFileDelete)):
            return "DELETE"
        return "PUT"

    def json_body(self) -> dict | None:
        if isinstance(self.action, PersonalFolderCreate):
            return {"name": self.action.name, "parent_folder_id": self.action.parent_folder_id}
        if isinstance(self.action, (PersonalFolderRename, PersonalFileRename)):
            return {"name": self.action.name}
        if isinstance(self.action, (PersonalFolderMove, PersonalFileMove)):
            return {"parent_folder_id": self.action.parent_folder_id}
        # Omitting force/replace/on_duplicate preserves Canvas's own safeguards.
        return None


def _object(canvas: Canvas, request: CanvasRequest) -> dict:
    value = dump(canvas.send(request))
    if not isinstance(value, dict):
        raise TypeError("Canvas did not return the requested personal file/folder object")
    return value


def _identity(value: dict, field: str, expected: int) -> None:
    if type(value.get(field)) is not int or value[field] != expected:
        raise ValueError("Canvas returned a different personal file, folder, or owner")


def _actor(canvas: Canvas) -> int:
    value = _object(canvas, _Profile()).get("id")
    if type(value) is not int or value < 1:
        raise ValueError("Canvas did not confirm the current user's identity")
    return value


def _permissions(value: dict, permission: str) -> None:
    # Some deployments add permissions; the standard File API omits them.
    # Never invent a required can_edit/can_delete field that Canvas lacks.
    if "permissions" in value:
        permissions = value["permissions"]
        if not isinstance(permissions, dict) or permissions.get(permission) is not True:
            raise ValueError(f"Canvas did not grant the {permission} permission")


def _accessible(value: dict) -> None:
    # A personal owner can manage a globally hidden/locked object. These two
    # user-specific API flags are the authoritative access check.
    if value.get("locked_for_user") is not False or value.get("hidden_for_user") is not False:
        raise ValueError("Personal file/folder is locked, hidden, or its access state is unknown")
    if value.get("workflow_state") == "deleted" or value.get("file_state") == "deleted":
        raise ValueError("Personal file/folder is deleted")


def _folder_state(value: dict, user_id: int) -> dict:
    _identity(value, "context_id", user_id)
    if value.get("context_type") != "User":
        raise ValueError("Only the current user's personal folders are supported")
    _ID.validate_python(value.get("id"))
    if "parent_folder_id" not in value:
        raise ValueError("Canvas did not return the folder's parent")
    if value["parent_folder_id"] is not None:
        _ID.validate_python(value["parent_folder_id"])
    if not isinstance(value.get("name"), str) or not value["name"]:
        raise ValueError("Canvas did not return the folder's name")
    _accessible(value)
    if value.get("for_submissions") is not False:
        raise ValueError("Submission folders and unknown submission-folder states are read-only")
    if (value["name"].casefold() in _SYSTEM_NAMES or value.get("unique_type")
            or value.get("category") == "icon_maker_icons"):
        raise ValueError("Canvas-managed system folders are outside personal file management")
    return {key: value[key] for key in _FOLDER_FIELDS if key in value}


def _folder_chain(canvas: Canvas, folder_id: int, user_id: int) -> dict:
    root = _folder_state(_object(canvas, _Folder("root")), user_id)
    if root["parent_folder_id"] is not None:
        raise ValueError("Canvas did not confirm the personal root folder")
    chain: list[dict] = []
    seen: set[int] = set()
    current_id = folder_id
    # Bounded and exact-ID traversal, never follow a returned URL or name/path.
    for _ in range(32):
        if current_id in seen:
            raise ValueError("Personal folder ancestry contains a cycle")
        seen.add(current_id)
        current = root if current_id == root["id"] else _folder_state(_object(canvas, _Folder(current_id)), user_id)
        _identity(current, "id", current_id)
        chain.append(current)
        parent = current["parent_folder_id"]
        if parent is None:
            if current["id"] != root["id"]:
                raise ValueError("Personal folder does not descend from the current user's root")
            return {"folder_id": folder_id, "root_folder_id": root["id"],
                    "folder": chain[0], "ancestors": list(reversed(chain[1:]))}
        current_id = parent
    raise ValueError("Personal folder ancestry exceeds the 32-folder safety bound")


def _writable_destination(state: dict) -> None:
    if state["folder"].get("can_upload") is not True:
        raise ValueError("Canvas did not confirm this personal folder can accept content")


def validated_personal_folder(canvas: Canvas, folder_id: int) -> dict:
    """Read-only destination snapshot for prepare and pre-upload revalidation.

    Allows the personal root, but excludes submission and system trees. Contains
    user_id, folder_id, root_folder_id, folder, and root-first ancestors. Compare
    the complete snapshot immediately before dispatching a prepared upload.
    """
    folder_id = _ID.validate_python(folder_id)
    user_id = _actor(canvas)
    result = {"user_id": user_id, **_folder_chain(canvas, folder_id, user_id)}
    _writable_destination(result)
    return result


def _file_state(canvas: Canvas, file_id: int, user_id: int, permission: str) -> tuple[dict, dict]:
    value = _object(canvas, _File(file_id))
    _identity(value, "id", file_id)
    _accessible(value)
    _permissions(value, permission)
    if "context_type" in value and value["context_type"] != "User":
        raise ValueError("Only personal files are supported")
    if "context_id" in value:
        _identity(value, "context_id", user_id)
    if "associated_with_submission" in value and value["associated_with_submission"] is not False:
        raise ValueError("Submitted files are read-only")
    # The standard attachment serializer exposes this category even if a
    # system icon has been moved out of its original generated folder.
    if value.get("category") == "icon_maker_icons":
        raise ValueError("Canvas-managed system files are outside personal file management")
    if not isinstance(value.get("display_name"), str) or not value["display_name"]:
        raise ValueError("Canvas did not return the file's name")
    folder_id = _ID.validate_python(value.get("folder_id"))
    folder = _folder_chain(canvas, folder_id, user_id)
    return {key: value[key] for key in _FILE_FIELDS if key in value}, folder


def snapshot(canvas: Canvas, p: dict) -> dict:
    """Bind exact user, object, ancestry, destination, and relevant access state."""
    action = _ADAPTER.validate_python(p)
    user_id = _actor(canvas)
    result: dict = {"user_id": user_id}
    deleting = isinstance(action, (PersonalFileDelete, PersonalFolderDelete))
    permission = "delete" if deleting else "update"
    if isinstance(action, PersonalFolderCreate):
        if action.name.casefold() in _SYSTEM_NAMES:
            raise ValueError("Canvas-managed system folder names are reserved")
    elif hasattr(action, "file_id"):
        result["file_id"] = action.file_id
        result["file"], result["source_folder"] = _file_state(canvas, action.file_id, user_id, permission)
    else:
        source = _folder_chain(canvas, action.folder_id, user_id)
        folder = source["folder"]
        if source["folder_id"] == source["root_folder_id"] or re.fullmatch(r"unfiled(?:\s+\d+)?", folder["name"], re.IGNORECASE):
            raise ValueError("The personal root and default unfiled folders cannot be changed")
        _permissions(folder, permission)
        if folder.get("can_upload") is False:
            raise ValueError("Canvas reports a read-only personal folder")
        if isinstance(action, PersonalFolderRename) and action.name.casefold() in _SYSTEM_NAMES:
            raise ValueError("Canvas-managed system folder names are reserved")
        result.update(folder_id=action.folder_id, source_folder=source)
        if isinstance(action, PersonalFolderDelete):
            # Counts come from an exact read and are rechecked just before the
            # DELETE. Canvas independently refuses nonempty deletion (no force).
            current = _object(canvas, _Folder(action.folder_id))
            _identity(current, "id", action.folder_id)
            if _folder_state(current, user_id) != folder:
                raise ValueError("Folder changed while checking whether it is empty")
            if any(type(current.get(key)) is not int or current[key] != 0 for key in ("files_count", "folders_count")):
                raise ValueError("Only confirmed empty personal folders can be deleted")
            result["empty"] = {"files_count": 0, "folders_count": 0}
    if hasattr(action, "parent_folder_id"):
        destination = _folder_chain(canvas, action.parent_folder_id, user_id)
        _writable_destination(destination)
        _permissions(destination["folder"], "create" if isinstance(action, PersonalFolderCreate) else "update")
        if isinstance(action, PersonalFolderMove) and action.folder_id in {
            destination["folder_id"], *(folder["id"] for folder in destination["ancestors"])
        }:
            raise ValueError("A folder cannot be moved into itself or its descendants")
        result["destination_folder"] = destination
    return result


def perform(canvas: Canvas, p: dict, target: dict) -> dict:
    """Revalidate, dispatch once, then confirm exact persisted state by GET."""
    action = _ADAPTER.validate_python(p)
    if snapshot(canvas, p) != target:
        raise ValueError("Personal file/folder state changed; prepare the action again")
    response = _object(canvas, _Mutation(action))
    user_id = target["user_id"]
    if isinstance(action, PersonalFolderCreate):
        object_id = _ID.validate_python(response.get("id"))
    else:
        object_id = action.file_id if hasattr(action, "file_id") else action.folder_id
        _identity(response, "id", object_id)
    # Actor/context checks also apply after the mutation. A permission/transport
    # failure is uncertain, not evidence that deletion succeeded.
    if _actor(canvas) != user_id:
        raise ValueError("Current user changed during personal file/folder operation")
    if isinstance(action, (PersonalFileDelete, PersonalFolderDelete)):
        source = target["source_folder"]
        parent_id = source["folder_id"] if isinstance(action, PersonalFileDelete) else source["folder"]["parent_folder_id"]
        _folder_chain(canvas, parent_id, user_id)
        request = _File(object_id) if isinstance(action, PersonalFileDelete) else _Folder(object_id)
        try:
            canvas.send(request)
        except CanvasError as exc:
            if exc.status != 404:
                raise
        else:
            raise ValueError("Exact personal file/folder deletion was not confirmed")
        return {"verified": True, "user_id": user_id,
                "file_id" if isinstance(action, PersonalFileDelete) else "folder_id": object_id, "deleted": True}
    if isinstance(action, (PersonalFileRename, PersonalFileMove)):
        current, folder = _file_state(canvas, object_id, user_id, "update")
        expected_name = action.name if isinstance(action, PersonalFileRename) else target["file"]["display_name"]
        expected_parent = action.parent_folder_id if isinstance(action, PersonalFileMove) else target["file"]["folder_id"]
        if current["display_name"] != expected_name or current["folder_id"] != expected_parent:
            raise ValueError("Exact personal file name and location were not confirmed")
        return {"verified": True, "user_id": user_id, "file_id": object_id, "file": current, "folder": folder["folder"]}
    current = _folder_chain(canvas, object_id, user_id)["folder"]
    expected_name = action.name if isinstance(action, (PersonalFolderCreate, PersonalFolderRename)) else target["source_folder"]["folder"]["name"]
    expected_parent = action.parent_folder_id if isinstance(action, (PersonalFolderCreate, PersonalFolderMove)) else target["source_folder"]["folder"]["parent_folder_id"]
    if current["name"] != expected_name or current["parent_folder_id"] != expected_parent:
        raise ValueError("Exact personal folder name and location were not confirmed")
    return {"verified": True, "user_id": user_id, "folder_id": object_id, "folder": current}
