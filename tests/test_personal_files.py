"""Synthetic real-client file workflows; all HTTP is intercepted in process."""

import copy
import json
from contextlib import contextmanager
from typing import Annotated

import httpx
import pytest
from canvas_py import Canvas
from canvas_py._config import CanvasConfig
from canvas_py._errors import CanvasError
from pydantic import Field, TypeAdapter, ValidationError

from canvas_connector import personal_files as files
from canvas_connector.actions import ActionStore

ADAPTER = TypeAdapter(Annotated[files.PersonalFileActionTypes, Field(discriminator="kind")])
CREATE = {"kind": "personal_folder_create", "parent_folder_id": 1, "name": "New folder"}
FOLDER_RENAME = {"kind": "personal_folder_rename", "folder_id": 2, "name": "Renamed"}
FOLDER_MOVE = {"kind": "personal_folder_move", "folder_id": 2, "parent_folder_id": 3}
FOLDER_DELETE = {"kind": "personal_folder_delete", "folder_id": 3}
FILE_RENAME = {"kind": "personal_file_rename", "file_id": 10, "name": "Renamed.txt"}
FILE_MOVE = {"kind": "personal_file_move", "file_id": 10, "parent_folder_id": 3}
FILE_DELETE = {"kind": "personal_file_delete", "file_id": 10}
ACTIONS = [CREATE, FOLDER_RENAME, FOLDER_MOVE, FOLDER_DELETE, FILE_RENAME, FILE_MOVE, FILE_DELETE]


def folder(folder_id, parent_id, name):
    return {"id": folder_id, "parent_folder_id": parent_id, "name": name,
            "full_name": "my files" if parent_id is None else "my files/" + name,
            "context_type": "User", "context_id": 7, "for_submissions": False,
            "locked": False, "hidden": False, "locked_for_user": False,
            "hidden_for_user": False, "can_upload": True, "files_count": 0, "folders_count": 0}


class PersonalState:
    def __init__(self):
        self.actor = 7
        self.folders = {1: folder(1, None, "my files"), 2: folder(2, 1, "Notes"),
                        3: folder(3, 1, "Archive"), 4: folder(4, 2, "Child")}
        self.folders[1].update(folders_count=2)
        self.folders[2].update(files_count=1, folders_count=1)
        self.files = {10: {"id": 10, "folder_id": 2, "display_name": "Notes.txt", "filename": "Notes.txt",
                          "size": 17, "updated_at": "2026-01-01T00:00:00Z", "modified_at": "2026-01-01T00:00:00Z",
                          "locked": False, "hidden": False, "locked_for_user": False, "hidden_for_user": False}}
        self.requests = []
        self.writes = []
        self.ignore_write = False
        self.write_error = None
        self.readback_error = None
        self.after_write = None
        self.response_override = None

    def handler(self, request):
        # No returned URLs, public download URLs, arbitrary users, external
        # domains, course/group contexts, or masquerading may reach the client.
        assert request.url.host == "school.example"
        assert request.headers["authorization"] == "Bearer synthetic-token"
        assert not request.url.params
        path = request.url.path.removeprefix("/api/v1/")
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, path, body))
        if request.method == "GET":
            assert body is None
            if path == "users/self/profile":
                return httpx.Response(200, json={"id": self.actor})
            if self.writes and self.readback_error and path == self.readback_error[0]:
                return httpx.Response(self.readback_error[1], json={"message": "readback unavailable"})
            if path.startswith("users/self/folders/"):
                object_id = path.rsplit("/", 1)[1]
                value = self.folders.get(1 if object_id == "root" else int(object_id))
            elif path.startswith("users/self/files/"):
                value = self.files.get(int(path.rsplit("/", 1)[1]))
            else:
                raise AssertionError(path)
            return httpx.Response(404, json={"message": "not found"}) if value is None else httpx.Response(200, json=copy.deepcopy(value))
        self.writes.append((request.method, path, body))
        assert not body or not ({"force", "replace", "on_duplicate", "parent_folder_path", "as_user_id"} & body.keys())
        if self.write_error:
            return httpx.Response(self.write_error, json={"message": "Cannot modify submitted file"})
        if request.method == "POST":
            assert path == "users/self/folders"
            assert set(body) == {"name", "parent_folder_id"}
            value = folder(20, body["parent_folder_id"], body["name"])
            if not self.ignore_write:
                self.folders[20] = value
        else:
            prefix, raw_id = path.split("/")
            assert prefix in {"files", "folders"}
            object_id = int(raw_id)
            collection = self.files if prefix == "files" else self.folders
            value = copy.deepcopy(collection[object_id])
            if request.method == "PUT":
                assert len(body) == 1
                if "name" in body:
                    value["display_name" if prefix == "files" else "name"] = body["name"]
                else:
                    assert "parent_folder_id" in body
                    value["folder_id" if prefix == "files" else "parent_folder_id"] = body["parent_folder_id"]
                if not self.ignore_write:
                    collection[object_id] = value
            else:
                assert request.method == "DELETE" and body is None
                if not self.ignore_write:
                    del collection[object_id]
        if self.after_write:
            self.after_write(self)
        return httpx.Response(200, json=self.response_override if self.response_override is not None else value)


@pytest.fixture(autouse=True)
def forbid_real_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected real HTTP transport in personal-file tests")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)


@contextmanager
def client(state):
    with (
        httpx.Client(transport=httpx.MockTransport(state.handler)) as http,
        Canvas(CanvasConfig("https://school.example", "synthetic-token"), http_client=http) as canvas,
    ):
        yield canvas


@pytest.mark.parametrize("payload,method,path,body", [
    (CREATE, "POST", "users/self/folders", {"name": "New folder", "parent_folder_id": 1}),
    (FOLDER_RENAME, "PUT", "folders/2", {"name": "Renamed"}),
    (FOLDER_MOVE, "PUT", "folders/2", {"parent_folder_id": 3}),
    (FOLDER_DELETE, "DELETE", "folders/3", None),
    (FILE_RENAME, "PUT", "files/10", {"name": "Renamed.txt"}),
    (FILE_MOVE, "PUT", "files/10", {"parent_folder_id": 3}),
    (FILE_DELETE, "DELETE", "files/10", None),
])
def test_all_workflows_use_exact_fixed_endpoint_and_verified_get(payload, method, path, body):
    state = PersonalState()
    with client(state) as canvas:
        target = files.snapshot(canvas, payload)
        assert target["user_id"] == 7
        assert not state.writes
        result = files.perform(canvas, payload, target)
    assert result["verified"] is True
    assert state.writes == [(method, path, body)]
    assert state.requests[-1][0] == "GET"
    assert files.ROUTES[payload["kind"]] == "canvas_manage_personal_files"
    if "delete" in payload["kind"]:
        assert result["deleted"] is True
    elif "file_" in payload["kind"]:
        assert result["file"]["display_name"] == payload.get("name", "Notes.txt")
        assert result["file"]["folder_id"] == payload.get("parent_folder_id", 2)
    else:
        assert result["folder"]["name"] == payload.get("name", "Notes")
        assert result["folder"]["parent_folder_id"] == payload.get("parent_folder_id", 1)


@pytest.mark.parametrize("payload", ACTIONS)
def test_success_response_without_persisted_change_is_not_verified(payload):
    state = PersonalState()
    state.ignore_write = True
    with client(state) as canvas:
        target = files.snapshot(canvas, payload)
        with pytest.raises((ValueError, CanvasError)):
            files.perform(canvas, payload, target)
    assert len(state.writes) == 1


@pytest.mark.parametrize("payload", ACTIONS)
def test_changed_owner_before_dispatch_does_not_write(payload):
    state = PersonalState()
    with client(state) as canvas:
        target = files.snapshot(canvas, payload)
        state.actor = 8
        with pytest.raises(ValueError):
            files.perform(canvas, payload, target)
    assert not state.writes


@pytest.mark.parametrize("field,value", [
    ("context_type", "Course"), ("context_type", "Group"), ("context_type", "user"),
    ("context_id", 8), ("context_id", True), ("context_id", "7"),
    ("for_submissions", True), ("for_submissions", None),
    ("locked_for_user", True), ("locked_for_user", None),
    ("hidden_for_user", True), ("hidden_for_user", 0),
    ("name", "Submissions"), ("name", "conversation attachments"),
    ("name", "profile pictures"), ("unique_type", "media"), ("category", "icon_maker_icons"),
    ("id", 99), ("parent_folder_id", True), ("parent_folder_id", None),
])
def test_source_folder_and_ancestry_must_be_owned_accessible_and_ordinary(field, value):
    state = PersonalState()
    state.folders[2][field] = value
    with client(state) as canvas, pytest.raises(ValueError):
        files.snapshot(canvas, FILE_RENAME)
    assert not state.writes


@pytest.mark.parametrize("field", ["for_submissions", "locked_for_user", "hidden_for_user", "context_type", "context_id", "parent_folder_id"])
def test_missing_authoritative_folder_fields_fail_closed(field):
    state = PersonalState()
    del state.folders[2][field]
    with client(state) as canvas, pytest.raises(ValueError):
        files.snapshot(canvas, FILE_RENAME)


@pytest.mark.parametrize("changes", [
    {"id": 11}, {"folder_id": 999}, {"folder_id": None}, {"context_type": "Course"},
    {"context_id": 9}, {"associated_with_submission": True}, {"associated_with_submission": None},
    {"locked_for_user": True}, {"hidden_for_user": True},
    {"permissions": {"update": False}}, {"permissions": {}},
    {"file_state": "deleted"}, {"category": "icon_maker_icons"},
])
def test_file_scope_and_restrictions_are_checked(changes):
    state = PersonalState()
    state.files[10].update(changes)
    with client(state) as canvas, pytest.raises((ValueError, CanvasError)):
        files.snapshot(canvas, FILE_RENAME)
    assert not state.writes


@pytest.mark.parametrize("payload,permission", [(FILE_RENAME, "update"), (FILE_DELETE, "delete"),
                                                (FOLDER_RENAME, "update"), (FOLDER_DELETE, "delete")])
def test_present_permissions_must_explicitly_allow_relevant_operation(payload, permission):
    state = PersonalState()
    value = state.files[10] if "file_id" in payload else state.folders[payload["folder_id"]]
    value["permissions"] = {permission: False, "read": True}
    with client(state) as canvas, pytest.raises(ValueError, match="permission"):
        files.snapshot(canvas, payload)
    assert not state.writes


def test_ordinary_api_shapes_do_not_need_invented_edit_permissions():
    state = PersonalState()
    assert "permissions" not in state.files[10]
    with client(state) as canvas:
        target = files.snapshot(canvas, FILE_RENAME)
        assert files.perform(canvas, FILE_RENAME, target)["verified"] is True


def test_global_hidden_and_locked_flags_do_not_override_owner_access():
    state = PersonalState()
    state.files[10].update(locked=True, hidden=True)
    state.folders[2].update(locked=True, hidden=True)
    with client(state) as canvas:
        target = files.snapshot(canvas, FILE_RENAME)
        assert files.perform(canvas, FILE_RENAME, target)["verified"] is True


@pytest.mark.parametrize("value", [False, None, 0, "true"])
def test_upload_destination_requires_actual_can_upload(value):
    state = PersonalState()
    state.folders[3]["can_upload"] = value
    with client(state) as canvas:
        with pytest.raises(ValueError, match="accept content"):
            files.validated_personal_folder(canvas, 3)
        with pytest.raises(ValueError, match="accept content"):
            files.snapshot(canvas, FILE_MOVE)
    assert not state.writes


def test_destination_snapshot_allows_root_and_binds_ancestry_without_transient_urls():
    state = PersonalState()
    with client(state) as canvas:
        root = files.validated_personal_folder(canvas, 1)
        assert root["folder_id"] == root["root_folder_id"] == 1
        assert root["ancestors"] == []
        nested = files.validated_personal_folder(canvas, 4)
        assert [f["id"] for f in nested["ancestors"]] == [1, 2]
        state.folders[4].update(files_url="https://evil.example", updated_at="later", files_count=7)
        assert files.validated_personal_folder(canvas, 4) == nested
        state.folders[2]["name"] = "New parent"
        assert files.validated_personal_folder(canvas, 4) != nested
    assert not state.writes


@pytest.mark.parametrize("payload", [FOLDER_RENAME, FOLDER_MOVE, FOLDER_DELETE])
@pytest.mark.parametrize("folder_id,name", [(1, "my files"), (2, "unfiled"), (2, "unfiled 2")])
def test_root_and_default_folder_cannot_be_mutated(payload, folder_id, name):
    state = PersonalState()
    state.folders[folder_id]["name"] = name
    with client(state) as canvas, pytest.raises(ValueError, match="cannot be changed"):
        files.snapshot(canvas, {**payload, "folder_id": folder_id})
    assert not state.writes


def test_ordinary_files_in_unfiled_can_be_managed():
    state = PersonalState()
    state.folders[2]["name"] = "unfiled"
    with client(state) as canvas:
        assert files.perform(canvas, FILE_RENAME, files.snapshot(canvas, FILE_RENAME))["verified"]


@pytest.mark.parametrize("parent", [2, 4])
def test_move_cannot_create_ancestry_cycle(parent):
    state = PersonalState()
    with client(state) as canvas, pytest.raises(ValueError, match="descendants"):
        files.snapshot(canvas, {**FOLDER_MOVE, "parent_folder_id": parent})
    assert not state.writes


def test_preexisting_ancestry_cycle_and_depth_bound():
    state = PersonalState()
    state.folders[2]["parent_folder_id"] = 4
    with client(state) as canvas, pytest.raises(ValueError, match="cycle"):
        files.validated_personal_folder(canvas, 2)
    state = PersonalState()
    for folder_id in range(5, 40):
        state.folders[folder_id] = folder(folder_id, folder_id - 1, f"Nested {folder_id}")
    with client(state) as canvas, pytest.raises(ValueError, match="32-folder"):
        files.validated_personal_folder(canvas, 39)


@pytest.mark.parametrize("key,value", [("files_count", 1), ("folders_count", 1),
                                      ("files_count", False), ("folders_count", None), ("files_count", "0")])
def test_only_confirmed_empty_folders_can_be_deleted(key, value):
    state = PersonalState()
    state.folders[3][key] = value
    with client(state) as canvas, pytest.raises(ValueError, match="empty"):
        files.snapshot(canvas, FOLDER_DELETE)
    assert not state.writes


@pytest.mark.parametrize("location,change", [
    ("file", {"display_name": "Changed.txt"}), ("file", {"folder_id": 3}),
    ("file", {"updated_at": "later"}), ("file", {"locked_for_user": True}),
    ("folder", {"parent_folder_id": 3}), ("folder", {"for_submissions": True}),
    ("folder", {"name": "Different"}),
])
def test_stale_moved_locked_or_submitted_source_never_writes(location, change):
    state = PersonalState()
    with client(state) as canvas:
        target = files.snapshot(canvas, FILE_RENAME)
        (state.files[10] if location == "file" else state.folders[2]).update(change)
        with pytest.raises(ValueError):
            files.perform(canvas, FILE_RENAME, target)
    assert not state.writes


@pytest.mark.parametrize("payload", [CREATE, FILE_MOVE, FOLDER_MOVE])
def test_stale_destination_permission_never_writes(payload):
    state = PersonalState()
    with client(state) as canvas:
        target = files.snapshot(canvas, payload)
        state.folders[payload["parent_folder_id"]]["can_upload"] = False
        with pytest.raises(ValueError):
            files.perform(canvas, payload, target)
    assert not state.writes


def test_folder_becoming_nonempty_after_preparation_never_writes():
    state = PersonalState()
    with client(state) as canvas:
        target = files.snapshot(canvas, FOLDER_DELETE)
        state.folders[3]["files_count"] = 1
        with pytest.raises(ValueError):
            files.perform(canvas, FOLDER_DELETE, target)
    assert not state.writes


@pytest.mark.parametrize("payload,path", [(FILE_DELETE, "users/self/files/10"), (FOLDER_DELETE, "users/self/folders/3")])
@pytest.mark.parametrize("status", [401, 403])
def test_delete_readback_permission_loss_is_not_absence(payload, path, status):
    state = PersonalState()
    state.readback_error = (path, status)
    with client(state) as canvas:
        target = files.snapshot(canvas, payload)
        with pytest.raises(CanvasError) as exc:
            files.perform(canvas, payload, target)
    assert exc.value.status == status
    assert len(state.writes) == 1


@pytest.mark.parametrize("payload", [FILE_DELETE, FILE_RENAME, FILE_MOVE])
def test_server_submission_restriction_is_propagated_without_fallback(payload):
    state = PersonalState()
    state.write_error = 403
    with client(state) as canvas:
        target = files.snapshot(canvas, payload)
        with pytest.raises(CanvasError) as exc:
            files.perform(canvas, payload, target)
    assert exc.value.status == 403
    assert len(state.writes) == 1
    assert state.files[10]["display_name"] == "Notes.txt"
    assert state.files[10]["folder_id"] == 2


@pytest.mark.parametrize("payload", ACTIONS)
def test_wrong_mutation_response_identity_is_not_verified(payload):
    state = PersonalState()
    state.response_override = {"id": 99}
    with client(state) as canvas:
        target = files.snapshot(canvas, payload)
        with pytest.raises((ValueError, CanvasError)):
            files.perform(canvas, payload, target)
    assert len(state.writes) == 1


@pytest.mark.parametrize("payload", [FILE_RENAME, FILE_MOVE])
@pytest.mark.parametrize("change", [{"id": 99}, {"folder_id": 1}, {"display_name": "Unexpected.txt"}, {"locked_for_user": True}])
def test_wrong_file_readback_is_not_verified(payload, change):
    state = PersonalState()
    state.after_write = lambda s: s.files[10].update(change)
    with client(state) as canvas:
        target = files.snapshot(canvas, payload)
        with pytest.raises(ValueError):
            files.perform(canvas, payload, target)
    assert len(state.writes) == 1


@pytest.mark.parametrize("payload", [CREATE, FOLDER_RENAME, FOLDER_MOVE])
@pytest.mark.parametrize("change", [{"context_id": 9}, {"parent_folder_id": 4}, {"name": "Unexpected"}])
def test_wrong_folder_readback_is_not_verified(payload, change):
    state = PersonalState()
    state.after_write = lambda s: s.folders[20 if payload is CREATE else 2].update(change)
    with client(state) as canvas:
        target = files.snapshot(canvas, payload)
        with pytest.raises(ValueError):
            files.perform(canvas, payload, target)
    assert len(state.writes) == 1


@pytest.mark.parametrize("payload", [FILE_DELETE, FOLDER_DELETE])
def test_deletion_does_not_succeed_if_parent_or_current_user_changes(payload):
    state = PersonalState()
    state.after_write = lambda s: setattr(s, "actor", 9)
    with client(state) as canvas:
        target = files.snapshot(canvas, payload)
        with pytest.raises(ValueError, match="Current user"):
            files.perform(canvas, payload, target)
    assert len(state.writes) == 1


@pytest.mark.parametrize("payload", ACTIONS)
@pytest.mark.parametrize("extra", [{"user_id": 8}, {"course_id": 1}, {"group_id": 2},
                                   {"url": "https://evil.example"}, {"force": True},
                                   {"replace": True}, {"on_duplicate": "overwrite"},
                                   {"parent_folder_path": "nested/path"}])
def test_models_reject_unbounded_targets_and_unsafe_options(payload, extra):
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({**payload, **extra})


@pytest.mark.parametrize("payload,field", [(CREATE, "parent_folder_id"), (FOLDER_MOVE, "folder_id"), (FILE_MOVE, "file_id")])
@pytest.mark.parametrize("value", [0, -1, True, "2", 2.0, 2**63])
def test_ids_are_strict_positive_and_bounded(payload, field, value):
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({**payload, field: value})


@pytest.mark.parametrize("payload", [CREATE, FOLDER_RENAME, FILE_RENAME])
@pytest.mark.parametrize("name", ["", " ", ".", "..", "a/b", "a\\b", "a\x00b", "a\nb", "a\rb", "a\tb", "a\x7fb", " x", "x ", "x" * 256])
def test_names_are_plain_and_bounded(payload, name):
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({**payload, "name": name})


def test_unicode_names_are_preserved_exactly():
    payload = {**FILE_RENAME, "name": "Révision 日本語.txt"}
    state = PersonalState()
    with client(state) as canvas:
        result = files.perform(canvas, payload, files.snapshot(canvas, payload))
    assert result["file"]["display_name"] == payload["name"]


@pytest.mark.parametrize("payload", ACTIONS)
def test_prepared_action_store_routes_and_executes_personal_operation_once(payload):
    state, store = PersonalState(), ActionStore()
    with client(state) as canvas:
        prepared = store.prepare(canvas, "alice", payload)
        assert prepared["execute"]["tool"] == "canvas_manage_personal_files"
        assert not state.writes
        receipt = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
        assert receipt["state"] == "succeeded", receipt
        assert receipt["result"]["verified"] is True
        assert store.execute(canvas, "alice", prepared["action_id"], lambda: None)["state"] == "succeeded"
    assert len(state.writes) == 1


@pytest.mark.parametrize("payload", ACTIONS)
def test_prepared_action_store_rejects_changed_ancestry_before_write(payload):
    state, store = PersonalState(), ActionStore()
    with client(state) as canvas:
        prepared = store.prepare(canvas, "alice", payload)
        state.folders[1]["name"] = "Changed root"
        receipt = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
    assert receipt["state"] == "rejected"
    assert receipt["result"]["upstream_write_attempted"] is False
    assert not state.writes


@pytest.mark.parametrize("payload,path", [(FILE_DELETE, "users/self/files/10"), (FOLDER_DELETE, "users/self/folders/3")])
def test_prepared_delete_with_denied_readback_stays_uncertain_and_cannot_replay(payload, path):
    state, store = PersonalState(), ActionStore()
    state.readback_error = (path, 403)
    with client(state) as canvas:
        prepared = store.prepare(canvas, "alice", payload)
        receipt = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
        assert receipt["state"] == "uncertain"
        assert store.execute(canvas, "alice", prepared["action_id"], lambda: None)["state"] == "uncertain"
        assert store.prepare(canvas, "alice", payload)["state"] == "uncertain"
    assert len(state.writes) == 1


@pytest.mark.parametrize("payload", ACTIONS)
def test_personal_mutation_explicitly_disables_retries_and_redirects(payload):
    request = files._Mutation(ADAPTER.validate_python(payload))
    assert request.allow_retries is False
    assert request.allow_redirects is False


@pytest.mark.parametrize("payload", ACTIONS)
@pytest.mark.parametrize("failure", [429, 403, "network", 302, 307, 308])
def test_unconfirmed_http_mutation_is_single_attempt_and_terminal(payload, failure):
    state, store = PersonalState(), ActionStore()
    dispatched = []

    def handler(request):
        dispatched.append((request.method, str(request.url)))
        # Record a followed target independently, including a redirect that
        # changes POST to GET. It must never be reached, even on the same origin.
        if request.url.path == "/api/v1/redirect-target":
            return httpx.Response(200, json={"id": 20})
        response = state.handler(request)
        if request.method == "GET":
            return response
        # Simulate the response being lost or replaced after the server applied
        # the mutation. An uncertain result is not evidence of a rollback.
        if failure == "network":
            raise httpx.ReadError("synthetic connection lost after apply", request=request)
        if failure in {302, 307, 308}:
            return httpx.Response(failure, headers={"Location": "/api/v1/redirect-target"})
        return httpx.Response(failure, json={"message": "Rate Limit Exceeded"}, headers={"Retry-After": "0"})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        Canvas(CanvasConfig("https://school.example", "synthetic-token"), http_client=http) as canvas,
    ):
        prepared = store.prepare(canvas, "alice", payload)
        assert not state.writes
        receipt = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
        assert receipt["state"] == "uncertain", receipt
        assert receipt["result"]["upstream_write_attempted"] is True
        requests_after_attempt = list(dispatched)
        assert store.execute(canvas, "alice", prepared["action_id"], lambda: None)["state"] == "uncertain"
        assert store.prepare(canvas, "alice", payload)["state"] == "uncertain"
        assert dispatched == requests_after_attempt
    assert len(state.writes) == 1
    assert not any("redirect-target" in url for _, url in dispatched)
