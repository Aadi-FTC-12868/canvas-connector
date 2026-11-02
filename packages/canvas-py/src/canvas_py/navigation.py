"""Student navigation graph over the same reads used by Canvas mobile apps.

The cache belongs to one Canvas client, is memory-only, and never masks a
failed refresh with stale data. Replacing/closing the client drops its cache.
No URL supplied by coursework is fetched directly.
"""
from __future__ import annotations

import copy
import json
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from ._errors import CanvasAuthError, CanvasError

if TYPE_CHECKING:
    from ._client import Canvas

CourseSection = Literal["modules", "pages", "assignments", "files", "quizzes", "announcements", "discussions", "syllabus", "grades"]
ItemType = Literal["module", "module_item", "page", "assignment", "file", "quiz", "discussion"]
AssetType = Literal["ModuleItem", "File", "Page", "Discussion", "Assignment", "Quiz", "ExternalTool"]

SECTIONS = set(CourseSection.__args__)
ITEM_TYPES = set(ItemType.__args__)
ASSET_TYPES = set(AssetType.__args__)
_MODULE_TYPES = {"Assignment": "assignment", "Page": "page", "File": "file", "Quiz": "quiz", "Discussion": "discussion"}
_SUMMARY_FIELDS = ("id", "page_id", "name", "title", "display_name", "course_code", "type", "position", "indent", "due_at", "unlock_at", "lock_at", "locked_for_user", "lock_explanation", "hidden_for_user", "state", "workflow_state", "items_count", "published", "points_possible", "submission", "completion_requirement", "content_details", "url", "html_url", "content_id", "page_url", "module_id", "external_url", "prerequisite_module_ids", "require_sequential_progress", "requirement_type", "completed_at")


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_dump(item) for item in value]
    if isinstance(value, dict):
        return {key: _dump(item) for key, item in value.items()}
    return value


def _positive(value: int | str, name: str = "id") -> int:
    if isinstance(value, bool) or not str(value).isdigit() or int(value) < 1:
        raise ValueError(f"{name} must be a positive integer")
    return int(value)


def _window(offset: int, limit: int) -> None:
    if isinstance(offset, bool) or isinstance(limit, bool) or offset < 0 or not 1 <= limit <= 100:
        raise ValueError("offset must be nonnegative and limit must be 1..100")


def _page(items: list[Any], offset: int, limit: int) -> dict[str, Any]:
    _window(offset, limit)
    return {"items": items[offset:offset + limit], "total": len(items), "offset": offset,
            "next_offset": offset + limit if offset + limit < len(items) else None}


class CanvasNavigation:
    """Read-only, compact application navigation with explicit freshness."""

    def __init__(self, canvas: Canvas, *, ttl_seconds: float = 90, max_entries: int = 128,
                 max_bytes: int = 8 * 1024 * 1024):
        self.canvas = canvas
        self.base_url = canvas.config.base_url.rstrip("/")
        self.ttl_seconds = max(0.0, ttl_seconds)
        self.max_entries, self.max_bytes = max(0, max_entries), max(0, max_bytes)
        self._cache: OrderedDict[tuple[Any, ...], tuple[float, str, Any, int]] = OrderedDict()
        self._bytes = 0
        self._generation = 0
        self._lock = threading.RLock()

    def clear_cache(self) -> dict[str, Any]:
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            self._bytes = 0
            self._generation += 1
        return {"status": "ok", "cleared_entries": count, "upstream_writes": False}

    def _load(self, key: tuple[Any, ...], loader: Callable[[], Any], refresh: bool,
              cache_result: bool = True) -> tuple[Any, dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            generation = self._generation
            hit = self._cache.get(key)
            if hit and cache_result and not refresh and now - hit[0] < self.ttl_seconds:
                self._cache.move_to_end(key)
                return copy.deepcopy(hit[2]), self._source(hit[1], True, now - hit[0])
            if hit:
                self._bytes -= self._cache.pop(key)[3]
        try:
            value = _dump(loader())
        except CanvasAuthError:
            self.clear_cache()
            raise
        fetched_at = datetime.now(UTC).isoformat()
        size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        with self._lock:
            if cache_result and generation == self._generation and self.max_entries and size <= self.max_bytes and self.ttl_seconds:
                # Another caller may have finished this key while our network read ran.
                old = self._cache.pop(key, None)
                if old:
                    self._bytes -= old[3]
                while self._cache and (len(self._cache) >= self.max_entries or self._bytes + size > self.max_bytes):
                    self._bytes -= self._cache.popitem(last=False)[1][3]
                self._cache[key] = (time.monotonic(), fetched_at, copy.deepcopy(value), size)
                self._bytes += size
        source = self._source(fetched_at, False, 0)
        if not cache_result:
            source.update({"cache_policy": "composed_response_not_cached", "ttl_seconds": 0})
        return value, source

    def _source(self, fetched_at: str, cached: bool, age: float) -> dict[str, Any]:
        return {"canvas_origin": self.base_url, "fetched_at": fetched_at, "cache_hit": cached,
                "age_seconds": round(age, 3), "ttl_seconds": self.ttl_seconds,
                "content_is_untrusted": True}

    def _read(self, key: tuple[Any, ...], loader: Callable[[], Any], refresh: bool,
              cache_result: bool = True) -> dict[str, Any]:
        try:
            data, source = self._load(key, loader, refresh, cache_result)
            return {"status": "ok", "data": data, "source": source}
        except CanvasAuthError:
            raise
        except CanvasError as error:
            # Never relabel a failed read as an empty course or cached success.
            status = {403: "forbidden", 404: "not_found_or_disabled"}.get(error.status, "unavailable")
            return {"status": status, "http_status": error.status,
                    "message": "Canvas did not provide this resource. Retry with refresh or open its Canvas link.",
                    "source": {"canvas_origin": self.base_url, "cache_hit": False,
                               "attempted_at": datetime.now(UTC).isoformat()}}

    def _route(self, course_id: int, item_type: str, item_id: Any, module_id: int | None = None) -> dict[str, Any]:
        route = {"tool": "canvas_open_item", "course_id": course_id, "item_type": item_type, "item_id": str(item_id)}
        if module_id is not None:
            route["module_id"] = module_id
        return route

    def _summary(self, raw: Any, course_id: int | None = None, item_type: str | None = None) -> dict[str, Any]:
        item = _dump(raw)
        result = {key: item[key] for key in _SUMMARY_FIELDS if item.get(key) is not None}
        if course_id is not None and item_type:
            item_id = item.get("url") if item_type == "page" else item.get("id")
            if item_id is not None:
                result["open"] = self._route(course_id, item_type, item_id)
        return result

    def _module_item(self, raw: Any, course_id: int, module_id: int) -> dict[str, Any]:
        item = self._summary(raw)
        item["module_id"] = module_id
        item["open"] = self._route(course_id, "module_item", item["id"], module_id)
        details = item.get("content_details") or {}
        item["access"] = "locked" if details.get("locked_for_user") else (
            "external" if item.get("type") in {"ExternalUrl", "ExternalTool"} else
            "heading" if item.get("type") == "SubHeader" else "available")
        return item

    def dashboard(self, *, refresh: bool = False) -> dict[str, Any]:
        def load() -> dict[str, Any]:
            courses = self.canvas.courses.list_active()
            return {"courses": [{**self._summary(course), "open": {"tool": "canvas_course_home", "course_id": course.id}}
                                for course in courses],
                    "navigation": {"workflow": "dashboard → course home/tabs → section → item → linked resource",
                                   "planner_tool": "planner_items", "inbox_tool": "list_inbox"}}
        return self._read(("dashboard",), load, refresh)

    def course_home(self, course_id: int, *, refresh: bool = False) -> dict[str, Any]:
        course_id = _positive(course_id, "course_id")

        def load() -> dict[str, Any]:
            course = _dump(self.canvas.courses.get(course_id, include=["term", "teachers", "course_progress", "total_scores", "syllabus_body"]))
            tabs = self.canvas.courses.tabs(course_id)
            enriched = []
            for tab in sorted(tabs, key=lambda t: (t.get("id") != "home", t.get("position") or 0)):
                entry = dict(tab)
                entry["access"] = "hidden" if tab.get("hidden") else "external" if tab.get("type") == "external" else "available"
                if tab.get("id") in SECTIONS:
                    entry["open"] = {"tool": "canvas_browse_course", "course_id": course_id, "section": tab["id"]}
                elif tab.get("html_url"):
                    entry["open"] = {"tool": "canvas_open_url", "url": tab["html_url"]}
                enriched.append(entry)
            default = course.get("default_view") or "unknown"
            section = {"modules": "modules", "assignments": "assignments", "syllabus": "syllabus"}.get(default)
            home: dict[str, Any] = {"default_view": default}
            if default == "wiki":
                # Failure is represented explicitly; an absent front page is not an empty page.
                home["content"] = self._read(("front_page", course_id), lambda: self.canvas.pages.front_page(course_id), refresh)
                if home["content"]["status"] == "ok":
                    page = home["content"]["data"]
                    home["links"] = self._links(page.get("body"), f"/courses/{course_id}/pages/{page.get('url', '')}")
            elif section:
                home["open"] = {"tool": "canvas_browse_course", "course_id": course_id, "section": section}
            else:
                home.update({"status": "browser_required", "html_url": f"{self.base_url}/courses/{course_id}",
                             "reason": "This home view uses Canvas web activity or an unsupported tool."})
            return {"course": {key: course.get(key) for key in ("id", "name", "course_code", "default_view", "term", "teachers", "course_progress")},
                    "home": home, "tabs": enriched,
                    "html_url": f"{self.base_url}/courses/{course_id}"}
        # An outer cached response must never extend its nested front page's TTL.
        return self._read(("home", course_id), load, refresh, cache_result=False)

    def browse_course(self, course_id: int, section: CourseSection = "modules", *, offset: int = 0,
                      limit: int = 30, refresh: bool = False) -> dict[str, Any]:
        course_id = _positive(course_id, "course_id")
        _window(offset, limit)
        if section not in SECTIONS:
            raise ValueError("unsupported course section")

        def load() -> Any:
            if section == "modules":
                return [self._summary(item, course_id, "module") for item in self.canvas.modules.list(course_id, include=[])]
            if section == "pages":
                return [self._summary(item, course_id, "page") for item in self.canvas.pages.list(course_id)]
            if section == "assignments":
                return [self._summary(item, course_id, "assignment") for item in self.canvas.assignments.list(course_id)]
            if section == "files":
                return [self._summary(item, course_id, "file") for item in self.canvas.files.list_course_files(course_id)]
            if section == "quizzes":
                return [self._summary(item, course_id, "quiz") for item in self.canvas.quizzes.list(course_id)]
            if section in {"announcements", "discussions"}:
                return [self._summary(item, course_id, "discussion") for item in self.canvas.discussions.list(course_id, only_announcements=section == "announcements")]
            course = _dump(self.canvas.courses.get(course_id, include=["syllabus_body", "total_scores", "current_grading_period_scores"]))
            if section == "syllabus":
                return {"body": course.get("syllabus_body"), "links": self._links(course.get("syllabus_body"), f"/courses/{course_id}/assignments/syllabus")}
            return {"enrollments": course.get("enrollments"), "meaning": "Grades as reported by Canvas; no locally calculated final grade.",
                    "assignments": {"tool": "canvas_browse_course", "course_id": course_id, "section": "assignments"}}
        result = self._read(("section", course_id, section), load, refresh)
        result.update({"course_id": course_id, "section": section,
                       "html_url": f"{self.base_url}/courses/{course_id}/{section}"})
        if result["status"] == "ok" and isinstance(result["data"], list):
            result["data"] = _page(result["data"], offset, limit)
        if section == "files" and result["status"] == "forbidden":
            result["alternative"] = {"tool": "canvas_browse_course", "course_id": course_id, "section": "modules",
                                     "reason": "Canvas may permit files linked from modules while denying file enumeration."}
        return result

    def open_item(self, course_id: int, item_type: ItemType, item_id: str, *, module_id: int | None = None,
                  offset: int = 0, limit: int = 30, refresh: bool = False) -> dict[str, Any]:
        course_id = _positive(course_id, "course_id")
        _window(offset, limit)
        if item_type not in ITEM_TYPES:
            raise ValueError("unsupported item type")
        if item_type != "page":
            item_id = str(_positive(item_id, "item_id"))
        if module_id is not None:
            module_id = _positive(module_id, "module_id")

        def load() -> Any:
            if item_type == "module":
                items = self.canvas.modules.items(course_id, int(item_id))
                return {"module_id": int(item_id), "items": [self._module_item(item, course_id, int(item_id)) for item in items]}
            if item_type == "module_item":
                item = None
                resolved_module = module_id
                if resolved_module is not None:
                    item = self.canvas.modules.get_item(course_id, resolved_module, int(item_id))
                else:
                    for module in self.canvas.modules.list(course_id):
                        item = next((x for x in module.items or [] if x.id == int(item_id)), None)
                        if item is not None:
                            resolved_module = module.id
                            break
                if item is None:
                    return {"access": "not_found_or_disabled", "module_item_id": item_id}
                data = self._module_item(item, course_id, resolved_module)
                item_data = _dump(item)
                if data["access"] == "available":
                    kind = _MODULE_TYPES.get(item_data.get("type"))
                    identity = item_data.get("page_url") if kind == "page" else item_data.get("content_id")
                    if kind and identity is not None:
                        data["content"] = self.open_item(course_id, kind, str(identity), refresh=refresh)
                    else:
                        data["access"] = "unsupported"
                if data["access"] == "external":
                    data["reason"] = "External tools require their own browser session; no Canvas token is forwarded."
                data["sequence"] = {"tool": "canvas_module_sequence", "course_id": course_id, "asset_type": "ModuleItem", "asset_id": item_id}
                return data
            if item_type == "page":
                data = self.canvas.pages.front_page(course_id) if item_id == "front_page" else self.canvas.pages.get(course_id, item_id)
            elif item_type == "assignment":
                data = _dump(self.canvas.assignments.get(course_id, int(item_id)))
            elif item_type == "file":
                data = _dump(self.canvas.files.get(int(item_id)))
            elif item_type == "quiz":
                data = self.canvas.quizzes.get(course_id, int(item_id))
            else:
                data = _dump(self.canvas.discussions.get(course_id, int(item_id)))
            data["access"] = "locked" if data.get("locked_for_user") else (
                "hidden" if item_type != "file" and data.get("hidden_for_user") else "available")
            html = data.get("body") or data.get("description") or data.get("message")
            data["links"] = self._links(html, data.get("html_url") or f"/courses/{course_id}")
            if item_type == "file":
                data["visibility"] = "link_only" if data.get("hidden_for_user") else "listed"
                data["content_tool"] = {"tool": "canvas_read_file", "file_id": int(item_id)}
                data["original_bytes_tool"] = {"tool": "fetch_file_content", "file_id": int(item_id), "as_text": False}
            if item_type == "quiz":
                data["scope"] = "Quiz metadata only. Starting attempts and accessing questions are not navigation reads."
            return data
        # Module item metadata may contain a separately cached page/assignment.
        # Recompose it so each dependency is checked against its own TTL.
        result = self._read(("item", course_id, item_type, item_id, module_id), load, refresh,
                            cache_result=item_type != "module_item")
        result["route"] = self._route(course_id, item_type, item_id, module_id)
        if result["status"] == "ok" and item_type == "module":
            result["data"] = {"module_id": int(item_id), **_page(result["data"]["items"], offset, limit)}
        return result

    def module_sequence(self, course_id: int, asset_type: AssetType, asset_id: str, *, refresh: bool = False) -> dict[str, Any]:
        course_id = _positive(course_id, "course_id")
        if asset_type not in ASSET_TYPES:
            raise ValueError("unsupported sequence asset type")
        if not asset_id or len(asset_id) > 500:
            raise ValueError("asset_id must be nonempty and at most 500 characters")
        return self._read(("sequence", course_id, asset_type, asset_id),
                          lambda: self.canvas.modules.sequence(course_id, asset_type, asset_id), refresh)

    def _links(self, html: str | None, context: str) -> list[dict[str, Any]]:
        links: list[dict[str, Any]] = []
        seen: set[str] = set()
        for tag in BeautifulSoup(html or "", "html.parser").find_all(["a", "img", "iframe", "audio", "video", "source"]):
            value = tag.get("href") if tag.name == "a" else tag.get("src")
            if not isinstance(value, str) or not value or value.startswith("#"):
                continue
            try:
                url = urljoin(urljoin(self.base_url + "/", context), value)
                scheme = urlsplit(url).scheme
            except ValueError:
                continue
            if url in seen or scheme not in {"http", "https"}:
                continue
            seen.add(url)
            route = self.resolve_url(url)
            links.append({"label": tag.get_text(" ", strip=True) or tag.get("alt") or tag.name,
                          "url": url, **route})
        return links

    def resolve_url(self, url: str) -> dict[str, Any]:
        """Parse an allowlisted Canvas web/API route without making a request."""
        if len(url) > 4096:
            return {"status": "unsupported", "reason": "URL exceeds navigation limit."}
        try:
            parts = urlsplit(urljoin(self.base_url + "/", url))
            _ = parts.port  # Validate malformed port syntax before route matching.
        except ValueError:
            return {"status": "unsupported", "reason": "Malformed URL."}
        base = urlsplit(self.base_url)
        if parts.username or parts.password or parts.scheme not in {"https", "http"}:
            return {"status": "unsupported", "reason": "Only ordinary HTTP(S) links can be navigated."}
        if (parts.scheme, parts.netloc.lower()) != (base.scheme, base.netloc.lower()):
            return {"status": "external", "reason": "Open in a browser; this server never fetches external links."}
        path = unquote(parts.path).rstrip("/")
        if "\\" in path or any(segment in {".", ".."} for segment in path.split("/")):
            return {"status": "unsupported", "reason": "Unsafe path segments."}
        path = re.sub(r"^/api/v1(?=/)", "", path)
        if path in {"", "/dashboard", "/courses"}:
            return {"status": "supported", "open": {"tool": "canvas_dashboard"}}
        file = re.fullmatch(r"/files/([1-9][0-9]*)(?:/(?:download|preview))?", path)
        if file:
            return {"status": "supported", "open": {"tool": "get_file", "file_id": int(file[1])}}
        match = re.fullmatch(r"/courses/([1-9][0-9]*)(?:/(.*))?", path)
        if not match:
            return {"status": "unsupported", "reason": "Link does not match a supported Canvas student route."}
        course_id, tail = int(match[1]), match[2] or ""
        if not tail:
            return {"status": "supported", "open": {"tool": "canvas_course_home", "course_id": course_id}}
        if tail in {"assignments/syllabus", "syllabus"}:
            tail = "syllabus"
        if tail in SECTIONS:
            return {"status": "supported", "open": {"tool": "canvas_browse_course", "course_id": course_id, "section": tail}}
        if tail in {"front_page", "pages/front_page"}:
            return {"status": "supported", "open": self._route(course_id, "page", "front_page")}
        item = re.fullmatch(r"(assignments|quizzes|discussion_topics|files|modules)/([1-9][0-9]*)(?:/(?:download|preview))?", tail)
        if item:
            kind = {"assignments": "assignment", "quizzes": "quiz", "discussion_topics": "discussion", "files": "file", "modules": "module"}[item[1]]
            return {"status": "supported", "open": self._route(course_id, kind, item[2])}
        module_item = re.fullmatch(r"modules/(?:(\d+)/)?items/([1-9][0-9]*)", tail)
        if module_item:
            return {"status": "supported", "open": self._route(course_id, "module_item", module_item[2], int(module_item[1]) if module_item[1] else None)}
        page = re.fullmatch(r"pages/([^/]+)", tail)
        if page:
            return {"status": "supported", "open": self._route(course_id, "page", page[1])}
        if tail.startswith("external_tools/"):
            return {"status": "external", "reason": "External-tool interaction is not implemented here; open it through Canvas or a supported browser/provider integration."}
        return {"status": "unsupported", "reason": "This Canvas route requires browser navigation."}

    def open_url(self, url: str, *, refresh: bool = False) -> dict[str, Any]:
        resolved = self.resolve_url(url)
        if resolved["status"] != "supported":
            return resolved
        route = dict(resolved["open"])
        tool = route.pop("tool")
        handlers = {"canvas_dashboard": self.dashboard, "canvas_course_home": self.course_home,
                    "canvas_browse_course": self.browse_course, "canvas_open_item": self.open_item,
                    "get_file": lambda file_id, refresh: self._read(("file", file_id), lambda: self.canvas.files.get(file_id), refresh)}
        return handlers[tool](**route, refresh=refresh)
