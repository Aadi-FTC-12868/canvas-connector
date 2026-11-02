"""Synthetic discussion attachment state behind the real Canvas HTTP client."""

import base64
import copy
import hashlib
import socket
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
from typing import Annotated

import httpx
import pytest
from canvas_py import Canvas
from canvas_py._config import CanvasConfig
from canvas_py._errors import CanvasError
from pydantic import Field, TypeAdapter, ValidationError

from canvas_connector import discussion_attachments as attachments

BASE = "https://school.example"
RAW = b"Reviewed attachment\x00\r\n"
ADAPTER = TypeAdapter(Annotated[attachments.DiscussionAttachmentActionTypes, Field(discriminator="kind")])


def payload(context="course", reply=False, **changes):
    return {"kind": "discussion_attachment_reply" if reply else "discussion_attachment_post",
            "context": {"context_type": context, f"{context}_id": 1 if context == "course" else 3},
            "topic_id": 2, "message": "<p>Reviewed message</p>", "filename": "evidence.txt", "content_type": "text/plain",
            "content_base64": base64.b64encode(RAW).decode(), **({"entry_id": 10} if reply else {}), **changes}


class DiscussionState:
    def __init__(self, context="course"):
        self.context = context
        self.base = f"/api/v1/{context}s/{1 if context == 'course' else 3}/discussion_topics/2"
        self.actor = 7
        self.course = {"id": 1, "name": "Course audience", "workflow_state": "available", "enrollments": [
            {"user_id": 7, "type": "student", "enrollment_state": "active", "role": "StudentEnrollment", "role_id": 4}]}
        self.group = {"id": 3, "name": "Group audience", "context_type": "Course", "course_id": 1, "is_public": False}
        self.membership = {"id": 4, "user_id": 7, "group_id": 3, "workflow_state": "accepted"}
        self.topic = {"id": 2, "title": "Actual target", "published": True, "locked": False, "locked_for_user": False,
                      "permissions": {"reply": True, "attach": True}, "require_initial_post": False, "user_can_see_posts": True}
        self.entries = {10: {"id": 10, "user_id": 8, "parent_id": None, "message": "Peer's post", "updated_at": "one"}}
        self.requests, self.writes = [], []
        self.file = None
        self.raw = RAW
        self.actual_name = None
        self.stored_filename = None
        self.after_write = None
        self.after_download = None
        self.created_change = None
        self.error = None
        self.download_url = BASE + "/files/90/download?verifier=synthetic-secret"
        self.downloads = 0

    def handler(self, request):
        assert request.url.host == "school.example", "No external destinations in fixture"
        assert request.headers["authorization"] == "Bearer synthetic-token"
        path, method = request.url.path, request.method
        self.requests.append((method, path, dict(request.url.params)))
        if self.error and self.error[:2] == (method, path):
            return httpx.Response(self.error[2], json={"message": "synthetic denied"})
        if method == "GET":
            assert not request.content
            if path == "/api/v1/users/self/profile":
                return httpx.Response(200, json={"id": self.actor})
            if path == "/api/v1/courses/1":
                return httpx.Response(200, json=self.course)
            if path == "/api/v1/groups/3":
                return httpx.Response(200, json=self.group)
            if path == "/api/v1/groups/3/users/self":
                return httpx.Response(200, json=self.membership)
            if path == self.base:
                assert request.url.params.get("include[]") == "sections"
                return httpx.Response(200, json=self.topic)
            if path == self.base + "/entry_list":
                entry = self.entries.get(int(request.url.params["ids[]"]))
                return httpx.Response(200, json=[copy.deepcopy(entry)] if entry else [])
            if path == "/api/v1/users/self/files/90":
                return httpx.Response(200, json=self.file)
            if path == "/files/90/download":
                self.downloads += 1
                response = httpx.Response(200, content=self.raw)
                if self.after_download:
                    self.after_download(self)
                return response
            raise AssertionError(f"Unexpected read {path}")
        assert method == "POST" and path in {self.base + "/entries", self.base + "/entries/10/replies"}
        mime = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content)
        parts = {part.get_param("name", header="content-disposition"): part for part in mime.iter_parts()}
        assert set(parts) == {"message", "attachment"}
        part = parts["attachment"]
        message = parts["message"].get_payload(decode=True).decode()
        raw = part.get_payload(decode=True)
        filename = part.get_filename()
        self.writes.append({"method": method, "path": path, "filename": filename, "message": message,
                            "raw": raw, "mime": part.get_content_type()})
        self.file = {"id": 90, "filename": self.stored_filename or filename,
                     "display_name": self.actual_name or filename, "size": len(raw), "url": self.download_url,
                     "locked_for_user": False, "hidden_for_user": False}
        self.entries[20] = {"id": 20, "user_id": self.actor, "parent_id": 10 if path.endswith("/replies") else None,
                            "message": message, "attachment": copy.deepcopy(self.file), "attachments": [copy.deepcopy(self.file)]}
        created = copy.deepcopy(self.entries[20])
        if self.created_change:
            self.created_change(created)
        if self.after_write:
            self.after_write(self)
        return httpx.Response(201, json=created)


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Only synthetic MockTransport requests are allowed")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


@contextmanager
def client(state):
    with (httpx.Client(transport=httpx.MockTransport(state.handler)) as http,
          Canvas(CanvasConfig(BASE, "synthetic-token"), http_client=http) as canvas):
        yield canvas


@pytest.mark.parametrize("context", ["course", "group"])
@pytest.mark.parametrize("reply", [False, True])
def test_exact_multipart_publish_and_independent_hash_readback(context, reply):
    state, p = DiscussionState(context), payload(context, reply)
    with client(state) as canvas:
        target = attachments.snapshot(canvas, p)
        assert target["user_id"] == 7 and target["context"] == p["context"]
        assert target["audience"]["name"] == context.title() + " audience"
        assert target["attachment"] == {"filename": "evidence.txt", "content_type": "text/plain",
                                         "bytes": len(RAW), "sha256": hashlib.sha256(RAW).hexdigest()}
        result = attachments.perform(canvas, p, target)
    assert result["verified"] and result["entry_id"] == 20 and result["parent_id"] == (10 if reply else None)
    assert result["attachment"]["sha256"] == target["attachment"]["sha256"]
    assert result["attachment"]["read"] == {"tool": "canvas_read_file", "file_id": 90}
    assert "verifier" not in str(result) and "url" not in result["attachment"]
    assert len(state.writes) == state.downloads == 1
    assert state.writes[0]["raw"] == RAW
    assert state.writes[0]["filename"] == "evidence.txt" and state.writes[0]["mime"] == "text/plain"
    assert sum(path == state.base + "/entry_list" and query.get("ids[]") == "20" for _, path, query in state.requests) == 2


@pytest.mark.parametrize("name", ["evidence-1.txt", "evidence-42.txt", "evidence-12345678-1234-1234-1234-123456789abc.txt"])
def test_canvas_collision_display_rename_is_reported_and_verified(name):
    state = DiscussionState()
    state.actual_name = name
    with client(state) as canvas:
        result = attachments.perform(canvas, payload(), attachments.snapshot(canvas, payload()))
    assert result["attachment"]["requested_filename"] == "evidence.txt"
    assert result["attachment"]["display_name"] == name
    assert result["attachment"]["display_name_changed"] is True


def test_storage_filename_encoding_or_dedup_does_not_change_requested_display_name():
    state = DiscussionState()
    state.stored_filename = "encoded%20prior%20name.txt"
    with client(state) as canvas:
        result = attachments.perform(canvas, payload(), attachments.snapshot(canvas, payload()))
    assert result["attachment"]["filename"] == state.stored_filename
    assert result["attachment"]["display_name"] == result["attachment"]["requested_filename"] == "evidence.txt"


@pytest.mark.parametrize("field,value", [
    ("locked", True), ("locked_for_user", True), ("published", False),
    ("permissions", None), ("permissions", {}), ("permissions", {"reply": True, "attach": False}),
    ("permissions", {"reply": False, "attach": True}), ("permissions", {"reply": True, "attach": 1}),
    ("id", 3), ("course_id", 9), ("context_type", "Group"), ("group_category_id", 5),
    ("is_section_specific", True),
])
def test_topic_identity_locks_permissions_and_ambiguous_audience_block_before_write(field, value):
    state = DiscussionState()
    state.topic[field] = value
    with client(state) as canvas, pytest.raises(ValueError):
        attachments.snapshot(canvas, payload())
    assert not state.writes


@pytest.mark.parametrize("mutation", [
    lambda s: s.course.update(id=99), lambda s: s.course.update(workflow_state="completed"),
    lambda s: s.course.update(access_restricted_by_date=True), lambda s: s.course.update(enrollments=[]),
    lambda s: s.course["enrollments"][0].update(type="teacher"),
    lambda s: s.course["enrollments"][0].update(user_id=8),
    lambda s: s.course["enrollments"][0].update(enrollment_state="invited"),
])
def test_only_current_active_student_course_membership(mutation):
    state = DiscussionState()
    mutation(state)
    with client(state) as canvas, pytest.raises(ValueError):
        attachments.snapshot(canvas, payload())
    assert not state.writes


@pytest.mark.parametrize("mutation", [
    lambda s: s.membership.update(user_id=8), lambda s: s.membership.update(group_id=4),
    lambda s: s.membership.update(workflow_state="invited"), lambda s: s.group.update(non_collaborative=True),
    lambda s: s.group.update(workflow_state="deleted"), lambda s: s.topic.update(group_id=4),
])
def test_exact_accepted_collaborative_group_membership(mutation):
    state = DiscussionState("group")
    mutation(state)
    with client(state) as canvas, pytest.raises(ValueError):
        attachments.snapshot(canvas, payload("group"))
    assert not state.writes


@pytest.mark.parametrize("mutation", [
    lambda s: s.topic.update(user_can_see_posts=False), lambda s: s.topic.update(subscription_hold="initial_post_required"),
    lambda s: s.entries[10].update(deleted=True), lambda s: s.entries[10].update(id=11),
    lambda s: s.entries[10].pop("parent_id"), lambda s: s.entries[10].update(discussion_topic_id=9),
    lambda s: s.entries[10].update(permissions={"reply": False}),
])
def test_reply_requires_visible_exact_parent_without_auto_post(mutation):
    state = DiscussionState()
    mutation(state)
    with client(state) as canvas, pytest.raises(ValueError):
        attachments.snapshot(canvas, payload(reply=True))
    assert not state.writes


def test_requested_initial_top_level_post_is_allowed_without_reading_hidden_peers():
    state = DiscussionState()
    state.topic.update(user_can_see_posts=False, require_initial_post=True, subscription_hold="initial_post_required")
    with client(state) as canvas:
        attachments.snapshot(canvas, payload())
    assert not any(path.endswith("entry_list") for _, path, _ in state.requests)


@pytest.mark.parametrize("mutation", [
    lambda s: s.topic.update(title="New audience title"), lambda s: s.topic.update(locked=True),
    lambda s: s.course.update(name="Changed course"), lambda s: setattr(s, "actor", 8),
    lambda s: s.entries[10].update(message="Changed peer post"),
])
def test_stale_snapshot_blocks_dispatch(mutation):
    state, p = DiscussionState(), payload(reply=True)
    with client(state) as canvas:
        target = attachments.snapshot(canvas, p)
        mutation(state)
        with pytest.raises(ValueError):
            attachments.perform(canvas, p, target)
    assert not state.writes


@pytest.mark.parametrize("mutation", [
    lambda s: s.entries[20].update(user_id=8), lambda s: s.entries[20].update(message="different"),
    lambda s: s.entries[20].update(parent_id=10), lambda s: s.entries[20].update(deleted=True),
    lambda s: s.entries[20]["attachment"].update(id=91),
    lambda s: s.entries[20]["attachment"].update(size=1),
    lambda s: s.entries[20]["attachments"].append(copy.deepcopy(s.file)),
    lambda s: s.entries[20].update(attachment=None, attachments=[]),
    lambda s: s.file.update(id=91), lambda s: s.file.update(display_name="other.txt"),
    lambda s: s.file.update(filename="changed.txt"), lambda s: s.file.update(size=99),
    lambda s: s.file.update(locked_for_user=True), lambda s: s.file.update(context_type="Course"),
    lambda s: s.file.update(context_id=8), lambda s: s.file.update(user_id=8),
    lambda s: setattr(s, "actor", 8), lambda s: setattr(s, "raw", b"wrong bytes"),
])
def test_wrong_saved_identity_message_attachment_metadata_or_hash_is_uncertain(mutation):
    state, p = DiscussionState(), payload()
    state.after_write = mutation
    with client(state) as canvas, pytest.raises(ValueError):
        attachments.perform(canvas, p, attachments.snapshot(canvas, p))
    assert len(state.writes) == 1


@pytest.mark.parametrize("mutation", [
    lambda s: s.entries[20].update(message="edited during download"),
    lambda s: s.entries[20]["attachment"].update(id=91),
    lambda s: s.entries[20].update(deleted=True),
    lambda s: setattr(s, "actor", 8),
])
def test_association_rechecked_after_downloading(mutation):
    state = DiscussionState()
    state.after_download = mutation
    with client(state) as canvas, pytest.raises(ValueError):
        attachments.perform(canvas, payload(), attachments.snapshot(canvas, payload()))
    assert state.downloads == len(state.writes) == 1


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_failed_independent_read_is_not_success_or_retry(status):
    state = DiscussionState()
    state.after_write = lambda s: setattr(s, "error", ("GET", "/api/v1/users/self/files/90", status))
    with client(state) as canvas, pytest.raises(CanvasError):
        attachments.perform(canvas, payload(), attachments.snapshot(canvas, payload()))
    assert len(state.writes) == 1


@pytest.mark.parametrize("url", ["http://external.example/file", "https://user:pass@external.example/file", "https://127.0.0.1/private"])
def test_untrusted_download_locations_never_receive_credentials(url, monkeypatch):
    state = DiscussionState()
    state.download_url = url
    # Private addresses are classified without permitting an actual connection.
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
    with client(state) as canvas, pytest.raises(CanvasError):
        attachments.perform(canvas, payload(), attachments.snapshot(canvas, payload()))
    assert len(state.writes) == 1 and not state.downloads


@pytest.mark.parametrize("field,value", [
    ("filename", "../file.txt"), ("filename", "bad\\file.txt"), ("filename", "bad\nheader.txt"),
    ("filename", "bad\x7ffile.txt"), ("filename", " x.txt"), ("filename", "."), ("filename", ""),
    ("filename", "a" * 241), ("content_type", "text/plain\r\nInjected: yes"),
    ("content_type", "text/plain; charset=utf8"), ("content_type", "text"),
    ("message", " "), ("message", "bad\x00message"), ("message", "x" * 100001),
    ("content_base64", ""), ("content_base64", "!!!!"), ("content_base64", "eA==\n"),
    ("content_base64", "eB=="), ("content_base64", "eA===="),
    ("topic_id", True), ("topic_id", "2"), ("topic_id", -1), ("topic_id", 2**63),
    ("entry_id", 10), ("attachment_file_ids", [90]), ("user_id", 9), ("remove_attachment", True),
    ("context", {"context_type": "account", "account_id": 1}),
    ("context", {"context_type": "course", "course_id": 1, "group_id": 3}),
])
def test_invalid_or_unauthorized_action_fields_rejected_without_io(field, value):
    with pytest.raises(ValidationError):
        ADAPTER.validate_python(payload(**{field: value}))


def test_exact_eight_mib_bound_and_one_byte_minimum():
    for raw in (b"x", b"x" * attachments.MAX_BYTES):
        assert ADAPTER.validate_python(payload(content_base64=base64.b64encode(raw).decode()))
    with pytest.raises(ValidationError):
        ADAPTER.validate_python(payload(content_base64=base64.b64encode(b"x" * (attachments.MAX_BYTES + 1)).decode()))


def test_unknown_or_unrelated_server_filename_is_not_verified():
    state = DiscussionState()
    state.actual_name = "unexpected-file.txt"
    with client(state) as canvas, pytest.raises(ValueError, match="display name"):
        attachments.perform(canvas, payload(), attachments.snapshot(canvas, payload()))
    assert len(state.writes) == 1


def test_equal_size_but_different_hash_is_uncertain():
    state = DiscussionState()
    state.raw = b"x" * len(RAW)
    with client(state) as canvas, pytest.raises(ValueError, match="bytes differ"):
        attachments.perform(canvas, payload(), attachments.snapshot(canvas, payload()))
    assert state.downloads == len(state.writes) == 1


def test_readback_download_enforces_eight_mib_bound():
    state = DiscussionState()
    state.raw = b"x" * (attachments.MAX_BYTES + 1)
    with client(state) as canvas, pytest.raises(CanvasError, match="byte limit"):
        attachments.perform(canvas, payload(), attachments.snapshot(canvas, payload()))
    assert state.downloads == len(state.writes) == 1


@pytest.mark.parametrize("mutation", [
    lambda e: e.update(id=True), lambda e: e.update(user_id=8), lambda e: e.update(parent_id=10),
    lambda e: e.update(message="server changed message"), lambda e: e.update(attachment=None, attachments=[]),
    lambda e: e["attachments"][0].update(id=90.0),
])
def test_invalid_created_response_remains_uncertain(mutation):
    state = DiscussionState()
    state.created_change = mutation
    with client(state) as canvas, pytest.raises(ValueError):
        attachments.perform(canvas, payload(), attachments.snapshot(canvas, payload()))
    assert len(state.writes) == 1


def test_section_audience_is_explicit_and_bound_to_snapshot():
    state = DiscussionState()
    state.topic.update(is_section_specific=True, sections=[{"id": 4, "name": "Section Four", "course_id": 1}])
    with client(state) as canvas:
        target = attachments.snapshot(canvas, payload())
        assert target["sections"] == state.topic["sections"]
        state.topic["sections"][0]["id"] = 5
        with pytest.raises(ValueError, match="changed after preparation"):
            attachments.perform(canvas, payload(), target)
    assert not state.writes
