"""Preservation wire contract and receipts against synthetic real HTTP clients."""
from __future__ import annotations

import asyncio
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
from canvas_py._errors import CanvasError
from cryptography.fernet import Fernet
from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError
from test_discussion_attachments import BASE, RAW, DiscussionState
from test_discussion_attachments import payload as upload_payload

from canvas_connector import discussion_edits as edits
from canvas_connector.actions import ActionStore
from canvas_connector.storage import Store
from canvas_connector.tools import register_tools


def payload(context="course", **changes):
    return {"kind": "discussion_attachment_edit",
            "context": {"context_type": context, f"{context}_id": 1 if context == "course" else 3},
            "topic_id": 2, "entry_id": 10, "message": "<p>Reviewed replacement message</p>", **changes}


class EditState(DiscussionState):
    def __init__(self, context="course", *, parent=None):
        super().__init__(context)
        self.settings = {"allow_student_discussion_editing": True}
        self.topic["permissions"]["attach"] = False  # Preserving a file is not a new upload.
        self.file = {"id": 90, "folder_id": 77, "filename": "existing.txt", "display_name": "Existing file.txt",
                     "size": len(RAW), "content-type": "text/plain", "updated_at": "file-version-one",
                     "modified_at": "file-version-one", "locked_for_user": False, "hidden_for_user": False,
                     "url": self.download_url}
        self.entries = {10: {"id": 10, "user_id": 7, "parent_id": parent, "message": "<p>Old message</p>",
                             "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
                             "attachment": copy.deepcopy(self.file), "attachments": [copy.deepcopy(self.file)]}}
        self.fail_write = None
        self.override_ack = None
        self.same_timestamp = False
        self.preserve = True
        self.entry_rows = None

    def handler(self, request):
        path, method = request.url.path, request.method
        assert request.url.host == "school.example"
        assert request.headers["authorization"] == "Bearer synthetic-token"
        if method == "GET" and path in {"/api/v1/courses/1/settings", "/api/v1/files/90", self.base}:
            self.requests.append((method, path, dict(request.url.params)))
            if self.error and self.error[:2] == (method, path):
                return httpx.Response(self.error[2], json={"message": "synthetic denied"})
            return httpx.Response(200, json=copy.deepcopy(
                self.settings if path.endswith("/settings") else self.file if path.endswith("/files/90") else self.topic))
        if method == "GET" and path == self.base + "/entry_list" and self.entry_rows is not None:
            self.requests.append((method, path, dict(request.url.params)))
            return httpx.Response(200, json=copy.deepcopy(self.entry_rows))
        if method != "PUT":
            return super().handler(request)
        self.requests.append((method, path, dict(request.url.params)))
        assert path == self.base + "/entries/10" and not request.url.params
        assert request.headers["content-type"] == "application/json"
        body = json.loads(request.content)
        assert set(body) == ({"message", "attachment"} if self.preserve else {"message"})
        if self.preserve:
            assert body["attachment"] == {} and type(body["attachment"]) is dict
        self.writes.append(copy.deepcopy(body))
        entry = self.entries[10]
        entry.update(message=body["message"], editor_id=self.actor)
        if not self.same_timestamp:
            entry["updated_at"] = "2026-01-02T00:00:00Z"
        ack = copy.deepcopy(entry)
        if self.after_write:
            self.after_write(self)
        if self.fail_write == "network":
            raise httpx.ReadError("synthetic lost response after apply", request=request)
        if self.fail_write in {302, 307, 308}:
            return httpx.Response(self.fail_write, headers={"Location": self.base + "/entries/10"})
        if self.fail_write:
            return httpx.Response(self.fail_write, json={"message": "Rate Limit Exceeded"}, headers={"Retry-After": "0"})
        return httpx.Response(200, json=self.override_ack if self.override_ack is not None else ack)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected real network in discussion edit tests")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


@contextmanager
def client(state, *, token="synthetic-token"):
    with (httpx.Client(transport=httpx.MockTransport(state.handler)) as http,
          Canvas(CanvasConfig(BASE, token), http_client=http) as canvas):
        yield canvas


@pytest.mark.parametrize("context", ["course", "group"])
@pytest.mark.parametrize("parent", [None, 8])
@pytest.mark.parametrize("storage", ["User", "Course", "Group"])
def test_exact_json_preservation_and_fresh_message_association_hash(context, parent, storage):
    state, p = EditState(context, parent=parent), payload(context)
    state.file.update(context_type=storage, context_id=123)  # No uploader-only assumption.
    with client(state) as canvas:
        target = edits.snapshot(canvas, p)
        assert target["entry"]["update_permission"] is None
        assert not state.writes
        result = edits.perform(canvas, p, target)
    assert state.writes == [{"message": p["message"], "attachment": {}}]
    assert result["verified"] and result["attachment_preserved"] and not result["no_op"]
    assert result["parent_id"] == parent and result["editor_id"] == 7
    assert result["attachment"]["id"] == 90
    assert result["attachment"]["sha256"] == hashlib.sha256(RAW).hexdigest()
    assert result["attachment"]["read"] == {"tool": "canvas_read_file", "file_id": 90}
    assert state.downloads == 3  # prepare, dispatch validation, independent saved state
    assert not any("users/self/files" in path for _, path, _ in state.requests)
    assert "synthetic-secret" not in str(target) + str(result)
    assert "url" not in target["file"] and "url" not in target["entry"]["attachment"]
    assert all(term in target["effect"] for term in ("history", "module", "mentions", "LTI"))


def test_account_group_does_not_require_parent_course_setting():
    state = EditState("group")
    state.group.update(context_type="Account", account_id=9)
    state.group.pop("course_id")
    state.settings = {"allow_student_discussion_editing": False}
    with client(state) as canvas:
        target = edits.snapshot(canvas, payload("group"))
        assert target["student_edit_setting"] is None
        assert edits.perform(canvas, payload("group"), target)["verified"]
    assert not any(path.endswith("/settings") for _, path, _ in state.requests)


@pytest.mark.parametrize("old_message", [None, ""])
def test_existing_file_only_message_can_be_edited(old_message):
    state = EditState()
    state.entries[10]["message"] = old_message
    with client(state) as canvas:
        assert edits.perform(canvas, payload(), edits.snapshot(canvas, payload()))["verified"]


@pytest.mark.parametrize("alias", ["attachment", "attachments"])
def test_single_native_attachment_alias_is_accepted(alias):
    state = EditState()
    state.entries[10].pop(alias)
    with client(state) as canvas:
        assert edits.perform(canvas, payload(), edits.snapshot(canvas, payload()))["verified"]


def test_same_serialized_updated_timestamp_does_not_invent_a_revision_increment():
    state = EditState()
    state.same_timestamp = True
    with client(state) as canvas:
        target = edits.snapshot(canvas, payload())
        result = edits.perform(canvas, payload(), target)
    assert result["updated_at"] == target["entry"]["updated_at"] and result["verified"]


@pytest.mark.parametrize("announcement", [True, False])
def test_own_entry_edit_does_not_require_permission_to_post_a_new_reply(announcement):
    state = EditState()
    state.topic.update(is_announcement=announcement, comments_disabled=announcement)
    state.topic["permissions"]["reply"] = False
    with client(state) as canvas:
        target = edits.snapshot(canvas, payload())
        assert edits.perform(canvas, payload(), target)["verified"]
    assert state.writes == [{"message": payload()["message"], "attachment": {}}]


def test_noop_message_is_verified_without_put():
    state, store = EditState(), ActionStore()
    p = payload(message=state.entries[10]["message"])
    with client(state) as canvas:
        prepared = store.prepare(canvas, "alice", p)
        receipt = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
        assert receipt["state"] == "succeeded" and receipt["result"]["no_op"]
        assert store.execute(canvas, "alice", prepared["action_id"], lambda: None) == receipt
    assert not state.writes


@pytest.mark.parametrize("mutation", [
    lambda s: setattr(s, "actor", 8),
    lambda s: s.course.update(workflow_state="completed"),
    lambda s: s.course.update(access_restricted_by_date=True),
    lambda s: s.course.update(enrollments=[]),
    lambda s: s.course["enrollments"][0].update(type="teacher"),
    lambda s: s.settings.update(allow_student_discussion_editing=False),
    lambda s: s.settings.clear(),
    lambda s: s.topic.update(id=3),
    lambda s: s.topic.update(course_id=9),
    lambda s: s.topic.update(locked=True),
    lambda s: s.topic.update(locked_for_user=True),
    lambda s: s.topic.update(published=False),
    lambda s: s.topic.update(lock_info={}),
    lambda s: s.topic.update(user_can_see_posts=False),
    lambda s: s.topic.update(subscription_hold="initial_post_required"),
    lambda s: s.topic.update(group_category_id=5),
    lambda s: s.topic.update(is_section_specific=True),
    lambda s: s.entries[10].update(user_id=8),
    lambda s: s.entries[10].update(id=11),
    lambda s: s.entries[10].update(discussion_topic_id=4),
    lambda s: s.entries[10].update(deleted=True),
    lambda s: s.entries[10].pop("parent_id"),
    lambda s: s.entries[10].update(parent_id=True),
    lambda s: s.entries[10].pop("updated_at"),
    lambda s: s.entries[10].update(permissions={"update": False}),
    lambda s: s.entries[10].update(permissions={"update": 1}),
    lambda s: s.entries[10].update(attachment=None, attachments=None),
    lambda s: s.entries[10].update(attachments=[]),
    lambda s: s.entries[10]["attachments"].append(copy.deepcopy(s.file)),
    lambda s: s.entries[10]["attachments"][0].update(id=91),
    lambda s: s.file.update(id=91),
    lambda s: s.file.update(display_name="changed.txt"),
    lambda s: s.file.update(locked_for_user=True),
    lambda s: s.file.update(hidden_for_user=True),
    lambda s: s.file.update(file_state="deleted"),
    lambda s: s.file.update(url=""),
    lambda s: setattr(s, "entry_rows", []),
    lambda s: setattr(s, "entry_rows", [s.entries[10], copy.deepcopy(s.entries[10])]),
])
def test_unavailable_or_ambiguous_target_is_rejected_before_write(mutation):
    state = EditState()
    mutation(state)
    with client(state) as canvas, pytest.raises((ValueError, CanvasError)):
        edits.snapshot(canvas, payload())
    assert not state.writes


@pytest.mark.parametrize("mutation", [
    lambda s: s.membership.update(user_id=8), lambda s: s.membership.update(group_id=4),
    lambda s: s.membership.update(workflow_state="invited"),
    lambda s: s.group.update(non_collaborative=True), lambda s: s.group.update(workflow_state="deleted"),
    lambda s: s.group.update(context_type="Unknown"), lambda s: s.group.update(course_id=True),
    lambda s: s.settings.update(allow_student_discussion_editing=False),
])
def test_group_access_requires_exact_membership_and_correct_parent_setting(mutation):
    state = EditState("group")
    mutation(state)
    with client(state) as canvas, pytest.raises((ValueError, CanvasError)):
        edits.snapshot(canvas, payload("group"))
    assert not state.writes


@pytest.mark.parametrize("size", [-1, True, "5", edits.MAX_BYTES + 1])
def test_declared_attachment_size_is_bounded_before_download(size):
    state = EditState()
    for value in (state.file, state.entries[10]["attachment"], state.entries[10]["attachments"][0]):
        value["size"] = size
    with client(state) as canvas, pytest.raises(ValueError):
        edits.snapshot(canvas, payload())
    assert state.downloads == 0 and not state.writes


def test_streamed_attachment_bytes_cannot_exceed_limit():
    state = EditState()
    state.raw = b"x" * (edits.MAX_BYTES + 1)
    with client(state) as canvas, pytest.raises(CanvasError, match="limit"):
        edits.snapshot(canvas, payload())
    assert not state.writes


@pytest.mark.parametrize("mutation", [
    lambda s: s.entries[10].update(message="Concurrent edit"),
    lambda s: s.entries[10].update(parent_id=8),
    lambda s: s.entries[10].update(updated_at="concurrent version"),
    lambda s: s.file.update(folder_id=88),
    lambda s: setattr(s, "raw", b"x" * len(RAW)),
    lambda s: s.settings.update(allow_student_discussion_editing=False),
])
def test_stale_preparation_rejects_before_dispatch(mutation):
    state, store = EditState(), ActionStore()
    with client(state) as canvas:
        prepared = store.prepare(canvas, "alice", payload())
        mutation(state)
        receipt = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
    assert receipt["state"] == "rejected"
    assert receipt["result"]["upstream_write_attempted"] is False and not state.writes


def test_state_changing_during_initial_download_is_not_a_stable_preparation():
    state = EditState()
    state.after_download = lambda s: s.entries[10].update(message="Concurrent edit")
    with client(state) as canvas, pytest.raises(ValueError, match="changed while"):
        edits.snapshot(canvas, payload())
    assert not state.writes


@pytest.mark.parametrize("mutation", [
    lambda s: s.entries[10].update(message="Canvas rewritten HTML"),
    lambda s: s.entries[10].update(editor_id=8),
    lambda s: s.entries[10].update(user_id=8),
    lambda s: s.entries[10].update(parent_id=8),
    lambda s: s.entries[10].update(attachment=None, attachments=None),
    lambda s: s.entries[10]["attachment"].update(id=91),
    lambda s: s.file.update(modified_at="different-file-version"),
    lambda s: setattr(s, "raw", b"x" * len(RAW)),
    lambda s: setattr(s, "error", ("GET", "/api/v1/files/90", 403)),
    lambda s: s.settings.update(allow_student_discussion_editing=False),
])
def test_unverified_edit_is_uncertain_and_terminal(mutation):
    state, store = EditState(), ActionStore()
    with client(state) as canvas:
        prepared = store.prepare(canvas, "alice", payload())
        state.after_write = mutation
        receipt = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
        assert receipt["state"] == "uncertain"
        after = len(state.requests)
        assert store.execute(canvas, "alice", prepared["action_id"], lambda: None) == receipt
        assert store.prepare(canvas, "alice", payload())["state"] == "uncertain"
        assert len(state.requests) == after
    assert len(state.writes) == 1


@pytest.mark.parametrize("failure", [429, 403, "network", 302, 307, 308])
def test_mutation_transport_is_one_attempt_with_no_redirect_or_retry(failure):
    state, store = EditState(), ActionStore()
    state.fail_write = failure
    request = edits._Edit(edits.DiscussionAttachmentEdit.model_validate(payload()))
    assert request.allow_retries is request.allow_redirects is False
    with client(state) as canvas:
        prepared = store.prepare(canvas, "alice", payload())
        receipt = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
        assert receipt["state"] == "uncertain"
    assert len(state.writes) == 1
    assert state.entries[10]["message"] == payload()["message"]  # uncertainty is not rollback


@pytest.mark.parametrize("changes", [
    {"attachment": {}}, {"attachment_id": 90}, {"remove_attachment": False},
    {"content_base64": "eA=="}, {"filename": "new.txt"}, {"user_id": 7}, {"force": True},
    {"message": ""}, {"message": "   "}, {"message": "x\x00y"}, {"message": "x" * 100001},
    {"entry_id": True}, {"topic_id": "2"}, {"entry_id": 0},
    {"context": {"context_type": "group", "group_id": 3, "course_id": 1}},
])
def test_model_only_accepts_reviewed_message_and_exact_context(changes):
    with pytest.raises(ValidationError):
        edits.DiscussionAttachmentEdit.model_validate(payload(**changes))


def test_external_attachment_storage_download_never_gets_canvas_authorization(monkeypatch):
    state = EditState()
    state.file["url"] = "https://storage.example/opaque-attachment?signature=synthetic-secret"
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))])
    transfers = []
    def storage(request):
        if request.url.host == "school.example":
            return state.handler(request)
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "storage.example"
        assert request.extensions["sni_hostname"] == "storage.example"
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        transfers.append(str(request.url))
        return httpx.Response(200, content=RAW)
    with (httpx.Client(transport=httpx.MockTransport(storage)) as http,
          Canvas(CanvasConfig(BASE, "synthetic-token"), http_client=http) as canvas):
        target = edits.snapshot(canvas, payload())
        assert edits.perform(canvas, payload(), target)["verified"]
    assert len(transfers) == 3 and "synthetic-secret" not in str(target)


def test_uncertain_receipt_survives_restart_without_any_additional_io(tmp_path):
    state, key, database = EditState(), Fernet.generate_key(), tmp_path / "edits.db"
    with client(state) as canvas:
        db = Store(database, key)
        try:
            store = ActionStore(persistence=db)
            prepared = store.prepare(canvas, "alice", payload())
            state.fail_write = "network"
            receipt = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
            assert receipt["state"] == "uncertain"
        finally:
            db.close()
        db = Store(database, key)
        try:
            reopened = ActionStore(persistence=db)
            before = len(state.requests)
            assert reopened.status(canvas, "alice", prepared["action_id"])["state"] == "uncertain"
            assert reopened.prepare(canvas, "alice", payload())["action_id"] == prepared["action_id"]
            assert reopened.execute(canvas, "alice", prepared["action_id"], lambda: None) == receipt
            assert len(state.requests) == before and len(state.writes) == 1
        finally:
            db.close()


def surface(canvas):
    context, authorize = {"canvas": canvas, "owner": "alice"}, Mock()
    mcp = FastMCP("attached-edit-boundary")
    register_tools(mcp, lambda: context["canvas"], authorize, owner=lambda: context["owner"])
    return mcp, context, authorize


def call(mcp, name, **arguments):
    result = asyncio.run(mcp.call_tool(name, arguments))
    assert isinstance(result, tuple) and isinstance(result[1], dict)
    return result[1]


def test_actual_mcp_executor_receipt_review_and_immutability():
    state, p = EditState(), payload()
    with client(state) as canvas:
        server, _, authorize = surface(canvas)
        prepared = call(server, "canvas_prepare_action", action=p)["data"]
        assert prepared["execute"]["tool"] == "canvas_edit_discussion_attachment"
        assert "synthetic-secret" not in str(prepared)
        p["message"] = "changed after review"
        wrong = call(server, "canvas_post_discussion_attachment", action_id=prepared["action_id"])
        assert wrong["status"] == "invalid_request" and not state.writes
        receipt = call(server, "canvas_edit_discussion_attachment", action_id=prepared["action_id"])["data"]
        assert receipt["state"] == "succeeded" and receipt["result"]["message"] == "Reviewed replacement message"
        assert state.writes == [{"message": payload()["message"], "attachment": {}}]
        assert call(server, "canvas_edit_discussion_attachment", action_id=prepared["action_id"])["data"] == receipt
        assert len(state.writes) == 1
        authorize.assert_called()
        tool = next(t for t in server._tool_manager.list_tools() if t.name == "canvas_edit_discussion_attachment")
        assert set(tool.parameters["properties"]) == {"action_id"}
        assert tool.annotations.readOnlyHint is False and tool.annotations.idempotentHint is False


@pytest.mark.parametrize("change", ["owner", "credentials"])
def test_actual_mcp_receipt_cannot_cross_account_or_owner(change):
    state = EditState()
    with client(state) as canvas, client(state, token="other-synthetic-token") as other:
        server, context, _ = surface(canvas)
        prepared = call(server, "canvas_prepare_action", action=payload())["data"]
        before = len(state.requests)
        context["owner" if change == "owner" else "canvas"] = "bob" if change == "owner" else other
        assert call(server, "canvas_edit_discussion_attachment", action_id=prepared["action_id"])["status"] == "invalid_request"
        assert len(state.requests) == before and not state.writes


def test_legacy_attached_edit_remains_rejected_and_new_upload_payload_is_unchanged(tmp_path):
    state = EditState()
    legacy = {"kind": "discussion_edit", "course_id": 1, "topic_id": 2, "entry_id": 10, "message": "legacy"}
    with client(state) as canvas, pytest.raises(ValueError, match="preserve"):
        ActionStore().prepare(canvas, "alice", legacy)
    upload = DiscussionState()
    db = Store(tmp_path / "upload.db", Fernet.generate_key())
    try:
        with client(upload) as canvas:
            prepared = ActionStore(persistence=db).prepare(canvas, "alice", upload_payload())
            encoded = db.get("canvas_action_payload", prepared["action_id"])["payload"]
            assert json.loads(encoded) == upload_payload()
            assert ActionStore(persistence=db).prepare(canvas, "alice", upload_payload())["action_id"] == prepared["action_id"]
    finally:
        db.close()


def test_legacy_unattached_edit_payload_fingerprint_and_persisted_execution(tmp_path):
    state = EditState()
    state.preserve = False
    state.entries[10].pop("attachment")
    state.entries[10].pop("attachments")
    legacy = {"kind": "discussion_edit", "course_id": 1, "topic_id": 2, "entry_id": 10, "message": "legacy edit"}
    key, database = Fernet.generate_key(), tmp_path / "legacy.db"
    with client(state) as canvas:
        db = Store(database, key)
        try:
            store = ActionStore(persistence=db)
            prepared = store.prepare(canvas, "alice", legacy)
            encoded = json.dumps(legacy, sort_keys=True, separators=(",", ":"))
            assert db.get("canvas_action_payload", prepared["action_id"])["payload"] == encoded
            expected = hashlib.sha256((store._identity(canvas, "alice") + "\0" + encoded).encode()).hexdigest()
            assert db.get("canvas_action", prepared["action_id"])["fingerprint"] == expected
        finally:
            db.close()
        db = Store(database, key)
        try:
            reopened = ActionStore(persistence=db)
            assert reopened.prepare(canvas, "alice", legacy)["action_id"] == prepared["action_id"]
            receipt = reopened.execute(canvas, "alice", prepared["action_id"], lambda: None)
            assert receipt["state"] == "succeeded"
            assert reopened.execute(canvas, "alice", prepared["action_id"], lambda: None) == receipt
        finally:
            db.close()
    assert state.writes == [{"message": legacy["message"]}]
