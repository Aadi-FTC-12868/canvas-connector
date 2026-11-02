"""Horizon SHOW mutates viewed state; public reads must use the collection."""

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
from canvas_py._errors import CanvasError, CanvasNotFoundError
from canvas_py._module_item_read import MAX_MODULE_ITEMS, read_module_item_raw
from canvas_py._request import CanvasRequest
from canvas_py.models.module import ModuleItem
from cryptography.fernet import Fernet
from mcp.server.fastmcp import FastMCP

from canvas_connector import module_progress
from canvas_connector.actions import ActionStore
from canvas_connector.storage import Store
from canvas_connector.tools import register_tools

ORIGIN = "https://school.example"
MODULE = "/api/v1/courses/1/modules/2"
COLLECTION = MODULE + "/items"
SHOW = COLLECTION + "/3"
READ = {"kind": "module_mark_read", "course_id": 1, "module_id": 2, "item_id": 3}
DONE = {"kind": "module_mark_done", "course_id": 1, "module_id": 2, "item_id": 3, "done": True}


class HorizonState:
    def __init__(self, requirement="must_view"):
        self.module = {"id": 2, "name": "Study", "state": "started",
                       "require_sequential_progress": False, "prerequisite_module_ids": []}
        self.item = {"id": 3, "module_id": 2, "title": "Reading", "type": "Page", "page_url": "reading",
                     "completion_requirement": {"type": requirement, "completed": False},
                     "content_details": {"locked_for_user": False}}
        self.pages = None
        self.page_error = None
        self.next_url = None
        self.redirect = None
        self.link_header = None
        self.requests, self.writes = [], []
        self.show_reads = 0
        self.ignore_write = False

    def handler(self, request):
        assert request.url.host == "school.example"
        assert request.headers["authorization"] == "Bearer synthetic-token"
        assert not request.content
        assert not {"student_id", "as_user_id"} & set(request.url.params)
        path, method = request.url.path, request.method
        self.requests.append((method, path, dict(request.url.params)))
        if method != "GET":
            assert (method, path) in {("POST", SHOW + "/mark_read"), ("PUT", SHOW + "/done"), ("DELETE", SHOW + "/done")}
            self.writes.append((method, path))
            if not self.ignore_write:
                self.item["completion_requirement"]["completed"] = method != "DELETE"
            return httpx.Response(200, json={"message": "OK"})
        if path == SHOW:
            # This precisely models the native Horizon SHOW side effect.
            self.show_reads += 1
            self.item["completion_requirement"]["completed"] = True
            return httpx.Response(200, json=copy.deepcopy(self.item))
        if path == COLLECTION:
            assert request.url.params.get_list("include[]") == ["content_details"]
            assert request.url.params["per_page"] == "100"
            page = int(request.url.params.get("page", 1))
            if self.redirect:
                return httpx.Response(302, headers={"Location": self.redirect})
            if self.page_error and self.page_error[0] == page:
                return httpx.Response(self.page_error[1], json={"message": "synthetic failed page"})
            pages = self.pages if self.pages is not None else [[self.item]]
            headers = {"Link": f'<{ORIGIN}{COLLECTION}?include%5B%5D=content_details&per_page=100&page={page + 1}>; rel="next"'} if page < len(pages) else {}
            if self.next_url:
                headers = {"Link": f'<{self.next_url}>; rel="next"'}
            if self.link_header:
                headers = {"Link": self.link_header}
            return httpx.Response(200, json=copy.deepcopy(pages[page - 1]), headers=headers)
        if path == MODULE:
            return httpx.Response(200, json=copy.deepcopy(self.module))
        if path == "/api/v1/courses/1/modules":
            return httpx.Response(200, json=[{**self.module, "items": [self.item], "items_count": 1}])
        if path == "/api/v1/users/self/profile":
            return httpx.Response(200, json={"id": 7})
        if path == "/api/v1/courses/1":
            return httpx.Response(200, json={"id": 1, "horizon_course": False})
        page = {"page_id": 4, "url": "reading", "title": "Reading", "published": True,
                "locked_for_user": False, "updated_at": "2026-01-01T00:00:00Z"}
        if path == "/api/v1/courses/1/pages":
            return httpx.Response(200, json=[page])
        if path == "/api/v1/courses/1/pages/page_id:4/revisions/latest":
            return httpx.Response(200, json={**page, "revision_id": 1, "latest": True, "body": "Synthetic reading"})
        raise AssertionError((method, path))


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("Only MockTransport may serve Horizon tests")
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fail)


@contextmanager
def client(state):
    with (httpx.Client(transport=httpx.MockTransport(state.handler)) as http,
          Canvas(CanvasConfig(ORIGIN, "synthetic-token"), http_client=http) as canvas):
        yield canvas


def mcp_for(canvas):
    mcp = FastMCP("horizon-read-safety")
    authorize = Mock()
    register_tools(mcp, lambda: canvas, authorize, owner=lambda: "alice")
    return mcp, authorize


def call(mcp, name, **arguments):
    result = asyncio.run(mcp.call_tool(name, arguments))
    assert isinstance(result, tuple) and isinstance(result[1], dict)
    return result[1]


def test_synthetic_show_fixture_reproduces_horizon_side_effect():
    state = HorizonState()
    request = type("NativeShow", (CanvasRequest,), {"path": "courses/1/modules/2/items/3"})()
    with client(state) as canvas:
        assert not state.item["completion_requirement"]["completed"]
        canvas.send(request)
    assert state.show_reads == 1 and state.item["completion_requirement"]["completed"] is True
    assert not state.writes  # HTTP method alone does not establish read safety.


def test_namespace_get_item_remains_typed_but_never_records_view():
    state = HorizonState()
    with client(state) as canvas:
        item = canvas.modules.get_item(1, 2, 3)
    assert isinstance(item, ModuleItem)
    assert item.id == 3 and item.module_id == 2 and item.completion_requirement["completed"] is False
    assert not state.show_reads and not state.writes and not state.item["completion_requirement"]["completed"]
    assert [path for _, path, _ in state.requests] == [COLLECTION]


@pytest.mark.parametrize("tool,arguments", [
    ("canvas_open_item", {"course_id": 1, "item_type": "module_item", "item_id": "3", "module_id": 2}),
    ("canvas_open_item", {"course_id": 1, "item_type": "module_item", "item_id": "3"}),
    ("canvas_open_url", {"url": ORIGIN + "/courses/1/modules/2/items/3"}),
    ("canvas_open_url", {"url": ORIGIN + "/api/v1/courses/1/modules/2/items/3"}),
    ("canvas_open_url", {"url": ORIGIN + "/courses/1/modules/items/3"}),
])
def test_public_open_item_and_url_never_call_mutating_show(tool, arguments):
    state = HorizonState()
    with client(state) as canvas:
        mcp, authorize = mcp_for(canvas)
        result = call(mcp, tool, **arguments)
    assert result["status"] == "ok"
    assert "Synthetic reading" in str(result)
    assert not state.show_reads and not state.writes and not state.item["completion_requirement"]["completed"]
    authorize.assert_not_called()


def test_prepare_and_preflight_are_reads_authorized_mark_read_is_one_post():
    state = HorizonState()
    with client(state) as canvas:
        mcp, authorize = mcp_for(canvas)
        prepared = call(mcp, "canvas_prepare_action", action=READ)["data"]
        assert prepared["state"] == "prepared"
        assert not state.show_reads and not state.writes and not state.item["completion_requirement"]["completed"]
        # Root's explicit executor performs preparation revalidation and readback.
        result = call(mcp, "canvas_update_module_progress", action_id=prepared["action_id"])["data"]
        assert result["state"] == "succeeded" and result["result"]["verified"] is True
        assert call(mcp, "canvas_update_module_progress", action_id=prepared["action_id"])["data"]["state"] == "succeeded"
    assert state.show_reads == 0
    assert state.writes == [("POST", SHOW + "/mark_read")]
    assert state.item["completion_requirement"]["completed"] is True
    authorize.assert_called()


def test_noop_mark_read_cannot_be_falsely_verified_by_show_readback():
    state = HorizonState()
    state.ignore_write = True
    with client(state) as canvas:
        store = ActionStore()
        prepared = store.prepare(canvas, "alice", READ)
        result = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
        assert result["state"] == "uncertain" and result["result"]["verified"] is False
        assert store.execute(canvas, "alice", prepared["action_id"], lambda: None)["state"] == "uncertain"
    assert not state.show_reads and not state.item["completion_requirement"]["completed"]
    assert state.writes == [("POST", SHOW + "/mark_read")]


def historical_target(requirement, completed):
    # Literal v0.7 raw-SHOW snapshot shape, including None in its selected
    # output fields although the original API omits published for students.
    return {"user_id": 7, "course_id": 1, "module_id": 2, "item_id": 3,
            "module": {"id": 2, "name": "Study", "state": "started", "published": None,
                       "workflow_state": None, "unlock_at": None, "require_sequential_progress": False,
                       "requirement_type": None, "prerequisite_module_ids": []},
            "item": {"id": 3, "module_id": 2, "title": "Reading", "type": "Page", "content_id": None,
                     "page_url": "reading", "external_url": None, "published": None,
                     "completion_requirement": {"type": requirement, "completed": completed},
                     "content_details": {"locked_for_user": False, "locked": None, "hidden": None,
                                         "unlock_at": None, "lock_at": None}}}


@pytest.mark.parametrize("payload,requirement,completed,method,suffix", [
    (DONE, "must_mark_done", False, "PUT", "done"),
    ({**DONE, "done": False}, "must_mark_done", True, "DELETE", "done"),
    (READ, "must_view", False, "POST", "mark_read"),
])
def test_persisted_legacy_progress_receipts_keep_snapshot_fingerprint_and_one_use(tmp_path, payload, requirement, completed, method, suffix):
    state = HorizonState(requirement)
    state.item["completion_requirement"]["completed"] = completed
    target = historical_target(requirement, completed)
    class OldFormatStore(ActionStore):
        def _snapshot(self, canvas, owner, p):
            return copy.deepcopy(target)
    key = Fernet.generate_key()
    database = tmp_path / "progress.db"
    with client(state) as canvas:
        db = Store(database, key)
        try:
            old = OldFormatStore(persistence=db, bind_credentials=False)
            prepared = old.prepare(canvas, "alice", payload)
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            assert db.get("canvas_action_payload", prepared["action_id"])["payload"] == encoded
            expected = hashlib.sha256((old._identity(canvas, "alice") + "\0" + encoded).encode()).hexdigest()
            assert db.get("canvas_action", prepared["action_id"])["fingerprint"] == expected
            assert db.get("canvas_action", prepared["action_id"])["snapshot"] == target
        finally:
            db.close()
        reopened = Store(database, key)
        try:
            current = ActionStore(persistence=reopened, bind_credentials=False)
            assert current.prepare(canvas, "alice", payload)["action_id"] == prepared["action_id"]
            assert not state.requests
            assert module_progress.snapshot(canvas, payload) == target
            assert "published" not in state.item
            result = current.execute(canvas, "alice", prepared["action_id"], lambda: None)
            assert result["state"] == "succeeded", result
            assert ActionStore(persistence=reopened, bind_credentials=False).execute(
                canvas, "alice", prepared["action_id"], lambda: None)["state"] == "succeeded"
        finally:
            reopened.close()
    assert not state.show_reads
    assert state.writes == [(method, SHOW + "/" + suffix)]


def test_paginated_exact_lookup_retains_raw_field_absence():
    state = HorizonState()
    state.pages = [[{**state.item, "id": 9}], [state.item]]
    with client(state) as canvas:
        raw = read_module_item_raw(canvas, 1, 2, 3)
    assert raw == state.item and "published" not in raw
    assert len(state.requests) == 2 and not state.show_reads


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_failed_later_page_is_not_a_successful_early_match_or_empty_result(status):
    state = HorizonState()
    state.pages = [[state.item], []]
    state.page_error = (2, status)
    with client(state) as canvas, pytest.raises(CanvasError) as error:
        read_module_item_raw(canvas, 1, 2, 3)
    assert error.value.status == (None if status == 404 else status) and not state.show_reads


@pytest.mark.parametrize("pages", [[], [[{"id": 9, "module_id": 2}]]])
def test_absence_is_not_found_only_after_complete_collection(pages):
    state = HorizonState()
    state.pages = pages or [[]]
    with client(state) as canvas, pytest.raises(CanvasNotFoundError) as error:
        read_module_item_raw(canvas, 1, 2, 3)
    assert error.value.status == 404 and not state.show_reads


@pytest.mark.parametrize("bad", [{"id": 3, "module_id": 9}, {"id": 3}, {"id": True, "module_id": 2},
                                  {"id": "3", "module_id": 2}, {"id": 3, "module_id": 2.0}, "not-an-item"])
def test_wrong_module_or_malformed_identity_is_incomplete_not_absent(bad):
    state = HorizonState()
    state.pages = [[bad]]
    with client(state) as canvas, pytest.raises(CanvasError) as error:
        read_module_item_raw(canvas, 1, 2, 3)
    assert error.value.status is None and not state.show_reads


def test_duplicate_ids_across_pages_are_not_accepted():
    state = HorizonState()
    state.pages = [[state.item], [state.item]]
    with client(state) as canvas, pytest.raises(CanvasError, match="duplicate"):
        read_module_item_raw(canvas, 1, 2, 3)
    assert not state.show_reads


@pytest.mark.parametrize("mode", ["success", "absent", "overflow", "denied", "duplicate"])
def test_lookup_closes_iterator_on_every_exit_and_bounds_consumption(mode):
    class Reader:
        closed = False
        consumed = 0
        def paginate(self, request):
            assert request.path == "courses/1/modules/2/items"
            try:
                count = MAX_MODULE_ITEMS + 10 if mode == "overflow" else 1
                for i in range(count):
                    self.consumed += 1
                    yield {"id": 3 + i if mode != "absent" else 9, "module_id": 2}
                if mode == "denied":
                    raise CanvasError("denied", status=403)
                if mode == "duplicate":
                    yield {"id": 3, "module_id": 2}
            finally:
                self.closed = True
    reader = Reader()
    if mode == "success":
        assert read_module_item_raw(reader, 1, 2, 3)["id"] == 3
    else:
        with pytest.raises(CanvasError):
            read_module_item_raw(reader, 1, 2, 3)
    assert reader.closed
    assert reader.consumed <= MAX_MODULE_ITEMS + 1


@pytest.mark.parametrize("status,expected", [(403, "forbidden"), (500, "unavailable")])
def test_public_lookup_denial_is_incomplete_not_not_found(status, expected):
    state = HorizonState()
    state.page_error = (1, status)
    with client(state) as canvas:
        mcp, _ = mcp_for(canvas)
        result = call(mcp, "canvas_open_item", course_id=1, item_type="module_item", item_id="3", module_id=2)
    assert result["status"] == expected and result["coverage"]["complete"] is False
    assert not state.show_reads and not state.writes


@pytest.mark.parametrize("destination", [SHOW, SHOW + "/mark_read", "/api/v1/courses/1/pages/reading",
                                        COLLECTION + ".json", "/api/v1/courses/1/modules/9/items"])
def test_pagination_link_cannot_escape_exact_collection_before_dispatch(destination):
    state = HorizonState()
    state.next_url = ORIGIN + destination
    with client(state) as canvas, pytest.raises(CanvasError, match="collection path"):
        read_module_item_raw(canvas, 1, 2, 3)
    assert [path for _, path, _ in state.requests] == [COLLECTION]
    assert not state.show_reads and not state.item["completion_requirement"]["completed"]


def test_collection_redirect_cannot_call_horizon_show():
    state = HorizonState()
    state.redirect = ORIGIN + SHOW
    with client(state) as canvas, pytest.raises(CanvasError, match="redirects are disabled"):
        read_module_item_raw(canvas, 1, 2, 3)
    assert [path for _, path, _ in state.requests] == [COLLECTION]
    assert not state.show_reads and not state.item["completion_requirement"]["completed"]


@pytest.mark.parametrize("link", [
    f'<{ORIGIN}{COLLECTION}?page=2>; rel=next',
    f'<{ORIGIN}{COLLECTION}?page=2>; rel = "next"',
    f'<{ORIGIN}{COLLECTION}?page=2>; rel="next last"',
    f'<{ORIGIN}{COLLECTION}?page=2>; rel="next", <{ORIGIN}{SHOW}>; rel="next"',
    f'<{ORIGIN}{COLLECTION}?page=2>; rel="next"; title="Extra parameter"',
    "not a valid link; rel=next",
])
@pytest.mark.parametrize("first_page_has_target", [True, False])
def test_unparsed_or_ambiguous_link_is_incomplete_never_success_or_false_absence(link, first_page_has_target):
    state = HorizonState()
    state.pages = [[state.item] if first_page_has_target else []]
    state.link_header = link
    with client(state) as canvas, pytest.raises(CanvasError, match="collection is incomplete") as error:
        read_module_item_raw(canvas, 1, 2, 3)
    assert error.value.status is None
    assert [path for _, path, _ in state.requests] == [COLLECTION]
    assert not state.show_reads
