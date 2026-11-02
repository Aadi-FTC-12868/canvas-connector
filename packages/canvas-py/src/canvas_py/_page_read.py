"""Read saved page HTML without Canvas's implicit module-progress write.

The Pages SHOW serializer and LIST with include[]=body call
context_module_action(:read). Metadata-only LIST and the literal latest
revision route do not. Latest alone does not enforce module/date locks, so
never expose its body without fresh, matching, unlocked list metadata.
Sources: https://canvas.instructure.com/doc/api/pages.html and Canvas LMS
lib/api/v1/wiki_page.rb, app/controllers/wiki_pages_api_controller.rb.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from itertools import islice
from typing import Any, Literal, Protocol

from ._errors import CanvasError, CanvasNotFoundError
from ._request import CanvasRequest, QueryPair

MAX_PAGES = 1000


class _Reader(Protocol):
    def send(self, request: CanvasRequest[Any]) -> Any: ...
    def paginate(self, request: CanvasRequest[Any]) -> Iterator[Any]: ...


class PageContentUnavailable(CanvasError):
    """A passive read cannot establish accessible saved HTML for this page."""


class _Object(CanvasRequest[dict[str, Any]]):
    allow_redirects = False

    def __init__(self, path: str, query: list[QueryPair] | None = None):
        self._path, self._query = path, query or []

    @property
    def path(self) -> str:
        return self._path

    def query_pairs(self) -> list[QueryPair]:
        return self._query

    def decode(self, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise PageContentUnavailable("Canvas did not provide the requested page resource")
        return data


class PageMetadataRequest(_Object):
    """Body-free collection; pagination may never add content includes."""

    response_is_paginated = True
    pin_pagination_path = True

    def __init__(self, context_path: str, *, per_page: int = 100):
        super().__init__(f"{context_path}/pages", [("per_page", per_page), ("sort", "title"), ("order", "asc")])

    def validate_pagination_query(self, pairs: list[tuple[str, str]], *, page_number: int) -> None:
        seen = set()
        for key, value in pairs:
            if key in seen or key not in {"page", "per_page", "sort", "order"}:
                raise PageContentUnavailable("Page metadata pagination changed the safe query")
            seen.add(key)
            if key in {"page", "per_page"}:
                if not value.isascii() or not value.isdigit() or not 0 < int(value) <= (100 if key == "per_page" else 2**63 - 1):
                    raise PageContentUnavailable("Page metadata pagination has an invalid window")
            elif value != {"sort": "title", "order": "asc"}[key]:
                raise PageContentUnavailable("Page metadata pagination changed the ordering")
        query = dict(pairs)
        # Canvas's own build_links_hash preserves the initial sort/order and
        # emits page/per_page. Missing keys can reset defaults and skip rows.
        expected = {key: str(value) for key, value in self._query}
        if any(query.get(key) != value for key, value in expected.items()):
            raise PageContentUnavailable("Page metadata pagination changed its original query")
        if int(query.get("page", "1")) != page_number:
            raise PageContentUnavailable("Page metadata pagination skipped or repeated a page")

    def decode(self, data: Any) -> list[dict[str, Any]]:
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise PageContentUnavailable("Canvas did not provide a complete page metadata collection")
        if any("body" in row or "block_editor_data" in row or "block_editor_attributes" in row for row in data):
            raise PageContentUnavailable("Canvas unexpectedly included content in a metadata-only page read")
        return data


def validate_page_selector(page_url: str) -> str:
    if (type(page_url) is not str or not 1 <= len(page_url) <= 255 or page_url in {".", ".."}
            or any(c in page_url for c in "/\\%?#") or any(ord(c) < 32 or ord(c) == 127 for c in page_url)):
        raise ValueError("page_url must be one Canvas page slug or ID")
    if page_url.startswith("page_id:"):
        tail = page_url.removeprefix("page_id:")
        if not tail.isascii() or not tail.isdigit() or not 0 < int(tail) <= 2**63 - 1:
            raise ValueError("page_id: requires a positive numeric page ID")
    return page_url


def _metadata(reader: _Reader, context_path: str) -> list[dict[str, Any]]:
    iterator = reader.paginate(PageMetadataRequest(context_path))
    try:
        rows = list(islice(iterator, MAX_PAGES + 1))
    finally:
        iterator.close()
    if len(rows) > MAX_PAGES:
        raise PageContentUnavailable("Page lookup exceeds 1000 records; metadata coverage is incomplete")
    ids, slugs = set(), set()
    for row in rows:
        if (type(row.get("page_id")) is not int or not 0 < row["page_id"] <= 2**63 - 1
                or not isinstance(row.get("url"), str) or not row["url"]):
            raise PageContentUnavailable("Canvas returned an invalid page identity")
        if row["page_id"] in ids or row["url"] in slugs:
            raise PageContentUnavailable("Canvas returned duplicate page identities")
        ids.add(row["page_id"])
        slugs.add(row["url"])
    return rows


def _select(rows: list[dict[str, Any]], page_url: str | None) -> dict[str, Any]:
    if page_url is None:
        matches = [row for row in rows if row.get("front_page") is True]
    elif page_url.startswith("page_id:"):
        matches = [row for row in rows if row["page_id"] == int(page_url.removeprefix("page_id:"))]
    else:
        # Native Canvas gives an exact URL precedence over a numeric page ID.
        matches = [row for row in rows if row["url"] == page_url]
        if not matches and page_url.isascii() and page_url.isdigit():
            matches = [row for row in rows if row["page_id"] == int(page_url)]
    if not matches:
        raise CanvasNotFoundError("The requested page is absent from the complete visible metadata collection", status=404)
    if len(matches) != 1:
        raise PageContentUnavailable("Canvas did not identify one unique front page")
    return matches[0]


def _gate(page: dict[str, Any]) -> None:
    if page.get("published") is not True:
        raise PageContentUnavailable("Page content is unavailable because publication is not confirmed")
    # Ruby considers an empty Hash truthy: even lock_info={} must block body.
    if page.get("locked_for_user") is not False or page.get("lock_info") is not None:
        raise PageContentUnavailable("Page content is locked or its current availability is unknown")
    if "editor" in page and page["editor"] != "rce":
        raise PageContentUnavailable("This page editor requires the Canvas browser; saved HTML is not supported")
    if not isinstance(page.get("title"), str) or not isinstance(page.get("updated_at"), str):
        raise PageContentUnavailable("Canvas omitted page revision identity metadata")
    try:
        stamp = datetime.fromisoformat(page["updated_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("missing timezone")
    except ValueError as error:
        raise PageContentUnavailable("Canvas did not confirm the page revision timestamp") from error


def _course_gate(reader: _Reader, context_id: int) -> bool | None:
    course = reader.send(_Object(f"courses/{context_id}", [("include[]", "horizon_course")]))
    if type(course.get("id")) is not int or course["id"] != context_id:
        raise PageContentUnavailable("Canvas returned a different page course")
    if "horizon_course" in course and type(course["horizon_course"]) is not bool:
        raise PageContentUnavailable("Canvas did not confirm the course rendering format")
    if course.get("horizon_course") is True:
        raise PageContentUnavailable("Course page rendering may use Horizon content; open this page in Canvas")
    return course.get("horizon_course")


def read_page_raw(reader: _Reader, context_type: Literal["course", "group"], context_id: int,
                  page_url: str | None = None) -> dict[str, Any]:
    """Return access-checked latest saved HTML, never an implicit page view.

    None selects the uniquely listed front page. An inaccessible Pages tab,
    unsupported editor, missing version, or incomplete enumeration fails
    honestly; there is deliberately no SHOW or include-body fallback.
    """
    if context_type not in {"course", "group"} or type(context_id) is not int or not 0 < context_id <= 2**63 - 1:
        raise ValueError("Page context must be one exact course or group ID")
    if page_url is not None:
        validate_page_selector(page_url)
    context_path = f"{context_type}s/{context_id}"
    horizon = _course_gate(reader, context_id) if context_type == "course" else None
    before = _select(_metadata(reader, context_path), page_url)
    _gate(before)
    revision = reader.send(_Object(f"{context_path}/pages/page_id:{before['page_id']}/revisions/latest"))
    if (revision.get("latest") is not True or type(revision.get("revision_id")) is not int
            or not 0 < revision["revision_id"] <= 2**63 - 1
            or any(revision.get(key) != before[key] for key in ("title", "url", "updated_at"))):
        raise PageContentUnavailable("Canvas did not confirm the exact current saved page revision")
    if not isinstance(revision.get("body"), str):
        raise PageContentUnavailable("This page has no supported saved HTML body; open the page in Canvas")
    if "last_edited_by" in before or "edited_by" in revision:
        old, new = before.get("last_edited_by"), revision.get("edited_by")
        # Imported pages legitimately have no author (omitted or JSON null).
        if not (old is None and new is None) and (
                not isinstance(old, dict) or not isinstance(new, dict) or type(old.get("id")) is not int
                or type(new.get("id")) is not int or not 0 < old["id"] <= 2**63 - 1 or old["id"] != new["id"]):
            raise PageContentUnavailable("Canvas did not confirm the page revision author")
    after = _select(_metadata(reader, context_path), page_url)
    _gate(after)
    if before != after:
        raise PageContentUnavailable("Page identity, revision, or availability changed during the read; refresh in Canvas")
    if context_type == "course" and _course_gate(reader, context_id) != horizon:
        raise PageContentUnavailable("Course rendering format changed during the page read")
    result = {**after, "body": revision["body"], "revision_id": revision["revision_id"],
            "content_source": "latest_saved_html_revision", "content_complete": False,
            "content_warning": "Saved HTML revision only; Canvas may render additional block or external content."}
    if context_type == "course":
        result["horizon_course"] = horizon
        if horizon is None:
            result["content_warning"] += " This Canvas installation did not report the course rendering format."
    return result
