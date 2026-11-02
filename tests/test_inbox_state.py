"""Synthetic native HTTP and MCP evidence for personal Inbox state changes."""
import asyncio
import copy
import hashlib
import json

import httpx
import pytest
from canvas_py import Canvas
from canvas_py._config import CanvasConfig
from cryptography.fernet import Fernet
from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from canvas_connector import inbox_state as inbox
from canvas_connector.actions import ActionStore
from canvas_connector.storage import Store
from canvas_connector.tools import register_tools

STAR = {"kind": "inbox_star_set", "conversation_id": 40, "starred": True}
SUB = {"kind": "inbox_subscription_set", "conversation_id": 40, "subscribed": False}
DELETE = {"kind": "inbox_messages_delete", "conversation_id": 40, "message_ids": [102]}
DELETE_ALL = {"kind": "inbox_conversation_delete", "conversation_id": 40}
T1, T2, T3 = (f"2026-01-0{day}T12:00:00+00:00" for day in (1, 2, 3))


def message(identity, author, date, *, generated=False):
    return {"id": identity, "author_id": author, "created_at": date, "generated": generated,
        "body": "Added a participant" if generated else f"Synthetic message {identity}",
        "participating_user_ids": [7, 8, 9], "attachments": [], "forwarded_messages": [], "media_comment": None}


class API:
    def __init__(self):
        self.calls = []
        self.actor = 7
        self.row = {"id": 40, "subject": "Synthetic study group", "workflow_state": "unread", "starred": False,
            "subscribed": True, "private": False, "message_count": 2, "last_message_at": T2,
            "last_authored_message_at": T2, "audience": [8, 9],
            "participants": [{"id": 7}, {"id": 8}, {"id": 9}], "visible": True,
            "messages": [message(103, 9, T3, generated=True), message(102, 7, T2), message(101, 8, T1)]}
        self.after_write = lambda: None
        self.ack_hook = lambda ack: ack
        self.skip_update = False
        self.write_status = None
        self.read_status = None
        self.read_status_after = None
        self.list_status = None
        self.list_target = False
        self.list_rows = {}
        self.list_links = {}

    @property
    def writes(self):
        return [request for request in self.calls if request.method != "GET"]

    def summary(self):
        return {key: copy.deepcopy(value) for key, value in self.row.items() if key != "messages"}

    def apply(self, request, body):
        if request.method == "PUT":
            assert set(body) == {"conversation"}
            changes = body["conversation"]
            assert len(changes) == 1 and set(changes) <= {"starred", "subscribed", "workflow_state"}
            if "workflow_state" in changes:  # The legacy archive action retains its original wire contract.
                self.row.update(changes)
                return
            assert all(type(value) is bool for value in changes.values())
            if "starred" in changes:
                self.row.update(changes)
            else:
                previous = self.row["subscribed"]
                self.row.update(changes)
                if previous != changes["subscribed"]:
                    if not changes["subscribed"] and self.row["workflow_state"] == "unread":
                        self.row["workflow_state"] = "read"
                    elif changes["subscribed"]:
                        human = [m for m in self.row["messages"] if not m["generated"]]
                        latest = max(human, key=lambda m: (m["created_at"], m["id"]))
                        if latest["created_at"] > self.row["last_message_at"]:
                            self.row["workflow_state"] = "unread"
                        self.row["last_message_at"] = latest["created_at"]
                        own = [m for m in human if m["author_id"] == 7]
                        self.row["last_authored_message_at"] = max(m["created_at"] for m in own) if own and (
                            self.row["last_authored_message_at"] is not None or latest["author_id"] == 7) else None
            return
        if request.method == "POST":
            assert set(body) == {"remove"}
            self.row["messages"] = [m for m in self.row["messages"] if m["id"] not in body["remove"]]
        else:
            assert request.method == "DELETE" and body is None
            self.row["messages"] = []
        human = [m for m in self.row["messages"] if not m["generated"]]
        if not human:
            self.row.update(messages=[], message_count=0, starred=False, last_message_at=None, last_authored_message_at=None)
            if self.row["workflow_state"] == "unread":
                self.row["workflow_state"] = "read"
        else:
            self.row["message_count"] = len(human)
            if self.row["subscribed"]:
                self.row["last_message_at"] = max(m["created_at"] for m in human)
            else:
                times = sorted([m["created_at"] for m in self.row["messages"]], reverse=True)
                newer = [t for t in times if t > self.row["last_message_at"]]
                self.row["last_message_at"] = (newer[0] if newer and len(times) != len(newer) else newer[-1] if newer else None)
            own = [m for m in human if m["author_id"] == 7]
            latest = max(human, key=lambda m: (m["created_at"], m["id"]))
            self.row["last_authored_message_at"] = max(m["created_at"] for m in own) if own and (
                self.row["last_authored_message_at"] is not None or latest["author_id"] == 7) else None

    def handle(self, request):
        self.calls.append(request)
        assert request.url.host == "school.example"
        assert request.headers["authorization"] == "Bearer synthetic-token"
        path = request.url.path
        if request.method == "GET" and path == "/api/v1/users/self/profile":
            assert not request.url.query
            return httpx.Response(200, json={"id": self.actor})
        if request.method == "GET" and path == "/api/v1/conversations/40":
            assert dict(request.url.params) == {"auto_mark_as_read": "false"}
            status = self.read_status_after if self.writes else self.read_status
            if status:
                return httpx.Response(status, json={"error": "not accessible"}, headers={"location": "/unsafe"})
            if self.row["message_count"] == 0:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json=copy.deepcopy(self.row))
        if request.method == "GET" and path == "/api/v1/conversations":
            assert self.writes, "Only independent deletion absence verification needs Inbox listing"
            assert request.url.params["per_page"] == "100"
            assert set(request.url.params) <= {"per_page", "scope", "page"}
            if self.list_status:
                return httpx.Response(self.list_status, json={"error": "unavailable"})
            scope, page = request.url.params.get("scope"), request.url.params.get("page", "1")
            rows = self.list_rows.get((scope, page), [{"id": 40}] if self.list_target else [])
            headers = {"link": self.list_links[(scope, page)]} if (scope, page) in self.list_links else {}
            return httpx.Response(200, json=rows, headers=headers)
        if request.method != "GET":
            assert path in {"/api/v1/conversations/40", "/api/v1/conversations/40/remove_messages"}
            assert not request.url.query
            if self.write_status == "transport":
                raise httpx.ReadTimeout("Synthetic transport loss", request=request)
            if self.write_status:
                return httpx.Response(self.write_status, json={"error": "rate limit" if self.write_status in {403, 429} else "failed"},
                    headers={"retry-after": "0", "location": "/replay"})
            body = json.loads(request.content) if request.content else None
            if body is not None:
                assert request.headers["content-type"] == "application/json"
            if not self.skip_update:
                self.apply(request, body)
            ack = self.ack_hook(self.summary())
            self.after_write()
            return httpx.Response(200, json=ack)
        raise AssertionError(f"Unexpected {request.method} {path}")


@pytest.fixture
def api():
    instance = API()
    with Canvas(CanvasConfig("https://school.example", "synthetic-token"),
                http_client=httpx.Client(transport=httpx.MockTransport(instance.handle))) as canvas:
        instance.canvas = canvas
        yield instance


def run(api, action):
    target = inbox.snapshot(api.canvas, action)
    return target, inbox.perform(api.canvas, action, target)


@pytest.mark.parametrize("action,field,value", [
    (STAR, "conversation_id", True), (STAR, "conversation_id", "40"), (STAR, "conversation_id", 0),
    (STAR, "starred", 1), (STAR, "starred", "false"), (SUB, "subscribed", None),
    (DELETE, "message_ids", []), (DELETE, "message_ids", [101, 101]),
    (DELETE, "message_ids", [True]), (DELETE, "message_ids", ["101"]),
    (DELETE, "message_ids", list(range(1, 22))), (DELETE_ALL, "extra", "unsafe"),
])
def test_strict_frozen_action_contract(action, field, value):
    with pytest.raises(ValidationError):
        inbox._ADAPTER.validate_python({**action, field: value})
    model = inbox._ADAPTER.validate_python(action)
    with pytest.raises(ValidationError):
        model.conversation_id = 99


def test_star_changes_only_personal_marker_with_independent_nonmarking_read(api):
    target, result = run(api, STAR)
    assert target["conversation"]["workflow_state"] == "unread"
    assert result["verified"] and result["own_view_only"]
    assert result["state"] == {**target["conversation"], "starred": True}
    assert result["remaining_message_ids"] == [101, 102, 103]
    assert len(api.writes) == 1 and api.writes[0].method == "PUT"
    assert json.loads(api.writes[0].content) == {"conversation": {"starred": True}}
    assert len([r for r in api.calls if r.url.path == "/api/v1/conversations/40" and r.method == "GET"]) == 2


@pytest.mark.parametrize("initial,expected", [("unread", "read"), ("read", "read"), ("archived", "archived")])
def test_unsubscribe_native_read_state_effect_preserves_timestamp(api, initial, expected):
    api.row["workflow_state"] = initial
    target, result = run(api, SUB)
    assert result["state"]["workflow_state"] == expected
    assert result["state"]["last_message_at"] == T2
    assert not result["state"]["subscribed"]
    assert "marks unread as read" in target["effect"]
    assert json.loads(api.writes[0].content) == {"conversation": {"subscribed": False}}


@pytest.mark.parametrize("initial,last,expected", [("archived", T1, "unread"), ("read", T1, "unread"),
    ("archived", T2, "archived"), ("read", T2, "read")])
def test_resubscribe_native_timestamp_and_unarchive_effect(api, initial, last, expected):
    api.row.update(workflow_state=initial, subscribed=False, last_message_at=last)
    target, result = run(api, {**SUB, "subscribed": True})
    assert result["state"]["workflow_state"] == expected
    assert result["state"]["last_message_at"] == T2
    assert target["expected_after"] == result["state"]
    assert "unarchive" in target["effect"]


@pytest.mark.parametrize("action", [STAR, SUB, {**SUB, "subscribed": True}])
def test_explicit_noop_remains_exactly_verified(api, action):
    field = "starred" if action["kind"] == "inbox_star_set" else "subscribed"
    api.row[field] = action[field]
    before = copy.deepcopy(api.row)
    _, result = run(api, action)
    assert result["verified"] and api.row == before


@pytest.mark.parametrize("action,setup,match", [
    (SUB, {"private": True}, "group conversations"),
    ({**SUB, "subscribed": True}, {"subscribed": False, "last_message_at": None}, "sent-only"),
    (DELETE, {"last_message_at": None}, "sent-only"),
    ({**DELETE, "message_ids": [999]}, {}, "currently visible"),
])
def test_unsupported_or_wrong_message_context_refused_before_write(api, action, setup, match):
    api.row.update(setup)
    with pytest.raises(ValueError, match=match):
        inbox.snapshot(api.canvas, action)
    assert not api.writes


def test_unsubscribe_nil_timestamp_is_supported_without_cache_recalculation(api):
    api.row["last_message_at"] = None
    _, result = run(api, SUB)
    assert result["verified"] and result["state"]["last_message_at"] is None


@pytest.mark.parametrize("selected,expected,last,authored", [
    ([102], [101, 103], T1, None), ([101], [102, 103], T2, T2), ([103], [101, 102], T2, T2),
])
def test_delete_own_other_author_or_generated_message_only_from_own_view(api, selected, expected, last, authored):
    target, result = run(api, {**DELETE, "message_ids": selected})
    assert result["remaining_message_ids"] == expected
    assert result["state"]["last_message_at"] == last
    assert result["state"]["last_authored_message_at"] == authored
    assert result["state"]["workflow_state"] == "unread"
    assert {m["id"] for m in target["selected_messages"]} == set(selected)
    assert "other participants' copies" in target["effect"]
    assert api.writes[0].url.path == "/api/v1/conversations/40/remove_messages"
    assert json.loads(api.writes[0].content) == {"remove": selected}


@pytest.mark.parametrize("selected,last,expected", [([102], T2, T3), ([101], T1, T2), ([103], T3, None)])
def test_unsubscribed_partial_delete_matches_native_cached_timestamp_algorithm(api, selected, last, expected):
    api.row.update(subscribed=False, last_message_at=last)
    _, result = run(api, {**DELETE, "message_ids": selected})
    assert result["state"]["last_message_at"] == expected


@pytest.mark.parametrize("action", [DELETE_ALL, {**DELETE, "message_ids": [101, 102]}])
@pytest.mark.parametrize("workflow", ["unread", "read", "archived"])
def test_all_human_removal_cleans_generated_events_and_requires_independent_absence(api, action, workflow):
    api.row.update(workflow_state=workflow, starred=True)
    target, result = run(api, action)
    assert result["verified"] and result["remaining_message_ids"] == []
    assert result["state"] == {**target["conversation"], "message_count": 0, "starred": False,
        "last_message_at": None, "last_authored_message_at": None,
        "workflow_state": "read" if workflow == "unread" else workflow}
    assert target["generated_cleanup_ids"] == ([103] if action["kind"] == "inbox_messages_delete" else [])
    lists = [r for r in api.calls if r.url.path == "/api/v1/conversations"]
    assert [r.url.params.get("scope") for r in lists] == ["inbox", "archived", "sent"]
    if action == DELETE_ALL:
        assert api.writes[0].method == "DELETE" and not api.writes[0].content
        assert "arriving before" in target["effect"]


def test_delete_whole_sent_only_conversation_is_supported(api):
    api.row["last_message_at"] = None
    _, result = run(api, DELETE_ALL)
    assert result["verified"]


@pytest.mark.parametrize("change", [
    {"id": 41}, {"id": "40"}, {"message_count": 3}, {"message_count": True}, {"private": "false"},
    {"workflow_state": "deleted"}, {"starred": None}, {"subscribed": 1}, {"audience": [7, 8]},
    {"audience": [8, 8]}, {"audience": [8, 99]}, {"participants": [{"id": 8}, {"id": 9}]},
    {"last_message_at": "2026-01-01"}, {"messages": []}, {"messages": None},
])
def test_native_identity_access_and_completeness_fail_closed(api, change):
    api.row.update(change)
    with pytest.raises(ValueError):
        inbox.snapshot(api.canvas, STAR)
    assert not api.writes


@pytest.mark.parametrize("field,value", [("id", 103), ("author_id", True), ("generated", None),
    ("participating_user_ids", [8, 9]), ("participating_user_ids", [7, 7]),
    ("created_at", None), ("body", None), ("attachments", None), ("forwarded_messages", None),
    ("media_comment", "unknown")])
def test_unknown_or_conflicting_message_shapes_refused(api, field, value):
    api.row["messages"][1][field] = value
    with pytest.raises(ValueError):
        inbox.snapshot(api.canvas, STAR)
    assert not api.writes


def test_forwarded_authors_are_not_added_to_delivery_audience_and_capabilities_are_omitted(api):
    forwarded = message(500, 99, T1)
    forwarded["participating_user_ids"] = [99, 100]
    forwarded["attachments"] = [{"id": 700, "size": 25, "filename": "notes.txt", "display_name": "notes.txt",
        "content-type": "text/plain", "url": "https://cdn.example/PRIVATE_PATH?verifier=SECRET"}]
    api.row["messages"][1]["forwarded_messages"] = [forwarded]
    api.row["participants"].append({"id": 99, "avatar_url": "https://cdn.example/PRIVATE_AVATAR"})
    target, result = run(api, STAR)
    assert target["conversation"]["audience"] == result["state"]["audience"] == [8, 9]
    assert "PRIVATE" not in json.dumps(target) and "SECRET" not in json.dumps(target)


def test_bounded_message_tree_and_body_limits(api):
    root = api.row["messages"][1]
    for n in range(7):
        child = message(500 + n, 99, T1)
        root["forwarded_messages"] = [child]
        root = child
    with pytest.raises(ValueError, match="unbounded message tree"):
        inbox.snapshot(api.canvas, STAR)
    api.row["messages"][1]["forwarded_messages"] = []
    api.row["messages"][1]["body"] = "é" * (1024 * 1024)
    with pytest.raises(ValueError, match="bounded review"):
        inbox.snapshot(api.canvas, STAR)


@pytest.mark.parametrize("change", [
    lambda api: api.row.update(starred=False), lambda api: api.row.update(workflow_state="read"),
    lambda api: api.row.update(audience=[8]), lambda api: api.row["messages"][1].update(body="Changed after write"),
    lambda api: api.row["messages"][1].update(author_id=8), lambda api: setattr(api, "actor", 8),
])
def test_independent_star_readback_detects_state_content_audience_and_actor_changes(api, change):
    target = inbox.snapshot(api.canvas, STAR)
    api.after_write = lambda: change(api)
    with pytest.raises(ValueError):
        inbox.perform(api.canvas, STAR, target)
    assert len(api.writes) == 1


@pytest.mark.parametrize("change", [{"id": 99}, {"message_count": 1}, {"starred": False}, {"subscribed": False}])
def test_acknowledgement_must_also_match_exact_native_state(api, change):
    target = inbox.snapshot(api.canvas, STAR)
    api.ack_hook = lambda ack: {**ack, **change}
    with pytest.raises(ValueError):
        inbox.perform(api.canvas, STAR, target)
    assert len(api.writes) == 1


@pytest.mark.parametrize("mode", ["bare404", "list403", "still-listed", "read403", "read200", "actor", "malformed-list"])
def test_zero_message_deletion_never_accepts_bare_404_or_lost_access(api, mode):
    target = inbox.snapshot(api.canvas, DELETE_ALL)
    if mode == "bare404":
        api.skip_update = True
        api.read_status_after = 404
    elif mode == "list403":
        api.list_status = 403
    elif mode == "still-listed":
        api.list_target = True
    elif mode == "read403":
        api.read_status_after = 403
    elif mode == "read200":
        before = copy.deepcopy(api.row)
        api.after_write = lambda: api.row.update(before)
    elif mode == "actor":
        api.after_write = lambda: setattr(api, "actor", 8)
    else:
        api.list_rows[("inbox", "1")] = {"unknown": []}
    with pytest.raises(Exception):
        inbox.perform(api.canvas, DELETE_ALL, target)
    assert len(api.writes) == 1


def test_bounded_paginated_absence_verification(api):
    api.list_rows[("inbox", "1")] = [{"id": 50}]
    api.list_rows[("inbox", "2")] = [{"id": 51}]
    api.list_links[("inbox", "1")] = '<https://school.example/api/v1/conversations?per_page=100&scope=inbox&page=2>; rel="next"'
    _, result = run(api, DELETE_ALL)
    assert result["verified"]
    assert len([r for r in api.calls if r.url.path == "/api/v1/conversations"]) == 4


@pytest.mark.parametrize("next_url", [
    "conversations/40?auto_mark_as_read=true", "conversations?per_page=100&scope=inbox&page=3",
    "conversations?page=2&scope=inbox", "conversations?per_page=100&page=2&scope=unread",
    "conversations?per_page=100&scope=inbox&page=2&filter[]=user_123", "conversations?per_page=100&page=2",
])
def test_absence_pagination_cannot_change_path_scope_or_skip_records(api, next_url):
    api.list_rows[("inbox", "1")] = [{"id": 50}]
    api.list_links[("inbox", "1")] = f'<https://school.example/api/v1/{next_url}>; rel="next"'
    with pytest.raises(Exception):
        run(api, DELETE_ALL)
    assert len([r for r in api.calls if r.url.path == "/api/v1/conversations"]) == 1


@pytest.mark.parametrize("rows", [[{"id": 50}, {"id": 50}], [{"id": x} for x in range(100, 1101)]])
def test_duplicate_or_oversized_list_cannot_prove_absence(api, rows):
    api.list_rows[("inbox", "1")] = rows
    with pytest.raises(ValueError):
        run(api, DELETE_ALL)


@pytest.mark.parametrize("status", [301, 302, 307, 308, 403, 429, 500, "transport"])
@pytest.mark.parametrize("action", [STAR, DELETE, DELETE_ALL])
def test_mutations_never_automatically_retry_or_redirect(api, status, action):
    target = inbox.snapshot(api.canvas, action)
    api.write_status = status
    with pytest.raises(Exception):
        inbox.perform(api.canvas, action, target)
    assert len(api.writes) == 1


def test_show_redirect_is_never_followed_or_used_as_empty_view(api):
    api.read_status = 302
    with pytest.raises(Exception):
        inbox.snapshot(api.canvas, STAR)
    assert len(api.calls) == 2 and not api.writes


def test_actual_mcp_routes_prepare_execute_and_receipt_replay(api):
    mcp = FastMCP("Synthetic Inbox")
    register_tools(mcp, lambda: api.canvas, lambda: None, owner=lambda: "alice")

    async def exercise():
        prepared = (await mcp.call_tool("canvas_prepare_action", {"action": STAR}))[1]["data"]
        action_id = prepared["action_id"]
        assert prepared["execute"]["tool"] == "canvas_update_inbox"
        result = (await mcp.call_tool("canvas_update_inbox", {"action_id": action_id}))[1]["data"]
        assert result["state"] == "succeeded"
        again = (await mcp.call_tool("canvas_update_inbox", {"action_id": action_id}))[1]["data"]
        assert again == result
        prepared = (await mcp.call_tool("canvas_prepare_action", {"action": DELETE_ALL}))[1]["data"]
        assert prepared["execute"]["tool"] == "canvas_delete_inbox_content"
        wrong = (await mcp.call_tool("canvas_update_inbox", {"action_id": prepared["action_id"]}))[1]
        assert wrong["status"] == "invalid_request" and len(api.writes) == 1
        deleted = (await mcp.call_tool("canvas_delete_inbox_content", {"action_id": prepared["action_id"]}))[1]["data"]
        assert deleted["state"] == "succeeded"
        again = (await mcp.call_tool("canvas_delete_inbox_content", {"action_id": prepared["action_id"]}))[1]["data"]
        assert again == deleted

    asyncio.run(exercise())
    assert len(api.writes) == 2


@pytest.mark.parametrize("change", [
    lambda api: api.row.update(starred=True), lambda api: api.row["messages"][1].update(body="New revision"),
    lambda api: api.row["messages"][1].update(participating_user_ids=[7, 8]),
    lambda api: api.row.update(audience=[8]), lambda api: setattr(api, "actor", 8),
])
def test_action_store_rejects_stale_review_before_dispatch(api, change):
    store = ActionStore()
    prepared = store.prepare(api.canvas, "alice", STAR)
    change(api)
    result = store.execute(api.canvas, "alice", prepared["action_id"], lambda: None)
    assert result["state"] == "rejected"
    assert result["result"]["upstream_write_attempted"] is False
    assert not api.writes


@pytest.mark.parametrize("action", [STAR, SUB, DELETE, DELETE_ALL])
def test_uncertain_receipt_is_terminal_and_never_replays(api, action):
    store = ActionStore()
    prepared = store.prepare(api.canvas, "alice", action)
    api.read_status_after = 403
    result = store.execute(api.canvas, "alice", prepared["action_id"], lambda: None)
    assert result["state"] == "uncertain" and not result["result"]["verified"]
    assert result["result"]["upstream_write_attempted"] is True
    assert store.execute(api.canvas, "alice", prepared["action_id"], lambda: None) == result
    assert len(api.writes) == 1


def test_persisted_legacy_inbox_update_payload_snapshot_fingerprint_and_replay_remain_unchanged(api, tmp_path):
    payload = {"kind": "inbox_update", "conversation_id": 40, "workflow_state": "archived"}
    old_snapshot = {"id": 40, "subject": "Synthetic study group", "workflow_state": "unread",
        "audience": [8, 9], "participants": [{"id": 7}, {"id": 8}, {"id": 9}]}
    database = Store(tmp_path / "legacy-state.db", Fernet.generate_key())
    try:
        store = ActionStore(persistence=database)
        prepared = store.prepare(api.canvas, "alice", payload)
        action_id = prepared["action_id"]
        assert prepared["review"]["target"] == old_snapshot
        assert {key: value for key, value in prepared["review"].items() if key not in {"target", "canvas_origin"}} == payload
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256((store._identity(api.canvas, "alice") + "\0" + encoded).encode()).hexdigest()
        assert database.get("canvas_action_payload", action_id)["payload"] == encoded
        assert database.get("canvas_action", action_id)["fingerprint"] == fingerprint
        assert database.get("canvas_action", action_id)["snapshot"] == old_snapshot
        current = ActionStore(persistence=database)
        count = len(api.calls)
        assert current.prepare(api.canvas, "alice", payload)["action_id"] == action_id
        assert len(api.calls) == count
        result = current.execute(api.canvas, "alice", action_id, lambda: None)
        assert result["state"] == "succeeded"
        count = len(api.calls)
        assert ActionStore(persistence=database).execute(api.canvas, "alice", action_id, lambda: None) == result
        assert len(api.calls) == count and len(api.writes) == 1
    finally:
        database.close()
