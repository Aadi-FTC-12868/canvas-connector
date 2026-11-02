"""Bookmark reads/writes use synthetic state and the real Canvas HTTP client."""

import copy
import json
import socket
from contextlib import contextmanager
from typing import Annotated

import httpx
import pytest
from canvas_py import Canvas
from canvas_py._config import CanvasConfig
from canvas_py._errors import CanvasError
from pydantic import Field, TypeAdapter, ValidationError

from canvas_connector import bookmarks
from canvas_connector.actions import ActionStore

BASE = "https://school.example"
PATH = "/api/v1/users/self/bookmarks"
ADAPTER = TypeAdapter(Annotated[bookmarks.BookmarkActionTypes, Field(discriminator="kind")])
CREATE = {"kind": "bookmark_create", "name": "New bookmark", "url": "https://docs.google.com/document/d/example/edit"}
UPDATE = {"kind": "bookmark_update", "bookmark_id": 2, "name": "New title"}
REORDER = {"kind": "bookmark_reorder", "bookmark_id": 2, "position": 0}
DELETE = {"kind": "bookmark_delete", "bookmark_id": 2}
ACTIONS = [CREATE, UPDATE, REORDER, DELETE]


def bookmark(bookmark_id, position, **changes):
    return {"id": bookmark_id, "position": position, "name": f"Bookmark {bookmark_id}",
            "url": f"/courses/{bookmark_id}", "data": {"active_tab": 1}, **changes}


class BookmarkState:
    def __init__(self):
        self.actor = 7
        self.bookmarks = {i: bookmark(i, i) for i in (1, 2, 3)}
        self.requests, self.writes = [], []
        self.page_size = 100
        self.ignore_write = False
        self.after_write = None
        self.response_override = None
        self.read_error = None
        self.readback_error = None
        self.list_override = None
        self.next_url = None

    def ordered(self):
        return sorted(self.bookmarks.values(), key=lambda item: (item["position"], item["id"]))

    def handler(self, request):
        assert request.url.host == "school.example"
        assert request.headers["authorization"] == "Bearer synthetic-token"
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, path, dict(request.url.params), body))
        if request.method == "GET":
            assert body is None
            if path == "/api/v1/users/self/profile":
                return httpx.Response(200, json={"id": self.actor})
            error = self.readback_error if self.writes and self.readback_error else self.read_error
            if error and path == error[0]:
                return httpx.Response(error[1], json={"message": "not available"})
            if path == PATH:
                if self.list_override is not None:
                    return httpx.Response(200, json=self.list_override)
                assert set(request.url.params) <= {"per_page", "page"}
                page = int(request.url.params.get("page", 1))
                rows = self.ordered()
                start = (page - 1) * self.page_size
                next_url = self.next_url
                if next_url is None and start + self.page_size < len(rows):
                    next_url = f"{BASE}{PATH}?page={page + 1}&per_page=100"
                headers = {"Link": f'<{next_url}>; rel="next"'} if next_url else {}
                return httpx.Response(200, json=copy.deepcopy(rows[start:start + self.page_size]), headers=headers)
            assert path.startswith(PATH + "/")
            assert not request.url.params
            value = self.bookmarks.get(int(path.rsplit("/", 1)[1]))
            return httpx.Response(404, json={"message": "missing"}) if value is None else httpx.Response(200, json=copy.deepcopy(value))
        self.writes.append((request.method, path, body))
        assert not request.url.params
        assert not body or set(body) <= {"name", "url", "position"}
        values = self.bookmarks if not self.ignore_write else copy.deepcopy(self.bookmarks)
        if request.method == "POST":
            assert path == PATH
            value = bookmark(20, max((item["position"] for item in values.values()), default=0) + 1,
                             name=body["name"], url=body["url"], data=None)
            values[20] = value
        else:
            bookmark_id = int(path.removeprefix(PATH + "/"))
            value = values[bookmark_id]
            if request.method == "DELETE":
                assert body is None
                del values[bookmark_id]
                for item in values.values():
                    if item["position"] > value["position"]:
                        item["position"] -= 1
            else:
                assert request.method == "PUT"
                value.update({key: body[key] for key in ("name", "url") if key in body})
        if body and "position" in body:
            old, new = value["position"], body["position"]
            for item in values.values():
                if item["id"] == value["id"]:
                    continue
                if new < old and new <= item["position"] < old:
                    item["position"] += 1
                if new > old and old < item["position"] <= new:
                    item["position"] -= 1
            value["position"] = new
        response = copy.deepcopy(value)
        if self.after_write:
            self.after_write(self)
        return httpx.Response(200, json=self.response_override if self.response_override is not None else response)


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected external network in bookmark test")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


@contextmanager
def client(state):
    with (
        httpx.Client(transport=httpx.MockTransport(state.handler)) as http,
        Canvas(CanvasConfig(BASE, "synthetic-token"), http_client=http) as canvas,
    ):
        yield canvas


@pytest.mark.parametrize("payload,method,path,body,expected_order", [
    (CREATE, "POST", PATH, {"name": CREATE["name"], "url": CREATE["url"]}, [(1, 1), (2, 2), (3, 3), (20, 4)]),
    ({**CREATE, "position": 2}, "POST", PATH, {"name": CREATE["name"], "url": CREATE["url"], "position": 2}, [(1, 1), (20, 2), (2, 3), (3, 4)]),
    (UPDATE, "PUT", PATH + "/2", {"name": "New title"}, [(1, 1), (2, 2), (3, 3)]),
    ({**UPDATE, "url": "http://example.test/topic"}, "PUT", PATH + "/2", {"name": "New title", "url": "http://example.test/topic"}, [(1, 1), (2, 2), (3, 3)]),
    (REORDER, "PUT", PATH + "/2", {"position": 0}, [(2, 0), (1, 2), (3, 3)]),
    ({**REORDER, "position": -3}, "PUT", PATH + "/2", {"position": -3}, [(2, -3), (1, 2), (3, 3)]),
    ({**REORDER, "position": 10}, "PUT", PATH + "/2", {"position": 10}, [(1, 1), (3, 2), (2, 10)]),
    (DELETE, "DELETE", PATH + "/2", None, [(1, 1), (3, 2)]),
])
def test_fixed_self_endpoint_exact_readback_and_integer_order_semantics(payload, method, path, body, expected_order):
    state = BookmarkState()
    state.page_size = 2
    with client(state) as canvas:
        target = bookmarks.snapshot(canvas, payload)
        assert target["user_id"] == 7 and not state.writes
        result = bookmarks.perform(canvas, payload, target)
    assert result["verified"] is True
    assert state.writes == [(method, path, body)]
    assert [(item["id"], item["position"]) for item in state.ordered()] == expected_order
    assert state.requests[-1][0] == "GET"
    assert bookmarks.ROUTES[payload["kind"]] == "canvas_manage_bookmarks"
    if "order" in result:
        assert [(item["id"], item["position"]) for item in result["order"]] == expected_order


def test_default_create_starts_at_one_and_preserves_negative_maximum():
    state = BookmarkState()
    state.bookmarks = {}
    with client(state) as canvas:
        assert bookmarks.perform(canvas, CREATE, bookmarks.snapshot(canvas, CREATE))["bookmark"]["position"] == 1
    state = BookmarkState()
    state.bookmarks = {1: bookmark(1, -10)}
    with client(state) as canvas:
        assert bookmarks.perform(canvas, CREATE, bookmarks.snapshot(canvas, CREATE))["bookmark"]["position"] == -9


def test_delete_ties_follow_server_position_then_id_order():
    state = BookmarkState()
    state.bookmarks = {1: bookmark(1, 3), 2: bookmark(2, 2), 3: bookmark(3, 2)}
    with client(state) as canvas:
        result = bookmarks.perform(canvas, DELETE, bookmarks.snapshot(canvas, DELETE))
    assert result["order"] == [{"id": 1, "position": 2}, {"id": 3, "position": 2}]


def test_bounded_reads_have_fresh_envelopes_and_do_not_fetch_links():
    state = BookmarkState()
    state.page_size = 2
    state.bookmarks[2]["url"] = "https://docs.google.com/document/d/example/edit"
    with client(state) as canvas:
        first = bookmarks.list_bookmarks(canvas, 0, 2)
        assert first["coverage"]["complete"] is False
        assert first["coverage"]["next_offset"] == 2
        assert len(first["data"]["bookmarks"]) == 2
        second = bookmarks.list_bookmarks(canvas, 2, 2)
        assert second["coverage"]["complete"] is False
        assert second["coverage"]["next_offset"] is None
        assert [item["id"] for item in second["data"]["bookmarks"]] == [3]
        all_rows = bookmarks.list_bookmarks(canvas)
        assert all_rows["coverage"]["complete"] is True
        detail = bookmarks.get_bookmark(canvas, 2)
        assert detail["source"]["checked_at"]
        assert detail["source"]["content_is_untrusted"] is True
        assert detail["data"]["bookmark"]["link"]["provider"] == "google_drive"
        assert detail["data"]["bookmark"]["link"]["access"] == "external_account_required"
        assert bookmarks.get_bookmark(canvas, 1)["data"]["bookmark"]["url"] == BASE + "/courses/1"
    assert not state.writes
    assert all(path == "/api/v1/users/self/profile" or path.startswith(PATH) for _, path, _, _ in state.requests)


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,test", "//other.example/path", "https://u:p@example.test", "https://example.test\\@evil.test"])
def test_unsafe_existing_urls_are_not_exposed_as_openable_links_but_can_be_deleted(url):
    state = BookmarkState()
    state.bookmarks[2]["url"] = url
    with client(state) as canvas:
        result = bookmarks.get_bookmark(canvas, 2)["data"]["bookmark"]
        assert result["url"] is None and result["url_available"] is False
        assert "link" not in result
        assert bookmarks.perform(canvas, DELETE, bookmarks.snapshot(canvas, DELETE))["deleted"] is True


@pytest.mark.parametrize("status", [403, 404, 500])
def test_failed_list_read_is_not_an_empty_success(status):
    state = BookmarkState()
    state.read_error = (PATH, status)
    with client(state) as canvas, pytest.raises(CanvasError) as error:
        bookmarks.list_bookmarks(canvas)
    assert error.value.status == status


@pytest.mark.parametrize("value", [{"bookmarks": []}, None, ["bad"]])
def test_wrong_list_shapes_fail(value):
    state = BookmarkState()
    state.list_override = value
    if value is None:
        state.list_override = {"bookmarks": None}
    with client(state) as canvas, pytest.raises(TypeError):
        bookmarks.list_bookmarks(canvas)


@pytest.mark.parametrize("payload", ACTIONS)
def test_success_write_response_without_saved_state_is_not_verified(payload):
    state = BookmarkState()
    state.ignore_write = True
    with client(state) as canvas:
        target = bookmarks.snapshot(canvas, payload)
        with pytest.raises((ValueError, CanvasError)):
            bookmarks.perform(canvas, payload, target)
    assert len(state.writes) == 1


@pytest.mark.parametrize("payload", ACTIONS)
def test_current_user_changes_never_write(payload):
    state = BookmarkState()
    with client(state) as canvas:
        target = bookmarks.snapshot(canvas, payload)
        state.actor = 8
        with pytest.raises(ValueError, match="changed"):
            bookmarks.perform(canvas, payload, target)
    assert not state.writes


@pytest.mark.parametrize("payload", [UPDATE, REORDER, DELETE])
@pytest.mark.parametrize("change", [{"name": "Elsewhere"}, {"url": "https://other.test/"}, {"position": 9}, {"data": {"active_tab": 2}}])
def test_target_changed_since_preparation_never_writes(payload, change):
    state = BookmarkState()
    with client(state) as canvas:
        target = bookmarks.snapshot(canvas, payload)
        state.bookmarks[2].update(change)
        with pytest.raises(ValueError):
            bookmarks.perform(canvas, payload, target)
    assert not state.writes


@pytest.mark.parametrize("payload", [CREATE, REORDER, DELETE])
def test_other_bookmark_order_change_invalidates_prepared_ordering(payload):
    state = BookmarkState()
    with client(state) as canvas:
        target = bookmarks.snapshot(canvas, payload)
        state.bookmarks[3]["position"] = 6
        with pytest.raises(ValueError, match="changed"):
            bookmarks.perform(canvas, payload, target)
    assert not state.writes


@pytest.mark.parametrize("payload", [CREATE, REORDER, DELETE])
def test_correct_target_but_wrong_affected_neighbor_order_is_not_verified(payload):
    state = BookmarkState()
    state.after_write = lambda s: s.bookmarks[1].update(position=100)
    with client(state) as canvas:
        target = bookmarks.snapshot(canvas, payload)
        with pytest.raises(ValueError, match="ordering was not confirmed"):
            bookmarks.perform(canvas, payload, target)
    assert len(state.writes) == 1


@pytest.mark.parametrize("payload", [UPDATE, REORDER])
@pytest.mark.parametrize("change", [{"id": 9}, {"name": "Wrong"}, {"url": "https://other.test"}, {"data": None}, {"position": 8}])
def test_exact_readback_preserves_other_fields(payload, change):
    state = BookmarkState()
    state.after_write = lambda s: s.bookmarks[2].update(change)
    with client(state) as canvas:
        target = bookmarks.snapshot(canvas, payload)
        with pytest.raises(ValueError):
            bookmarks.perform(canvas, payload, target)
    assert len(state.writes) == 1


@pytest.mark.parametrize("status", [401, 403, 500])
def test_delete_readback_denial_or_error_is_not_absence(status):
    state = BookmarkState()
    state.readback_error = (PATH + "/2", status)
    with client(state) as canvas:
        target = bookmarks.snapshot(canvas, DELETE)
        with pytest.raises(CanvasError) as error:
            bookmarks.perform(canvas, DELETE, target)
    assert error.value.status == status
    assert len(state.writes) == 1


@pytest.mark.parametrize("payload", ACTIONS)
def test_wrong_mutation_identity_is_not_verified(payload):
    state = BookmarkState()
    state.response_override = {"id": 1}
    with client(state) as canvas:
        target = bookmarks.snapshot(canvas, payload)
        with pytest.raises(ValueError):
            bookmarks.perform(canvas, payload, target)
    assert len(state.writes) == 1


@pytest.mark.parametrize("payload", ACTIONS)
def test_actor_changes_after_dispatch_is_uncertain(payload):
    state = BookmarkState()
    state.after_write = lambda s: setattr(s, "actor", 8)
    with client(state) as canvas:
        target = bookmarks.snapshot(canvas, payload)
        with pytest.raises(ValueError, match="Current user"):
            bookmarks.perform(canvas, payload, target)
    assert len(state.writes) == 1


@pytest.mark.parametrize("actor", [True, None, "7", 0, 2**63])
def test_current_user_identity_requires_strict_positive_id(actor):
    state = BookmarkState()
    state.actor = actor
    with client(state) as canvas, pytest.raises(ValidationError):
        bookmarks.list_bookmarks(canvas)


def test_extra_server_user_id_must_match_but_normal_bookmarks_omit_it():
    state = BookmarkState()
    state.bookmarks[2]["user_id"] = 8
    with client(state) as canvas, pytest.raises(ValueError):
        bookmarks.snapshot(canvas, DELETE)
    assert not state.writes


@pytest.mark.parametrize("override", [[bookmark(1, 1), bookmark(1, 1)], [bookmark(2, 2), bookmark(1, 1)]])
def test_duplicate_or_unsorted_order_is_not_used_for_mutation(override):
    state = BookmarkState()
    state.list_override = override
    with client(state) as canvas, pytest.raises(ValueError):
        bookmarks.snapshot(canvas, CREATE)
    assert not state.writes


def test_order_discovery_bound_prevents_unverified_mutations():
    state = BookmarkState()
    state.bookmarks = {i: bookmark(i, i) for i in range(1, 1002)}
    with client(state) as canvas, pytest.raises(ValueError, match="1000-bookmark"):
        bookmarks.snapshot(canvas, CREATE)
    assert not state.writes
    assert sum(path == PATH for _, path, _, _ in state.requests) == 11


def test_create_cannot_exceed_post_write_order_verification_bound():
    state = BookmarkState()
    state.bookmarks = {i: bookmark(i, i) for i in range(1, 1001)}
    with client(state) as canvas, pytest.raises(ValueError, match="would exceed"):
        bookmarks.snapshot(canvas, CREATE)
    assert not state.writes


def test_list_window_stops_reading_after_lookahead():
    state = BookmarkState()
    state.bookmarks = {i: bookmark(i, i) for i in range(1, 1002)}
    with client(state) as canvas:
        result = bookmarks.list_bookmarks(canvas, limit=10)
    assert len(result["data"]["bookmarks"]) == 10
    assert result["coverage"]["next_offset"] == 10
    assert sum(path == PATH for _, path, _, _ in state.requests) == 1


def test_foreign_pagination_link_never_receives_credentials_or_network_request():
    state = BookmarkState()
    state.page_size = 1
    state.next_url = "https://other.example/api/v1/users/self/bookmarks?page=2"
    with client(state) as canvas, pytest.raises(CanvasError):
        bookmarks.list_bookmarks(canvas, limit=2)
    assert len(state.requests) == 2


@pytest.mark.parametrize("value", [True, "1", 1.0, None, -(2**31) - 1, 2**31])
@pytest.mark.parametrize("payload", [REORDER, {**CREATE, "position": 2}])
def test_native_positions_are_bounded_strict_integers(payload, value):
    if payload["kind"] == "bookmark_create" and value is None:
        assert ADAPTER.validate_python({**payload, "position": value}).position is None
    else:
        with pytest.raises(ValidationError):
            ADAPTER.validate_python({**payload, "position": value})


@pytest.mark.parametrize("value", [True, "2", 2.0, 0, -1, 2**63])
def test_bookmark_ids_are_strict_positive_and_bounded(value):
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({**DELETE, "bookmark_id": value})


@pytest.mark.parametrize("name", ["", " ", "name\n", "name\x00", "name\x7f", "x" * 256, 7])
def test_new_names_require_bounded_visible_text(name):
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({**CREATE, "name": name})


@pytest.mark.parametrize("url", ["", "/courses/1", "//example.test", "javascript:alert(1)", "data:text/plain,hi", "file:///tmp/a",
    "https://user:password@example.test", "https://@example.test", "https://example.test/a b", " https://example.test", "https://example.test\n",
    "https://example.test\\@other.test", "https://example.test:bad", "https://example.test:70000", "https://example.test:0", "https://user%40example.test", "https://", "x" * 4097])
def test_new_urls_are_bounded_absolute_http_and_never_credentials(url):
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({**CREATE, "url": url})


@pytest.mark.parametrize("extras", [{"user_id": 8}, {"as_user_id": 8}, {"course_id": 1}, {"data": {}}, {"bookmark_ids": [1, 2]}, {"force": True}])
@pytest.mark.parametrize("payload", ACTIONS)
def test_no_other_user_raw_data_or_bulk_mutation_inputs(payload, extras):
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({**payload, **extras})


def test_update_needs_at_least_one_content_field_and_reorder_is_explicit():
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({"kind": "bookmark_update", "bookmark_id": 2})
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({**UPDATE, "position": 1})


@pytest.mark.parametrize("offset,limit", [(-1, 2), (10001, 2), (True, 2), (0, 0), (0, 101), (0, True)])
def test_read_window_is_bounded_before_network(offset, limit):
    state = BookmarkState()
    with client(state) as canvas, pytest.raises(ValueError):
        bookmarks.list_bookmarks(canvas, offset, limit)
    assert not state.requests


@pytest.mark.parametrize("payload", ACTIONS)
def test_action_store_prepares_verifies_and_never_replays(payload):
    state, store = BookmarkState(), ActionStore()
    with client(state) as canvas:
        prepared = store.prepare(canvas, "alice", payload)
        assert prepared["execute"]["tool"] == "canvas_manage_bookmarks"
        assert not state.writes
        result = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
        assert result["state"] == "succeeded", result
        assert result["result"]["verified"] is True
        assert store.execute(canvas, "alice", prepared["action_id"], lambda: None)["state"] == "succeeded"
    assert len(state.writes) == 1


@pytest.mark.parametrize("payload", ACTIONS)
def test_action_store_rejects_stale_account_before_dispatch(payload):
    state, store = BookmarkState(), ActionStore()
    with client(state) as canvas:
        prepared = store.prepare(canvas, "alice", payload)
        state.actor = 8
        result = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
    assert result["state"] == "rejected"
    assert result["result"]["upstream_write_attempted"] is False
    assert not state.writes


def test_uncertain_delete_is_not_replayed_or_reprepared_automatically():
    state, store = BookmarkState(), ActionStore()
    state.readback_error = (PATH + "/2", 403)
    with client(state) as canvas:
        prepared = store.prepare(canvas, "alice", DELETE)
        result = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
        assert result["state"] == "uncertain"
        assert store.execute(canvas, "alice", prepared["action_id"], lambda: None)["state"] == "uncertain"
        assert store.prepare(canvas, "alice", DELETE)["state"] == "uncertain"
    assert len(state.writes) == 1
