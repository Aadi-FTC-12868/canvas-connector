"""Shared group management through synthetic real HTTP; never live Canvas."""
import asyncio
import base64
import copy
import hashlib
import json
import socket
from contextlib import contextmanager
from unittest.mock import Mock

import httpx
import pytest
from canvas_py import Canvas
from canvas_py._config import CanvasConfig
from canvas_py._errors import CanvasError, CanvasRateLimitError
from cryptography.fernet import Fernet
from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from canvas_connector import group_files as files
from canvas_connector.actions import ActionStore
from canvas_connector.storage import Store
from canvas_connector.tools import register_tools

BASE = "https://school.example"
CREATE = {"kind": "group_folder_create", "group_id": 8, "parent_folder_id": 1, "name": "New folder"}
FOLDER_RENAME = {"kind": "group_folder_rename", "group_id": 8, "folder_id": 2, "name": "Renamed"}
FOLDER_MOVE = {"kind": "group_folder_move", "group_id": 8, "folder_id": 2, "parent_folder_id": 3}
FOLDER_DELETE = {"kind": "group_folder_delete", "group_id": 8, "folder_id": 3}
FILE_RENAME = {"kind": "group_file_rename", "group_id": 8, "file_id": 10, "name": "Renamed.txt"}
FILE_MOVE = {"kind": "group_file_move", "group_id": 8, "file_id": 10, "parent_folder_id": 3}
FILE_DELETE = {"kind": "group_file_delete", "group_id": 8, "file_id": 10}
ACTIONS = [CREATE, FOLDER_RENAME, FOLDER_MOVE, FOLDER_DELETE, FILE_RENAME, FILE_MOVE, FILE_DELETE]


def folder(ident, parent, name):
    return {"id": ident, "parent_folder_id": parent, "name": name, "context_type": "Group", "context_id": 8,
        "full_name": name, "for_submissions": False, "can_upload": True, "locked": False, "hidden": False,
        "locked_for_user": False, "hidden_for_user": False, "updated_at": "version-one", "position": ident}


class State:
    def __init__(self):
        self.actor = 7
        self.group = {"id": 8, "name": "Shared group", "context_type": "Course", "course_id": 9,
                      "non_collaborative": False, "is_public": False, "join_level": "invitation_only"}
        self.member = {"id": 50, "user_id": 7, "group_id": 8, "workflow_state": "accepted"}
        self.rights = {key: True for key in ("participate", "read_files", "manage_files_add", "manage_files_edit", "manage_files_delete")}
        self.settings = {"usage_rights_required": False}
        self.native_required = False
        self.folders = {1: folder(1, None, "files"), 2: folder(2, 1, "Notes"),
                        3: folder(3, 1, "Archive"), 4: folder(4, 2, "Child")}
        self.files = {10: {"id": 10, "folder_id": 2, "filename": "existing.txt", "display_name": "Existing.txt",
            "size": 17, "updated_at": "version-one", "modified_at": "content-version", "locked": False,
            "hidden": False, "locked_for_user": False, "hidden_for_user": False, "usage_rights": None,
            "user": {"id": 99}, "url": BASE + "/files/10/download?signature=synthetic-secret"}}
        self.own_hidden = set()
        self.requests, self.writes = [], []
        self.fail_write = None
        self.after_write = None
        self.read_errors = {}
        self.overrides = {}
        self.links = {}
        self.bookmarks = False
        self.ignore_write = False

    def folder_row(self, ident, seen=None):
        row = copy.deepcopy(self.folders[ident])
        seen = set() if seen is None else seen
        if ident in seen:
            return row
        seen.add(ident)
        parent = row["parent_folder_id"]
        parent_row = self.folder_row(parent, seen) if parent in self.folders else None
        row["full_name"] = parent_row["full_name"] + "/" + row["name"] if parent_row else row["name"]
        row["hidden"] = row["hidden"] or ("folder", ident) in self.own_hidden or bool(parent_row and parent_row["hidden"])
        row["folders_count"] = sum(r["parent_folder_id"] == ident for r in self.folders.values())
        row["files_count"] = sum(r["folder_id"] == ident for r in self.files.values())
        return row

    def file_row(self, ident):
        row = copy.deepcopy(self.files[ident])
        parent = self.folder_row(row["folder_id"])
        row["hidden"] = row["hidden"] or ("file", ident) in self.own_hidden or parent["hidden"]
        return row

    def response(self, request, value):
        path = request.url.path.removeprefix("/api/v1/")
        if path in self.overrides:
            value = self.overrides[path](value) if callable(self.overrides[path]) else self.overrides[path]
        if not isinstance(value, list):
            return httpx.Response(404, json={"message": "not found"}) if value is None else httpx.Response(200, json=value)
        token = request.url.params.get("page", "1")
        encoded = token.removeprefix("bookmark:")
        start = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))[0] if token.startswith("bookmark:") else (int(token) - 1) * 100
        headers = {}
        if path in self.links:
            headers["Link"] = self.links[path]
        elif start + 100 < len(value):
            next_page = ("bookmark:" + base64.urlsafe_b64encode(json.dumps([start + 100]).encode()).decode().rstrip("=")
                         if self.bookmarks else str(start // 100 + 2))
            headers["Link"] = f'<{request.url.copy_set_param("page", next_page)}>; rel="next"'
        return httpx.Response(200, json=value[start:start + 100], headers=headers)

    def handler(self, request):
        assert request.url.host == "school.example"
        assert request.headers["authorization"] == "Bearer synthetic-token"
        path = request.url.path.removeprefix("/api/v1/")
        assert not path.endswith("/root"), "Preparation must never initialize a root"
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, path, list(request.url.params.multi_items()), body))
        if request.method == "GET":
            if path in self.read_errors:
                return httpx.Response(self.read_errors[path], json={"message": "synthetic denied"})
            if path == "users/self/profile": value = {"id": self.actor}
            elif path == "groups/8": value = self.group
            elif path == "groups/8/users/self": value = self.member
            elif path == "groups/8/permissions":
                value = {key: self.rights[key] for key in request.url.params.get_list("permissions[]") if key in self.rights}
            elif path == "courses/9/settings": value = self.settings
            elif path == "groups/8/folders": value = [self.folder_row(i) for i in sorted(self.folders)]
            elif path.startswith("groups/8/folders/"):
                ident = int(path.rsplit("/", 1)[1]); value = self.folder_row(ident) if ident in self.folders else None
            elif path.startswith("groups/8/files/"):
                ident = int(path.rsplit("/", 1)[1]); value = self.file_row(ident) if ident in self.files else None
                if value and "user" not in request.url.params.get_list("include[]"): value.pop("user", None)
            elif path.startswith("folders/") and path.endswith("/folders"):
                ident = int(path.split("/")[1]); value = [self.folder_row(i) for i in sorted(self.folders)
                    if self.folders[i]["parent_folder_id"] == ident]
            elif path.startswith("folders/") and path.endswith("/files"):
                ident = int(path.split("/")[1]); value = [self.file_row(i) for i in sorted(self.files) if self.files[i]["folder_id"] == ident]
            else: raise AssertionError(path)
            return self.response(request, copy.deepcopy(value))
        assert not request.url.params
        assert not body or not ({"locked", "hidden", "force", "on_duplicate", "parent_folder_path", "source_folder_id"} & body.keys())
        self.writes.append((request.method, path, copy.deepcopy(body)))
        if path == "groups/8/folders":
            parent = body.get("parent_folder_id")
            if parent is None:
                if not self.folders: self.folders[1] = folder(1, None, "files")
                candidates = [f for f in self.folders.values() if f["parent_folder_id"] == 1 and f["name"] == "unfiled"]
                if not candidates:
                    self.folders[90] = folder(90, 1, "unfiled")
                    parent = 90
                else: parent = candidates[0]["id"]
            ident = 100
            if not self.ignore_write: self.folders[ident] = folder(ident, parent, body["name"])
            response = self.folder_row(ident) if ident in self.folders else {"id": ident}
        else:
            kind, ident = path.split("/"); ident = int(ident)
            if request.method == "DELETE":
                response = self.file_row(ident) if kind == "files" else self.folder_row(ident)
                if not self.ignore_write: (self.files if kind == "files" else self.folders).pop(ident)
            elif kind == "files":
                if not self.ignore_write:
                    row = self.files[ident]
                    if "name" in body: row["display_name"] = body["name"]
                    if "parent_folder_id" in body: row["folder_id"] = body["parent_folder_id"]
                    row["locked"] = self.native_required and row.get("usage_rights") is None
                    row["updated_at"] = "version-two"
                response = self.file_row(ident); response.pop("usage_rights", None)
            else:
                if not self.ignore_write:
                    self.folders[ident].update(body, updated_at="version-two", position=0)
                response = self.folder_row(ident)
        if self.after_write: self.after_write(self)
        if self.fail_write == "network": raise httpx.ReadError("synthetic lost response after apply", request=request)
        if self.fail_write in {302, 307, 308}:
            return httpx.Response(self.fail_write, headers={"Location": str(request.url)})
        if self.fail_write: return httpx.Response(self.fail_write, json={"message": "Rate Limit Exceeded"}, headers={"Retry-After": "0"})
        return httpx.Response(200, json=response)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError("No actual network is allowed")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


@contextmanager
def client(state, token="synthetic-token"):
    with (httpx.Client(transport=httpx.MockTransport(state.handler)) as http,
          Canvas(CanvasConfig(BASE, token), http_client=http) as canvas):
        yield canvas


@pytest.mark.parametrize("action", ACTIONS)
def test_real_shared_group_operation_exactly_once_and_no_download_urls(action):
    state = State()
    with client(state) as canvas:
        target = files.snapshot(canvas, action)
        result = files.perform(canvas, action, target)
    assert result["verified"] and result["group_id"] == 8 and result["user_id"] == 7
    assert len(state.writes) == 1
    assert "synthetic-secret" not in str(target) + str(result)
    assert not any("users/self/files" in path for _, path, _, _ in state.requests)
    if action in (FOLDER_RENAME, FOLDER_MOVE):
        assert target["subtree"]["items"] == 3
        assert result["subtree"]["coverage"] == "complete_active_api_visible_subtree"
        assert "deleted rows" in target["subtree"]["native_effect"]
    if action in (FILE_RENAME, FILE_MOVE):
        assert state.files[10]["user"]["id"] == 99  # Another member's upload; ordinary REST omits attribution.
        assert "user" not in target["file"]
        assert result["verification"].startswith("metadata_only")


@pytest.mark.parametrize("absent", ["root", "default", "neither"])
def test_default_create_passively_reviews_native_initialization(absent):
    state = State()
    if absent == "root": state.folders.clear(); state.files.clear()
    if absent == "neither": state.folders[90] = folder(90, 1, "unfiled")
    action = {"kind": "group_folder_create", "group_id": 8, "name": "First project"}
    with client(state) as canvas:
        target = files.snapshot(canvas, action)
        assert not state.writes
        result = files.perform(canvas, action, target)
    assert state.writes == [("POST", "groups/8/folders", {"name": "First project"})]
    assert result["native_default"] == {"root_was_absent": absent == "root", "default_was_absent": absent != "neither",
                                        "observed_parent_folder_id": 90}


def test_multiple_default_candidates_require_explicit_parent_only():
    state = State()
    state.folders[90] = folder(90, 1, "unfiled"); state.folders[91] = folder(91, 1, "unfiled 2")
    with client(state) as canvas:
        with pytest.raises(ValueError, match="explicit"):
            files.native_default_target(canvas, 8)
        assert files.perform(canvas, {**CREATE, "parent_folder_id": 91}, files.snapshot(canvas, {**CREATE, "parent_folder_id": 91}))["verified"]


def test_noncanonical_root_requires_explicit_parent_for_native_default():
    state = State(); state.folders[1]["name"] = "Legacy root"
    with client(state) as canvas:
        with pytest.raises(ValueError, match="explicit"):
            files.native_default_target(canvas, 8)
        assert files.perform(canvas, CREATE, files.snapshot(canvas, CREATE))["verified"]


def test_hidden_root_default_ambiguity_is_not_reported_absent():
    state = State(); state.own_hidden.add(("folder", 1)); state.folders[90] = folder(90, 1, "unfiled")
    with client(state) as canvas:
        with pytest.raises(ValueError, match="explicit"):
            files.native_default_target(canvas, 8)
        assert files.folder_target(canvas, 8, 90)["folder"]["hidden"] is True


def test_hidden_root_without_default_can_initialize_known_absent_default():
    state = State(); state.own_hidden.add(("folder", 1))
    action = {"kind": "group_folder_create", "group_id": 8, "name": "First project"}
    with client(state) as canvas:
        target = files.snapshot(canvas, action)
        assert target["native_default"]["default_was_absent"] is True
        result = files.perform(canvas, action, target)
        assert result["folder"]["hidden"] is True


@pytest.mark.parametrize("policy,rights,before,expected", [
    (True, None, False, True), (False, None, True, False),
    (True, {"license": "private"}, True, False), (None, {"license": "private"}, True, False),
    (None, None, False, None), (None, None, True, None),
])
def test_reviewed_known_and_unknown_publication_outcomes(policy, rights, before, expected):
    state = State()
    if policy is None: state.group.update(context_type="Account", account_id=5); state.group.pop("course_id")
    else: state.settings["usage_rights_required"] = policy
    state.native_required = policy is not False
    state.files[10].update(locked=before, usage_rights=rights)
    with client(state) as canvas:
        target = files.snapshot(canvas, FILE_RENAME)
        assert target["publication"]["expected_locked"] is expected
        assert target["publication"]["known"] is (expected is not None)
        result = files.perform(canvas, FILE_RENAME, target)
    assert result["publication"]["observed_locked"] is (state.native_required and rights is None)
    assert state.writes[0][2] == {"name": "Renamed.txt"}


def test_unknown_publication_in_public_group_has_explicit_reviewed_audience():
    state = State(); state.group.update(is_public=True, context_type="Account", account_id=5); state.group.pop("course_id")
    with client(state) as canvas:
        target = files.snapshot(canvas, FILE_RENAME)
        assert target["publication"]["known"] is False
        assert target["audience"]["is_public"] is True and "beyond group members" in target["audience"]["description"]
        result = files.perform(canvas, FILE_RENAME, target)
    assert result["audience"] == target["audience"]


@pytest.mark.parametrize("action", [FILE_MOVE, FOLDER_MOVE])
def test_move_into_hidden_destination_explicitly_reviews_inheritance(action):
    state = State(); state.own_hidden.add(("folder", 3))
    with client(state) as canvas:
        target = files.snapshot(canvas, action)
        assert target["visibility"]["expected_hidden"] is True
        assert files.perform(canvas, action, target)["visibility"]["observed_hidden"] is True


@pytest.mark.parametrize("own_hidden", [False, True])
@pytest.mark.parametrize("is_folder", [False, True])
def test_move_out_of_hidden_parent_reviews_unknown_and_verifies_observed(own_hidden, is_folder):
    state = State(); state.own_hidden.add(("folder", 2))
    action = {**FOLDER_MOVE, "folder_id": 4} if is_folder else FILE_MOVE
    if own_hidden: state.own_hidden.add(("folder", 4) if is_folder else ("file", 10))
    with client(state) as canvas:
        target = files.snapshot(canvas, action)
        assert target["visibility"]["known"] is False and target["visibility"]["expected_hidden"] is None
        result = files.perform(canvas, action, target)
    assert result["visibility"]["observed_hidden"] is own_hidden


@pytest.mark.parametrize("action", [{**FILE_RENAME, "name": "Existing.txt"}, {**FILE_MOVE, "parent_folder_id": 2},
    {**FOLDER_RENAME, "name": "Notes"}, {**FOLDER_MOVE, "parent_folder_id": 1}])
def test_noop_zero_write(action):
    state = State()
    with client(state) as canvas:
        assert files.perform(canvas, action, files.snapshot(canvas, action))["no_op"]
    assert not state.writes


@pytest.mark.parametrize("mutation", [
    lambda s: setattr(s, "actor", 8), lambda s: s.member.update(group_id=9),
    lambda s: s.member.update(workflow_state="invited"), lambda s: s.group.update(non_collaborative=True),
    lambda s: s.group.update(id=9), lambda s: s.group.update(workflow_state="deleted"),
    lambda s: s.group.update(is_public=None), lambda s: s.group.pop("name"),
    lambda s: s.group.update(context_type="Unknown"), lambda s: s.rights.update(participate=False),
    lambda s: s.rights.update(read_files=False), lambda s: s.rights.update(manage_files_edit=False),
    lambda s: s.rights.update(manage_files_edit="true"), lambda s: s.rights.pop("manage_files_edit"),
    lambda s: s.folders[2].update(context_id=9), lambda s: s.folders[2].update(context_type="User"),
    lambda s: s.folders[2].update(for_submissions=True), lambda s: s.folders[2].update(can_upload=False),
    lambda s: s.folders[2].update(parent_folder_id=4), lambda s: s.files[10].update(context_id=9),
    lambda s: s.files[10].update(context_type="User"), lambda s: s.files[10].update(associated_with_submission=True),
    lambda s: s.files[10].update(permissions={"update": False}), lambda s: s.files[10].update(locked_for_user="false"),
    lambda s: s.settings.update(usage_rights_required="true"), lambda s: s.files[10].update(usage_rights={}),
])
def test_native_access_and_identity_fail_closed_before_write(mutation):
    state = State(); mutation(state)
    with client(state) as canvas, pytest.raises((ValueError, CanvasError)):
        files.snapshot(canvas, FILE_RENAME)
    assert not state.writes


def test_metadata_permissions_do_not_invent_download_requirement():
    state = State(); state.files[10].update(locked=True, locked_for_user=True, hidden_for_user=True, url="")
    with client(state) as canvas:
        assert files.snapshot(canvas, FILE_DELETE)["file"]["locked_for_user"] is True
        assert files.perform(canvas, FILE_DELETE, files.snapshot(canvas, FILE_DELETE))["deleted"]


def test_root_is_a_protected_source():
    state = State(); state.folders[90] = folder(90, 1, "unfiled")
    with client(state) as canvas, pytest.raises(ValueError, match="root folder"):
        files.snapshot(canvas, {**FOLDER_RENAME, "folder_id": 1})


@pytest.mark.parametrize("action", [FOLDER_RENAME, FOLDER_MOVE, FOLDER_DELETE])
def test_native_default_candidate_is_ordinary_with_future_upload_effect(action):
    state = State(); state.folders[90] = folder(90, 1, "unfiled")
    action = {**action, "folder_id": 90}
    with client(state) as canvas:
        target = files.snapshot(canvas, action)
        assert "future uploads" in target["effect"]
        assert files.perform(canvas, action, target)["verified"]


def test_case_sensitive_native_names_remain_available():
    state = State(); state.folders[90] = folder(90, 1, "Unfiled")
    with client(state) as canvas:
        assert files.perform(canvas, {**FOLDER_RENAME, "folder_id": 90}, files.snapshot(canvas, {**FOLDER_RENAME, "folder_id": 90}))["verified"]
        assert files.snapshot(canvas, {**CREATE, "name": "archive"})


def test_delete_is_empty_only_and_counts_cannot_hide_contents():
    state = State()
    with client(state) as canvas:
        with pytest.raises(ValueError, match="empty"):
            files.snapshot(canvas, {**FOLDER_DELETE, "folder_id": 2})
        state.overrides["groups/8/folders/2"] = lambda row: {**row, "files_count": 0, "folders_count": 0}
        with pytest.raises(ValueError, match="counts"):
            files.snapshot(canvas, FOLDER_RENAME)
    assert not state.writes


@pytest.mark.parametrize("parent", [2, 4])
def test_move_cannot_reparent_inside_subtree(parent):
    state = State()
    with client(state) as canvas, pytest.raises(ValueError, match="descendants"):
        files.snapshot(canvas, {**FOLDER_MOVE, "parent_folder_id": parent})


@pytest.mark.parametrize("bookmark", [False, True])
def test_complete_native_pagination_and_subtree_bounds(bookmark):
    state = State(); state.bookmarks = bookmark
    for ident in range(100, 250): state.folders[ident] = folder(ident, 2, f"Child {ident}")
    with client(state) as canvas:
        target = files.snapshot(canvas, FOLDER_RENAME)
        assert target["subtree"]["items"] == 153
        assert files.perform(canvas, FOLDER_RENAME, target)["verified"]


@pytest.mark.parametrize("link", [
    '<https://school.example/api/v1/groups/8/folders/2?per_page=100&page=2>; rel="next"',
    '<https://evil.example/api/v1/groups/8/folders?per_page=100&page=2>; rel="next"',
    '<https://school.example/api/v1/groups/8/folders?per_page=99&page=2>; rel="next"',
    '<https://school.example/api/v1/groups/8/folders?per_page=100&page=3>; rel="next"',
    '<https://school.example/api/v1/groups/8/folders?per_page=100&page=first>; rel="next"',
    '<https://school.example/api/v1/groups/8/folders?per_page=100&page=bookmark:[100]>; rel="next"',
    '<https://school.example/api/v1/groups/8/folders?per_page=100&page=bookmark:W10=>; rel="next"',
    '<https://school.example/api/v1/groups/8/folders?per_page=100&page=2&search_term=hidden>; rel="next"',
    '<https://school.example/api/v1/groups/8/folders?per_page=100&page=2>; rel=next',
])
def test_bad_next_link_never_dispatches_second_request(link):
    state = State(); state.links["groups/8/folders"] = link
    with client(state) as canvas, pytest.raises((ValueError, CanvasError)):
        files.native_default_target(canvas, 8)
    assert len(state.requests) == 1 and not state.writes


@pytest.mark.parametrize("token", [[100], {"_binary": "opaque"}, "cursor", 3, None])
def test_native_bookmark_json_shapes_are_preserved(token):
    encoded = base64.urlsafe_b64encode(json.dumps(token).encode()).decode().rstrip("=")
    request = files._Request("groups/8/folders", query=[("per_page", 100)])
    request.validate_pagination_query([("per_page", "100"), ("page", "bookmark:" + encoded)], page_number=2)


def test_discovery_duplicates_and_overflow_are_not_complete():
    state = State(); state.overrides["groups/8/folders"] = lambda rows: rows + [rows[0]]
    with client(state) as canvas:
        with pytest.raises(ValueError, match="duplicate"): files.native_default_target(canvas, 8)
        state.overrides.clear()
        for ident in range(100, 1100): state.folders[ident] = folder(ident, 1, f"Folder {ident}")
        with pytest.raises(ValueError, match="bounded"): files.native_default_target(canvas, 8)
        assert files.folder_target(canvas, 8, 2)["folder_id"] == 2  # Unrelated folders do not bound explicit targets.


def test_complete_subtree_enforces_1000_total_items_including_root():
    state = State()
    for ident in range(100, 1098):
        state.files[ident] = {**state.files[10], "id": ident, "display_name": f"File {ident}"}
    with client(state) as canvas, pytest.raises(ValueError, match="bounded|1000"):
        files.snapshot(canvas, FOLDER_RENAME)
    assert not state.writes


def test_explicit_ancestry_and_descendant_depth_are_bounded():
    state = State()
    for ident in range(100, 133): state.folders[ident] = folder(ident, 4 if ident == 100 else ident - 1, f"Depth {ident}")
    with client(state) as canvas:
        with pytest.raises(ValueError, match="32"):
            files.folder_target(canvas, 8, 132)
        with pytest.raises(ValueError, match="32"):
            files.snapshot(canvas, FOLDER_RENAME)
    assert not state.writes


@pytest.mark.parametrize("path", ["users/self/profile", "groups/8", "groups/8/users/self", "groups/8/permissions"])
def test_preparation_reads_never_follow_redirects_to_side_effecting_get(path):
    state = State()
    def handler(request):
        if request.url.path.removeprefix("/api/v1/") == path:
            state.requests.append(("GET", path, [], None))
            return httpx.Response(302, headers={"Location": BASE + "/api/v1/groups/8/folders/root"})
        return state.handler(request)
    with (httpx.Client(transport=httpx.MockTransport(handler)) as http,
          Canvas(CanvasConfig(BASE, "synthetic-token"), http_client=http) as canvas, pytest.raises(CanvasError)):
        files.group_access(canvas, 8, "manage_files_edit")
    assert state.requests[-1][1] == path


def test_discovery_iterator_is_closed_after_bound_rejection():
    iterator = Mock()
    iterator.__iter__ = Mock(return_value=iter([{"id": 1}, {"id": 2}]))
    canvas = Mock(); canvas.paginate.return_value = iterator
    with pytest.raises(ValueError, match="bounded"):
        files._all(canvas, "folders/2/files", maximum=1)
    iterator.close.assert_called_once()


@pytest.mark.parametrize("mutation", [lambda s: s.member.update(workflow_state="deleted"),
    lambda s: s.files[10].update(display_name="Concurrent.txt"), lambda s: s.files[10].update(modified_at="changed"),
    lambda s: s.folders[4].update(name="Concurrent child"), lambda s: s.rights.update(manage_files_edit=False)])
def test_stale_subtree_or_member_is_rejected_before_dispatch(mutation):
    state, store = State(), ActionStore()
    with client(state) as canvas:
        prepared = store.prepare(canvas, "alice", FOLDER_RENAME)
        mutation(state)
        receipt = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
    assert receipt["state"] == "rejected" and not state.writes


@pytest.mark.parametrize("failure", [429, 403, "network", 302, 307, 308])
@pytest.mark.parametrize("action", ACTIONS)
def test_uncertain_one_write_never_replays_even_reprepare(action, failure):
    state, store = State(), ActionStore(); state.fail_write = failure
    with client(state) as canvas:
        prepared = store.prepare(canvas, "alice", action)
        result = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
        assert result["state"] == "uncertain"
        count = len(state.requests)
        assert store.execute(canvas, "alice", prepared["action_id"], lambda: None) == result
        assert store.prepare(canvas, "alice", action)["state"] == "uncertain"
        assert len(state.requests) == count
    assert len(state.writes) == 1


@pytest.mark.parametrize("mutation", [lambda s: s.files[10].update(size=20),
    lambda s: s.files[10].update(locked=True), lambda s: s.files[10].update(usage_rights={"license": "changed"}),
    lambda s: s.member.update(workflow_state="invited"), lambda s: s.files[10].update(folder_id=4),
    lambda s: s.files[10].update(modified_at="changed-content"), lambda s: s.folders[2].update(name="Changed destination")])
def test_failed_independent_file_readback_is_uncertain(mutation):
    state, store = State(), ActionStore(); state.after_write = mutation
    with client(state) as canvas:
        prepared = store.prepare(canvas, "alice", FILE_RENAME)
        assert store.execute(canvas, "alice", prepared["action_id"], lambda: None)["state"] == "uncertain"


def test_legacy_modified_timestamp_fallback_can_become_native_stored_timestamp():
    state = State(); state.files[10]["modified_at"] = state.files[10]["updated_at"]
    state.after_write = lambda s: s.files[10].update(modified_at=s.files[10]["updated_at"])
    with client(state) as canvas:
        assert files.perform(canvas, FILE_RENAME, files.snapshot(canvas, FILE_RENAME))["verified"]


@pytest.mark.parametrize("mutation", [lambda s: s.folders[4].update(parent_folder_id=3),
    lambda s: s.folders[4].update(locked=True), lambda s: s.files[10].update(size=18),
    lambda s: s.folders.update({99: folder(99, 2, "Concurrent child")}), lambda s: s.files.pop(10)])
def test_subtree_structure_or_independent_content_changes_are_uncertain(mutation):
    state, store = State(), ActionStore(); state.after_write = mutation
    with client(state) as canvas:
        prepared = store.prepare(canvas, "alice", FOLDER_RENAME)
        assert store.execute(canvas, "alice", prepared["action_id"], lambda: None)["state"] == "uncertain"


def test_unknown_visibility_still_requires_consistent_observed_inheritance():
    state, store = State(), ActionStore()
    state.own_hidden.update({("folder", 2), ("folder", 4)})
    state.folders[5] = folder(5, 4, "Grandchild")
    state.after_write = lambda s: s.overrides.update({"folders/4/folders": lambda rows: [{**r, "hidden": False} for r in rows]})
    action = {**FOLDER_MOVE, "folder_id": 4}
    with client(state) as canvas:
        prepared = store.prepare(canvas, "alice", action)
        assert prepared["review"]["target"]["visibility"]["known"] is False
        assert store.execute(canvas, "alice", prepared["action_id"], lambda: None)["state"] == "uncertain"


def test_default_initialization_before_failed_post_is_uncertain_not_repeated():
    state, store = State(), ActionStore(); state.folders.clear(); state.files.clear(); state.fail_write = "network"
    action = {"kind": "group_folder_create", "group_id": 8, "name": "New"}
    with client(state) as canvas:
        prepared = store.prepare(canvas, "alice", action)
        assert not state.folders
        receipt = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
        assert receipt["state"] == "uncertain" and {1, 90} <= state.folders.keys()
        assert store.prepare(canvas, "alice", action)["state"] == "uncertain"
        assert store.execute(canvas, "alice", prepared["action_id"], lambda: None) == receipt
    assert len(state.writes) == 1


@pytest.mark.parametrize("status", [401, 403, 404])
@pytest.mark.parametrize("action,path", [(FILE_DELETE, "groups/8/files/10"), (FOLDER_DELETE, "groups/8/folders/3")])
def test_deletion_only_exact_404_with_surviving_access_proves_absence(status, action, path):
    state, store = State(), ActionStore(); state.after_write = lambda s: s.read_errors.update({path: status})
    with client(state) as canvas:
        prepared = store.prepare(canvas, "alice", action)
        result = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
    assert result["state"] == ("succeeded" if status == 404 else "uncertain")


def test_unknown_publication_readback_requires_actual_boolean():
    state, store = State(), ActionStore()
    state.group.update(context_type="Account", account_id=5); state.group.pop("course_id")
    state.after_write = lambda s: s.files[10].update(locked=None)
    with client(state) as canvas:
        prepared = store.prepare(canvas, "alice", FILE_RENAME)
        assert prepared["review"]["target"]["publication"]["known"] is False
        assert store.execute(canvas, "alice", prepared["action_id"], lambda: None)["state"] == "uncertain"


@pytest.mark.parametrize("status", [403, 404])
def test_unreadable_course_policy_remains_explicit_unknown(status):
    state = State(); state.read_errors["courses/9/settings"] = status
    with client(state) as canvas:
        target = files.snapshot(canvas, FILE_RENAME)
        assert target["publication"]["policy"]["known"] is False
        assert target["publication"]["expected_locked"] is None
        assert files.perform(canvas, FILE_RENAME, target)["verified"]


def test_rate_limit_403_is_operational_failure_not_unknown_policy():
    state = State()
    def handler(request):
        if request.url.path.endswith("/settings"):
            return httpx.Response(403, json={"message": "Rate Limit Exceeded"}, headers={"Retry-After": "0"})
        return state.handler(request)
    with (httpx.Client(transport=httpx.MockTransport(handler)) as http,
          Canvas(CanvasConfig(BASE, "synthetic-token"), http_client=http) as canvas, pytest.raises(CanvasRateLimitError)):
        files.snapshot(canvas, FILE_RENAME)
    assert not state.writes


@pytest.mark.parametrize("changes", [{"name": "../file"}, {"name": " file"}, {"name": "x\n"}, {"name": ""},
    {"group_id": True}, {"parent_folder_id": "1"}, {"force": True}, {"locked": False}, {"user_id": 7}])
def test_strict_reviewed_input(changes):
    with pytest.raises(ValidationError): files._ADAPTER.validate_python({**CREATE, **changes})


def test_uncertain_receipt_persists_across_restart(tmp_path):
    state = State(); key = Fernet.generate_key(); path = tmp_path / "actions.db"
    with client(state) as canvas:
        db = Store(path, key)
        try:
            store = ActionStore(persistence=db); prepared = store.prepare(canvas, "alice", FILE_RENAME)
            state.fail_write = "network"
            receipt = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
        finally: db.close()
        db = Store(path, key)
        try:
            store = ActionStore(persistence=db); count = len(state.requests)
            assert store.status(canvas, "alice", prepared["action_id"])["state"] == "uncertain"
            assert store.execute(canvas, "alice", prepared["action_id"], lambda: None) == receipt
            assert store.prepare(canvas, "alice", FILE_RENAME)["action_id"] == prepared["action_id"]
            assert len(state.requests) == count
        finally: db.close()


def test_actual_mcp_review_executor_account_and_write_annotations():
    state = State()
    with client(state) as canvas:
        context, authorize = {"owner": "alice"}, Mock()
        server = FastMCP("group-files-test")
        register_tools(server, lambda: canvas, authorize, owner=lambda: context["owner"])
        def call(name, **args): return asyncio.run(server.call_tool(name, args))[1]
        prepared = call("canvas_prepare_action", action=FILE_RENAME)["data"]
        assert prepared["execute"]["tool"] == "canvas_manage_group_files"
        assert "synthetic-secret" not in str(prepared)
        assert call("canvas_manage_personal_files", action_id=prepared["action_id"])["status"] == "invalid_request"
        context["owner"] = "bob"; count = len(state.requests)
        assert call("canvas_manage_group_files", action_id=prepared["action_id"])["status"] == "invalid_request"
        assert len(state.requests) == count
        context["owner"] = "alice"
        receipt = call("canvas_manage_group_files", action_id=prepared["action_id"])["data"]
        assert receipt["state"] == "succeeded"
        assert call("canvas_manage_group_files", action_id=prepared["action_id"])["data"] == receipt
        assert len(state.writes) == 1
        tool = next(t for t in server._tool_manager.list_tools() if t.name == "canvas_manage_group_files")
        assert set(tool.parameters["properties"]) == {"action_id"}
        assert tool.annotations.readOnlyHint is False and tool.annotations.idempotentHint is False
        authorize.assert_called()


def test_existing_personal_payload_fingerprint_and_receipt_stay_unchanged(tmp_path):
    from test_personal_files import FILE_RENAME as legacy
    from test_personal_files import PersonalState
    state = PersonalState(); key = Fernet.generate_key(); path = tmp_path / "legacy.db"
    with client(state) as canvas:
        db = Store(path, key)
        try:
            store = ActionStore(persistence=db); prepared = store.prepare(canvas, "alice", legacy)
            encoded = json.dumps(legacy, sort_keys=True, separators=(",", ":"))
            assert db.get("canvas_action_payload", prepared["action_id"])["payload"] == encoded
            assert db.get("canvas_action", prepared["action_id"])["fingerprint"] == hashlib.sha256(
                (store._identity(canvas, "alice") + "\0" + encoded).encode()).hexdigest()
        finally: db.close()
        db = Store(path, key)
        try:
            store = ActionStore(persistence=db)
            assert store.prepare(canvas, "alice", legacy)["action_id"] == prepared["action_id"]
            receipt = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
            assert receipt["state"] == "succeeded"
            assert store.execute(canvas, "alice", prepared["action_id"], lambda: None) == receipt
        finally: db.close()
    assert len(state.writes) == 1
