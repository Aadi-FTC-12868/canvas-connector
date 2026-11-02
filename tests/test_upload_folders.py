"""Prepared folder-targeted uploads using only in-process HTTP transports."""

import base64
import hashlib
import json
import socket
from contextlib import contextmanager

import httpx
import pytest
from canvas_py import Canvas
from canvas_py._config import CanvasConfig
from pydantic import ValidationError

from canvas_connector.actions import ACTION_ADAPTER, ActionStore

BASE = "https://school.example"
CONTENT = b"Personal notes"
PAYLOAD = {"kind": "upload", "destination": "user", "filename": "notes.txt",
           "content_base64": base64.b64encode(CONTENT).decode(), "parent_folder_id": 3}


def folder(folder_id, parent_id, name):
    return {"id": folder_id, "parent_folder_id": parent_id, "name": name,
            "context_type": "User", "context_id": 7, "for_submissions": False,
            "locked_for_user": False, "hidden_for_user": False, "can_upload": True}


class UploadState:
    def __init__(self):
        self.actor = 7
        self.folders = {1: folder(1, None, "my files"), 3: folder(3, 1, "Notes"), 4: folder(4, 1, "Other")}
        self.api_requests = []
        self.upload_targets = []
        self.transfers = []
        self.downloads = []
        self.metadata_reads = 0
        self.metadata_changes = {}
        self.folder_changes = {}
        self.content = CONTENT

    def metadata(self, **changes):
        destination = self.upload_targets[-1].get("parent_folder_id", 1)
        return {"id": 55, "folder_id": destination, "filename": "notes.txt", "display_name": "notes.txt",
                "size": len(CONTENT), "content-type": "text/plain", "url": BASE + "/files/55/download", **changes}

    def api(self, request):
        assert request.url.host == "school.example"
        assert request.headers["authorization"] == "Bearer synthetic-token"
        assert not request.url.params
        path = request.url.path
        self.api_requests.append((request.method, path))
        if path == "/api/v1/users/self/profile":
            assert request.method == "GET"
            return httpx.Response(200, json={"id": self.actor})
        if path.startswith("/api/v1/users/self/folders/"):
            assert request.method == "GET"
            raw_id = path.rsplit("/", 1)[1]
            folder_id = 1 if raw_id == "root" else int(raw_id)
            data = self.folders.get(folder_id)
            if folder_id in self.folder_changes:
                return httpx.Response(self.folder_changes[folder_id], json={"message": "folder unavailable"})
            return httpx.Response(200, json=data) if data else httpx.Response(404, json={"message": "not found"})
        if path == "/api/v1/users/self/files":
            assert request.method == "POST"
            body = json.loads(request.content)
            assert body["on_duplicate"] == "rename"
            assert "parent_folder_path" not in body
            self.upload_targets.append(body)
            return httpx.Response(200, json={"upload_url": BASE + "/storage/mock-upload", "upload_params": {"key": "synthetic"}})
        if path == "/api/v1/files/55":
            assert request.method == "GET"
            self.metadata_reads += 1
            changes = self.metadata_changes.get(self.metadata_reads, {})
            return httpx.Response(200, json=self.metadata(**changes))
        if path == "/files/55/download":
            assert request.method == "GET"
            self.downloads.append(path)
            return httpx.Response(200, content=self.content)
        raise AssertionError(f"Unexpected Canvas request: {request.method} {request.url}")

    def storage(self, request):
        assert str(request.url) == BASE + "/storage/mock-upload"
        assert request.method == "POST"
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        assert b'filename="notes.txt"' in request.content and CONTENT in request.content
        self.transfers.append(request.content)
        return httpx.Response(201, json=self.metadata())


@pytest.fixture(autouse=True)
def forbid_external_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected external network access in upload-folder tests")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


@contextmanager
def client(state):
    with (
        httpx.Client(transport=httpx.MockTransport(state.api)) as http,
        Canvas(CanvasConfig(BASE, "synthetic-token"), http_client=http) as canvas,
    ):
        canvas._http._anonymous_client_factory = lambda **kwargs: httpx.Client(transport=httpx.MockTransport(state.storage))
        yield canvas


@pytest.mark.parametrize("folder_id", [1, 3])
def test_chosen_personal_folder_upload_verifies_transfer_bytes_and_saved_location(folder_id):
    state, store = UploadState(), ActionStore()
    with client(state) as canvas:
        prepared = store.prepare(canvas, "alice", {**PAYLOAD, "parent_folder_id": folder_id})
        assert prepared["review"]["parent_folder_id"] == folder_id
        assert prepared["review"]["target"]["folder_id"] == folder_id
        assert prepared["review"]["target"]["user_id"] == 7
        assert prepared["execute"]["tool"] == "canvas_upload_file"
        assert not state.upload_targets and not state.transfers
        receipt = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
        assert receipt["state"] == "succeeded", receipt
        assert receipt["result"] == {"id": 55, "filename": "notes.txt", "size": len(CONTENT),
                                      "verified": True, "submitted": False, "folder_id": folder_id}
        assert store.execute(canvas, "alice", prepared["action_id"], lambda: None) == receipt
    assert state.upload_targets == [{"name": "notes.txt", "size": len(CONTENT), "content_type": "text/plain",
                                     "on_duplicate": "rename", "parent_folder_id": folder_id}]
    assert len(state.transfers) == 1
    assert len(state.downloads) == 1
    assert state.metadata_reads == 3


@pytest.mark.parametrize("change", [
    {"name": "Different"}, {"parent_folder_id": 4}, {"context_id": 8}, {"context_type": "Course"},
    {"can_upload": False}, {"for_submissions": True}, {"locked_for_user": True}, {"hidden_for_user": True},
])
def test_stale_or_unwritable_folder_blocks_upload_dispatch(change):
    state, store = UploadState(), ActionStore()
    with client(state) as canvas:
        prepared = store.prepare(canvas, "alice", PAYLOAD)
        state.folders[3].update(change)
        receipt = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
    assert receipt["state"] == "rejected"
    assert receipt["result"]["upstream_write_attempted"] is False
    assert not state.upload_targets and not state.transfers


@pytest.mark.parametrize("status", [403, 404])
def test_unreadable_destination_is_not_treated_as_an_empty_root_upload(status):
    state, store = UploadState(), ActionStore()
    with client(state) as canvas:
        prepared = store.prepare(canvas, "alice", PAYLOAD)
        state.folder_changes[3] = status
        receipt = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
    assert receipt["state"] == "rejected"
    assert not state.upload_targets and not state.transfers


def test_current_user_change_blocks_upload_dispatch():
    state, store = UploadState(), ActionStore()
    with client(state) as canvas:
        prepared = store.prepare(canvas, "alice", PAYLOAD)
        state.actor = 9
        receipt = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
    assert receipt["state"] == "rejected"
    assert not state.upload_targets


@pytest.mark.parametrize("read_number,change", [(1, {"folder_id": 4}), (3, {"folder_id": 4}),
                                                (3, {"folder_id": None}), (3, {"id": 56})])
def test_wrong_upload_or_latest_folder_is_uncertain_and_never_replayed(read_number, change):
    state, store = UploadState(), ActionStore()
    state.metadata_changes[read_number] = change
    with client(state) as canvas:
        prepared = store.prepare(canvas, "alice", PAYLOAD)
        receipt = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
        assert receipt["state"] == "uncertain"
        assert receipt["result"]["upstream_write_attempted"] is True
        assert store.execute(canvas, "alice", prepared["action_id"], lambda: None)["state"] == "uncertain"
        assert store.prepare(canvas, "alice", PAYLOAD)["state"] == "uncertain"
    assert len(state.upload_targets) == len(state.transfers) == 1


def test_chosen_folder_does_not_skip_existing_byte_hash_verification():
    state, store = UploadState(), ActionStore()
    state.content = b"X" * len(CONTENT)
    with client(state) as canvas:
        prepared = store.prepare(canvas, "alice", PAYLOAD)
        receipt = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
    assert receipt["state"] == "uncertain"
    assert len(state.upload_targets) == len(state.transfers) == len(state.downloads) == 1


@pytest.mark.parametrize("destination", ["assignment", "submission_comment"])
def test_custom_folder_cannot_change_assignment_or_comment_upload_context(destination):
    with pytest.raises(ValidationError, match="only for personal"):
        ACTION_ADAPTER.validate_python({**PAYLOAD, "destination": destination, "course_id": 1, "assignment_id": 2})


@pytest.mark.parametrize("extras", [{"course_id": 1}, {"assignment_id": 2}, {"group_id": 1},
                                    {"user_id": 9}, {"parent_folder_path": "folder/path"}])
def test_custom_folder_has_no_alternative_context_or_path_selector(extras):
    with pytest.raises(ValidationError):
        ACTION_ADAPTER.validate_python({**PAYLOAD, **extras})


@pytest.mark.parametrize("value", [0, -1, True, "3", 3.0])
def test_custom_folder_id_is_a_strict_positive_integer(value):
    with pytest.raises(ValidationError):
        ACTION_ADAPTER.validate_python({**PAYLOAD, "parent_folder_id": value})


def test_optional_folder_preserves_legacy_payload_fingerprint_and_root_upload():
    state, store = UploadState(), ActionStore()
    legacy = {key: value for key, value in PAYLOAD.items() if key != "parent_folder_id"}
    expected_payload = {**legacy, "course_id": None, "assignment_id": None}
    with client(state) as canvas:
        prepared = store.prepare(canvas, "alice", legacy)
        record = store.records[prepared["action_id"]]
        encoded = json.dumps(expected_payload, sort_keys=True, separators=(",", ":"))
        assert record["payload"] == encoded
        assert record["fingerprint"] == hashlib.sha256((record["owner"] + "\0" + encoded).encode()).hexdigest()
        assert record["snapshot"] == {}
        assert "parent_folder_id" not in prepared["review"]
        assert store.prepare(canvas, "alice", {**legacy, "parent_folder_id": None})["action_id"] == prepared["action_id"]
        receipt = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
        assert receipt["state"] == "succeeded"
        assert "folder_id" not in receipt["result"]
    assert "parent_folder_id" not in state.upload_targets[0]
    assert len(state.upload_targets) == len(state.transfers) == 1
    assert not any("/folders/" in path for _, path in state.api_requests)


def test_different_chosen_folders_have_distinct_prepared_intents():
    state, store = UploadState(), ActionStore()
    with client(state) as canvas:
        first = store.prepare(canvas, "alice", PAYLOAD)
        other = store.prepare(canvas, "alice", {**PAYLOAD, "parent_folder_id": 4})
    assert first["action_id"] != other["action_id"]
    assert not state.upload_targets
