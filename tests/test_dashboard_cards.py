"""Native dashboard ordering through real Canvas + MockTransport only."""

import asyncio
import copy
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

from canvas_connector import dashboard_cards as d
from canvas_connector.actions import ActionStore
from canvas_connector.storage import Store
from canvas_connector.tools import register_tools

ORIGIN = "https://school.example"
CARDS = "/api/v1/dashboard/dashboard_cards"
POSITIONS = "/api/v1/users/self/dashboard_positions"
PROFILE = "/api/v1/users/self/profile"
ACTION = {"kind": "dashboard_reorder", "course_ids": [3, 1, 2]}


def card(course_id, **changes):
    return {"id": course_id, "assetString": f"course_{course_id}", "shortName": f"Course {course_id}",
            "longName": f"Long course {course_id}", "originalName": f"Course {course_id}",
            "courseCode": f"C{course_id}", "enrollmentType": "StudentEnrollment", "enrollmentState": "active",
            "isFavorited": False, "isK5Subject": False, "isHomeroom": False, "published": True,
            "href": f"/courses/{course_id}", "image": "https://untrusted.invalid/image?token=never-fetch", **changes}


class Dashboard:
    def __init__(self):
        self.profile = {"id": 7, "k5_user": False}
        self.cards = [card(2), card(1), card(3)]
        self.positions = {"course_2": "-1", "course_1": "0", "group_19": " +7 "}
        self.requests = []
        self.errors = {}
        self.redirects = {}
        self.mutate_on = None
        self.after_write = None
        self.ignore_write = False
        self.ack_override = None
        self.card_override = None
        self.link = None
        self.put_error = None

    @property
    def writes(self):
        return [request for request in self.requests if request.method != "GET"]

    def ordered(self):
        result = []
        for item in self.cards:
            row = copy.deepcopy(item)
            saved = self.positions.get(row["assetString"])
            row["position"] = int(saved) if saved is not None else None
            result.append(row)
        return sorted(result, key=lambda row: 1001 if row["position"] is None else row["position"])

    def handler(self, request):
        assert request.url.host == "school.example"
        assert request.headers["authorization"] == "Bearer synthetic-token"
        assert not request.url.params
        self.requests.append(request)
        path = request.url.path
        if self.mutate_on:
            self.mutate_on(self, request)
        if request.method == "PUT":
            assert path == POSITIONS
            body = json.loads(request.content)
            assert set(body) == {"dashboard_positions"}
            assert all(key.startswith("course_") for key in body["dashboard_positions"])
            assert "group_19" not in body["dashboard_positions"]
            if self.put_error == "network":
                raise httpx.ReadError("synthetic response lost", request=request)
            if self.put_error is not None:
                return httpx.Response(self.put_error, json={"message": "rate limit"},
                                      headers={"Retry-After": "0", "Location": ORIGIN + POSITIONS})
            if not self.ignore_write:
                self.positions.update(body["dashboard_positions"])
            acknowledged = copy.deepcopy(self.positions if self.ack_override is None else self.ack_override)
            if self.after_write:
                self.after_write(self)
            return httpx.Response(200, json={"dashboard_positions": acknowledged})
        assert request.method == "GET" and request.content == b""
        if path in self.errors:
            return httpx.Response(self.errors[path], json={"message": "denied"})
        if path in self.redirects:
            return httpx.Response(302, headers={"Location": self.redirects[path]})
        if path == PROFILE:
            value = self.profile
        elif path == POSITIONS:
            value = {"dashboard_positions": self.positions}
        elif path == CARDS:
            value = self.ordered() if self.card_override is None else self.card_override
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=copy.deepcopy(value), headers={"Link": self.link} if path == CARDS and self.link else {})


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("Dashboard tests must use MockTransport")
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fail)


@contextmanager
def client(state, token="synthetic-token"):
    with (httpx.Client(transport=httpx.MockTransport(state.handler)) as http,
          Canvas(CanvasConfig(ORIGIN, token), http_client=http) as canvas):
        yield canvas


def test_read_reports_exact_native_card_order_missing_positions_and_legacy_strings():
    state = Dashboard()
    with client(state) as canvas:
        result = d.read_dashboard_cards(canvas)
    assert result["status"] == "ok" and result["coverage"]["complete"]
    assert result["coverage"]["all_enrollments"] is False
    assert result["data"]["course_ids"] == [2, 1, 3]
    assert result["data"]["dashboard_positions"] == state.positions
    assert result["data"]["cards"][-1]["position"] is None
    assert result["data"]["reorder_supported"] is True
    assert result["data"]["active_dashboard_view_verified"] is False
    assert "untrusted.invalid" not in json.dumps(result)
    assert {request.url.path for request in state.requests} == {PROFILE, CARDS, POSITIONS}
    assert not state.writes


def test_equal_and_missing_positions_keep_native_ties_without_alphabetic_reconstruction():
    state = Dashboard()
    state.positions = {"course_2": "0", "course_1": 0}
    state.cards[0]["shortName"] = "Z last alphabetically"
    state.cards[1]["shortName"] = "A first alphabetically"
    with client(state) as canvas:
        result = d.read_dashboard_cards(canvas)
    assert result["data"]["course_ids"] == [2, 1, 3]


def test_native_favorites_can_legitimately_exceed_fifty_cards():
    state = Dashboard()
    state.cards = [card(i) for i in range(1, 61)]
    state.positions = {}
    with client(state) as canvas:
        result = d.read_dashboard_cards(canvas)
    assert len(result["data"]["cards"]) == 60 and result["coverage"]["complete"]
    assert "favorites can exceed" in result["coverage"]["selection_note"]


def test_merge_changes_only_exact_requested_cards_and_independently_reads_everything_back():
    state = Dashboard()
    with client(state) as canvas:
        target = d.snapshot(canvas, ACTION)
        assert not state.writes
        result = d.perform(canvas, ACTION, target)
    assert result["verified"] and result["course_ids"] == [3, 1, 2]
    assert result["unrelated_positions_preserved"]
    assert result["dashboard_positions"] == {"course_2": 3, "course_1": 2, "course_3": 1, "group_19": " +7 "}
    assert len(state.writes) == 1
    assert json.loads(state.writes[0].content) == {"dashboard_positions": {"course_3": 1, "course_1": 2, "course_2": 3}}
    after = state.requests[state.requests.index(state.writes[0]) + 1:]
    assert [request.url.path for request in after] == [PROFILE, POSITIONS, CARDS, POSITIONS, PROFILE]


@pytest.mark.parametrize("ids", [[], [1, 1], [True, 2, 3], ["1", 2, 3], [0, 2, 3], [-1, 2, 3], [2**63, 2, 3], list(range(1, 1002))])
def test_typed_permutation_validation_happens_before_network(ids):
    state = Dashboard()
    with client(state) as canvas, pytest.raises(ValidationError):
        d.snapshot(canvas, {**ACTION, "course_ids": ids})
    assert not state.requests


@pytest.mark.parametrize("extra", [{"user_id": 9}, {"positions": {}}, {"context_type": "group"}, {"kind": "course_reorder"}])
def test_action_cannot_select_another_owner_or_arbitrary_preference(extra):
    state = Dashboard()
    with client(state) as canvas, pytest.raises(ValidationError):
        d.snapshot(canvas, {**ACTION, **extra})
    assert not state.requests


@pytest.mark.parametrize("ids", [[1, 2], [1, 2, 99], [1, 2, 3, 4]])
def test_only_complete_fresh_native_card_permutations_are_allowed(ids):
    state = Dashboard()
    with client(state) as canvas, pytest.raises(ValueError, match="complete permutation"):
        d.snapshot(canvas, {**ACTION, "course_ids": ids})
    assert not state.writes


@pytest.mark.parametrize("change", [
    lambda state: state.profile.update(id=8),
    lambda state: state.profile.update(k5_user=True),
    lambda state: state.positions.update(group_19="8"),
    lambda state: state.cards.append(card(4)),
    lambda state: state.cards.pop(),
    lambda state: state.cards[0].update(shortName="Changed nickname"),
    lambda state: state.cards[0].update(enrollmentState="completed"),
])
def test_stale_actor_layout_cards_and_any_position_block_before_put(change):
    state = Dashboard()
    with client(state) as canvas:
        target = d.snapshot(canvas, ACTION)
        change(state)
        with pytest.raises(ValueError):
            d.perform(canvas, ACTION, target)
    assert not state.writes


@pytest.mark.parametrize("patch", [{"k5_user": True}, {"k5_user": None}, {"k5_user": 0}, {}])
def test_k5_or_unknown_profile_layout_returns_honest_read_handoff_and_refuses_write(patch):
    state = Dashboard()
    state.profile = {"id": 7, **patch}
    with client(state) as canvas:
        result = d.read_dashboard_cards(canvas)
        with pytest.raises(ValueError, match="non-K5"):
            d.snapshot(canvas, ACTION)
    assert result["coverage"]["complete"] is False
    assert result["data"]["reorder_supported"] is False and result["data"]["handoff"]
    assert not state.writes


@pytest.mark.parametrize("flag,value", [("isK5Subject", True), ("isHomeroom", True), ("isK5Subject", None), ("isHomeroom", 0)])
def test_course_k5_flags_do_not_override_current_users_confirmed_non_k5_mode(flag, value):
    state = Dashboard()
    state.cards[0][flag] = value
    with client(state) as canvas:
        result = d.read_dashboard_cards(canvas)
        target = d.snapshot(canvas, ACTION)
        saved = d.perform(canvas, ACTION, target)
    assert result["coverage"]["complete"] is True and result["data"]["layout"] == "native_cards"
    assert result["data"]["active_dashboard_view_verified"] is False
    assert saved["verified"] and saved["active_dashboard_view_verified"] is False
    assert target["cards"][0][flag] is (value if type(value) is bool else None)
    assert len(state.writes) == 1


@pytest.mark.parametrize("value", [-1001, 1001, True, 1.5, None, "", "1.5", "1e2", "0x10", "2oops"])
def test_invalid_saved_positions_cannot_be_silently_normalized(value):
    state = Dashboard()
    state.positions["group_19"] = value
    with client(state) as canvas, pytest.raises(ValueError):
        d.read_dashboard_cards(canvas)
    assert not state.writes


@pytest.mark.parametrize("value,card_position", [(" +001 ", 1), ("-1000", -1000), ("1000", 1000), (0, 0)])
def test_supported_legacy_position_strings_preserve_raw_map_value(value, card_position):
    state = Dashboard()
    state.positions = {"course_2": value}
    with client(state) as canvas:
        result = d.read_dashboard_cards(canvas)
    assert result["data"]["dashboard_positions"]["course_2"] == value
    assert result["data"]["cards"][0]["position"] == card_position


@pytest.mark.parametrize("bad", [[card(1, assetString="group_1")], [card(1), card(1)], {"cards": []}, [None], [card(True)]])
def test_malformed_duplicate_and_wrong_context_native_cards_are_unavailable(bad):
    state = Dashboard()
    state.positions = {}
    state.card_override = bad
    with client(state) as canvas, pytest.raises((ValueError, ValidationError)):
        d.read_dashboard_cards(canvas)
    assert not state.writes


def test_native_position_disagreement_or_wrong_sort_is_rejected():
    state = Dashboard()
    state.card_override = state.ordered()
    state.card_override[0]["position"] = 4
    with client(state) as canvas, pytest.raises(ValueError, match="disagree"):
        d.snapshot(canvas, ACTION)
    state.card_override = list(reversed(state.ordered()))
    with client(state) as canvas, pytest.raises(ValueError, match="native dashboard position order"):
        d.snapshot(canvas, ACTION)
    assert not state.writes


@pytest.mark.parametrize("kind", ["positions", "cards", "resulting_positions"])
def test_card_and_full_position_maps_are_bounded(kind):
    state = Dashboard()
    if kind == "cards":
        state.cards = [card(i) for i in range(1, 1002)]
        state.positions = {}
    else:
        state.positions = {f"group_{i}": 0 for i in range(1, 1001 + (kind == "positions"))}
    with client(state) as canvas, pytest.raises(ValueError, match="1000|bounded"):
        d.snapshot(canvas, ACTION)
    assert not state.writes


@pytest.mark.parametrize("change", [
    lambda state: state.profile.update(id=8),
    lambda state: state.profile.update(k5_user=True),
    lambda state: state.cards.append(card(4)),
    lambda state: state.cards[0].update(shortName="Concurrent change"),
    lambda state: state.positions.update(group_19=9),
    lambda state: state.positions.pop("group_19"),
    lambda state: state.positions.update(course_3=100),
])
def test_after_put_races_or_lost_unrelated_positions_never_report_verified(change):
    state = Dashboard()
    state.after_write = change
    with client(state) as canvas:
        target = d.snapshot(canvas, ACTION)
        with pytest.raises(ValueError):
            d.perform(canvas, ACTION, target)
    assert len(state.writes) == 1


@pytest.mark.parametrize("status", [302, 307, 308, 403, 429, 500, "network"])
def test_mutation_failures_and_redirects_are_never_replayed(status):
    state = Dashboard()
    state.put_error = status
    with client(state) as canvas:
        target = d.snapshot(canvas, ACTION)
        with pytest.raises(CanvasError):
            d.perform(canvas, ACTION, target)
    assert len(state.writes) == 1


@pytest.mark.parametrize("path", [PROFILE, POSITIONS, CARDS])
def test_read_permission_denial_is_not_empty_cards(path):
    state = Dashboard()
    state.errors[path] = 403
    with client(state) as canvas, pytest.raises(CanvasError):
        d.read_dashboard_cards(canvas)
    assert not state.writes


@pytest.mark.parametrize("path", [PROFILE, POSITIONS, CARDS])
def test_same_origin_read_redirect_cannot_escape_fixed_routes(path):
    state = Dashboard()
    state.redirects[path] = ORIGIN + "/api/v1/courses/1/pages/show-would-record-view"
    with client(state) as canvas, pytest.raises(CanvasError):
        d.read_dashboard_cards(canvas)
    assert not any("show-would" in request.url.path for request in state.requests)


@pytest.mark.parametrize("next_url", [ORIGIN + CARDS + "?page=2", ORIGIN + "/api/v1/courses/1/pages/reading", "https://other.invalid/"])
def test_unexpected_native_pagination_is_incomplete_without_following_it(next_url):
    state = Dashboard()
    state.link = f'<{next_url}>; rel="next"'
    with client(state) as canvas, pytest.raises((CanvasError, ValueError)):
        d.read_dashboard_cards(canvas)
    assert sum(request.url.path == CARDS for request in state.requests) == 1


def test_actor_or_map_change_while_reading_cannot_make_prepared_snapshot():
    state = Dashboard()
    def change(state, request):
        if request.url.path == CARDS:
            state.profile["id"] = 8
    state.mutate_on = change
    with client(state) as canvas, pytest.raises(ValueError, match="changed"):
        d.snapshot(canvas, ACTION)
    assert not state.writes


def mcp(canvas, store=None, owner=None):
    server, authorize = FastMCP("dashboard-cards"), Mock()
    register_tools(server, lambda: canvas, authorize, owner=owner or (lambda: "alice"), store=store)
    return server, authorize


def call(server, name, **arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    assert isinstance(result, tuple) and isinstance(result[1], dict)
    return result[1]


def test_actual_mcp_read_prepare_execute_and_terminal_replay():
    state = Dashboard()
    with client(state) as canvas:
        server, authorize = mcp(canvas)
        read = call(server, "canvas_dashboard_cards")
        assert read["data"]["course_ids"] == [2, 1, 3] and read["coverage"]["complete"]
        prepared = call(server, "canvas_prepare_action", action=ACTION)["data"]
        assert prepared["execute"]["tool"] == "canvas_reorder_dashboard" and not state.writes
        result = call(server, "canvas_reorder_dashboard", action_id=prepared["action_id"])["data"]
        assert result["state"] == "succeeded" and result["result"]["verified"]
        assert call(server, "canvas_reorder_dashboard", action_id=prepared["action_id"])["data"] == result
        assert len(state.writes) == 1 and authorize.called


def test_actual_mcp_wrong_executor_and_owner_cannot_consume_receipt():
    state, tenant = Dashboard(), {"owner": "alice"}
    with client(state) as canvas:
        server, authorize = mcp(canvas, owner=lambda: tenant["owner"])
        prepared = call(server, "canvas_prepare_action", action=ACTION)["data"]
        wrong = call(server, "canvas_manage_bookmarks", action_id=prepared["action_id"])
        assert wrong["status"] == "invalid_request"
        tenant["owner"] = "bob"
        denied = call(server, "canvas_reorder_dashboard", action_id=prepared["action_id"])
        assert denied["status"] == "invalid_request" and not state.writes
        assert authorize.call_count == 2  # Executor scope checks precede receipt lookup.


def test_actual_mcp_uncertain_write_is_terminal_and_never_retried():
    state = Dashboard()
    with client(state) as canvas:
        server, _ = mcp(canvas)
        prepared = call(server, "canvas_prepare_action", action=ACTION)["data"]
        state.after_write = lambda state: state.positions.pop("group_19")
        result = call(server, "canvas_reorder_dashboard", action_id=prepared["action_id"])["data"]
        assert result["state"] == "uncertain"
        assert call(server, "canvas_reorder_dashboard", action_id=prepared["action_id"])["data"] == result
        assert len(state.writes) == 1


def test_dashboard_receipt_is_immutable_credential_bound_and_persistent(tmp_path):
    state = Dashboard()
    payload = copy.deepcopy(ACTION)
    key, database = Fernet.generate_key(), tmp_path / "dashboard.db"
    with client(state) as canvas:
        db = Store(database, key)
        try:
            store = ActionStore(persistence=db)
            prepared = store.prepare(canvas, "alice", payload)
            encoded = db.get("canvas_action_payload", prepared["action_id"])["payload"]
            assert json.loads(encoded) == ACTION
            payload["course_ids"].reverse()
        finally:
            db.close()
        db = Store(database, key)
        try:
            reopened = ActionStore(persistence=db)
            before = len(state.requests)
            assert reopened.prepare(canvas, "alice", ACTION)["action_id"] == prepared["action_id"]
            assert len(state.requests) == before
            with client(state, token="other-synthetic-token") as other, pytest.raises(ValueError):
                reopened.execute(other, "alice", prepared["action_id"], lambda: None)
            assert len(state.requests) == before and not state.writes
            result = reopened.execute(canvas, "alice", prepared["action_id"], lambda: None)
            assert result["state"] == "succeeded" and result["result"]["course_ids"] == ACTION["course_ids"]
            after = len(state.requests)
            assert ActionStore(persistence=db).execute(canvas, "alice", prepared["action_id"], lambda: None) == result
            assert len(state.requests) == after and len(state.writes) == 1
        finally:
            db.close()


def test_preexisting_progress_receipt_still_executes_once_after_dashboard_registration(tmp_path):
    from test_horizon_read_safety import READ, HorizonState, historical_target
    from test_horizon_read_safety import client as progress_client

    # Literal snapshot from before safe module reads/dashboard registration.
    target = historical_target("must_view", False)
    class LegacyStore(ActionStore):
        def _snapshot(self, canvas, owner, p):
            return copy.deepcopy(target)
    key, database = Fernet.generate_key(), tmp_path / "legacy-progress.db"
    state = HorizonState()
    with progress_client(state) as canvas:
        db = Store(database, key)
        try:
            prepared = LegacyStore(persistence=db).prepare(canvas, "alice", READ)
            encoded = db.get("canvas_action_payload", prepared["action_id"])["payload"]
            fingerprint = db.get("canvas_action", prepared["action_id"])["fingerprint"]
        finally:
            db.close()
        db = Store(database, key)
        try:
            current = ActionStore(persistence=db)
            assert current.prepare(canvas, "alice", READ)["action_id"] == prepared["action_id"]
            assert db.get("canvas_action_payload", prepared["action_id"])["payload"] == encoded
            assert db.get("canvas_action", prepared["action_id"])["fingerprint"] == fingerprint
            assert not state.requests
            result = current.execute(canvas, "alice", prepared["action_id"], lambda: None)
            assert result["state"] == "succeeded"
            assert current.execute(canvas, "alice", prepared["action_id"], lambda: None) == result
            assert len(state.writes) == 1 and not state.show_reads
        finally:
            db.close()


@pytest.mark.parametrize("case", ["ignored_write", "wrong_ack", "wrong_map_shape"])
def test_unverified_acknowledgment_cannot_report_success(case):
    state = Dashboard()
    if case == "ignored_write":
        state.ignore_write = True
    elif case == "wrong_ack":
        state.ack_override = {"course_3": 1}
    else:
        state.ack_override = []
    with client(state) as canvas:
        target = d.snapshot(canvas, ACTION)
        with pytest.raises(ValueError):
            d.perform(canvas, ACTION, target)
    assert len(state.writes) == 1


def test_empty_native_dashboard_is_not_a_claim_of_no_enrollments():
    state = Dashboard()
    state.cards = []
    with client(state) as canvas:
        result = d.read_dashboard_cards(canvas)
    assert result["data"]["cards"] == [] and result["coverage"]["complete"]
    assert result["coverage"]["all_enrollments"] is False and result["data"]["reorder_supported"] is False


def test_k5_flag_type_change_cannot_compare_equal_to_false_during_read():
    state = Dashboard()
    def change(state, request):
        if request.url.path == CARDS:
            state.profile["k5_user"] = 0
    state.mutate_on = change
    with client(state) as canvas, pytest.raises(ValueError, match="changed"):
        d.snapshot(canvas, ACTION)
    assert not state.writes


def test_actual_mcp_native_read_permission_denial_and_annotations():
    state = Dashboard()
    state.errors[CARDS] = 403
    with client(state) as canvas:
        server, authorize = mcp(canvas)
        registered = {tool.name: tool for tool in server._tool_manager.list_tools()}
        assert registered["canvas_dashboard_cards"].annotations.readOnlyHint is True
        assert registered["canvas_reorder_dashboard"].annotations.readOnlyHint is False
        assert registered["canvas_reorder_dashboard"].annotations.idempotentHint is False
        assert set(registered["canvas_reorder_dashboard"].parameters["properties"]) == {"action_id"}
        result = call(server, "canvas_dashboard_cards")
    assert result["status"] == "forbidden" and result["coverage"]["complete"] is False
    assert result["data"] is None and not state.writes
    authorize.assert_not_called()
