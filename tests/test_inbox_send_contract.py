"""Current Canvas create/batch semantics and exact non-marking send readback."""
import asyncio
import copy
import hashlib
import json
import socket

import httpx
import pytest
from canvas_py import Canvas
from canvas_py._config import CanvasConfig
from cryptography.fernet import Fernet
from mcp.server.fastmcp import FastMCP

from canvas_connector.actions import ActionStore
from canvas_connector.storage import Store
from canvas_connector.tools import register_tools

ACTION = {"kind": "inbox_send", "recipients": [8, 9], "subject": "Study notes", "body": "Reviewed message.", "group_conversation": False}


class API:
    def __init__(self):
        self.requests = []
        self.actor = 7
        self.threads = {}
        self.write_error = None
        self.after_create = lambda rows: None
        self.ack_transform = lambda rows: rows
        self.after_profile = None
        self.hide_readback = False
        self.force_batch = False

    @property
    def writes(self):
        return [request for request in self.requests if request.method == "POST"]

    def handle(self, request):
        self.requests.append(request)
        assert request.url.host == "school.example"
        assert request.headers["authorization"] == "Bearer synthetic-token"
        path = request.url.path
        if request.method == "GET" and path == "/api/v1/users/self/profile":
            actor = self.after_profile if self.writes and self.after_profile is not None else self.actor
            return httpx.Response(200, json={"id": actor})
        if request.method == "POST":
            assert path == "/api/v1/conversations"
            if self.write_error == "network":
                raise httpx.ReadError("response lost after possible send", request=request)
            if self.write_error is not None:
                return httpx.Response(self.write_error, json={"error": "rate limit"},
                    headers={"location": "https://school.example/api/v1/conversations", "retry-after": "0"})
            body = json.loads(request.content)
            assert set(body) == {"recipients", "subject", "body", "force_new", "group_conversation", "mode"}
            assert body["mode"] == "sync"
            # Native create rejects JSON integer recipient values.
            if any(type(recipient) is not str for recipient in body["recipients"]):
                return httpx.Response(400, json={"error": "recipients invalid"})
            recipient_ids = [int(recipient) for recipient in body["recipients"]]
            direct_group = body["group_conversation"] and not body["force_new"] and not self.force_batch
            groups = [recipient_ids] if direct_group else [[recipient] for recipient in recipient_ids]
            acknowledged = []
            for index, recipients in enumerate(groups):
                identity = 30 + index
                participants = sorted(set(recipients) | {self.actor})
                message = {"id": 100 + index, "author_id": self.actor, "body": body["body"],
                    "generated": False, "created_at": "2026-01-01T00:00:00Z", "media_comment": None,
                    "attachments": [], "forwarded_messages": [], "participating_user_ids": participants}
                row = {"id": identity, "subject": body["subject"], "private": False, "workflow_state": "unread",
                    "audience": [user for user in participants if user != self.actor],
                    "participants": [{"id": user, "name": f"User {user}"} for user in participants],
                    "messages": [message]}
                self.threads[identity] = row
                ack = copy.deepcopy(row)
                if not direct_group:
                    # ConversationBatch responses have no messages field.
                    del ack["messages"]
                acknowledged.append(ack)
            self.after_create(list(self.threads.values()))
            return httpx.Response(201, json=self.ack_transform(acknowledged))
        if request.method == "GET" and path.startswith("/api/v1/conversations/"):
            assert request.url.params.get("auto_mark_as_read") == "false"
            assert set(request.url.params) == {"auto_mark_as_read"}
            if self.hide_readback:
                return httpx.Response(403, json={"error": "denied"})
            identity = int(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=copy.deepcopy(self.threads[identity]))
        raise AssertionError((request.method, path))


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("Inbox tests must use synthetic MockTransport")
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fail)


@pytest.fixture
def api():
    state = API()
    with Canvas(CanvasConfig("https://school.example", "synthetic-token"),
                http_client=httpx.Client(transport=httpx.MockTransport(state.handle))) as canvas:
        state.canvas = canvas
        yield state


def run(api, action=ACTION):
    store = ActionStore()
    prepared = store.prepare(api.canvas, "alice", action)
    assert not api.requests  # Historical preparation target remains {}.
    result = store.execute(api.canvas, "alice", prepared["action_id"], lambda: None)
    return store, prepared, result


@pytest.mark.parametrize("group", [True, False])
@pytest.mark.parametrize("recipients", [[8], [8, 9], [7]])
def test_native_group_or_individual_delivery_is_exact_and_never_marks_read(api, group, recipients):
    action = {**ACTION, "group_conversation": group, "recipients": recipients}
    store, prepared, result = run(api, action)
    assert result["state"] == "succeeded" and result["result"]["verified"]
    body = json.loads(api.writes[0].content)
    assert body["recipients"] == [str(user) for user in recipients]
    assert body["force_new"] is (not group)
    assert len(result["result"]["conversations"]) == (1 if group else len(recipients))
    assert all(row["workflow_state"] == "unread" for row in api.threads.values())
    count = len(api.requests)
    assert store.execute(api.canvas, "alice", prepared["action_id"], lambda: None) == result
    assert len(api.requests) == count and len(api.writes) == 1


def test_native_batch_summary_without_message_ids_is_verified_by_independent_read(api):
    _, _, result = run(api)
    assert result["state"] == "succeeded"
    assert {row["messages"][0]["id"] for row in result["result"]["conversations"]} == {100, 101}


def test_group_request_returning_separate_threads_is_uncertain(api):
    api.force_batch = True
    _, _, result = run(api, {**ACTION, "group_conversation": True})
    assert result["state"] == "uncertain" and len(api.writes) == 1


@pytest.mark.parametrize("mutate", [
    lambda row: row.update(audience=[99]),
    lambda row: row.update(audience=[8, 99]),
    lambda row: row.update(subject="Different subject"),
    lambda row: row.update(private=True),
    lambda row: row.update(id=999),
    lambda row: row.update(participants=[{"id": 8}]),
    lambda row: row["participants"].append({"id": 99}),
    lambda row: row["messages"][0].update(author_id=8),
    lambda row: row["messages"][0].update(body="Different message"),
    lambda row: row["messages"][0].update(generated=True),
    lambda row: row["messages"][0].update(attachments=[{"id": 99}]),
    lambda row: row["messages"][0].update(forwarded_messages=[{"id": 99}]),
    lambda row: row["messages"][0].update(media_comment={"id": "unexpected-video"}),
    lambda row: row["messages"][0].update(participating_user_ids=[7, 8, 99]),
    lambda row: row["messages"][0].update(participating_user_ids=[8]),
    lambda row: row["messages"][0].pop("attachments"),
    lambda row: row["messages"][0].pop("participating_user_ids"),
    lambda row: row["messages"].append({**row["messages"][0], "id": 500}),
])
def test_wrong_identity_audience_content_or_unreviewed_attachment_never_reports_success(api, mutate):
    api.after_create = lambda rows: mutate(rows[0])
    store, prepared, result = run(api)
    assert result["state"] == "uncertain" and not result["result"]["verified"]
    assert store.execute(api.canvas, "alice", prepared["action_id"], lambda: None) == result
    assert len(api.writes) == 1


@pytest.mark.parametrize("transform", [lambda rows: rows[:1], lambda rows: rows + rows[:1],
    lambda rows: [rows[0], rows[0]], lambda rows: [], lambda rows: rows[0]])
def test_missing_extra_duplicate_or_nonarray_conversation_ack_is_uncertain(api, transform):
    api.ack_transform = transform
    _, _, result = run(api)
    assert result["state"] == "uncertain" and len(api.writes) == 1


def test_individual_threads_cannot_duplicate_one_recipient_and_omit_another(api):
    def duplicate(rows):
        rows[1]["audience"] = rows[0]["audience"]
        rows[1]["participants"] = rows[0]["participants"]
        rows[1]["messages"][0]["participating_user_ids"] = [7, 8]
    api.after_create = duplicate
    _, _, result = run(api)
    assert result["state"] == "uncertain"


def test_group_ack_message_identity_must_match_independent_readback(api):
    def wrong_id(rows):
        rows[0]["messages"][0]["id"] = 999
        return rows
    api.ack_transform = wrong_id
    _, _, result = run(api, {**ACTION, "group_conversation": True})
    assert result["state"] == "uncertain"


@pytest.mark.parametrize("case", ["denied", "actor_changed", "async_placeholder"])
def test_unverifiable_readback_stays_uncertain(api, case):
    if case == "denied":
        api.hide_readback = True
    elif case == "actor_changed":
        api.after_profile = 99
    else:
        api.after_create = lambda rows: rows[0]["messages"][0].update(id=0)
    _, _, result = run(api)
    assert result["state"] == "uncertain" and len(api.writes) == 1


@pytest.mark.parametrize("status", [302, 307, 308, 403, 429, 500, "network"])
def test_send_is_never_retried_or_redirected_after_transport_uncertainty(api, status):
    api.write_error = status
    store, prepared, result = run(api)
    assert result["state"] == "uncertain"
    assert store.execute(api.canvas, "alice", prepared["action_id"], lambda: None) == result
    assert len(api.writes) == 1


@pytest.mark.parametrize("recipients", [[8, 8], [7, 8]])
def test_ambiguous_recipient_lists_do_not_dispatch(api, recipients):
    _, _, result = run(api, {**ACTION, "recipients": recipients})
    assert result["state"] != "succeeded" and not api.writes


def test_persisted_legacy_send_payload_snapshot_fingerprint_and_terminal_replay(api, tmp_path):
    database = Store(tmp_path / "legacy-inbox.db", Fernet.generate_key())
    try:
        old = ActionStore(persistence=database)
        prepared = old.prepare(api.canvas, "alice", ACTION)
        action_id = prepared["action_id"]
        encoded = json.dumps(ACTION, sort_keys=True, separators=(",", ":"))
        identity = old._identity(api.canvas, "alice")
        fingerprint = hashlib.sha256((identity + "\0" + encoded).encode()).hexdigest()
        assert database.get("canvas_action_payload", action_id)["payload"] == encoded
        assert database.get("canvas_action", action_id)["snapshot"] == {}
        assert database.get("canvas_action", action_id)["fingerprint"] == fingerprint
        current = ActionStore(persistence=database)
        assert current.prepare(api.canvas, "alice", ACTION)["action_id"] == action_id and not api.requests
        result = current.execute(api.canvas, "alice", action_id, lambda: None)
        assert result["state"] == "succeeded"
        count = len(api.requests)
        assert ActionStore(persistence=database).execute(api.canvas, "alice", action_id, lambda: None) == result
        assert len(api.requests) == count and len(api.writes) == 1
    finally:
        database.close()


def test_actual_mcp_executor_uses_native_send_contract(api):
    mcp = FastMCP("native-inbox")
    register_tools(mcp, lambda: api.canvas, lambda: None, owner=lambda: "alice")

    async def check():
        prepared = (await mcp.call_tool("canvas_prepare_action", {"action": {**ACTION, "group_conversation": True}}))[1]["data"]
        assert prepared["execute"]["tool"] == "canvas_send_message" and not api.writes
        result = (await mcp.call_tool("canvas_send_message", {"action_id": prepared["action_id"]}))[1]
        assert result["status"] == "succeeded" and result["data"]["result"]["verified"]
        assert len(result["data"]["result"]["conversations"]) == 1
        assert len(api.writes) == 1

    asyncio.run(check())
