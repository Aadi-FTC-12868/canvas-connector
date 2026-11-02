"""End-to-end mocked navigation flows: completeness, identity, cache, boundaries."""
from __future__ import annotations

from collections import Counter

import httpx
import pytest

from canvas_py import Canvas
from canvas_py._config import CanvasConfig
from canvas_py._errors import CanvasAuthError
from canvas_py.navigation import CanvasNavigation

ORIGIN = "https://school.example"


def make_canvas(handler):
    return Canvas(CanvasConfig(base_url=ORIGIN, token="test-token"),
                  http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_link_only_file_remains_readable_and_routes_to_document_reader():
    with make_canvas(lambda request: httpx.Response(200, json={
        "id": 12, "display_name": "linked.pdf", "hidden_for_user": True,
        "locked_for_user": False,
    })) as canvas:
        result = canvas.navigation.open_item(1, "file", "12")
    assert result["data"]["access"] == "available"
    assert result["data"]["visibility"] == "link_only"
    assert result["data"]["content_tool"] == {"tool": "canvas_read_file", "file_id": 12}


def test_modules_hydrates_missing_and_truncated_inline_items_through_all_pages():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/modules"):
            return httpx.Response(200, json=[
                {"id": 10, "items_count": 2},
                {"id": 20, "items_count": 2, "items": [{"id": 201}]},
                {"id": 30, "items_count": 1, "items": [{"id": 301}]},
            ])
        module_id = int(request.url.path.split("/")[-2])
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[{"id": module_id * 10 + 2}])
        return httpx.Response(200, json=[{"id": module_id * 10 + 1}], headers={
            "Link": f'<{ORIGIN}/api/v1/courses/1/modules/{module_id}/items?page=2>; rel="next"'})

    canvas = make_canvas(handler)
    modules = canvas.modules.list(1)
    assert [[i.id for i in module.items] for module in modules] == [[101, 102], [201, 202], [301]]
    assert len(calls) == 5


def test_modules_without_item_include_does_not_fetch_items():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=[{"id": 10, "items_count": 100}])

    canvas = make_canvas(handler)
    result = canvas.navigation.browse_course(1)
    assert len(calls) == 1
    assert result["data"]["items"][0]["open"]["item_type"] == "module"


def test_module_overview_preserves_progression_and_completion_context():
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/api/v1/courses/1/modules"
        return httpx.Response(200, json=[{"id": 10, "prerequisite_module_ids": [4],
            "require_sequential_progress": False, "requirement_type": "one",
            "state": "completed", "completed_at": "2026-01-02T03:04:05Z"}])
    result = make_canvas(handler).navigation.browse_course(1)
    module = result["data"]["items"][0]
    assert module["prerequisite_module_ids"] == [4]
    assert module["require_sequential_progress"] is False
    assert module["requirement_type"] == "one"
    assert module["state"] == "completed" and module["completed_at"].startswith("2026-01-02T03:04:05")


def test_dashboard_home_page_item_and_links_follow_typed_navigation():
    def handler(request):
        path = request.url.path
        if path == "/api/v1/courses":
            return httpx.Response(200, json=[{"id": 1, "name": "Demo"}])
        if path == "/api/v1/courses/1":
            assert "syllabus_body" in request.url.params.get_list("include[]")
            return httpx.Response(200, json={"id": 1, "name": "Demo", "default_view": "wiki"})
        if path.endswith("/tabs"):
            return httpx.Response(200, json=[{"id": "modules", "position": 2}, {"id": "home", "position": 3}, {"id": "files", "hidden": True}])
        if path.endswith("/front_page"):
            return httpx.Response(200, json={"page_id": 5, "url": "welcome", "body": '<a href="/courses/1/assignments/7">Read</a><img src="/files/3/preview"><a href="https://external.example/tool">Tool</a>'})
        if path.endswith("/assignments/7"):
            return httpx.Response(200, json={"id": 7, "name": "Read", "description": "The assignment"})
        raise AssertionError(path)

    nav = make_canvas(handler).navigation
    course_id = nav.dashboard()["data"]["courses"][0]["open"]["course_id"]
    home = nav.course_home(course_id)
    assert home["data"]["tabs"][0]["id"] == "home"
    assert home["data"]["tabs"][1]["access"] == "hidden"
    links = home["data"]["home"]["links"]
    assert links[0]["open"]["item_type"] == "assignment"
    assert links[1]["open"] == {"tool": "get_file", "file_id": 3}
    assert links[2]["status"] == "external"
    assert nav.open_url(links[0]["url"])["data"]["name"] == "Read"


def test_front_page_url_uses_front_page_endpoint():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"url": "welcome", "body": "Hello"})

    nav = make_canvas(handler).navigation
    assert nav.open_url("/courses/1/pages/front_page")["data"]["body"] == "Hello"
    assert calls == ["/api/v1/courses/1/front_page"]


@pytest.mark.parametrize("url,status", [
    ("https://external.example/courses/1/pages/demo", "external"),
    ("https://school.example.evil.test/courses/1", "external"),
    ("http://school.example/courses/1", "external"),
    ("https://user:password@school.example/courses/1", "unsupported"),
    ("/api/v1/courses/1/assignments/7/submissions", "unsupported"),
    ("/courses/1/external_tools/4", "external"),
    ("/courses/1/pages/%2e%2e", "unsupported"),
    ("http://[broken", "unsupported"),
    ("javascript:alert(1)", "unsupported"),
])
def test_external_unsafe_and_unsupported_urls_never_make_network_requests(url, status):
    def handler(request):
        raise AssertionError("A refused URL made a network request")

    assert make_canvas(handler).navigation.open_url(url)["status"] == status


def test_module_item_lock_stops_content_fetch_and_preserves_requirements():
    def handler(request):
        assert request.url.path == "/api/v1/courses/1/modules/10/items/101"
        return httpx.Response(200, json={"id": 101, "type": "Assignment", "content_id": 7,
            "completion_requirement": {"type": "must_submit", "completed": False},
            "content_details": {"locked_for_user": True, "lock_explanation": "Finish previous module"}})

    result = make_canvas(handler).navigation.open_item(1, "module_item", "101", module_id=10)
    assert result["data"]["access"] == "locked"
    assert result["data"]["completion_requirement"]["completed"] is False
    assert "content" not in result["data"]


def test_module_item_reads_target_and_sequence_never_marks_read():
    calls = []

    def handler(request):
        assert request.method == "GET"
        calls.append(request.url.path)
        if request.url.path.endswith("/items/101"):
            return httpx.Response(200, json={"id": 101, "type": "Page", "page_url": "intro", "content_details": {"locked_for_user": False}})
        if request.url.path.endswith("/pages/intro"):
            return httpx.Response(200, json={"page_id": 4, "url": "intro", "body": "Intro"})
        assert request.url.params["asset_type"] == "ModuleItem"
        return httpx.Response(200, json={"items": [{"prev": {"id": 100}, "current": {"id": 101}, "next": {"id": 103}}]})

    nav = make_canvas(handler).navigation
    opened = nav.open_item(1, "module_item", "101", module_id=10)
    assert opened["data"]["content"]["data"]["body"] == "Intro"
    sequence = nav.module_sequence(1, "ModuleItem", "101")
    assert sequence["data"]["items"][0]["next"]["id"] == 103
    assert len(calls) == 3


def test_files_forbidden_is_not_empty_and_has_modules_alternative():
    nav = make_canvas(lambda request: httpx.Response(403, json={"message": "forbidden"})).navigation
    result = nav.browse_course(1, "files")
    assert result["status"] == "forbidden"
    assert "data" not in result
    assert result["alternative"]["section"] == "modules"


def test_navigation_cache_window_refresh_and_mutation_isolation():
    calls = Counter()

    def handler(request):
        calls[request.url.path] += 1
        return httpx.Response(200, json=[{"page_id": i, "url": f"page-{i}", "title": f"Page {i}"} for i in range(1, 6)])

    nav = make_canvas(handler).navigation
    first = nav.browse_course(1, "pages", limit=2)
    first["data"]["items"][0]["title"] = "Mutated"
    second = nav.browse_course(1, "pages", offset=2, limit=2)
    assert second["source"]["cache_hit"] is True
    assert second["data"]["next_offset"] == 4
    assert sum(calls.values()) == 1
    original = nav.browse_course(1, "pages", limit=2)
    assert original["data"]["items"][0]["title"] == "Page 1"
    refreshed = nav.browse_course(1, "pages", refresh=True)
    assert refreshed["source"]["cache_hit"] is False
    assert sum(calls.values()) == 2


def test_failed_refresh_clears_stale_entry_and_auth_clears_entire_cache():
    status = 200

    def handler(request):
        return httpx.Response(status, json=[{"id": 1}] if status == 200 else {"message": "Denied"})

    nav = make_canvas(handler).navigation
    nav.dashboard()
    status = 401
    with pytest.raises(CanvasAuthError):
        nav.dashboard(refresh=True)
    assert nav.clear_cache()["cleared_entries"] == 0
    with pytest.raises(CanvasAuthError):
        nav.dashboard()


def test_cache_is_bounded_and_not_shared_between_accounts():
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(200, json=[])

    canvas = make_canvas(handler)
    nav = CanvasNavigation(canvas, max_entries=1)
    nav.dashboard()
    nav.browse_course(1, "pages")
    nav.dashboard()
    assert count == 3
    assert nav.clear_cache()["cleared_entries"] == 1
    make_canvas(handler).navigation.dashboard()
    assert count == 4


def test_cached_result_larger_than_byte_budget_is_not_stored():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[{"id": 1, "name": "a long course name"}])

    nav = CanvasNavigation(make_canvas(handler), max_bytes=1)
    nav.dashboard()
    nav.dashboard()
    assert calls == 2


def test_cache_ttl_expires_and_clear_during_inflight_read_cannot_repopulate(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("canvas_py.navigation.time.monotonic", lambda: clock[0])
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[])

    nav = CanvasNavigation(make_canvas(handler), ttl_seconds=2)
    nav.dashboard()
    clock[0] += 3
    assert nav.dashboard()["source"]["cache_hit"] is False
    assert calls == 2

    def invalidated_loader():
        nav.clear_cache()
        return {"old_account_data": True}

    nav._load(("inflight",), invalidated_loader, False)
    assert nav.clear_cache()["cleared_entries"] == 0


def test_module_item_composition_cannot_extend_nested_page_ttl(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("canvas_py.navigation.time.monotonic", lambda: clock[0])
    calls = Counter()

    def handler(request):
        calls[request.url.path] += 1
        if request.url.path.endswith("/items/101"):
            return httpx.Response(200, json={"id": 101, "type": "Page", "page_url": "intro"})
        return httpx.Response(200, json={"page_id": 4, "url": "intro", "body": f"Version {calls[request.url.path]}"})

    nav = make_canvas(handler).navigation
    nav.open_item(1, "page", "intro")
    clock[0] += 80
    first = nav.open_item(1, "module_item", "101", module_id=10)
    assert first["data"]["content"]["source"]["age_seconds"] == 80
    clock[0] += 80
    second = nav.open_item(1, "module_item", "101", module_id=10)
    assert second["source"]["cache_policy"] == "composed_response_not_cached"
    assert second["data"]["content"]["source"]["cache_hit"] is False
    assert second["data"]["content"]["data"]["body"] == "Version 2"
    assert calls["/api/v1/courses/1/pages/intro"] == 2


def test_home_composition_cannot_extend_front_page_ttl(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("canvas_py.navigation.time.monotonic", lambda: clock[0])
    calls = Counter()

    def handler(request):
        calls[request.url.path] += 1
        if request.url.path.endswith("/tabs"):
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/front_page"):
            return httpx.Response(200, json={"url": "welcome", "body": f"Version {calls[request.url.path]}"})
        return httpx.Response(200, json={"id": 1, "default_view": "wiki"})

    nav = make_canvas(handler).navigation
    nav.course_home(1)
    clock[0] += 80
    first = nav.course_home(1)
    assert first["data"]["home"]["content"]["source"]["age_seconds"] == 80
    clock[0] += 80
    second = nav.course_home(1)
    assert second["data"]["home"]["content"]["source"]["cache_hit"] is False
    assert second["data"]["home"]["content"]["data"]["body"] == "Version 2"


@pytest.mark.parametrize("item_id", ["../7", "0", "-1", "7/submissions"])
def test_invalid_numeric_id_rejected_before_request(item_id):
    nav = make_canvas(lambda request: pytest.fail("Unexpected request")).navigation
    with pytest.raises(ValueError):
        nav.open_item(1, "assignment", item_id)
