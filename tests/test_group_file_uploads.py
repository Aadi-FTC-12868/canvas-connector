"""Native shared-group multipart uploads using only synthetic HTTP transports."""
import asyncio
import base64
import copy
import hashlib
import json
import socket
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
from unittest.mock import Mock

import httpx
import pytest
from canvas_py import Canvas
from canvas_py._config import CanvasConfig
from canvas_py._errors import CanvasError
from cryptography.fernet import Fernet
from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from canvas_connector import group_file_uploads as uploads
from canvas_connector.actions import ACTION_ADAPTER, ActionStore
from canvas_connector.storage import Store
from canvas_connector.tools import register_tools

BASE = "https://school.example"
RAW = b"Reviewed shared-group file\x00\r\n"
P = {"kind": "group_file_upload", "group_id": 9, "filename": "note.txt", "content_type": "text/plain",
     "content_base64": base64.b64encode(RAW).decode()}
STAMP = "2026-01-01T00:00:00Z"


def folder(folder_id, parent, name, **extra):
    return {"id": folder_id, "name": name, "full_name": name if parent is None else "files/" + name,
            "parent_folder_id": parent, "context_type": "Group", "context_id": 9, "can_upload": True,
            "for_submissions": False, "locked": False, "hidden": False, "locked_for_user": False,
            "hidden_for_user": False, "created_at": STAMP, "updated_at": STAMP, **extra}


class State:
    def __init__(self, *, root=True, default=True, account=False, required=False):
        self.actor = 7
        self.group = {"id": 9, "name": "Synthetic shared group", "context_type": "Account" if account else "Course",
            "account_id" if account else "course_id": 33 if account else 11,
            "is_public": False, "workflow_state": "available", "non_collaborative": False, "join_level": "invitation_only"}
        self.member = {"id": 17, "user_id": 7, "group_id": 9, "workflow_state": "accepted"}
        self.rights = {"participate": True, "read_files": True, "manage_files_add": True}
        self.folders = {1: folder(1, None, "files")} if root else {}
        if default:
            self.folders[3] = folder(3, 1, "unfiled")
        self.settings = {"usage_rights_required": required}
        self.observed_locked = required
        self.calls, self.targets, self.transfers = [], [], []
        self.file, self.ack = None, None
        self.downloaded = 0
        self.raw = RAW
        self.name, self.stored_name = "note.txt", "note.txt"
        self.fail, self.storage_fail = None, None
        self.after_upload, self.after_download = None, None
        self.on_read = None
        self.metadata_reads = 0
        self.confirm = False
        self.list_override, self.list_link = None, None
        self.actual_folder = None

    def api(self, request):
        assert request.url.host == "school.example"
        assert request.headers.get("authorization") == "Bearer synthetic-token"
        path, method = request.url.path, request.method
        self.calls.append((method, path, list(request.url.params.multi_items())))
        if self.fail and self.fail[:2] == (method, path):
            return httpx.Response(self.fail[2], json={"message": "private upstream detail"},
                headers={"Retry-After": "0", "Location": BASE + "/api/v1/unexpected"})
        if path == "/api/v1/users/self/profile":
            assert method == "GET"
            return httpx.Response(200, json={"id": self.actor})
        if path == "/api/v1/groups/9":
            assert method == "GET" and not request.url.params
            return httpx.Response(200, json=self.group)
        if path == "/api/v1/groups/9/users/self":
            assert method == "GET"
            return httpx.Response(200, json=self.member)
        if path == "/api/v1/groups/9/permissions":
            assert method == "GET" and set(request.url.params.get_list("permissions[]")) == set(self.rights)
            return httpx.Response(200, json=self.rights)
        if path == "/api/v1/courses/11/settings":
            assert method == "GET"
            return httpx.Response(200, json=self.settings)
        if path == "/api/v1/groups/9/folders":
            assert method == "GET" and request.url.params.get("per_page") == "100"
            rows = self.list_override if self.list_override is not None else list(self.folders.values())
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(200, json=rows[(page-1)*100:page*100],
                headers={"Link": self.list_link} if self.list_link else {})
        if path.startswith("/api/v1/groups/9/folders/"):
            assert method == "GET" and not request.url.params
            assert not path.endswith("/root"), "A passive root GET would create missing storage"
            row = self.folders.get(int(path.rsplit("/", 1)[1]))
            return httpx.Response(200, json=row) if row else httpx.Response(404, json={"message": "missing"})
        if path == "/api/v1/groups/9/files":
            assert method == "POST"
            body = json.loads(request.content)
            self.targets.append(body)
            assert body == {"name": "note.txt", "size": len(RAW), "content_type": "text/plain", "on_duplicate": "rename",
                            **({"parent_folder_id": body["parent_folder_id"]} if "parent_folder_id" in body else {})}
            if "parent_folder_id" not in body:
                self.folders.setdefault(1, folder(1, None, "files"))
                self.folders.setdefault(3, folder(3, 1, "unfiled", hidden=self.folders[1]["hidden"]))
            self.actual_folder = body.get("parent_folder_id", 3)
            # Native save may touch parent counters/timestamps; this is not an ancestry change.
            self.folders[self.actual_folder]["updated_at"] = "2026-01-02T00:00:00Z"
            return httpx.Response(200, json={"upload_url": BASE + "/storage/upload", "upload_params": {"key": "synthetic-object"}})
        if path == "/api/v1/files/90/create_success":
            assert method == "GET" and not request.url.params
            return httpx.Response(200, json=self.ack)
        if path == "/api/v1/groups/9/files/90":
            assert method == "GET" and request.url.params.get_list("include[]") == ["usage_rights", "user"]
            self.metadata_reads += 1
            if self.on_read:
                self.on_read(self)
            return httpx.Response(200, json=self.file)
        if path == "/files/90/download":
            assert method == "GET"
            self.downloaded += 1
            response = httpx.Response(200, content=self.raw)
            if self.after_download:
                self.after_download(self)
            return response
        raise AssertionError(f"Unexpected API operation: {method} {path}")

    def storage(self, request):
        assert request.url == BASE + "/storage/upload" and request.method == "POST"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        mime = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content)
        parts = list(mime.iter_parts())
        assert parts[-1].get_param("name", header="content-disposition") == "file"
        assert parts[-1].get_filename() == "note.txt" and parts[-1].get_payload(decode=True) == RAW
        assert parts[-1].get_content_type() == "text/plain"
        assert parts[0].get_payload(decode=True) == b"synthetic-object"
        self.transfers.append(request.content)
        if self.storage_fail:
            return httpx.Response(self.storage_fail, json={"message": "private storage detail"}, headers={"Retry-After": "0"})
        # Native File JSON need not contain context_type/id; those are proven by scoped reads and ancestry.
        self.file = {"id": 90, "folder_id": self.actual_folder, "display_name": self.name, "filename": self.stored_name,
            "size": len(RAW), "content-type": "text/plain", "url": BASE + "/files/90/download?verifier=PRIVATE_SENTINEL",
            "locked": self.observed_locked, "hidden": self.folders[self.actual_folder]["hidden"], "locked_for_user": False, "hidden_for_user": False,
            "created_at": STAMP, "updated_at": STAMP, "modified_at": STAMP, "usage_rights": None,
            "user": {"id": 7, "display_name": "Synthetic actor", "avatar_url": "https://cdn.example/PRIVATE_SENTINEL"}}
        self.ack = {key: value for key, value in self.file.items() if key not in {"usage_rights", "user"}}
        if self.after_upload:
            self.after_upload(self)
        if self.confirm:
            return httpx.Response(302, headers={"Location": BASE + "/api/v1/files/90/create_success"})
        return httpx.Response(201, json=copy.deepcopy(self.ack))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Only synthetic transports are allowed")
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", denied)


@contextmanager
def client(state, *, token="synthetic-token"):
    with httpx.Client(transport=httpx.MockTransport(state.api), cookies={"canvas_session": "synthetic-cookie"}) as http, Canvas(CanvasConfig(BASE, token), http_client=http) as canvas:
        canvas._http._anonymous_client_factory = lambda **kwargs: httpx.Client(transport=httpx.MockTransport(state.storage))
        yield canvas


@pytest.mark.parametrize("parent", [None, 1, 4])
@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("confirm", [False, True])
def test_exact_native_group_upload_wire_and_readback(parent, required, confirm):
    state = State(required=required)
    state.folders[4] = folder(4, 1, "Notes")
    state.confirm = confirm
    p = {**P, "parent_folder_id": parent}
    with client(state) as canvas:
        target = uploads.snapshot(canvas, p)
        assert not state.targets and not state.transfers
        assert target["publication"]["expected_locked"] is required
        result = uploads.perform(canvas, p, target)
    assert result["verified"] and result["group_id"] == 9 and result["user_id"] == 7
    assert result["folder_id"] == (parent or 3) and result["root_folder_id"] == 1
    assert result["sha256"] == hashlib.sha256(RAW).hexdigest() and result["bytes"] == len(RAW)
    assert result["publication"]["observed_locked"] is required and result["publication"]["prediction_verified"] is True
    assert result["submitted"] is result["overwritten"] is result["notification_delivery_verified"] is False
    assert len(state.targets) == len(state.transfers) == state.downloaded == 1 and state.metadata_reads == 2
    assert "parent_folder_id" in state.targets[0] if parent else "parent_folder_id" not in state.targets[0]
    assert "PRIVATE_SENTINEL" not in json.dumps(result) and "content_base64" not in json.dumps(target)
    assert not any(path.endswith("/root") or "/submissions/" in path for _, path, _ in state.calls)


@pytest.mark.parametrize("root,default", [(False, False), (True, False), (True, True)])
def test_native_default_initialization_only_with_authorized_upload(root, default):
    state = State(root=root, default=default)
    original = copy.deepcopy(state.folders)
    with client(state) as canvas:
        target = uploads.snapshot(canvas, P)
        assert state.folders == original and not state.targets
        assert target["destination"]["root_was_absent"] is (not root)
        assert target["destination"]["default_was_absent"] is (not default)
        result = uploads.perform(canvas, P, target)
    assert result["root_was_absent_before_upload"] is (not root)
    assert result["default_was_absent_before_upload"] is (not default)
    assert result["native_destination_observed_after_upload"] and result["folder_id"] == 3
    assert "created_root" not in result and "created_folder" not in result


@pytest.mark.parametrize("account,status", [(True, None), (False, 403), (False, 404), (False, None)])
@pytest.mark.parametrize("locked", [True, False])
def test_unknown_policy_is_explicitly_reviewed_then_actual_lock_verified(account, status, locked):
    state = State(account=account)
    state.group["is_public"] = True
    state.observed_locked = locked
    if status:
        state.fail = ("GET", "/api/v1/courses/11/settings", status)
    elif not account:
        state.settings = {}
    with client(state) as canvas:
        target = uploads.snapshot(canvas, P)
        assert target["publication"]["expected_locked"] is None
        assert "whether the new file is locked or unlocked" in target["publication"]["effect"]
        assert target["audience"]["is_public"] is True
        result = uploads.perform(canvas, P, target)
    observed = result["publication"]
    assert observed["expected_locked"] is None and observed["observed_locked"] is locked
    assert observed["actual_access_verified"] and observed["prediction_verified"] is False
    if account:
        assert not any("/settings" in path for _, path, _ in state.calls)


@pytest.mark.parametrize("value", ["false", 0, None, []])
def test_malformed_policy_is_not_treated_as_unknown(value):
    state = State()
    state.settings["usage_rights_required"] = value
    with client(state) as canvas, pytest.raises(ValueError):
        uploads.snapshot(canvas, P)
    assert not state.targets


def test_policy_401_is_not_treated_as_unknown():
    state = State()
    state.fail = ("GET", "/api/v1/courses/11/settings", 401)
    with client(state) as canvas, pytest.raises(CanvasError):
        uploads.snapshot(canvas, P)
    assert not state.targets


@pytest.mark.parametrize("name", ["note-1.txt", "note-12345678-1234-1234-1234-123456789abc.txt"])
def test_collision_rename_is_distinguished_from_requested_name(name):
    state = State()
    state.name, state.stored_name = name, "encoded%20name.txt"
    with client(state) as canvas:
        result = uploads.perform(canvas, P, uploads.snapshot(canvas, P))
    assert result["filename"] == name and result["stored_filename"] == "encoded%20name.txt"
    assert result["requested_filename"] == "note.txt" and result["display_name_changed"]


@pytest.mark.parametrize("change", [
    lambda s: s.member.update(user_id=8), lambda s: s.member.update(group_id=10),
    lambda s: s.member.update(workflow_state="invited"), lambda s: s.group.update(id=10),
    lambda s: s.group.update(non_collaborative=True), lambda s: s.group.pop("is_public"),
    lambda s: s.group.update(is_public="false"), lambda s: s.rights.update(participate=False),
    lambda s: s.rights.update(read_files=False), lambda s: s.rights.update(manage_files_add=False),
    lambda s: s.folders[3].update(context_id=10), lambda s: s.folders[3].update(context_type="Course"),
    lambda s: s.folders[3].update(for_submissions=True), lambda s: s.folders[3].update(can_upload=False),
    lambda s: s.folders.update({5: folder(5, 1, "unfiled 2")}),
    lambda s: s.folders.update({5: folder(5, None, "files")}),
])
def test_inexact_access_or_destination_refuses_before_upload(change):
    state = State()
    change(state)
    with client(state) as canvas, pytest.raises((ValueError, CanvasError)):
        uploads.snapshot(canvas, P)
    assert not state.targets and not state.transfers


@pytest.mark.parametrize("parent", [3, 99])
def test_explicit_parent_requires_acyclic_complete_ancestry(parent):
    state = State()
    state.folders[3]["parent_folder_id"] = parent
    with client(state) as canvas, pytest.raises((ValueError, CanvasError)):
        uploads.snapshot(canvas, {**P, "parent_folder_id": 3})
    assert not state.targets


def test_explicit_parent_can_resolve_ambiguous_native_default():
    state = State()
    state.folders[5] = folder(5, 1, "unfiled 2")
    with client(state) as canvas:
        result = uploads.perform(canvas, {**P, "parent_folder_id": 5}, uploads.snapshot(canvas, {**P, "parent_folder_id": 5}))
    assert result["verified"] and result["folder_id"] == 5


@pytest.mark.parametrize("field,value", [("filename", "../a.txt"), ("filename", " a.txt"), ("filename", "a\x7f.txt"),
    ("filename", "a\\b.txt"), ("content_type", "text/plain;x=1"), ("content_base64", "Zg==\n"),
    ("content_base64", "Zh=="), ("content_base64", ""), ("group_id", True), ("group_id", "9"),
    ("group_id", 0), ("parent_folder_id", 0), ("parent_folder_id", True), ("parent_folder_id", "3"),
    ("actor_id", 7), ("submit_assignment", True), ("on_duplicate", "overwrite"), ("parent_folder_path", "folder"),
    ("url", "https://example.org/a"), ("file_path", "/tmp/example")])
def test_strict_immutable_upload_model(field, value):
    with pytest.raises((ValueError, ValidationError)):
        ACTION_ADAPTER.validate_python({**P, field: value})


def test_exact_payload_size_bound_and_frozen_model():
    for size in (1, uploads.MAX_BYTES):
        action = uploads.GroupFileUpload.model_validate({**P, "content_base64": base64.b64encode(b"x" * size).decode()})
        with pytest.raises(ValidationError):
            action.filename = "changed.txt"
    with pytest.raises(ValidationError):
        uploads.GroupFileUpload.model_validate({**P, "content_base64": base64.b64encode(b"x" * (uploads.MAX_BYTES + 1)).decode()})


@pytest.mark.parametrize("change", [
    lambda s: s.file.update(folder_id=1), lambda s: s.file.update(id=91),
    lambda s: s.file.update(context_id=10), lambda s: s.file.update(context_type="User"),
    lambda s: s.file.update(user={"id": 8}), lambda s: s.file.pop("user"),
    lambda s: s.file.update(user={"id": True}), lambda s: s.file.update(locked=True),
    lambda s: s.file.pop("locked"), lambda s: s.file.pop("usage_rights"),
    lambda s: s.file.update(usage_rights={"license": "private"}), lambda s: s.file.update(size=1),
    lambda s: s.file.update(display_name="other.txt"), lambda s: s.file.update(filename="different.txt"),
    lambda s: s.file.pop("hidden_for_user"), lambda s: setattr(s, "raw", b"changed"),
    lambda s: s.member.update(workflow_state="requested"), lambda s: s.group.update(is_public=True),
    lambda s: s.folders[3].update(name="renamed"), lambda s: s.settings.update(usage_rights_required=True),
])
def test_exact_saved_file_context_actor_bytes_and_state_are_independently_verified(change):
    state = State()
    state.after_upload = change
    with client(state) as canvas, pytest.raises((ValueError, CanvasError)):
        uploads.perform(canvas, P, uploads.snapshot(canvas, P))
    assert len(state.targets) == len(state.transfers) == 1


@pytest.mark.parametrize("change", [lambda s: s.file.update(modified_at="2026-01-03T00:00:00Z"),
    lambda s: s.file.update(hidden=True), lambda s: s.file.update(user={"id": 8}),
    lambda s: s.folders[3].update(name="renamed"), lambda s: s.rights.update(manage_files_add=False)])
def test_byte_verification_is_bracketed_by_fresh_metadata_and_access(change):
    state = State()
    state.after_download = change
    with client(state) as canvas, pytest.raises(ValueError):
        uploads.perform(canvas, P, uploads.snapshot(canvas, P))
    assert state.downloaded == len(state.transfers) == 1


@pytest.mark.parametrize("status", [302, 307, 429, 500])
@pytest.mark.parametrize("stage", ["target", "confirm"])
def test_upload_mutations_and_finalize_are_never_replayed_or_redirected(status, stage):
    state = State()
    state.confirm = stage == "confirm"
    path = "/api/v1/groups/9/files" if stage == "target" else "/api/v1/files/90/create_success"
    state.fail = ("POST" if stage == "target" else "GET", path, status)
    with client(state) as canvas, pytest.raises(CanvasError):
        uploads.perform(canvas, P, uploads.snapshot(canvas, P))
    assert sum(p == path for _, p, _ in state.calls) == 1
    assert len(state.transfers) == (stage == "confirm")


@pytest.mark.parametrize("status", [302, 307, 429, 500])
def test_storage_failure_never_reposts_bytes(status):
    state = State()
    state.storage_fail = status
    with client(state) as canvas, pytest.raises(CanvasError):
        uploads.perform(canvas, P, uploads.snapshot(canvas, P))
    assert len(state.targets) == len(state.transfers) == 1 and state.downloaded == 0


@pytest.mark.parametrize("query", ["page=3&per_page=100", "page=2&per_page=10", "per_page=100", "page=2&per_page=100&search_term=notes"])
def test_incomplete_catalog_cannot_prove_native_default(query):
    state = State()
    state.list_override = [folder(i, None if i == 1 else 1, "files" if i == 1 else f"Folder {i}") for i in range(1, 101)]
    state.list_link = f'<{BASE}/api/v1/groups/9/folders?{query}>; rel="next"'
    with client(state) as canvas, pytest.raises(ValueError):
        uploads.snapshot(canvas, P)
    assert sum(path == "/api/v1/groups/9/folders" for _, path, _ in state.calls) == 1 and not state.targets


def tools(canvas, store=None, owner=lambda: "alice"):
    server, authorize = FastMCP("synthetic-group-upload"), Mock()
    register_tools(server, lambda: canvas, authorize, owner=owner, store=store)
    registered = {tool.name: tool for tool in server._tool_manager.list_tools()}
    calls = {name: (lambda *args, fn=tool.fn, **kwargs: asyncio.run(fn(*args, **kwargs))) for name, tool in registered.items()}
    return registered, calls, authorize


def test_mcp_routing_authorization_safe_review_and_exact_complete_receipt():
    state = State()
    with client(state) as canvas:
        registered, call, authorize = tools(canvas)
        tool = registered["canvas_upload_group_file"]
        assert tool.annotations.readOnlyHint is False and set(tool.parameters["properties"]) == {"action_id"}
        prepared = call["canvas_prepare_action"](P)["data"]
        action_id = prepared["action_id"]
        assert prepared["execute"] == {"tool": "canvas_upload_group_file", "action_id": action_id}
        assert "content_base64" not in json.dumps(prepared) and "PRIVATE_SENTINEL" not in json.dumps(prepared)
        authorize.assert_not_called()
        assert call["canvas_upload_file"](action_id)["status"] == "invalid_request"
        assert call["canvas_manage_group_files"](action_id)["status"] == "invalid_request"
        assert not state.targets
        result = call["canvas_upload_group_file"](action_id)
        assert result["status"] == "succeeded", result
        assert result["coverage"]["complete"] is result["coverage"]["verification"] is True
        assert "PRIVATE_SENTINEL" not in json.dumps(result)
        count = len(state.calls)
        assert call["canvas_upload_group_file"](action_id)["status"] == "succeeded" and len(state.calls) == count
    assert len(state.targets) == len(state.transfers) == 1


@pytest.mark.parametrize("change", [lambda s: s.group.update(is_public=True), lambda s: s.member.update(workflow_state="invited"),
    lambda s: s.folders[3].update(can_upload=False), lambda s: s.settings.update(usage_rights_required=True)])
def test_stale_prepared_target_is_rejected_before_any_upload(change):
    state, store = State(), ActionStore()
    with client(state) as canvas:
        action_id = store.prepare(canvas, "alice", P)["action_id"]
        change(state)
        result = store.execute(canvas, "alice", action_id, lambda: None)
        assert result["state"] == "rejected" and result["result"]["upstream_write_attempted"] is False
    assert not state.targets and not state.transfers


@pytest.mark.parametrize("uncertain", [False, True])
def test_persisted_prepared_and_terminal_receipts_never_repeat_upload(tmp_path, uncertain):
    state = State()
    database = Store(tmp_path / "group-upload.db", Fernet.generate_key())
    try:
        with client(state) as canvas:
            first = ActionStore(persistence=database)
            action_id = first.prepare(canvas, "alice", P)["action_id"]
            if uncertain:
                state.after_download = lambda s: s.file.update(user={"id": 8})
            resumed = ActionStore(persistence=database)
            assert resumed.prepare(canvas, "alice", P)["action_id"] == action_id
            result = resumed.execute(canvas, "alice", action_id, lambda: None)
            assert result["state"] == ("uncertain" if uncertain else "succeeded")
            count = len(state.calls)
            assert ActionStore(persistence=database).execute(canvas, "alice", action_id, lambda: None) == result
            assert len(state.calls) == count
            assert database.get("canvas_action_payload", action_id) is None
    finally:
        database.close()
    assert len(state.targets) == len(state.transfers) == 1


def test_receipt_is_bound_to_connector_owner_and_canvas_credentials():
    state, store = State(), ActionStore()
    with client(state) as canvas, client(state, token="another-synthetic-token") as other:
        action_id = store.prepare(canvas, "alice", P)["action_id"]
        with pytest.raises(ValueError):
            store.execute(canvas, "bob", action_id, lambda: None)
        with pytest.raises(ValueError):
            store.execute(other, "alice", action_id, lambda: None)
    assert not state.targets


def test_disabled_mcp_writes_leave_prepared_upload_unattempted():
    state = State()
    with client(state) as canvas:
        _, call, authorize = tools(canvas)
        action_id = call["canvas_prepare_action"](P)["data"]["action_id"]
        authorize.side_effect = ValueError("Write access is disabled")
        assert call["canvas_upload_group_file"](action_id)["status"] == "invalid_request"
        assert call["canvas_action_status"](action_id)["data"]["state"] == "prepared"
    assert not state.targets


def test_persisted_legacy_upload_payload_fingerprint_and_replay_are_unchanged(tmp_path):
    from test_upload_folders import PAYLOAD, UploadState, client as legacy_client
    state = UploadState()
    legacy = {key: value for key, value in PAYLOAD.items() if key != "parent_folder_id"}
    expected = {**legacy, "course_id": None, "assignment_id": None}
    database = Store(tmp_path / "legacy-upload.db", Fernet.generate_key())
    try:
        with legacy_client(state) as canvas:
            store = ActionStore(persistence=database)
            prepared = store.prepare(canvas, "alice", legacy)
            action_id = prepared["action_id"]
            encoded = json.dumps(expected, sort_keys=True, separators=(",", ":"))
            fingerprint = hashlib.sha256((store._identity(canvas, "alice") + "\0" + encoded).encode()).hexdigest()
            assert database.get("canvas_action_payload", action_id)["payload"] == encoded
            assert database.get("canvas_action", action_id)["fingerprint"] == fingerprint
            assert database.get("canvas_action", action_id)["snapshot"] == {}
            current = ActionStore(persistence=database)
            assert current.prepare(canvas, "alice", {**legacy, "parent_folder_id": None})["action_id"] == action_id
            _, call, _ = tools(canvas, current)
            assert call["canvas_upload_group_file"](action_id)["status"] == "invalid_request"
            receipt = current.execute(canvas, "alice", action_id, lambda: None)
            assert receipt["state"] == "succeeded" and "folder_id" not in receipt["result"]
            count = len(state.api_requests)
            assert ActionStore(persistence=database).execute(canvas, "alice", action_id, lambda: None) == receipt
            assert len(state.api_requests) == count
    finally:
        database.close()
    assert len(state.upload_targets) == len(state.transfers) == 1 and "parent_folder_id" not in state.upload_targets[0]


def test_mcp_uncertain_readback_is_terminal_and_does_not_leak_private_errors():
    state = State()
    state.on_read = lambda s: s.file.update(user={"id": 8})
    with client(state) as canvas:
        _, call, _ = tools(canvas)
        action_id = call["canvas_prepare_action"](P)["data"]["action_id"]
        result = call["canvas_upload_group_file"](action_id)
        assert result["status"] == "uncertain" and result["coverage"]["verification"] is False
        assert result["coverage"]["complete"] is False and "PRIVATE_SENTINEL" not in json.dumps(result)
        assert result["data"]["result"]["upstream_write_attempted"] is True
        count = len(state.calls)
        assert call["canvas_upload_group_file"](action_id)["status"] == "uncertain"
        assert len(state.calls) == count
    assert len(state.transfers) == len(state.targets) == 1


def test_hidden_root_default_ambiguity_requires_explicit_folder():
    state = State()
    # Native serializer inherits hidden from the root, hiding the child's own
    # workflow-state evidence. An explicit selected child remains verifiable.
    state.folders[1]["hidden"] = state.folders[3]["hidden"] = True
    with client(state) as canvas:
        with pytest.raises(ValueError, match="explicit|parent_folder_id"):
            uploads.snapshot(canvas, P)
        p = {**P, "parent_folder_id": 3}
        result = uploads.perform(canvas, p, uploads.snapshot(canvas, p))
    assert result["verified"] and result["folder_id"] == 3


def test_explicit_root_upload_does_not_invent_default_folder_existence():
    state = State(default=False)
    p = {**P, "parent_folder_id": 1}
    with client(state) as canvas:
        result = uploads.perform(canvas, p, uploads.snapshot(canvas, p))
    assert result["verified"] and result["folder_id"] == 1
    assert result["root_was_absent_before_upload"] is False
    assert result["default_was_absent_before_upload"] is None
    assert set(state.folders) == {1}
    assert not any(path == "/api/v1/groups/9/folders" for _, path, _ in state.calls)


def test_hidden_root_without_default_can_initialize_and_verify_actual_native_parent():
    state = State(default=False)
    state.folders[1]["hidden"] = True
    with client(state) as canvas:
        target = uploads.snapshot(canvas, P)
        assert target["destination"]["default_was_absent"] is True and not state.targets
        result = uploads.perform(canvas, P, target)
    assert result["verified"] and result["folder_id"] == 3
    assert result["publication"]["hidden"] is True
    assert result["default_was_absent_before_upload"] is True
    assert len(state.targets) == len(state.transfers) == 1


@pytest.mark.parametrize("folder_id,name,parent", [(1, "files", None), (4, "Notes", 1), (4, "unfiled", 3)])
def test_native_default_readback_cannot_accept_other_actual_parent(folder_id, name, parent):
    state = State()
    if folder_id != 1:
        state.folders[folder_id] = folder(folder_id, parent, name)
    def changed(s):
        s.file["folder_id"] = s.ack["folder_id"] = folder_id
    state.after_upload = changed
    with client(state) as canvas, pytest.raises(ValueError):
        uploads.perform(canvas, P, uploads.snapshot(canvas, P))
    assert len(state.transfers) == 1


def test_new_native_root_must_be_exact_canvas_files_root():
    state, store = State(root=False, default=False), ActionStore()
    def wrong_root(s):
        s.folders[1].update(name="Other", full_name="Other")
        s.folders[3]["full_name"] = "Other/unfiled"
    state.after_upload = wrong_root
    with client(state) as canvas:
        action_id = store.prepare(canvas, "alice", P)["action_id"]
        result = store.execute(canvas, "alice", action_id, lambda: None)
        assert result["state"] == "uncertain" and result["result"]["verified"] is False
        count = len(state.calls)
        assert store.execute(canvas, "alice", action_id, lambda: None) == result
        assert len(state.calls) == count
    assert len(state.targets) == len(state.transfers) == 1
