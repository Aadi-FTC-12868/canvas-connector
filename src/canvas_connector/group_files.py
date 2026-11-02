"""Reviewed shared group-file operations with native member permissions.

Canvas Files/Groups APIs and native source at instructure/canvas-lms commit
1c9f0bb8013ed69c4f2efe11fd483025469b7e6c: Folder policy/recursive path updates,
Attachment#set_publish_state_for_usage_rights, and FilesController#api_update.
https://developerdocs.instructure.com/services/canvas/resources/files
https://developerdocs.instructure.com/services/canvas/resources/groups

Group members may manage shared files uploaded by others. Ordinary File JSON
does not expose update/delete or submission-association permission witnesses;
native mutation denials remain authoritative. No recursive delete or replay.
"""
from __future__ import annotations

import base64
import json
import re
from itertools import islice, pairwise
from typing import Annotated, Any, Literal

from canvas_py import Canvas
from canvas_py._errors import CanvasError, CanvasNotFoundError, CanvasPermissionError
from canvas_py._request import CanvasRequest
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from .responses import dump

MAX_ITEMS = 1000
MAX_DEPTH = 32
Id = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
_ID = TypeAdapter(Id)
_RIGHTS = {"manage_files_add", "manage_files_edit", "manage_files_delete"}
_FOLDER_FIELDS = ("id", "name", "full_name", "parent_folder_id", "context_type", "context_id",
    "locked", "hidden", "locked_for_user", "hidden_for_user", "for_submissions", "can_upload",
    "lock_at", "unlock_at", "category", "unique_type", "created_at", "updated_at", "position", "permissions")
_FILE_FIELDS = ("id", "folder_id", "display_name", "filename", "size", "content-type", "created_at",
    "updated_at", "modified_at", "locked", "hidden", "locked_for_user", "hidden_for_user", "lock_at",
    "unlock_at", "visibility_level", "category", "context_type", "context_id", "associated_with_submission", "permissions")
_PROTECTED = {"submissions", "student_annotation_documents", "icon_maker_icons"}
_RACE = "Canvas has no atomic edit precondition here; concurrent group changes can make the result uncertain."


class _Action(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    group_id: Id


class _Named(_Action):
    name: Annotated[str, Field(strict=True, min_length=1, max_length=255)]

    @field_validator("name")
    @classmethod
    def plain_name(cls, value: str) -> str:
        if value != value.strip() or value in {".", ".."} or any(
                char in "/\\" or ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Use a plain name without paths, control characters or surrounding whitespace")
        return value


class GroupFolderCreate(_Named):
    kind: Literal["group_folder_create"]
    parent_folder_id: Id | None = None


class GroupFolderRename(_Named):
    kind: Literal["group_folder_rename"]
    folder_id: Id


class GroupFolderMove(_Action):
    kind: Literal["group_folder_move"]
    folder_id: Id
    parent_folder_id: Id


class GroupFolderDelete(_Action):
    kind: Literal["group_folder_delete"]
    folder_id: Id


class GroupFileRename(_Named):
    kind: Literal["group_file_rename"]
    file_id: Id


class GroupFileMove(_Action):
    kind: Literal["group_file_move"]
    file_id: Id
    parent_folder_id: Id


class GroupFileDelete(_Action):
    kind: Literal["group_file_delete"]
    file_id: Id


GroupFileActionTypes = (GroupFolderCreate | GroupFolderRename | GroupFolderMove | GroupFolderDelete
    | GroupFileRename | GroupFileMove | GroupFileDelete)
KINDS = ["group_folder_create", "group_folder_rename", "group_folder_move", "group_folder_delete",
    "group_file_rename", "group_file_move", "group_file_delete"]
ROUTES = {kind: "canvas_manage_group_files" for kind in KINDS}
_ADAPTER = TypeAdapter(Annotated[GroupFileActionTypes, Field(discriminator="kind")])


class _Request(CanvasRequest[Any]):
    allow_redirects = False
    pin_pagination_path = True

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
        expected = sorted((key, str(value)) for key, value in self._query if key != "page")
        actual = sorted((key, value) for key, value in pairs if key != "page")
        pages = [value for key, value in pairs if key == "page"]
        if actual != expected or len(pages) > 1 or (page_number == 1 and pages):
            raise ValueError("Canvas changed the complete group-file collection scope")
        if page_number > 1:
            if len(pages) != 1 or not 0 < len(pages[0]) <= 4096:
                raise ValueError("Canvas omitted a bounded group-file continuation")
            page = pages[0]
            if page.isascii() and page.isdigit():
                valid = page == str(page_number)
            elif page.startswith("bookmark:"):
                # Native JSONToken is unpadded URL-safe base64 of JSON, not
                # literal JSON. Preserve arbitrary JSON tokens, including
                # scalars and _binary wrappers; "first" is never a next page.
                token = page.removeprefix("bookmark:")
                try:
                    if not re.fullmatch(r"[A-Za-z0-9_-]+", token):
                        raise ValueError("Invalid bookmark alphabet")
                    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
                    json.loads(raw)
                    valid = base64.urlsafe_b64encode(raw).decode().rstrip("=") == token
                except (ValueError, UnicodeError):
                    valid = False
            else:
                valid = False
            if not valid:
                raise ValueError("Canvas skipped or changed the group-file page sequence")


def _object(canvas: Canvas, path: str, **kwargs: Any) -> dict:
    value = dump(canvas.send(_Request(path, **kwargs)))
    if not isinstance(value, dict):
        raise ValueError("Canvas did not return the requested group-file object")  # noqa: TRY004 - invalid upstream shape
    return value


def _all(canvas: Canvas, path: str, *, query: list | None = None, maximum: int = MAX_ITEMS) -> list[dict]:
    iterator = canvas.paginate(_Request(path, query=[*(query or []), ("per_page", 100)]))
    try:
        rows = [dump(row) for row in islice(iterator, maximum + 1)]
    except TypeError as error:
        raise ValueError("Canvas did not return a complete group-file collection") from error
    finally:
        iterator.close()
    if len(rows) > maximum or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Group-file discovery exceeds its complete bounded scope; use Canvas")
    ids = [_ID.validate_python(row.get("id")) for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Canvas returned duplicate group-file collection identities")
    return rows


def _identity(row: dict, key: str, expected: int) -> None:
    if type(row.get(key)) is not int or row[key] != expected:
        raise ValueError("Canvas did not confirm the exact group, member, folder or file")


def group_access(canvas: Canvas, group_id: int, permission: str) -> dict:
    """Current accepted membership and actual native operation permissions."""
    _ID.validate_python(group_id)
    if permission not in _RIGHTS:
        raise ValueError("Unsupported group-file permission")
    actor = _ID.validate_python(_object(canvas, "users/self/profile").get("id"))
    group = _object(canvas, f"groups/{group_id}")
    _identity(group, "id", group_id)
    _text(group, "name", 255)
    if type(group.get("is_public")) is not bool:
        raise ValueError("Canvas did not identify whether this group has a public audience")
    if (group.get("non_collaborative", False) is not False or
            group.get("workflow_state", "available") not in {"available", "active"}):
        raise ValueError("Group-file operations require an active collaborative group")
    member = _object(canvas, f"groups/{group_id}/users/self")
    _identity(member, "user_id", actor)
    _identity(member, "group_id", group_id)
    if member.get("workflow_state") != "accepted":
        raise ValueError("Group-file operations require the current member's accepted membership")
    context = group.get("context_type")
    if context not in {"Course", "Account"}:
        raise ValueError("Canvas did not identify the group's parent context")
    _ID.validate_python(group.get("course_id" if context == "Course" else "account_id"))
    wanted = ["participate", "read_files", permission]
    rights = _object(canvas, f"groups/{group_id}/permissions", query=[("permissions[]", key) for key in wanted])
    if any(rights.get(key) is not True for key in wanted):
        raise ValueError("Canvas did not grant current participation and the required shared-file permission")
    return {"user_id": actor, "group": {key: group[key] for key in (
        "id", "name", "context_type", "course_id", "account_id", "non_collaborative", "workflow_state",
        "is_public", "public", "join_level", "group_category_id") if key in group},
        "membership": {key: member[key] for key in ("id", "user_id", "group_id", "workflow_state") if key in member},
        "permissions": {key: True for key in wanted}}


def publication_policy(canvas: Canvas, access: dict) -> dict:
    """Known course policy or explicit unknown account/unavailable policy."""
    group = access["group"]
    context = group["context_type"]
    context_id = group["course_id" if context == "Course" else "account_id"]
    result = {"known": False, "usage_rights_required": None, "context_type": context,
              "context_id": context_id, "source": "unavailable_account_policy"}
    if context == "Course":
        result["source"] = "unavailable_course_settings"
        try:
            settings = _object(canvas, f"courses/{context_id}/settings")
        except (CanvasPermissionError, CanvasNotFoundError):
            pass
        else:
            required = settings.get("usage_rights_required")
            if "usage_rights_required" not in settings:
                return result
            if type(required) is not bool:
                raise ValueError("Canvas returned an invalid usage-rights policy")
            result.update(known=True, usage_rights_required=required, source="course_settings")
    return result


def _accessible(row: dict) -> None:
    for key in ("locked", "hidden", "locked_for_user", "hidden_for_user"):
        if type(row.get(key)) is not bool:
            raise ValueError("Canvas did not establish the raw file/folder access state")
    # Metadata management rights and download visibility are distinct native
    # permissions. Preserve these raw flags; actual management rights and
    # scoped reads are checked separately. Uploads verify readable bytes too.
    if row.get("workflow_state") == "deleted" or row.get("file_state") == "deleted":
        raise ValueError("This group file/folder is deleted")
    if row.get("category") in _PROTECTED or row.get("unique_type"):
        raise ValueError("Canvas-managed system files/folders are outside this operation")


def _text(row: dict, key: str, limit: int = 4096) -> None:
    if not isinstance(row.get(key), str) or not 0 < len(row[key]) <= limit:
        raise ValueError("Canvas did not return bounded group-file names or version metadata")


def _folder(row: dict, group_id: int) -> dict:
    _ID.validate_python(row.get("id"))
    _identity(row, "context_id", group_id)
    if row.get("context_type") != "Group" or "parent_folder_id" not in row:
        raise ValueError("This folder is outside the exact group or has unknown ancestry")
    if row["parent_folder_id"] is not None:
        _ID.validate_python(row["parent_folder_id"])
    for key in ("name", "full_name"):
        _text(row, key)
    _accessible(row)
    if row.get("for_submissions") is not False or type(row.get("can_upload")) is not bool:
        raise ValueError("Submission folders or unknown writable states are not supported")
    return {key: row[key] for key in _FOLDER_FIELDS if key in row}


def _catalog(canvas: Canvas, group_id: int) -> tuple[list[dict], dict | None]:
    # Native /folders/root creates a missing root. Discover an existing root
    # through the passive list instead; never initialize storage in preparation.
    rows = _all(canvas, f"groups/{group_id}/folders")
    for row in rows:
        _identity(row, "context_id", group_id)
        if row.get("context_type") != "Group" or "parent_folder_id" not in row:
            raise ValueError("Canvas returned an ambiguous group folder collection")
    roots = [row for row in rows if row["parent_folder_id"] is None]
    if len(roots) > 1 or (not roots and rows):
        raise ValueError("Canvas did not identify unambiguous existing group ancestry")
    return rows, _folder(roots[0], group_id) if roots else None


def native_default_target(canvas: Canvas, group_id: int) -> dict:
    """Passive native unfiled discovery; only an authorized POST creates it."""
    _ID.validate_python(group_id)
    rows, root = _catalog(canvas, group_id)
    if root is not None and root["name"] != "files":
        raise ValueError("The existing root is not Canvas's native default root; choose an explicit parent_folder_id")
    matching = [row for row in rows if root is not None and row["parent_folder_id"] == root["id"] and
                isinstance(row.get("name"), str) and re.fullmatch(r"unfiled(?:\s\d+)?", row["name"])]
    if root and root["hidden"] and matching:
        raise ValueError("The hidden root masks native default selection; choose an explicit parent_folder_id")
    candidates = [row for row in matching if row.get("hidden") is False]
    if len(candidates) > 1:
        raise ValueError("Multiple native default folders exist; choose an explicit parent_folder_id")
    folder = folder_target(canvas, group_id, candidates[0]["id"])["folder"] if candidates else None
    return {"group_id": group_id, "root_folder_id": root["id"] if root else None,
            "folder_id": folder["id"] if folder else None, "root": root, "folder": folder,
            "root_was_absent": root is None, "default_was_absent": folder is None}


def folder_target(canvas: Canvas, group_id: int, folder_id: int, write: bool = True) -> dict:
    """Validate an existing same-group destination/source and its ancestry."""
    _ID.validate_python(group_id)
    _ID.validate_python(folder_id)
    chain, seen = [], set()
    current_id = folder_id
    for _ in range(MAX_DEPTH):
        if current_id in seen:
            raise ValueError("Group folder ancestry contains a cycle")
        seen.add(current_id)
        raw = _object(canvas, f"groups/{group_id}/folders/{current_id}")
        _identity(raw, "id", current_id)
        current = _folder(raw, group_id)
        chain.append(current)
        if current["parent_folder_id"] is None:
            for child, parent in pairwise(chain):
                if parent["hidden"] and not child["hidden"]:
                    raise ValueError("Canvas returned inconsistent inherited ancestor visibility")
                if child["full_name"] != parent["full_name"] + "/" + child["name"]:
                    raise ValueError("Canvas returned inconsistent group ancestry paths")
            if write and chain[0]["can_upload"] is not True:
                raise ValueError("Canvas did not grant management of this folder's contents")
            return {"group_id": group_id, "folder_id": folder_id, "root_folder_id": current["id"],
                    "folder": chain[0], "ancestors": chain[1:]}
        current_id = current["parent_folder_id"]
    raise ValueError("Group folder ancestry exceeds 32 levels; use Canvas")


def observed_native_default(canvas: Canvas, group_id: int, folder_id: int, previous: dict) -> dict:
    """Verify the actual native-choice parent after an authorized creation.

    The returned object's exact scoped folder ID resolves native selection;
    do not rerun an ambiguous default predictor and mistake observed for absent.
    Concurrent creation is possible; this does not assert unique causation.
    """
    destination = folder_target(canvas, group_id, folder_id)
    root = destination["ancestors"][-1] if destination["ancestors"] else destination["folder"]
    if (destination["folder"]["parent_folder_id"] != destination["root_folder_id"] or root["name"] != "files" or
            not re.fullmatch(r"unfiled(?:\s\d+)?", destination["folder"]["name"]) or
            any(previous[key] is not None and previous[key] != destination[key] for key in ("root_folder_id", "folder_id"))):
        raise ValueError("Canvas changed the previously reviewed native default destination")
    if previous.get("root") is not None and _without(root, {"updated_at", "position"}) != _without(previous["root"], {"updated_at", "position"}):
        raise ValueError("The existing native root changed during storage initialization")
    return destination


def file_metadata(row: dict) -> dict:
    """Validated safe projection; never return a signed download URL."""
    _ID.validate_python(row.get("id"))
    _ID.validate_python(row.get("folder_id"))
    for key in ("filename", "display_name", "updated_at", "modified_at"):
        _text(row, key, 1024)
    if type(row.get("size")) is not int or not 0 <= row["size"] <= 2**63 - 1:
        raise ValueError("Canvas did not establish the existing file size")
    _accessible(row)
    if row.get("associated_with_submission") is not None and row["associated_with_submission"] is not False:
        raise ValueError("Submitted files are not managed through group-file actions")
    result = {key: row[key] for key in _FILE_FIELDS if key in row}
    if "usage_rights" in row:
        rights = row["usage_rights"]
        if rights is not None and (not isinstance(rights, dict) or not rights):
            raise ValueError("Canvas returned an unknown file usage-rights association")
        if isinstance(rights, dict) and any(value is not None and
                (not isinstance(value, str) or len(value) > 4096) for value in rights.values()):
            raise ValueError("Canvas returned unbounded file usage-rights metadata")
        result["usage_rights"] = ({key: rights[key] for key in (
            "legal_copyright", "use_justification", "license", "license_name") if key in rights}
            if isinstance(rights, dict) else None)
        if isinstance(result["usage_rights"], dict) and not result["usage_rights"]:
            raise ValueError("Canvas did not identify a native usage-rights association")
    if isinstance(row.get("user"), dict) and "id" in row["user"]:
        result["user"] = {"id": _ID.validate_python(row["user"]["id"])}
    return result


def read_group_file(canvas: Canvas, group_id: int, file_id: int, *, include_user: bool = False) -> dict:
    """Internal raw file read; caller must keep URL private and project metadata."""
    _ID.validate_python(group_id)
    _ID.validate_python(file_id)
    query = [("include[]", "usage_rights"), *([("include[]", "user")] if include_user else [])]
    row = _object(canvas, f"groups/{group_id}/files/{file_id}", query=query)
    _identity(row, "id", file_id)
    if "context_id" in row:
        _identity(row, "context_id", group_id)
    if "context_type" in row and row["context_type"] != "Group":
        raise ValueError("Canvas returned a file outside this group")
    file_metadata(row)
    parent = folder_target(canvas, group_id, row["folder_id"], write=False)
    if parent["folder"]["hidden"] and not row["hidden"]:
        raise ValueError("Canvas returned inconsistent inherited file visibility")
    return row


def _permission(row: dict, key: str) -> None:
    if "permissions" in row and (not isinstance(row["permissions"], dict) or
            (key in row["permissions"] and row["permissions"][key] is not True)):
        raise ValueError("Canvas explicitly denied this file/folder operation")


def _ordinary_source(target: dict) -> None:
    folder = target["folder"]
    if folder["id"] == target["root_folder_id"]:
        raise ValueError("The group root folder cannot be renamed, moved or deleted")


def _subtree(canvas: Canvas, group_id: int, folder: dict) -> dict:
    """Complete active API-visible subtree, including its root, within bounds."""
    folders, files, queue = {}, {}, [(folder["id"], 1, folder)]
    while queue:
        folder_id, depth, listed = queue.pop(0)
        if depth > MAX_DEPTH or folder_id in folders:
            raise ValueError("Group subtree has a cycle or exceeds 32 levels")
        raw = _object(canvas, f"groups/{group_id}/folders/{folder_id}")
        _identity(raw, "id", folder_id)
        current = _folder(raw, group_id)
        if current != listed:
            raise ValueError("A folder changed during complete subtree discovery")
        folders[folder_id] = current
        remaining = MAX_ITEMS - len(folders) - len(files) - len(queue)
        if remaining < 0:
            raise ValueError("Group subtree exceeds 1000 active items")
        children = _all(canvas, f"folders/{folder_id}/folders", maximum=remaining)
        children_files = _all(canvas, f"folders/{folder_id}/files", maximum=remaining - len(children),
                              query=[("include[]", "usage_rights")])
        for key, rows in (("folders_count", children), ("files_count", children_files)):
            if type(raw.get(key)) is not int or raw[key] != len(rows):
                raise ValueError("Group folder counts disagree with complete active contents")
        for child in children:
            _identity(child, "parent_folder_id", folder_id)
            item = _folder(child, group_id)
            if current["hidden"] and not item["hidden"]:
                raise ValueError("Canvas returned inconsistent inherited folder visibility")
            if item["full_name"] != current["full_name"] + "/" + item["name"]:
                raise ValueError("Canvas returned inconsistent descendant folder paths")
            if item["id"] in folders or any(item["id"] == queued for queued, _, _ in queue):
                raise ValueError("Group subtree contains duplicate folder identities")
            queue.append((item["id"], depth + 1, item))
        for child in children_files:
            _identity(child, "folder_id", folder_id)
            if "context_id" in child:
                _identity(child, "context_id", group_id)
            if "context_type" in child and child["context_type"] != "Group":
                raise ValueError("Canvas returned a descendant file outside this group")
            if current["hidden"] and child.get("hidden") is not True:
                raise ValueError("Canvas returned inconsistent inherited file visibility")
            if child["id"] in files:
                raise ValueError("Group subtree contains duplicate file identities")
            files[child["id"]] = file_metadata(child)
    return {"folders": sorted(folders.values(), key=lambda row: row["id"]),
            "files": sorted(files.values(), key=lambda row: row["id"]),
            "coverage": "complete_active_api_visible_subtree", "items": len(folders) + len(files),
            "native_effect": "Canvas recursively updates folder paths, including deleted rows not exposed by this inventory. " + _RACE}


def _siblings(canvas: Canvas, group_id: int, folder_id: int, *, files: bool, name: str, excluding: int | None) -> list[dict]:
    rows = _all(canvas, f"folders/{folder_id}/{'files' if files else 'folders'}")
    projected = []
    for row in rows:
        _identity(row, "folder_id" if files else "parent_folder_id", folder_id)
        field = "display_name" if files else "name"
        _text(row, field)
        if not files:
            _identity(row, "context_id", group_id)
            if row.get("context_type") != "Group":
                raise ValueError("Canvas returned a destination sibling outside this group")
        if row["id"] != excluding and row[field] == name:
            raise ValueError("The destination already contains that name; choose a different name")
        projected.append({"id": row["id"], field: row[field]})
    return sorted(projected, key=lambda row: row["id"])


def _right(action: GroupFileActionTypes) -> str:
    return "manage_files_add" if isinstance(action, GroupFolderCreate) else (
        "manage_files_delete" if isinstance(action, (GroupFileDelete, GroupFolderDelete)) else "manage_files_edit")


def _audience(access: dict) -> dict:
    group = access["group"]
    return {"group_id": group["id"], "group_name": group["name"], "is_public": group["is_public"],
        "description": ("This is a public group: publishing or unhiding shared files may make them visible beyond group members under Canvas access rules."
            if group["is_public"] else "Shared files are for this group's members and others authorized by Canvas; this is not personal storage.")}


def _visibility(before: bool, source_parent: dict, destination: dict) -> dict:
    if destination["hidden"]:
        expected = True
    elif not source_parent["hidden"]:
        expected = before
    else:
        expected = None
    return {"known": expected is not None, "before_hidden": before, "expected_hidden": expected,
            "changes_hidden": before != expected if expected is not None else None,
            "effect": "Canvas inherits visibility from the exact destination. When prediction is unknown, this may hide or unhide shared content; authorize that native outcome. No visibility override is sent."}


def snapshot(canvas: Canvas, p: dict) -> dict:
    action = _ADAPTER.validate_python(p)
    access = group_access(canvas, action.group_id, _right(action))
    target = {"access": access, "audience": _audience(access),
              "effect": "Change shared group storage for the reviewed audience. " + _RACE}
    if isinstance(action, GroupFolderCreate):
        parent = action.parent_folder_id
        if parent is None:
            default = native_default_target(canvas, action.group_id)
            target["native_default"] = default
            parent = default["folder_id"]
            target["effect"] += " The authorized POST uses Canvas's native unfiled folder and may initialize the root/default folders."
        destination = folder_target(canvas, action.group_id, parent) if parent is not None else None
        target["destination"] = destination
        target["siblings"] = (_siblings(canvas, action.group_id, parent, files=False, name=action.name, excluding=None)
                              if parent is not None else [])
    elif hasattr(action, "file_id"):
        file = file_metadata(read_group_file(canvas, action.group_id, action.file_id))
        _permission(file, "delete" if isinstance(action, GroupFileDelete) else "update")
        target.update(file=file, source=folder_target(canvas, action.group_id, file["folder_id"]))
        if not isinstance(action, GroupFileDelete):
            parent = action.parent_folder_id if isinstance(action, GroupFileMove) else file["folder_id"]
            name = action.name if isinstance(action, GroupFileRename) else file["display_name"]
            target["destination"] = folder_target(canvas, action.group_id, parent)
            target["siblings"] = _siblings(canvas, action.group_id, parent, files=True, name=name, excluding=action.file_id)
            policy = publication_policy(canvas, access)
            no_op = name == file["display_name"] and parent == file["folder_id"]
            expected = file["locked"] if no_op else (False if policy["usage_rights_required"] is False or
                isinstance(file.get("usage_rights"), dict) else True if policy["usage_rights_required"] is True and
                "usage_rights" in file and file["usage_rights"] is None else None)
            target["publication"] = {"policy": policy, "known": expected is not None,
                "before_locked": file["locked"], "expected_locked": expected,
                "changes_locked": expected != file["locked"] if expected is not None else None,
                "effect": "Canvas recomputes this shared file's locked flag from usage rights. An unknown prediction may publish or unpublish it for the reviewed group audience; authorize that native outcome."}
            target["no_op"] = no_op
            target["visibility"] = (_visibility(file["hidden"], target["source"]["folder"], target["destination"]["folder"])
                if isinstance(action, GroupFileMove) and not no_op else
                {"known": True, "before_hidden": file["hidden"], "expected_hidden": file["hidden"], "changes_hidden": False})
    else:
        source = folder_target(canvas, action.group_id, action.folder_id)
        _ordinary_source(source)
        _permission(source["folder"], "delete" if isinstance(action, GroupFolderDelete) else "update")
        target.update(source=source, subtree=_subtree(canvas, action.group_id, source["folder"]))
        if (source["folder"]["parent_folder_id"] == source["root_folder_id"] and
                re.fullmatch(r"unfiled(?:\s\d+)?", source["folder"]["name"])):
            target["effect"] += " This is a native default-folder candidate; future uploads may select or create another unfiled folder."
        if isinstance(action, GroupFolderDelete):
            if target["subtree"]["items"] != 1:
                raise ValueError("Only empty group folders can be deleted; recursive deletion is not supported")
        else:
            parent = action.parent_folder_id if isinstance(action, GroupFolderMove) else source["folder"]["parent_folder_id"]
            name = action.name if isinstance(action, GroupFolderRename) else source["folder"]["name"]
            destination = folder_target(canvas, action.group_id, parent)
            if action.folder_id in {destination["folder_id"], *(row["id"] for row in destination["ancestors"])}:
                raise ValueError("A group folder cannot be moved into itself or its descendants")
            if len(destination["ancestors"]) + 2 + _subtree_depth(target["subtree"], action.folder_id) - 1 > MAX_DEPTH:
                raise ValueError("The moved subtree would exceed the 32-level group ancestry bound")
            target["destination"] = destination
            target["siblings"] = _siblings(canvas, action.group_id, parent, files=False, name=name, excluding=action.folder_id)
            target["no_op"] = name == source["folder"]["name"] and parent == source["folder"]["parent_folder_id"]
            target["visibility"] = (_visibility(source["folder"]["hidden"], source["ancestors"][0], destination["folder"])
                if isinstance(action, GroupFolderMove) and not target["no_op"] else
                {"known": True, "before_hidden": source["folder"]["hidden"], "expected_hidden": source["folder"]["hidden"], "changes_hidden": False})
    if group_access(canvas, action.group_id, _right(action)) != access:
        raise ValueError("Current group membership or permissions changed during preparation")
    return target


def _subtree_depth(tree: dict, root_id: int) -> int:
    parents = {row["id"]: row["parent_folder_id"] for row in tree["folders"]}
    deepest = 1
    for item in parents:
        depth, current = 1, item
        while current != root_id:
            current, depth = parents[current], depth + 1
        deepest = max(deepest, depth)
    return deepest


def _without(row: dict, keys: set[str]) -> dict:
    return {key: value for key, value in row.items() if key not in keys}


def _verify_location(canvas: Canvas, group_id: int, previous: dict) -> None:
    current = folder_target(canvas, group_id, previous["folder_id"])
    old_rows, new_rows = [previous["folder"], *previous["ancestors"]], [current["folder"], *current["ancestors"]]
    if (current["root_folder_id"] != previous["root_folder_id"] or
            [_without(row, {"updated_at", "position"}) for row in old_rows] !=
            [_without(row, {"updated_at", "position"}) for row in new_rows]):
        raise ValueError("A reviewed surviving source/destination or its ancestry changed")


def _parent_target(source: dict) -> dict:
    return {"folder_id": source["folder"]["parent_folder_id"], "root_folder_id": source["root_folder_id"],
            "folder": source["ancestors"][0], "ancestors": source["ancestors"][1:]}


def perform(canvas: Canvas, p: dict, target: dict) -> dict:
    action = _ADAPTER.validate_python(p)
    if snapshot(canvas, p) != target:
        raise ValueError("Shared group-file state changed after review; prepare again")
    if target.get("no_op"):
        return _result(canvas, action, target, no_op=True)
    create = isinstance(action, GroupFolderCreate)
    file_action = hasattr(action, "file_id")
    deleting = isinstance(action, (GroupFileDelete, GroupFolderDelete))
    object_id = None if create else action.file_id if file_action else action.folder_id
    path = f"groups/{action.group_id}/folders" if create else f"{'files' if file_action else 'folders'}/{object_id}"
    body = {"name": action.name} if hasattr(action, "name") else None
    if getattr(action, "parent_folder_id", None) is not None:
        body = {**(body or {}), "parent_folder_id": action.parent_folder_id}
    response = _object(canvas, path, method="POST" if create else "DELETE" if deleting else "PUT", body=body)
    if create:
        object_id = _ID.validate_python(response.get("id"))
    else:
        _identity(response, "id", object_id)
    if group_access(canvas, action.group_id, _right(action)) != target["access"]:
        raise ValueError("Current group membership or permissions changed during the operation")
    if deleting:
        _verify_location(canvas, action.group_id, target["source"] if file_action else _parent_target(target["source"]))
        try:
            _object(canvas, f"groups/{action.group_id}/{'files' if file_action else 'folders'}/{object_id}")
        except CanvasError as error:
            if error.status != 404:
                raise
        else:
            raise ValueError("Exact group file/folder deletion was not confirmed")
        if group_access(canvas, action.group_id, _right(action)) != target["access"]:
            raise ValueError("Group access changed while confirming deletion")
        return {"verified": True, "group_id": action.group_id, "user_id": target["access"]["user_id"],
                "file_id" if file_action else "folder_id": object_id, "deleted": True, "audience": target["audience"]}
    if file_action:
        current = file_metadata(read_group_file(canvas, action.group_id, object_id))
        expected = dict(target["file"])
        locked = target["publication"]["expected_locked"]
        hidden = target["visibility"]["expected_hidden"]
        expected.update(display_name=action.name if isinstance(action, GroupFileRename) else expected["display_name"],
                        folder_id=action.parent_folder_id if isinstance(action, GroupFileMove) else expected["folder_id"],
                        locked=current["locked"] if locked is None else locked,
                        hidden=current["hidden"] if hidden is None else hidden)
        if locked is None or locked != target["file"]["locked"]:
            expected["locked_for_user"] = current["locked_for_user"]
        if hidden is None or hidden != target["file"]["hidden"]:
            expected["hidden_for_user"] = current["hidden_for_user"]
        # modified_at can first become persisted on an old attachment whose
        # API value previously fell back to updated_at. This is metadata-only
        # verification, not a claim of independently verified remote bytes.
        mutable_timestamps = {"updated_at"}
        if target["file"]["modified_at"] == target["file"]["updated_at"]:
            mutable_timestamps.add("modified_at")
        if (_without(current, mutable_timestamps) != _without(expected, mutable_timestamps) or
                publication_policy(canvas, target["access"]) != target["publication"]["policy"]):
            raise ValueError("Exact group file metadata and reviewed publication effect were not confirmed")
        saved = {**target, "file": current}
        _verify_location(canvas, action.group_id, target["destination"])
        if target["source"]["folder_id"] != target["destination"]["folder_id"]:
            _verify_location(canvas, action.group_id, target["source"])
    else:
        current_target = folder_target(canvas, action.group_id, object_id)
        current = current_target["folder"]
        name = action.name if hasattr(action, "name") else target["source"]["folder"]["name"]
        parent = action.parent_folder_id if hasattr(action, "parent_folder_id") else target["source"]["folder"]["parent_folder_id"]
        if create and parent is None:
            previous = target["native_default"]
            parent = current["parent_folder_id"]
            observed_native_default(canvas, action.group_id, parent, previous)
        destination = folder_target(canvas, action.group_id, parent)
        if target.get("destination") is not None:
            _verify_location(canvas, action.group_id, target["destination"])
        if not create and target["source"]["folder"]["parent_folder_id"] != parent:
            _verify_location(canvas, action.group_id, _parent_target(target["source"]))
        if current["name"] != name or current["parent_folder_id"] != parent:
            raise ValueError("The exact group folder name and parent were not confirmed")
        if current["full_name"] != destination["folder"]["full_name"] + "/" + name:
            raise ValueError("The exact group folder full path was not confirmed")
        if not create:
            expected_tree = target["subtree"]
            actual_tree = _subtree(canvas, action.group_id, current)
            hidden = target["visibility"]["expected_hidden"]
            expected_files = []
            for old, new in zip(expected_tree["files"], actual_tree["files"]):
                expected_file = {**old, "hidden": new["hidden"] if hidden is None else True if hidden else old["hidden"]}
                if hidden is None or expected_file["hidden"] != old["hidden"]:
                    expected_file["hidden_for_user"] = new["hidden_for_user"]
                expected_files.append(expected_file)
            if (len(actual_tree["files"]) != len(expected_tree["files"]) or actual_tree["files"] != expected_files or
                    len(actual_tree["folders"]) != len(expected_tree["folders"])):
                raise ValueError("The complete reviewed active group subtree changed during the operation")
            old_prefix = target["source"]["folder"]["full_name"]
            for old, new in zip(expected_tree["folders"], actual_tree["folders"], strict=True):
                expected = dict(old)
                expected["hidden"] = new["hidden"] if hidden is None else True if hidden else old["hidden"]
                if hidden is None or expected["hidden"] != old["hidden"]:
                    expected["hidden_for_user"] = new["hidden_for_user"]
                if old["id"] == object_id:
                    expected.update(name=name, parent_folder_id=parent)
                suffix = old["full_name"][len(old_prefix):]
                if old["full_name"] != old_prefix and not old["full_name"].startswith(old_prefix + "/"):
                    raise ValueError("Canvas did not provide consistent reviewed subtree paths")
                expected["full_name"] = current["full_name"] + suffix
                if _without(new, {"updated_at", "position"}) != _without(expected, {"updated_at", "position"}):
                    raise ValueError("The reviewed recursive group folder path changes were not confirmed")
        saved = {**target, "folder": current}
        if not create:
            saved["observed_subtree"] = {"folders": [{"id": row["id"], "hidden": row["hidden"],
                "hidden_for_user": row["hidden_for_user"]} for row in actual_tree["folders"]],
                "files": [{"id": row["id"], "hidden": row["hidden"], "hidden_for_user": row["hidden_for_user"]}
                          for row in actual_tree["files"]]}
    if group_access(canvas, action.group_id, _right(action)) != target["access"]:
        raise ValueError("Group access changed during independent readback")
    return _result(canvas, action, saved, no_op=False)


def _result(canvas: Canvas, action: GroupFileActionTypes, saved: dict, *, no_op: bool) -> dict:
    result = {"verified": True, "group_id": action.group_id, "user_id": saved["access"]["user_id"],
              "no_op": no_op, "audience": saved["audience"]}
    if hasattr(action, "file_id"):
        result.update(file_id=action.file_id, file=saved["file"],
            publication={**saved["publication"], "observed_locked": saved["file"]["locked"]},
            visibility={**saved["visibility"], "observed_hidden": saved["file"]["hidden"]},
            verification="metadata_only; remote file bytes were not downloaded",
            html_url=f"{canvas.config.base_url}/groups/{action.group_id}/files/{action.file_id}",
            read={"tool": "canvas_read_file", "file_id": action.file_id})
    else:
        folder = saved.get("folder") or saved["source"]["folder"]
        result.update(folder_id=folder["id"], folder=folder,
                      html_url=f"{canvas.config.base_url}/groups/{action.group_id}/files/folder/{folder['id']}")
        if "subtree" in saved:
            result["subtree"] = {"items": saved["subtree"]["items"], "coverage": saved["subtree"]["coverage"],
                                 "native_effect": saved["subtree"]["native_effect"]}
            result["visibility"] = {**saved["visibility"], "observed_hidden": folder["hidden"]}
            if "observed_subtree" in saved:
                result["observed_subtree_visibility"] = saved["observed_subtree"]
        if "native_default" in saved:
            result["native_default"] = {key: saved["native_default"][key] for key in ("root_was_absent", "default_was_absent")}
            result["native_default"]["observed_parent_folder_id"] = folder["parent_folder_id"]
    return result
