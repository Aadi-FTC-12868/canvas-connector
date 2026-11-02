"""Actual multipart/Canvas HTTP contract with synthetic conversation storage."""
import base64
import copy
import hashlib
import json
import socket
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser

import httpx
import pytest
from canvas_py import Canvas
from canvas_py._config import CanvasConfig
from canvas_py._errors import CanvasError
from pydantic import ValidationError

from canvas_connector import inbox_attachments as attachments

BASE = "https://school.example"
RAW = b"Reviewed inbox attachment\x00\r\n"
P = {"kind": "conversation_attachment_upload", "filename": "note.txt", "content_type": "text/plain",
     "content_base64": base64.b64encode(RAW).decode()}


def folder(folder_id, parent, name):
    return {"id": folder_id, "name": name, "parent_folder_id": parent, "context_type": "User",
            "context_id": 7, "can_upload": True, "locked_for_user": False, "hidden_for_user": False,
            "for_submissions": False}


class State:
    def __init__(self, existing=True, root=True):
        self.actor = 7
        self.folders = {1: folder(1, None, "my files")} if root else {}
        if existing:
            self.folders[3] = folder(3, 1, "conversation attachments")
        self.calls, self.targets, self.transfers = [], [], []
        self.file = None
        self.downloaded = 0
        self.raw = RAW
        self.list_override = None
        self.list_link = None
        self.fail = None
        self.storage_fail = None
        self.after_upload = None
        self.after_download = None
        self.name = "note.txt"
        self.stored_name = "note.txt"
        self.confirm = False

    def api(self, request):
        assert request.url.host == "school.example"
        assert request.headers.get("authorization") == "Bearer synthetic-token"
        path, method = request.url.path, request.method
        self.calls.append((method, path, dict(request.url.params)))
        if self.fail and self.fail[:2] == (method, path):
            return httpx.Response(self.fail[2], json={"message": "synthetic denied"},
                headers={"Retry-After": "0", "Location": BASE + "/api/v1/unexpected"})
        if path == "/api/v1/users/self/profile":
            assert method == "GET"
            return httpx.Response(200, json={"id": self.actor})
        if path == "/api/v1/users/self/folders":
            assert method == "GET" and request.url.params.get("per_page") == "100"
            rows = self.list_override if self.list_override is not None else list(self.folders.values())
            page = int(request.url.params.get("page", "1"))
            headers = {"Link": self.list_link} if self.list_link else {}
            return httpx.Response(200, json=rows[(page-1)*100:page*100], headers=headers)
        if path.startswith("/api/v1/users/self/folders/"):
            assert method == "GET" and not request.url.params
            data = self.folders.get(int(path.rsplit("/", 1)[1]))
            return httpx.Response(200, json=data) if data else httpx.Response(404, json={"message": "absent"})
        if path == "/api/v1/users/self/files":
            assert method == "POST"
            body = json.loads(request.content)
            self.targets.append(body)
            assert body["name"] == "note.txt" and body["size"] == len(RAW)
            assert body["content_type"] == "text/plain" and body["on_duplicate"] == "rename"
            if "parent_folder_path" in body:
                assert body["parent_folder_path"] == "conversation attachments"
                self.folders.setdefault(1, folder(1, None, "my files"))
                self.folders[3] = folder(3, 1, "conversation attachments")
            else:
                assert body["parent_folder_id"] == 3
            return httpx.Response(200, json={"upload_url": BASE + "/storage/upload", "upload_params": {"key": "synthetic"}})
        if path == "/api/v1/users/self/files/90" or path == "/api/v1/files/90/create_success":
            assert method == "GET"
            return httpx.Response(200, json=self.file)
        if path == "/files/90/download":
            assert method == "GET"
            self.downloaded += 1
            result = httpx.Response(200, content=self.raw)
            if self.after_download:
                self.after_download(self)
            return result
        raise AssertionError(f"Unexpected API operation: {method} {path}")

    def storage(self, request):
        assert request.url == BASE + "/storage/upload" and request.method == "POST"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        mime = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content)
        parts = list(mime.iter_parts())
        assert parts[-1].get_param("name", header="content-disposition") == "file"
        assert parts[-1].get_filename() == "note.txt" and parts[-1].get_payload(decode=True) == RAW
        self.transfers.append(request.content)
        if self.storage_fail:
            return httpx.Response(self.storage_fail, json={"message": "synthetic storage failure"}, headers={"Retry-After": "0"})
        self.file = {"id": 90, "folder_id": 3, "display_name": self.name, "filename": self.stored_name,
            "size": len(RAW), "url": BASE + "/files/90/download?verifier=synthetic-secret",
            "locked_for_user": False, "hidden_for_user": False, "context_type": "User", "context_id": 7}
        if self.after_upload:
            self.after_upload(self)
        if self.confirm:
            return httpx.Response(302, headers={"Location": BASE + "/api/v1/files/90/create_success"})
        return httpx.Response(201, json=copy.deepcopy(self.file))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Only synthetic transports are allowed")
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", denied)


@contextmanager
def client(state):
    with httpx.Client(transport=httpx.MockTransport(state.api)) as http, Canvas(CanvasConfig(BASE, "synthetic-token"), http_client=http) as canvas:
        canvas._http._anonymous_client_factory = lambda **kwargs: httpx.Client(transport=httpx.MockTransport(state.storage))
        yield canvas


@pytest.mark.parametrize("existing,root", [(True, True), (False, True), (False, False)])
@pytest.mark.parametrize("confirm", [False, True])
def test_native_folder_upload_is_separate_from_sending(existing, root, confirm):
    state = State(existing, root)
    state.confirm = confirm
    with client(state) as canvas:
        target = attachments.snapshot(canvas, P)
        assert target["creates_folder"] is (not existing)
        assert target["creates_root"] is (not root)
        assert not state.targets and not state.transfers
        result = attachments.perform(canvas, P, target)
    assert result["verified"] and result["message_sent"] is False and result["submitted"] is False
    assert result["folder_id"] == 3 and result["actor_id"] == 7
    assert result["sha256"] == hashlib.sha256(RAW).hexdigest() and result["bytes"] == len(RAW)
    assert len(state.targets) == len(state.transfers) == state.downloaded == 1
    assert "synthetic-secret" not in json.dumps(result) and "content_base64" not in json.dumps(target)
    assert all("conversations" not in path for _, path, _ in state.calls)


@pytest.mark.parametrize("name", ["note-1.txt", "note-12345678-1234-1234-1234-123456789abc.txt"])
def test_native_collision_and_storage_filename_are_preserved(name):
    state = State()
    state.name, state.stored_name = name, "encoded%20name.txt"
    with client(state) as canvas:
        result = attachments.perform(canvas, P, attachments.snapshot(canvas, P))
    assert result["filename"] == name and result["stored_filename"] == "encoded%20name.txt"
    assert result["requested_filename"] == "note.txt" and result["display_name_changed"]


def test_matching_folder_can_be_nested_because_canvas_selects_by_name():
    state = State()
    state.folders[2] = folder(2, 1, "Notes")
    state.folders[3]["parent_folder_id"] = 2
    with client(state) as canvas:
        result = attachments.perform(canvas, P, attachments.snapshot(canvas, P))
    assert result["verified"] and state.targets[0]["parent_folder_id"] == 3


@pytest.mark.parametrize("change", [
    lambda s: s.folders.update({4: folder(4, 1, "conversation attachments")}),
    lambda s: s.folders[3].update(context_id=8),
    lambda s: s.folders[3].update(context_type="Course"),
    lambda s: s.folders[3].update(parent_folder_id=3),
    lambda s: s.folders[3].update(parent_folder_id=99),
    lambda s: s.folders[3].update(can_upload=False),
    lambda s: s.folders[3].update(locked_for_user=True),
    lambda s: s.folders[3].pop("hidden_for_user"),
    lambda s: s.folders.update({4: folder(4, None, "my files")}),
    lambda s: s.folders[1].update(name="other root"),
])
def test_ambiguous_ownership_ancestry_or_access_refuses_before_upload(change):
    state = State()
    change(state)
    with client(state) as canvas, pytest.raises(ValueError):
        attachments.snapshot(canvas, P)
    assert not state.targets and not state.transfers


@pytest.mark.parametrize("query", ["page=3&per_page=100", "page=2&per_page=10", "per_page=100", "page=2&per_page=100&search_term=notes"])
def test_incomplete_folder_pagination_cannot_prove_unique_destination(query):
    state = State()
    state.list_override = [folder(i, None if i == 1 else 1, "my files" if i == 1 else f"Folder {i}") for i in range(1, 101)]
    state.list_link = f'<{BASE}/api/v1/users/self/folders?{query}>; rel="next"'
    with client(state) as canvas, pytest.raises(ValueError):
        attachments.snapshot(canvas, P)
    assert sum(path == "/api/v1/users/self/folders" for _, path, _ in state.calls) == 1
    assert not state.targets


@pytest.mark.parametrize("status", [302, 307, 429, 500])
@pytest.mark.parametrize("stage", ["target", "confirm"])
def test_upload_target_and_finalize_never_retry_or_follow_redirect(status, stage):
    state = State()
    state.confirm = stage == "confirm"
    path = "/api/v1/users/self/files" if stage == "target" else "/api/v1/files/90/create_success"
    state.fail = ("POST" if stage == "target" else "GET", path, status)
    with client(state) as canvas, pytest.raises(CanvasError):
        attachments.perform(canvas, P, attachments.snapshot(canvas, P))
    assert sum(p == path for _, p, _ in state.calls) == 1
    assert len(state.transfers) == (stage == "confirm")


@pytest.mark.parametrize("change", [
    lambda s: s.file.update(folder_id=4),
    lambda s: s.file.update(context_id=8),
    lambda s: s.file.update(context_type="Course"),
    lambda s: s.file.update(hidden_for_user=True),
    lambda s: s.file.update(size=1),
    lambda s: s.file.update(display_name="unreviewed.txt"),
    lambda s: s.folders.update({4: folder(4, 1, "conversation attachments")}),
    lambda s: setattr(s, "raw", b"changed"),
    lambda s: setattr(s, "actor", 8),
])
def test_post_upload_changes_never_produce_verified_receipt(change):
    state = State()
    state.after_upload = change
    with client(state) as canvas, pytest.raises((ValueError, CanvasError)):
        attachments.perform(canvas, P, attachments.snapshot(canvas, P))
    assert len(state.transfers) == 1


def test_metadata_race_after_download_is_uncertain():
    state = State()
    state.after_download = lambda s: s.file.update(display_name="renamed.txt")
    with client(state) as canvas, pytest.raises(ValueError):
        attachments.perform(canvas, P, attachments.snapshot(canvas, P))
    assert state.downloaded == len(state.transfers) == 1


@pytest.mark.parametrize("field,value", [("filename", "../a.txt"), ("filename", " a.txt"),
    ("filename", "a\x7f.txt"), ("content_type", "text/plain;x=1"), ("content_base64", "Zg==\n"),
    ("content_base64", "Zh=="), ("content_base64", ""), ("actor_id", 7)])
def test_untrusted_upload_parameters_are_rejected(field, value):
    with pytest.raises((ValueError, ValidationError)):
        attachments.ConversationAttachmentUpload.model_validate({**P, field: value})


@pytest.mark.parametrize("field,value", [("actor_id", 8), ("folder_id", 4), ("bytes", True),
    ("sha256", "wrong"), ("destination", "assignment"), ("id", True), ("filename", "../file")])
def test_reused_receipt_must_match_exact_owner_folder_and_bytes(field, value):
    state = State()
    with client(state) as canvas:
        receipt = attachments.perform(canvas, P, attachments.snapshot(canvas, P))
        file = {key: receipt[key] for key in ("id", "actor_id", "folder_id", "filename", "stored_filename", "bytes", "sha256", "destination")}
        assert attachments.verify_attachments(canvas, [file]) == [file]
        file[field] = value
        with pytest.raises(ValueError):
            attachments.verify_attachments(canvas, [file])


def test_sending_existing_upload_does_not_require_new_upload_permission():
    state = State()
    with client(state) as canvas:
        receipt = attachments.perform(canvas, P, attachments.snapshot(canvas, P))
        file = {key: receipt[key] for key in ("id", "actor_id", "folder_id", "filename", "stored_filename", "bytes", "sha256", "destination")}
        state.folders[3]["can_upload"] = False
        assert attachments.verify_attachments(canvas, [file]) == [file]
