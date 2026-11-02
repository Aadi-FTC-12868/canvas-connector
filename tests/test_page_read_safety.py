"""Passive page reads never invoke Canvas's implicit must-view completion."""

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
from canvas_py._errors import CanvasError, CanvasNotFoundError
from canvas_py._page_read import MAX_PAGES, PageMetadataRequest, read_page_raw
from canvas_py.endpoints.navigation import GetPage, ListPages
from mcp.server.fastmcp import FastMCP

from canvas_connector import group_content
from canvas_connector.tools import register_tools

ORIGIN = "https://school.example"
COLLECTION = "/api/v1/courses/1/pages"
LATEST = COLLECTION + "/page_id:4/revisions/latest"
SECRET = "Synthetic saved reading"


class PageState:
    def __init__(self):
        self.page = {"page_id": 4, "url": "reading", "title": "Reading", "front_page": True,
                     "published": True, "locked_for_user": False, "updated_at": "2026-01-01T00:00:00Z"}
        self.revision = {"revision_id": 2, "latest": True, "url": "reading", "title": "Reading",
                         "updated_at": self.page["updated_at"], "body": SECRET}
        self.course = {"id": 1, "default_view": "wiki", "horizon_course": False}
        self.membership = {"group_id": 2, "user_id": 7, "workflow_state": "accepted"}
        self.requests = []
        self.viewed = False
        self.rows = None
        self.after_revision = None
        self.errors = {}
        self.next_link = None
        self.redirects = {}
        self.lists = 0

    def handler(self, request):
        assert request.url.host == "school.example"
        assert request.headers["authorization"] == "Bearer synthetic-token"
        assert request.method == "GET" and request.content == b""
        self.requests.append(request)
        path = request.url.path
        if path in self.redirects:
            return httpx.Response(302, headers={"Location": self.redirects[path]})
        if path in self.errors:
            return httpx.Response(self.errors[path], json={"message": "synthetic unavailable"})
        if path == "/api/v1/courses/1":
            return httpx.Response(200, json=self.course)
        if path == "/api/v1/courses/1/tabs":
            return httpx.Response(200, json=[])
        if path in {COLLECTION, "/api/v1/groups/2/pages"}:
            if "body" in request.url.params.get_list("include[]"):
                self.viewed = True
                return httpx.Response(200, json=[{**self.page, "body": SECRET}])
            page = int(request.url.params.get("page", "1"))
            if page == 1:
                self.lists += 1
            pages = self.rows if self.rows is not None else [[self.page]]
            headers = {}
            if page < len(pages):
                headers["Link"] = f'<{request.url.copy_set_param("page", page + 1)}>; rel="next"'
            if self.next_link:
                headers["Link"] = self.next_link
            return httpx.Response(200, json=copy.deepcopy(pages[page - 1]), headers=headers)
        if path.endswith("/revisions/latest"):
            result = copy.deepcopy(self.revision)
            if self.after_revision:
                self.after_revision(self)
            return httpx.Response(200, json=result)
        if path in {COLLECTION + "/reading", "/api/v1/courses/1/front_page"}:
            self.viewed = True
            return httpx.Response(200, json={**self.page, "body": SECRET})
        item = {"id": 3, "module_id": 2, "type": "Page", "page_url": "reading",
                "completion_requirement": {"type": "must_view", "completed": self.viewed}}
        if path == "/api/v1/courses/1/modules/2/items":
            return httpx.Response(200, json=[item])
        if path == "/api/v1/courses/1/modules":
            return httpx.Response(200, json=[{"id": 2, "items": [item], "items_count": 1}])
        if path == "/api/v1/users/self/profile":
            return httpx.Response(200, json={"id": 7})
        if path == "/api/v1/groups/2":
            return httpx.Response(200, json={"id": 2, "workflow_state": "available"})
        if path == "/api/v1/groups/2/users/self":
            return httpx.Response(200, json=self.membership)
        raise AssertionError(path)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("Page safety tests require MockTransport")
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fail)


@contextmanager
def client(state):
    with (httpx.Client(transport=httpx.MockTransport(state.handler)) as http,
          Canvas(CanvasConfig(ORIGIN, "synthetic-token"), http_client=http) as canvas):
        yield canvas


def call(canvas, tool, **kwargs):
    mcp, authorize = FastMCP("passive-pages"), Mock()
    register_tools(mcp, lambda: canvas, authorize, owner=lambda: "alice")
    result = asyncio.run(mcp.call_tool(tool, kwargs))
    authorize.assert_not_called()
    assert isinstance(result, tuple) and isinstance(result[1], dict)
    return result[1]


@pytest.mark.parametrize("native_request", [GetPage(1, "reading"), ListPages(1)])
def test_native_show_and_body_include_fixture_reproduce_implicit_completion(native_request):
    state = PageState()
    if isinstance(native_request, ListPages):
        native_request.query_pairs = lambda: [("include[]", "body")]
    with client(state) as canvas:
        canvas.send(native_request)
    assert state.viewed


@pytest.mark.parametrize("tool,arguments", [
    ("canvas_open_item", {"course_id": 1, "item_type": "page", "item_id": "reading"}),
    ("canvas_open_item", {"course_id": 1, "item_type": "page", "item_id": "page_id:4"}),
    ("canvas_open_item", {"course_id": 1, "item_type": "module_item", "item_id": "3", "module_id": 2}),
    ("canvas_open_url", {"url": ORIGIN + "/courses/1/pages/reading"}),
    ("canvas_open_url", {"url": ORIGIN + "/api/v1/courses/1/pages/reading"}),
    ("canvas_open_url", {"url": ORIGIN + "/courses/1/modules/2/items/3"}),
    ("canvas_open_url", {"url": ORIGIN + "/courses/1/front_page"}),
    ("canvas_course_home", {"course_id": 1}),
    ("canvas_open_group_item", {"group_id": 2, "item_type": "page", "item_id": "reading"}),
])
def test_actual_mcp_page_navigation_never_records_view_and_reports_partial_saved_html(tool, arguments):
    state = PageState()
    with client(state) as canvas:
        result = call(canvas, tool, **arguments)
    assert result["status"] == "ok" and result["coverage"]["complete"] is False
    assert SECRET in json.dumps(result) and "latest_saved_html_revision" in json.dumps(result)
    assert not state.viewed
    assert state.lists == 2
    assert all("body" not in request.url.params.get_list("include[]") for request in state.requests)


@pytest.mark.parametrize("selector", ["reading", "4", "page_id:4", None])
def test_exact_identity_latest_literal_and_course_flag_checked_on_both_sides(selector):
    state = PageState()
    with client(state) as canvas:
        result = read_page_raw(canvas, "course", 1, selector)
    assert result["body"] == SECRET and result["page_id"] == 4 and result["revision_id"] == 2
    assert [request.url.path for request in state.requests] == [
        "/api/v1/courses/1", COLLECTION, LATEST, COLLECTION, "/api/v1/courses/1"]
    assert all(request.url.params.get_list("include[]") == ["horizon_course"]
               for request in state.requests if request.url.path == "/api/v1/courses/1")


def test_numeric_slug_takes_precedence_over_numeric_identity():
    state = PageState()
    state.rows = [[state.page, {**state.page, "page_id": 5, "url": "4", "front_page": False}]]
    state.revision["url"] = "4"
    with client(state) as canvas:
        result = canvas.pages.get(1, "4")
    assert result["page_id"] == 5
    assert any(request.url.path.endswith("/page_id:5/revisions/latest") for request in state.requests)


@pytest.mark.parametrize("patch", [
    {"published": False}, {"published": None}, {"published": 1}, {"locked_for_user": True},
    {"locked_for_user": None}, {"locked_for_user": 0}, {"lock_info": {}}, {"lock_info": []},
    {"lock_info": ""}, {"editor": None}, {"editor": "block_editor"}, {"editor": "block_content_editor"},
    {"editor": "unknown"}, {"updated_at": None}, {"updated_at": "bad"}, {"updated_at": "2026-01-01"},
    {"page_id": True}, {"page_id": 0}, {"title": None},
])
def test_unsafe_or_unverifiable_metadata_never_fetches_latest(patch):
    state = PageState()
    state.page.update(patch)
    with client(state) as canvas, pytest.raises(CanvasError):
        canvas.pages.get(1, "reading")
    assert not any(request.url.path.endswith("/revisions/latest") for request in state.requests)
    assert not state.viewed


@pytest.mark.parametrize("field", ["published", "locked_for_user", "updated_at", "title"])
def test_required_omitted_metadata_is_not_defaulted_to_available(field):
    state = PageState()
    state.page.pop(field)
    with client(state) as canvas, pytest.raises(CanvasError):
        canvas.pages.get(1, "reading")
    assert not any(request.url.path == LATEST for request in state.requests)


@pytest.mark.parametrize("editor", ["omitted", "rce"])
@pytest.mark.parametrize("body", [SECRET, ""])
def test_classic_and_rce_saved_html_keep_explicit_partial_provenance(editor, body):
    state = PageState()
    if editor != "omitted":
        state.page["editor"] = editor
    state.revision["body"] = body
    state.course.pop("horizon_course")
    with client(state) as canvas:
        result = canvas.pages.get(1, "reading")
    assert result["body"] == body and result["content_complete"] is False
    assert result["horizon_course"] is None and "did not report" in result["content_warning"]


@pytest.mark.parametrize("value", [True, None, "false", 0, 1])
def test_known_horizon_and_invalid_present_course_flag_stop_before_content(value):
    state = PageState()
    state.course["horizon_course"] = value
    with client(state) as canvas, pytest.raises(CanvasError):
        canvas.pages.get(1, "reading")
    assert len(state.requests) == 1 and not state.viewed


@pytest.mark.parametrize("patch", [
    {"latest": False}, {"latest": 1}, {"revision_id": 0}, {"revision_id": True},
    {"title": "Changed"}, {"url": "another-page"}, {"updated_at": "2026-01-02T00:00:00Z"},
    {"body": None}, {"body": {}},
])
def test_latest_revision_identity_and_representation_must_match(patch):
    state = PageState()
    state.revision.update(patch)
    with client(state) as canvas, pytest.raises(CanvasError):
        canvas.pages.get(1, "reading")
    assert not state.viewed


@pytest.mark.parametrize("patch", [
    {"page_id": 5}, {"url": "new-slug"}, {"title": "Changed"},
    {"updated_at": "2026-01-02T00:00:00Z"}, {"published": False}, {"locked_for_user": True},
    {"lock_info": {}}, {"editor": "block_editor"}, {"front_page": False},
])
def test_changed_metadata_after_latest_never_releases_body(patch):
    state = PageState()
    state.after_revision = lambda state: state.page.update(patch)
    with client(state) as canvas, pytest.raises(CanvasError):
        canvas.pages.get(1, "reading")
    assert state.lists == 2 and not state.viewed


def test_course_rendering_change_after_latest_never_releases_body():
    state = PageState()
    state.after_revision = lambda state: state.course.update(horizon_course=True)
    with client(state) as canvas, pytest.raises(CanvasError):
        canvas.pages.get(1, "reading")
    assert not state.viewed


@pytest.mark.parametrize("path", [COLLECTION, LATEST])
@pytest.mark.parametrize("status", [403, 404, 500])
def test_denied_disabled_or_unversioned_page_is_not_successful_empty_content(path, status):
    state = PageState()
    state.errors[path] = status
    with client(state) as canvas:
        result = call(canvas, "canvas_open_item", course_id=1, item_type="page", item_id="reading")
    assert result["status"] != "ok" and result["coverage"]["complete"] is False
    assert SECRET not in json.dumps(result) and not state.viewed


def test_complete_late_page_match_requires_both_complete_enumerations():
    state = PageState()
    state.rows = [[{**state.page, "page_id": 9, "url": "other", "front_page": False}], [state.page]]
    with client(state) as canvas:
        assert canvas.pages.get(1, "reading")["body"] == SECRET
    assert sum(request.url.path == COLLECTION for request in state.requests) == 4


@pytest.mark.parametrize("rows", [[], [{"page_id": 9, "url": "other"}]])
def test_complete_absence_is_404(rows):
    state = PageState()
    state.rows = [rows]
    with client(state) as canvas, pytest.raises(CanvasNotFoundError):
        canvas.pages.get(1, "reading")
    assert not any(request.url.path == LATEST for request in state.requests)


@pytest.mark.parametrize("duplicate", ["id", "slug", "front_page"])
def test_duplicate_identity_or_front_page_cannot_pick_arbitrary_target(duplicate):
    state = PageState()
    other = {**state.page, "page_id": 5, "url": "another", "front_page": False}
    other[{"id": "page_id", "slug": "url", "front_page": "front_page"}[duplicate]] = state.page[
        {"id": "page_id", "slug": "url", "front_page": "front_page"}[duplicate]]
    state.rows = [[state.page], [other]]
    with client(state) as canvas, pytest.raises(CanvasError):
        canvas.pages.front_page(1)
    assert not any(request.url.path == LATEST for request in state.requests)


@pytest.mark.parametrize("query", [
    "include%5B%5D=body", "include=body", "includes=body", "includes%5B%5D=body", "body=true",
    "include%255B%255D=body", "sort=updated_at", "order=desc", "page=2&page=3", "per_page=1000",
])
def test_next_page_cannot_inject_body_or_change_collection_query_before_dispatch(query):
    state = PageState()
    state.next_link = f'<{ORIGIN}{COLLECTION}?page=2&{query}>; rel="next"'
    with client(state) as canvas, pytest.raises(CanvasError):
        canvas.pages.get(1, "reading")
    assert len(state.requests) == 2 and not state.viewed


@pytest.mark.parametrize("destination", [COLLECTION + "/reading", LATEST, "/api/v1/courses/2/pages"])
def test_diverted_pagination_never_dispatches_other_routes(destination):
    state = PageState()
    state.next_link = f'<{ORIGIN}{destination}>; rel="next"'
    with client(state) as canvas, pytest.raises(CanvasError):
        canvas.pages.get(1, "reading")
    assert len(state.requests) == 2 and not state.viewed


@pytest.mark.parametrize("path", [COLLECTION, LATEST, "/api/v1/courses/1"])
def test_metadata_revision_and_course_redirects_never_dispatch_show(path):
    state = PageState()
    state.redirects[path] = ORIGIN + COLLECTION + "/reading"
    with client(state) as canvas, pytest.raises(CanvasError):
        canvas.pages.get(1, "reading")
    assert not state.viewed
    assert not any(request.url.path.endswith("/pages/reading") for request in state.requests)


def test_initial_metadata_query_cannot_include_body():
    state = PageState()
    request = PageMetadataRequest("courses/1")
    request._query.append(("include[]", "body"))
    with client(state) as canvas, pytest.raises(CanvasError):
        list(canvas.paginate(request))
    assert not state.requests


def test_full_metadata_limit_and_iterator_closure():
    class Reader:
        count = 0
        closed = False

        def paginate(self, request):
            try:
                for item in range(MAX_PAGES + 100):
                    self.count += 1
                    yield {"page_id": item + 1, "url": f"page-{item}"}
            finally:
                self.closed = True

    reader = Reader()
    with pytest.raises(CanvasError, match="1000"):
        read_page_raw(reader, "group", 2, "reading")
    assert reader.count == MAX_PAGES + 1 and reader.closed


@pytest.mark.parametrize("selector", ["../x", "a/b", "a%2fb", "a?x=y", "a#x", "page_id:0", "page_id:true", "\x7f"])
def test_invalid_selector_rejected_before_network(selector):
    state = PageState()
    with client(state) as canvas, pytest.raises(ValueError):
        canvas.pages.get(1, selector)
    assert not state.requests


def test_group_membership_rechecked_before_body_release():
    state = PageState()
    state.after_revision = lambda state: state.membership.update(workflow_state="deleted")
    with client(state) as canvas, pytest.raises(ValueError, match="accepted"):
        group_content.get_group_page(canvas, 2, "reading")
    assert not state.viewed


@pytest.mark.parametrize("query", [
    "page=2&per_page=100&order=asc", "page=2&per_page=100&sort=title",
    "page=2&sort=title&order=asc", "page=2&per_page=20&sort=title&order=asc",
    "page=3&per_page=100&sort=title&order=asc", "per_page=100&sort=title&order=asc",
])
def test_metadata_pagination_requires_original_order_window_and_next_ordinal(query):
    state = PageState()
    state.next_link = f'<{ORIGIN}{COLLECTION}?{query}>; rel="next"'
    with client(state) as canvas, pytest.raises(CanvasError):
        canvas.pages.get(1, "reading")
    assert len(state.requests) == 2 and not state.viewed


def test_internal_page_handoff_reason_survives_navigation_but_upstream_errors_stay_sanitized():
    state = PageState()
    state.course["horizon_course"] = True
    with client(state) as canvas:
        result = call(canvas, "canvas_open_item", course_id=1, item_type="page", item_id="reading")
    assert result["status"] == "unavailable"
    assert "Horizon" in json.dumps(result) and "Canvas page link" in json.dumps(result)
    assert SECRET not in json.dumps(result)
    state = PageState()
    state.errors[LATEST] = 403
    with client(state) as canvas:
        result = call(canvas, "canvas_open_item", course_id=1, item_type="page", item_id="reading")
    assert "synthetic unavailable" not in json.dumps(result)
    assert result["status"] == "forbidden"


@pytest.mark.parametrize("old,new", [(None, None), ("omitted", None), (None, "omitted"), ({"id": 7}, {"id": 7})])
def test_matching_optional_or_imported_null_revision_authors_are_valid(old, new):
    state = PageState()
    if old != "omitted":
        state.page["last_edited_by"] = old
    if new != "omitted":
        state.revision["edited_by"] = new
    with client(state) as canvas:
        assert canvas.pages.get(1, "reading")["body"] == SECRET
    assert not state.viewed


@pytest.mark.parametrize("old,new", [(None, {"id": 7}), ({"id": 7}, None), ({"id": 7}, {"id": 8}),
                                     ({"id": True}, {"id": 1}), ({"id": 7}, "omitted")])
def test_mismatched_revision_authors_are_unavailable(old, new):
    state = PageState()
    state.page["last_edited_by"] = old
    if new != "omitted":
        state.revision["edited_by"] = new
    with client(state) as canvas, pytest.raises(CanvasError, match="author"):
        canvas.pages.get(1, "reading")
    assert not state.viewed


def test_group_front_page_slug_is_exact_identity_not_front_page_discovery():
    state = PageState()
    state.page.update(url="front_page", front_page=False)
    state.revision["url"] = "front_page"
    state.rows = [[state.page, {**state.page, "page_id": 5, "url": "welcome", "front_page": True}]]
    with client(state) as canvas:
        result = group_content.get_group_page(canvas, 2, "front_page")
    assert result["page_id"] == 4 and result["url"] == "front_page" and result["front_page"] is False
    assert any(request.url.path == "/api/v1/groups/2/pages/page_id:4/revisions/latest" for request in state.requests)
    assert not any("page_id:5" in request.url.path for request in state.requests)
