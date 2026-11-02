"""Synthetic native Inbox requests; never contact Canvas or deliver messages."""
import asyncio
import copy
import json
import socket
import sys
import types
from urllib.parse import urlencode

import httpx
import pytest
from canvas_py import Canvas
from canvas_py._config import CanvasConfig
from canvas_py._errors import CanvasError
from cryptography.fernet import Fernet
from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from canvas_connector import inbox_messages as im
from canvas_connector.actions import ActionStore
from canvas_connector.storage import Store
from canvas_connector.tools import register_tools


def message(identity, *, author=8, participants=None, body="Original visible text", generated=False):
    return {"id": identity, "author_id": author, "body": body, "generated": generated,
            "created_at": "2026-01-01T00:00:00Z", "participating_user_ids": participants or [7, 8],
            "attachments": [], "forwarded_messages": [], "media_comment": None}


def thread(identity, messages=None, *, audience=None, private=False, subject="Existing subject"):
    audience = [8] if audience is None else audience
    messages = [message(101), message(102, author=7)] if messages is None else messages
    return {"id": identity, "subject": subject, "private": private, "audience": audience,
            "participants": [{"id": user, "name": f"User {user}"} for user in sorted({7, *audience})],
            "message_count": sum(not row["generated"] for row in messages), "messages": messages,
            "workflow_state": "unread", "context_code": None}


FORWARD = {"kind": "inbox_forward", "source_conversation_id": 10, "message_ids": [101],
           "recipients": [9, 11], "subject": "Forwarded notes", "body": "Please review these notes.",
           "group_conversation": True}
REPLY = {"kind": "inbox_reply_audience", "conversation_id": 10, "recipients": [8, 9],
         "body": "Reviewed reply", "included_message_ids": [101], "upload_action_ids": []}
ATTACHED = {"kind": "inbox_send_attachments", "recipients": [9], "subject": "My file",
            "body": "Please read the file.", "group_conversation": False, "upload_action_ids": ["upload-1"]}
FILE = {"id": 50, "actor_id": 7, "folder_id": 60, "filename": "notes.txt", "stored_filename": "notes.txt", "bytes": 4,
        "sha256": "a" * 64, "destination": "conversation_attachment"}


class API:
    def __init__(self):
        self.actor = 7
        self.requests = []
        self.threads = {10: thread(10)}
        self.unmessageable = set()
        self.search_results = [{"id": 8, "name": "Student Eight", "full_name": "Student Eight"}]
        self.search_page_size = 100
        self.search_link = None
        self.exact_override = None
        self.after_post = lambda state, rows: None
        self.ack_transform = lambda rows: rows
        self.write_error = None
        self.async_id = False
        self.readback_denied = False
        self.after_actor = None
        self.file_calls = 0
        self.enrollments = [{"id": 30, "course_id": 1, "user_id": 7, "type": "StudentEnrollment", "enrollment_state": "active"}]

    @property
    def writes(self):
        return [request for request in self.requests if request.method != "GET"]

    def verify_files(self, canvas, files):
        self.file_calls += 1
        assert canvas is self.canvas
        assert files == [FILE]
        return copy.deepcopy(files)

    def handle(self, request):
        self.requests.append(request)
        assert request.url.host == "school.example"
        assert request.headers["authorization"] == "Bearer synthetic-token"
        path = request.url.path
        if path == "/api/v1/users/self/profile":
            actor = self.after_actor if self.writes and self.after_actor is not None else self.actor
            return httpx.Response(200, json={"id": actor})
        if path == "/api/v1/courses/1":
            return httpx.Response(200, json={"id": 1, "workflow_state": "available"})
        if path == "/api/v1/courses/1/enrollments":
            assert dict(request.url.params) == {"user_id": "7", "state[]": "active", "per_page": "100"}
            return httpx.Response(200, json=self.enrollments)
        if path == "/api/v1/search/recipients":
            assert request.method == "GET"
            assert request.url.params["per_page"] == "100"
            if "user_id" in request.url.params:
                user_id = int(request.url.params["user_id"])
                rows = [] if user_id in self.unmessageable else [{"id": user_id, "name": f"User {user_id}"}]
                if self.exact_override is not None:
                    rows = self.exact_override
                return httpx.Response(200, json=rows)
            page = request.url.params.get("page", "1")
            page = int(page.split(":")[-1])
            start = (page - 1) * self.search_page_size
            rows = self.search_results[start:start + self.search_page_size]
            headers = {}
            if start + self.search_page_size < len(self.search_results):
                headers["link"] = f'<{request.url.copy_set_param("page", f"bookmark:{page+1}")}>; rel="next"'
            if self.search_link:
                headers["link"] = self.search_link(request)
            return httpx.Response(200, json=rows, headers=headers)
        if request.method == "GET" and path.startswith("/api/v1/conversations/"):
            assert dict(request.url.params) == {"auto_mark_as_read": "false"}
            if self.writes and self.readback_denied:
                return httpx.Response(403, json={"error": "denied"})
            return httpx.Response(200, json=copy.deepcopy(self.threads[int(path.rsplit("/", 1)[-1])]))
        assert request.method == "POST"
        assert path == "/api/v1/conversations" or path == "/api/v1/conversations/10/add_message"
        assert "/add_recipients" not in path
        if self.write_error == "lost":
            raise httpx.ReadError("response lost", request=request)
        if self.write_error:
            return httpx.Response(self.write_error, json={"error": "failed"},
                headers={"location": "https://school.example/api/v1/conversations", "retry-after": "0"})
        body = json.loads(request.content)
        assert all(type(user_id) is str for user_id in body["recipients"])
        recipients = [int(value) for value in body["recipients"]]
        if path.endswith("/add_message"):
            assert set(body) <= {"body", "recipients", "included_messages", "attachment_ids"}
            row = self.threads[10]
            row["audience"] = sorted((set(row["audience"]) | set(recipients)) - {7})
            for selected in row["messages"]:
                if selected["id"] in body["included_messages"]:
                    selected["participating_user_ids"] = sorted(set(selected["participating_user_ids"]) | set(recipients) | {7})
            outgoing = message(201, author=7, participants=sorted(set(recipients) | {7}), body=body["body"])
            row["messages"].append(outgoing)
            row["message_count"] += 1
            self.set_files(outgoing, body)
            row["participants"] = [{"id": user} for user in sorted(set(row["audience"]) | {7})]
            ack = copy.deepcopy(row)
            ack["messages"] = [copy.deepcopy(outgoing)]
            if self.async_id:
                ack["messages"][0]["id"] = 0
            self.after_post(self, [row])
            return httpx.Response(200, json=self.ack_transform(ack))
        assert body["mode"] == "sync"
        direct = body["group_conversation"] and not body["force_new"]
        groups = [recipients] if direct else [[user] for user in recipients]
        rows, acknowledgements = [], []
        for index, users in enumerate(groups):
            outgoing = message(201 + index, author=7, body=body["body"], participants=sorted(set(users) | {7}))
            self.set_files(outgoing, body)
            if "forwarded_message_ids" in body:
                assert body["from_conversation_id"] == 10
                outgoing["forwarded_messages"] = [copy.deepcopy(item) for item in self.threads[10]["messages"]
                    if item["id"] in body["forwarded_message_ids"]]
            row = thread(20 + index, [outgoing], audience=sorted(set(users) - {7}), subject=body["subject"])
            indirect = {item["author_id"] for item in outgoing["forwarded_messages"]} - set(users) - {7}
            row["participants"].extend({"id": user} for user in indirect)
            self.threads[row["id"]] = row
            rows.append(row)
            ack = copy.deepcopy(row)
            if not direct:
                del ack["messages"]  # native batch summary
            acknowledgements.append(ack)
        self.after_post(self, rows)
        return httpx.Response(201, json=self.ack_transform(acknowledgements))

    @staticmethod
    def set_files(outgoing, body):
        if body.get("attachment_ids"):
            assert body["attachment_ids"] == [50]
            outgoing["attachments"] = [{"id": 50, "size": 4, "filename": "notes.txt", "display_name": "notes.txt", "content-type": "text/plain"}]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("Only synthetic Inbox MockTransport is allowed")
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny)


@pytest.fixture
def api():
    state = API()
    with Canvas(CanvasConfig("https://school.example", "synthetic-token"),
                http_client=httpx.Client(transport=httpx.MockTransport(state.handle))) as canvas:
        state.canvas = canvas
        yield state


def run(api, action=FORWARD, files=None):
    target = im.snapshot(api.canvas, action, files=files)
    assert not api.writes
    assert im.snapshot(api.canvas, action, files=files) == target
    return im.perform(api.canvas, action, target)


@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("recipients", [[9], [9, 11], [7]])
def test_native_forward_preserves_exact_content_and_indirect_authors_are_not_audience(api, group, recipients):
    result = run(api, {**FORWARD, "group_conversation": group, "recipients": recipients})
    assert result["verified"]
    rows = result["conversations"]
    assert len(rows) == (1 if group else len(recipients))
    assert all(row["message"]["forwarded_messages"][0]["author_id"] == 8 for row in rows)
    assert all(8 not in row["audience"] for row in rows)
    assert len(api.writes) == 1
    payload = json.loads(api.writes[0].content)
    assert payload["force_new"] is not group and payload["forwarded_message_ids"] == [101]
    assert all(row["workflow_state"] == "unread" for row in api.threads.values())


def test_native_forward_nested_media_and_attachment_metadata_omit_signed_urls(api):
    nested = message(99, author=12, participants=[8, 12])
    nested["media_comment"] = {"media_id": "m-1", "media_type": "video", "display_name": "Clip", "url": "https://cdn.invalid/secret?token=opaque"}
    nested["attachments"] = [{"id": 88, "size": 7, "display_name": "report.txt", "filename": "report.txt", "url": "https://cdn.invalid/secret?token=opaque"}]
    api.threads[10]["messages"][0]["forwarded_messages"] = [nested]
    result = run(api)
    assert result["verified"] and "opaque" not in json.dumps(result)
    assert result["conversations"][0]["message"]["forwarded_messages"][0]["forwarded_messages"][0]["media_comment"]["media_id"] == "m-1"


def test_selective_reply_grants_only_reviewed_history_and_persists_membership(api):
    target = im.snapshot(api.canvas, REPLY)
    assert target["delivery"]["new_group_participants"] == [9]
    result = im.perform(api.canvas, REPLY, target)
    assert result["verified"] and result["audience"] == [8, 9]
    assert result["message"]["participating_user_ids"] == [7, 8, 9]
    assert result["coverage"]["complete"] is False
    assert result["coverage"]["recipient_active_history_copies_verified"] is False
    assert api.threads[10]["messages"][0]["participating_user_ids"] == [7, 8, 9]
    assert api.threads[10]["messages"][1]["participating_user_ids"] == [7, 8]


@pytest.mark.parametrize("private", [False, True])
def test_selective_subset_reply_keeps_unchosen_existing_members_but_not_delivery(api, private):
    api.threads[10]["private"] = private
    result = run(api, {**REPLY, "recipients": [8], "included_message_ids": []})
    assert result["verified"] and result["message"]["participating_user_ids"] == [7, 8]
    assert "coverage" not in result


def test_participating_user_ids_are_treated_as_identities_not_active_copy_evidence(api):
    api.threads[10]["messages"][0]["participating_user_ids"] += [8]
    assert run(api, REPLY)["verified"]


def test_course_selective_reply_checks_current_student_and_all_current_roles(api):
    api.threads[10]["context_code"] = "course_1"
    assert run(api, REPLY)["verified"]
    assert any(request.url.path.endswith("/enrollments") for request in api.requests)


@pytest.mark.parametrize("role", ["TeacherEnrollment", "TaEnrollment"])
def test_instructor_or_mixed_roles_cannot_trigger_native_audience_broadening(api, role):
    api.threads[10]["context_code"] = "course_1"
    api.enrollments.append({**api.enrollments[0], "id": 31, "type": role})
    with pytest.raises(ValueError, match="student role"):
        im.snapshot(api.canvas, REPLY)
    assert not api.writes


def test_selected_history_explicitly_reviews_native_original_author_regrant(api):
    action = {**REPLY, "recipients": [9]}
    target = im.snapshot(api.canvas, action)
    assert target["delivery"]["included_history_grants"] == [{"message_id": 101,
        "selected_recipients": [9], "original_author_id": 8, "may_restore_original_author_copy": True}]
    result = im.perform(api.canvas, action, target)
    assert result["verified"] and result["message"]["participating_user_ids"] == [7, 9]
    assert result["included_history_grants"] == target["delivery"]["included_history_grants"]
    assert result["coverage"]["recipient_active_history_copies_verified"] is False


def test_ambiguous_account_context_cannot_hide_instructor_audience_expansion(api):
    api.threads[10]["context_code"] = "account_1"
    with pytest.raises(ValueError, match="mask a course instructor delivery policy"):
        im.snapshot(api.canvas, REPLY)
    assert not api.writes
    # A new forward has no instructor-dependent course reply branch.
    assert run(api, FORWARD)["verified"]


def test_original_author_newly_selected_for_membership_has_regrant_review(api):
    api.threads[10]["audience"] = []
    api.threads[10]["participants"] = [{"id": 7}]
    action = {**REPLY, "recipients": [8]}
    target = im.snapshot(api.canvas, action)
    assert target["delivery"]["included_history_grants"][0]["may_restore_original_author_copy"] is True
    assert im.perform(api.canvas, action, target)["verified"]


@pytest.mark.parametrize("action", [FORWARD, REPLY])
@pytest.mark.parametrize("case", ["unmessageable", "different_user", "mixed_self", "not_visible", "wrong_thread", "wrong_actor", "incomplete_count"])
def test_prepare_refuses_unproven_audience_identity_or_visible_history_without_writes(api, action, case):
    action = copy.deepcopy(action)
    if case == "unmessageable":
        api.unmessageable.add(action["recipients"][0])
    elif case == "different_user":
        api.exact_override = [{"id": 99, "name": "Different user"}]
    elif case == "mixed_self":
        action["recipients"] = [7, 8]
    elif case == "not_visible":
        action["message_ids" if action["kind"] == "inbox_forward" else "included_message_ids"] = [999]
    elif case == "wrong_thread":
        api.threads[10]["id"] = 99
    elif case == "wrong_actor":
        api.actor = 99
    else:
        api.threads[10]["message_count"] = 1
    with pytest.raises(ValueError):
        im.snapshot(api.canvas, action)
    assert not api.writes


@pytest.mark.parametrize("case", ["private", "cannot_reply"])
def test_reply_respects_private_membership_and_returned_permission(api, case):
    api.threads[10][case] = True
    with pytest.raises(ValueError):
        im.snapshot(api.canvas, REPLY)
    assert not api.writes


@pytest.mark.parametrize("field,value", [("recipients", [True]), ("recipients", ["8"]), ("recipients", [8, 8]),
    ("group_conversation", 1), ("group_conversation", "true"), ("message_ids", [101, 101]),
    ("message_ids", [0]), ("source_conversation_id", True), ("subject", " "), ("body", "\x00"),
    ("recipients", list(range(1, 22))), ("extra", "not permitted")])
def test_forward_model_is_strict_and_bounded(field, value):
    with pytest.raises(ValidationError):
        im.InboxForward.model_validate({**FORWARD, field: value})


@pytest.mark.parametrize("field", ["included_message_ids", "upload_action_ids"])
def test_reply_duplicate_selected_history_or_upload_receipts_rejected(field):
    value = [101, 101] if field == "included_message_ids" else ["upload", "upload"]
    with pytest.raises(ValidationError):
        im.InboxReplyAudience.model_validate({**REPLY, field: value})


@pytest.mark.parametrize("mutate", [
    lambda row: row.update(audience=[99]),
    lambda row: row.update(subject="Changed"),
    lambda row: row.update(private=True),
    lambda row: row["messages"][0].update(body="Changed"),
    lambda row: row["messages"][0].update(author_id=9),
    lambda row: row["messages"][0].update(participating_user_ids=[7, 9, 11, 99]),
    lambda row: row["messages"][0].update(forwarded_messages=[]),
    lambda row: row["messages"][0]["forwarded_messages"][0].update(body="Changed source"),
    lambda row: row["messages"][0]["forwarded_messages"][0].update(author_id=99),
    lambda row: row["messages"][0].update(media_comment={"media_id": "unexpected"}),
])
def test_wrong_forward_content_or_delivery_cannot_be_verified(api, mutate):
    api.after_post = lambda _, rows: mutate(rows[0])
    with pytest.raises(ValueError):
        run(api)
    assert len(api.writes) == 1


@pytest.mark.parametrize("case", ["unreviewed_history", "omitted_history", "wrong_audience", "async", "wrong_ack", "actor_change", "denied"])
def test_reply_postdispatch_uncertainty_never_reposts(api, case):
    if case == "unreviewed_history":
        api.after_post = lambda _, rows: rows[0]["messages"][1]["participating_user_ids"].append(9)
    elif case == "omitted_history":
        api.after_post = lambda _, rows: rows[0]["messages"][0].update(participating_user_ids=[7, 8])
    elif case == "wrong_audience":
        api.after_post = lambda _, rows: rows[0]["messages"][-1].update(participating_user_ids=[7, 8])
    elif case == "async":
        api.async_id = True
        def delay(_, rows):
            rows[0]["messages"].pop()
            rows[0]["message_count"] -= 1
        api.after_post = delay
    elif case == "wrong_ack":
        api.ack_transform = lambda row: {**row, "messages": [{"id": 999}]}
    elif case == "actor_change":
        api.after_actor = 99
    else:
        api.readback_denied = True
    with pytest.raises((ValueError, CanvasError)):
        run(api, REPLY)
    assert len(api.writes) == 1


@pytest.mark.parametrize("action", [FORWARD, REPLY])
@pytest.mark.parametrize("status", [302, 307, 308, 403, 429, 500, "lost"])
def test_mutations_do_not_follow_redirects_or_retry(api, action, status):
    api.write_error = status
    with pytest.raises((ValueError, CanvasError, httpx.TransportError)):
        run(api, action)
    assert len(api.writes) == 1


def test_automatic_reply_by_other_author_does_not_look_like_extra_authored_send(api):
    def auto(_, rows):
        rows[0]["messages"].append(message(999, author=9, body="Away", participants=[7, 9, 11]))
        rows[0]["message_count"] += 1
    api.after_post = auto
    assert run(api)["verified"]


def test_async_reply_is_confirmed_only_when_fresh_baseline_identifies_exact_new_message(api):
    api.async_id = True
    result = run(api, REPLY)
    assert result["verified"] and result["message"]["id"] == 201
    assert len(api.writes) == 1


@pytest.mark.parametrize("mode", ["depth", "count", "body", "messages_shape", "missing_attachments", "media_identity"])
def test_unbounded_or_incomplete_source_is_rejected_before_send(api, mode):
    original = api.threads[10]["messages"][0]
    if mode == "depth":
        current = original
        for index in range(6):
            current["forwarded_messages"] = [message(500 + index)]
            current = current["forwarded_messages"][0]
    elif mode == "count":
        original["forwarded_messages"] = [message(500 + index) for index in range(1000)]
    elif mode == "body":
        original["body"] = "x" * 256001
    elif mode == "messages_shape":
        api.threads[10]["messages"] = {}
    elif mode == "missing_attachments":
        original.pop("attachments")
    else:
        original["media_comment"] = {"media_type": "video"}
    with pytest.raises(ValueError):
        im.snapshot(api.canvas, FORWARD)
    assert not api.writes


def test_discovery_is_current_user_scoped_paginated_and_nonmarking(api):
    api.search_results = [{"id": number, "name": f"User {number}"} for number in range(1, 202)]
    result = im.read_recipients(api.canvas, search="User", context="course_1", offset=100, limit=100)
    assert len(result["data"]["recipients"]) == 100
    assert result["coverage"]["total"] == 201 and result["coverage"]["next_offset"] == 200
    assert not result["coverage"]["complete"] and not api.writes
    result = im.read_recipients(api.canvas, user_id=8, from_conversation_id=10)
    assert result["coverage"]["complete"] and result["data"]["recipients"][0]["id"] == 8


@pytest.mark.parametrize("kwargs", [{}, {"search": " "}, {"context": "course_1_students"},
    {"user_id": True}, {"user_id": 8, "search": "Eight"}, {"from_conversation_id": 10, "search": "Eight"},
    {"context": "https://evil.example"}, {"search": "a", "limit": 101}])
def test_discovery_rejects_ambiguous_or_unbounded_inputs(api, kwargs):
    with pytest.raises(ValueError):
        im.read_recipients(api.canvas, **kwargs)
    assert not api.writes


@pytest.mark.parametrize("change", ["path", "query", "size", "skip", "no_page"])
def test_recipient_pagination_cannot_broaden_search_or_skip_records(api, change):
    api.search_results = [{"id": number, "name": "User"} for number in range(1, 201)]
    def link(request):
        path = "/api/v1/courses/1/modules/2/items/3" if change == "path" else request.url.path
        query = dict(request.url.params)
        if change != "no_page":
            query["page"] = "3" if change == "skip" else "2"
        if change == "query":
            query["search"] = "broadened"
        if change == "size":
            query["per_page"] = "200"
        return f'<https://school.example{path}?{urlencode(query)}>; rel="next"'
    api.search_link = link
    with pytest.raises((ValueError, CanvasError)):
        im.read_recipients(api.canvas, search="User")
    assert len([r for r in api.requests if r.url.path.endswith("/recipients")]) == 1
    assert all("modules" not in r.url.path for r in api.requests)


@pytest.mark.parametrize("action", [ATTACHED, {**REPLY, "upload_action_ids": ["upload-1"]}])
def test_receipt_files_are_validated_at_prepare_and_dispatch_then_independent_message_read(api, action, monkeypatch):
    monkeypatch.setitem(sys.modules, "canvas_connector.inbox_attachments", types.SimpleNamespace(verify_attachments=api.verify_files))
    result = run(api, action, files=[FILE])
    assert result["verified"] and api.file_calls == 3
    assert json.loads(api.writes[0].content)["attachment_ids"] == [50]


@pytest.mark.parametrize("case", ["no_receipt", "extra_file", "wrong_actor", "dropped", "wrong_size", "wrong_name"])
def test_attachment_receipt_or_native_filter_mismatch_never_reports_success(api, monkeypatch, case):
    monkeypatch.setitem(sys.modules, "canvas_connector.inbox_attachments", types.SimpleNamespace(verify_attachments=api.verify_files))
    files = [FILE]
    if case == "no_receipt":
        files = []
    elif case == "extra_file":
        files = [FILE, FILE]
    elif case == "wrong_actor":
        monkeypatch.setitem(sys.modules, "canvas_connector.inbox_attachments", types.SimpleNamespace(
            verify_attachments=lambda *_: [{**FILE, "actor_id": 99}]))
    elif case == "dropped":
        api.after_post = lambda _, rows: rows[0]["messages"][0].update(attachments=[])
    else:
        field, value = ("size", 5) if case == "wrong_size" else ("display_name", "changed.txt")
        api.after_post = lambda _, rows: rows[0]["messages"][0]["attachments"][0].update({field: value})
    with pytest.raises(ValueError):
        run(api, ATTACHED, files=files)
    assert len(api.writes) == (1 if case in {"dropped", "wrong_size", "wrong_name"} else 0)


@pytest.mark.parametrize("action", [FORWARD, REPLY])
def test_actual_mcp_prepare_wrong_executor_execute_coverage_and_replay(api, action):
    mcp = FastMCP("advanced-inbox")
    register_tools(mcp, lambda: api.canvas, lambda: None, owner=lambda: "alice")

    async def check():
        discovery = (await mcp.call_tool("canvas_inbox_recipients", {"user_id": 8, "from_conversation_id": 10}))[1]
        assert discovery["coverage"]["complete"] and discovery["data"]["recipients"][0]["id"] == 8
        prepared = (await mcp.call_tool("canvas_prepare_action", {"action": action}))[1]["data"]
        assert prepared["execute"]["tool"] == "canvas_send_message" and not api.writes
        count = len(api.requests)
        denied = (await mcp.call_tool("canvas_update_profile", {"action_id": prepared["action_id"]}))[1]
        assert denied["status"] == "invalid_request" and len(api.requests) == count
        result = (await mcp.call_tool("canvas_send_message", {"action_id": prepared["action_id"]}))[1]
        assert result["status"] == "succeeded" and result["data"]["result"]["verified"]
        if action["kind"] == "inbox_reply_audience":
            assert result["coverage"]["complete"] is False
            assert result["coverage"]["recipient_active_history_copies_verified"] is False
        count = len(api.requests)
        replay = (await mcp.call_tool("canvas_send_message", {"action_id": prepared["action_id"]}))[1]
        assert replay["data"] == result["data"] and len(api.requests) == count and len(api.writes) == 1

    asyncio.run(check())


@pytest.mark.parametrize("case", ["source_content", "audience", "messageability", "permission", "actor"])
def test_real_action_store_rejects_stale_prepared_targets_before_dispatch(api, case):
    store = ActionStore()
    prepared = store.prepare(api.canvas, "alice", REPLY)
    if case == "source_content":
        api.threads[10]["messages"][0]["body"] = "Changed after review"
    elif case == "audience":
        api.threads[10]["audience"].append(12)
        api.threads[10]["participants"].append({"id": 12})
    elif case == "messageability":
        api.unmessageable.add(9)
    elif case == "permission":
        api.threads[10]["cannot_reply"] = True
    else:
        api.actor = 99
    result = store.execute(api.canvas, "alice", prepared["action_id"], lambda: None)
    assert result["state"] == "rejected" and result["result"]["upstream_write_attempted"] is False
    assert not api.writes


def test_real_persistent_receipt_keeps_uncertain_forward_terminal_without_replay(api, tmp_path):
    database = Store(tmp_path / "inbox.db", Fernet.generate_key())
    try:
        store = ActionStore(persistence=database)
        prepared = store.prepare(api.canvas, "alice", FORWARD)
        api.write_error = "lost"
        result = store.execute(api.canvas, "alice", prepared["action_id"], lambda: None)
        assert result["state"] == "uncertain" and not result["result"]["verified"]
        count = len(api.requests)
        reloaded = ActionStore(persistence=database)
        assert reloaded.execute(api.canvas, "alice", prepared["action_id"], lambda: None) == result
        assert len(api.requests) == count and len(api.writes) == 1
        with pytest.raises(ValueError):
            reloaded.status(api.canvas, "other-owner", prepared["action_id"])
    finally:
        database.close()
