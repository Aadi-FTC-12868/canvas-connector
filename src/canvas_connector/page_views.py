"""Explicit native page views, separate from passive saved-HTML reads.

Canvas Pages SHOW logs access and its serializer can invoke module :read.
Only perform() dispatches that request, once, without redirects or retries.
Preparation uses metadata and a latest-revision summary permission witness;
neither exposes revision HTML or substitutes SHOW for an unavailable list.
Sources: Canvas WikiPagesApiController#show/#show_revision and
lib/api/v1/wiki_page.rb at 1c9f0bb8013ed69c4f2efe11fd483025469b7e6c.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime
from itertools import islice
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from canvas_py import Canvas
from canvas_py._errors import CanvasNotFoundError, CanvasPermissionError
from canvas_py._page_read import PageMetadataRequest
from canvas_py._request import CanvasRequest
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .group_content import ContentContext, CourseContext, context_path
from .responses import dump, external_link, public_url

Id = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
_ID = TypeAdapter(Id)
_MAX_BYTES = 1024 * 1024
_PAGE_FIELDS = ("page_id", "url", "title", "created_at", "updated_at", "editing_roles",
                "last_edited_by", "published", "hide_from_students", "front_page",
                "todo_date", "publish_at", "locked_for_user", "lock_info", "lock_explanation", "editor")
_CONTENT_FIELDS = ("body", "block_editor_attributes", "block_editor_data")
_EFFECT = ("Open this page through Canvas's native page-view API. Canvas records access and may mark "
           "linked module requirements read. The view does not attest understanding, and the response "
           "does not independently verify every module-progress effect. No page content is edited.")
_URL = re.compile(r"(?:https?://|//)[^\s<>\"']+", re.IGNORECASE)
_CANVAS_PATH = re.compile(r"/(?:courses|groups)/[1-9][0-9]*(?:/(?:pages/[^/?#]+|assignments/[1-9][0-9]*|"
                          r"discussion_topics/[1-9][0-9]*|files/[1-9][0-9]*(?:/(?:preview|download))?))?/?$|"
                          r"/files/[1-9][0-9]*(?:/(?:preview|download))?/?$")
_SECRET_KEYS = {"authorization", "auth", "jwt", "idtoken", "accesstoken", "refreshtoken", "token", "verifier", "signature", "sig", "key",
                "password", "secret", "clientsecret", "apikey", "accesskey", "privatekey", "credential",
                "credentials", "cookie", "setcookie", "policy", "externalcontentreference"}
_MEDIA_KEYS = {"src", "poster", "download_url", "thumbnail_url", "image_url", "file", "image", "video", "audio"}
_URL_KEYS = {"url", "href", "link", "src", "action", "poster", "data", "download_url", "thumbnail_url", "image_url"}
_HTML_ATTRS = {"href", "src", "alt", "title", "width", "height", "colspan", "rowspan", "scope", "lang", "dir", "id"}


def _secret(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return (normalized in _SECRET_KEYS or normalized.startswith(("xamz", "xgoog"))
            or normalized.endswith(("token", "secret", "signature", "password")))


class PageView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["page_view"]
    context: ContentContext
    page_id: Id


PageViewActionTypes = PageView
KINDS = ["page_view"]
ROUTES = {"page_view": "canvas_view_page"}


class _Read(CanvasRequest[Any]):
    allow_redirects = False

    def __init__(self, path: str, query: list | None = None):
        self._path, self.query = path, query or []

    @property
    def path(self) -> str:
        return self._path

    def query_pairs(self) -> list:
        return self.query


class _NativeView(_Read):
    # GET is a mutation here. The general HTTP method guard cannot infer that.
    allow_retries = False
    allow_redirects = False


class _Enrollments(_Read):
    response_is_paginated = True
    pin_pagination_path = True

    def validate_pagination_query(self, pairs: list[tuple[str, str]], *, page_number: int) -> None:
        expected = [(key, str(value)) for key, value in self.query]
        actual = [(key, value) for key, value in pairs if key != "page"]
        pages = [value for key, value in pairs if key == "page"]
        valid_page = pages == ([] if page_number == 1 else [str(page_number)]) or pages == [str(page_number)]
        if page_number > 1 and len(pages) == 1 and pages[0].startswith("bookmark:") and len(pages[0]) > len("bookmark:"):
            valid_page = True
        if sorted(actual) != sorted(expected) or not valid_page or any(len(value) > 4096 for value in pages):
            raise ValueError("Canvas enrollment pagination changed the reviewed student filter")


def _bounded(value: Any) -> None:
    count = 0

    def walk(node: Any, depth: int) -> None:
        nonlocal count
        count += 1
        if count > 10000 or depth > 20:
            raise ValueError("Native page data exceeded the supported structure limit")
        if isinstance(node, dict):
            for key, child in node.items():
                if not isinstance(key, str) or len(key) > 1000:
                    raise ValueError("Canvas returned an invalid page field")
                walk(child, depth + 1)
        elif isinstance(node, list):
            for child in node:
                walk(child, depth + 1)
        elif isinstance(node, str):
            if len(node) > _MAX_BYTES:
                raise ValueError("Native page text exceeded its supported size")
        elif node is not None and type(node) not in {int, bool, float}:
            raise ValueError("Canvas returned an unsupported page value")
        elif type(node) is float and not math.isfinite(node):
            raise ValueError("Canvas returned an invalid page number")

    walk(value, 0)
    if len(json.dumps(value, ensure_ascii=False).encode()) > _MAX_BYTES:
        raise ValueError("Native page response exceeded its supported size")


def _object(canvas: Canvas, request: CanvasRequest) -> dict:
    value = dump(canvas.send(request))
    if not isinstance(value, dict):
        raise ValueError("Canvas did not return the requested page access object")
    _bounded(value)
    return value


def _identity(row: dict, key: str, value: int) -> None:
    if type(row.get(key)) is not int or row[key] != value:
        raise ValueError(f"Canvas did not confirm the requested {key}")


def _access(canvas: Canvas, action: PageView) -> dict:
    actor = _ID.validate_python(_object(canvas, _Read("users/self/profile")).get("id"))
    if isinstance(action.context, CourseContext):
        course_id = action.context.course_id
        course = _object(canvas, _Read(f"courses/{course_id}", [("include[]", "horizon_course")]))
        _identity(course, "id", course_id)
        request = _Enrollments(f"courses/{course_id}/enrollments", [
            ("user_id", actor), ("type[]", "StudentEnrollment"), ("state[]", "active"),
            ("state[]", "completed"), ("per_page", 100)])
        iterator = canvas.paginate(request)
        try:
            rows = [dump(row) for row in islice(iterator, 1001)]
        finally:
            iterator.close()
        if not rows or len(rows) > 1000:
            raise ValueError("Native page viewing requires bounded current-self student enrollment evidence")
        selected, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Canvas returned an invalid student enrollment")
            _bounded(row)
            identity = _ID.validate_python(row.get("id"))
            _identity(row, "user_id", actor)
            _identity(row, "course_id", course_id)
            if identity in seen or row.get("type") != "StudentEnrollment" or row.get("enrollment_state") not in {"active", "completed"}:
                raise ValueError("Canvas did not confirm distinct current-self student enrollments")
            seen.add(identity)
            selected.append({key: row.get(key) for key in ("id", "user_id", "course_id", "type", "enrollment_state")})
        return {"user_id": actor, "context": action.context.model_dump(),
                "course": {key: course[key] for key in ("id", "name", "workflow_state", "horizon_course") if key in course},
                "enrollments": sorted(selected, key=lambda row: row["id"])}
    group_id = action.context.group_id
    group = _object(canvas, _Read(f"groups/{group_id}"))
    _identity(group, "id", group_id)
    if group.get("non_collaborative") is True or group.get("workflow_state", "available") not in {"available", "active"}:
        raise ValueError("This group is inactive or has no collaborative pages")
    member = _object(canvas, _Read(f"groups/{group_id}/users/self"))
    _identity(member, "user_id", actor)
    _identity(member, "group_id", group_id)
    if member.get("workflow_state") != "accepted":
        raise ValueError("Native group page viewing requires accepted current-self membership")
    return {"user_id": actor, "context": action.context.model_dump(),
            "group": {key: group[key] for key in ("id", "name", "course_id", "context_type", "workflow_state", "non_collaborative") if key in group},
            "membership": {key: member[key] for key in ("id", "user_id", "group_id", "workflow_state") if key in member}}


def _metadata(canvas: Canvas, action: PageView) -> dict:
    iterator = canvas.paginate(PageMetadataRequest(context_path(action.context)))
    try:
        rows = list(islice(iterator, 1001))
    except (CanvasPermissionError, CanvasNotFoundError):
        return {"status": "unavailable", "complete": False,
                "reason": "Canvas did not provide the Pages collection; this does not establish native page-view denial."}
    finally:
        iterator.close()
    if len(rows) > 1000:
        return {"status": "incomplete", "complete": False, "reason": "Page metadata collection exceeds 1000 records."}
    ids, slugs, match = set(), set(), None
    for row in rows:
        _bounded(row)
        identity = _ID.validate_python(row.get("page_id"))
        slug = row.get("url")
        if not isinstance(slug, str) or not slug or identity in ids or slug in slugs:
            raise ValueError("Canvas returned invalid or duplicate page metadata identities")
        ids.add(identity)
        slugs.add(slug)
        if identity == action.page_id:
            match = {key: row[key] for key in _PAGE_FIELDS if key in row}
    return {"status": "available" if match is not None else "not_listed", "complete": True, "page": match}


def _summary(canvas: Canvas, action: PageView) -> dict:
    path = f"{context_path(action.context)}/pages/page_id:{action.page_id}/revisions/latest"
    try:
        row = _object(canvas, _Read(path, [("summary", True)]))
    except CanvasNotFoundError as exc:
        raise ValueError("A current saved-revision witness is unavailable; native page access may still work in Canvas") from exc
    revision = _ID.validate_python(row.get("revision_id"))
    stamp = row.get("updated_at")
    if row.get("latest") is not True or not isinstance(stamp, str):
        raise ValueError("Canvas did not provide a current page-revision summary; use native Canvas")
    try:
        if datetime.fromisoformat(stamp.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("missing timezone")
    except ValueError as exc:
        raise ValueError("Canvas did not provide a valid page revision timestamp") from exc
    author = row.get("edited_by")
    if author is not None:
        if not isinstance(author, dict):
            raise ValueError("Canvas returned an invalid page revision author")
        author = {"id": _ID.validate_python(author.get("id"))}
    # The summary serializer has no page_id/title/url. The fixed request path
    # binds its page identity; native SHOW must independently return that ID.
    return {"revision_id": revision, "updated_at": stamp, "latest": True, "edited_by": author}


def snapshot(canvas: Canvas, p: dict) -> dict:
    action = PageView.model_validate(p)
    access = _access(canvas, action)
    metadata = _metadata(canvas, action)
    summary = _summary(canvas, action)
    page = metadata.get("page")
    if page and "updated_at" in page and page["updated_at"] != summary["updated_at"]:
        raise ValueError("Page metadata changed before its revision witness; prepare a fresh view")
    target, links, redacted = _safe_content(
        {**access, "page_id": action.page_id, "metadata": metadata, "revision_summary": summary}, canvas.config.base_url)
    return {**target, "links": links, "metadata_redacted": redacted,
            "effect": _EFFECT, "native_view_dispatched": False, "progress_verified": False}


def _safe_content(value: Any, origin: str) -> tuple[Any, list[dict], bool]:
    """Preserve useful links, but never pass opaque storage capabilities through."""
    links: list[dict] = []
    redacted = False
    base = urlsplit(origin)

    def safe_url(raw: str, *, media: bool = False) -> str:
        nonlocal redacted
        absolute = urljoin(origin + "/", raw)
        safe = public_url(absolute)
        parts = urlsplit(safe)
        query_pairs = parse_qsl(parts.query, keep_blank_values=True)
        filtered_query = [(key, value) for key, value in query_pairs if not _secret(key)]
        fragment_pairs = parse_qsl(parts.fragment, keep_blank_values=True)
        filtered_fragment = [(key, value) for key, value in fragment_pairs if not _secret(key)]
        if filtered_fragment != fragment_pairs or filtered_query != query_pairs:
            safe = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(filtered_query),
                               urlencode(filtered_fragment) if filtered_fragment != fragment_pairs else parts.fragment))
            parts = urlsplit(safe)
            redacted = True
        host = (parts.hostname or "").lower()
        same_origin = (parts.scheme, host, parts.port or (443 if parts.scheme == "https" else 80)) == (
            base.scheme, base.hostname, base.port or (443 if base.scheme == "https" else 80))
        known = host in {"docs.google.com", "drive.google.com", "onedrive.live.com"} or host.endswith((".sharepoint.com", ".office.com"))
        storage = any(part in host for part in ("cloudfront", "googleusercontent", "blob.core", "amazonaws", "cdn", "storage"))
        # Same-origin API/download capability routes are not ordinary page links.
        if (not safe or (same_origin and not _CANVAS_PATH.fullmatch(parts.path))
                or (not same_origin and not known and (media or storage or safe != absolute))):
            redacted = True
            return ""
        if same_origin and parts.path.endswith("/download"):
            safe = safe.split("?", 1)[0].removesuffix("/download")
        if safe != absolute:
            redacted = True
        handoff = external_link(safe, origin)
        if handoff not in links:
            links.append(handoff)
        return safe

    def text_value(text: str, *, media: bool = False, depth: int = 0) -> str:
        nonlocal redacted
        if text.lstrip().startswith(("{", "[")):
            try:
                decoded = json.loads(text)
            except (ValueError, RecursionError):
                decoded = None
            if isinstance(decoded, (dict, list)):
                _bounded(decoded)
                return json.dumps(walk(decoded, media=media, depth=depth + 1), ensure_ascii=False)
        if text.startswith(("/files/", "/courses/", "/groups/", "/api/")) and not any(char.isspace() for char in text):
            return safe_url(text, media=media) or "[resource URL omitted]"
        if media and re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", text):
            return safe_url(text, media=True) or "[resource URL omitted]"
        if "<" in text and ">" in text:
            soup = BeautifulSoup(text, "html.parser")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
                redacted = True
            for tag in soup.find_all(True):
                for attr in list(tag.attrs):
                    if attr.lower() not in _HTML_ATTRS:
                        del tag.attrs[attr]
                        redacted = True
                    elif attr.lower() in {"href", "src", "action", "poster", "data"}:
                        safe = safe_url(str(tag.attrs[attr]), media=tag.name != "a" or attr.lower() != "href")
                        if safe:
                            tag.attrs[attr] = safe
                        else:
                            del tag.attrs[attr]
            text = str(soup)
        return _URL.sub(lambda match: safe_url(match.group(), media=media) or "[resource URL omitted]", text)

    def walk(node: Any, *, media: bool = False, depth: int = 0) -> Any:
        nonlocal redacted
        if depth > 20:
            raise ValueError("Native page content exceeded its supported nesting")
        if isinstance(node, dict):
            media = media or (isinstance(node.get("type"), str)
                              and node["type"] in {"image", "audio", "video", "file", "attachment", "embed"})
            result = {}
            for key, child in node.items():
                if _secret(key):
                    redacted = True
                    continue
                if (isinstance(child, str) and key.lower() in _URL_KEYS
                        and (child.startswith(("/", ".")) or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", child))):
                    result[key] = safe_url(child, media=media or key.lower() in _MEDIA_KEYS) or "[resource URL omitted]"
                else:
                    result[key] = walk(child, media=media or key.lower() in _MEDIA_KEYS, depth=depth + 1)
            return result
        if isinstance(node, list):
            return [walk(child, media=media, depth=depth + 1) for child in node]
        if isinstance(node, str):
            return text_value(node, media=media, depth=depth)
        return node

    return walk(value), links, redacted


def perform(canvas: Canvas, p: dict, target: dict) -> dict:
    action = PageView.model_validate(p)
    _identity(target, "page_id", action.page_id)
    actor = _ID.validate_python(target.get("user_id"))
    if target.get("context") != action.context.model_dump() or target.get("effect") != _EFFECT:
        raise ValueError("Prepared native page view does not match this action")
    path = f"{context_path(action.context)}/pages/page_id:{action.page_id}"
    page = _object(canvas, _NativeView(path))
    _identity(page, "page_id", action.page_id)
    if not isinstance(page.get("title"), str) or not isinstance(page.get("url"), str) or not page["url"]:
        raise ValueError("Canvas omitted native page identity metadata")
    for key, expected in (("course_id", getattr(action.context, "course_id", None)),
                          ("group_id", getattr(action.context, "group_id", None))):
        if key in page and (expected is None or type(page[key]) is not int or page[key] != expected):
            raise ValueError("Canvas returned a conflicting page context")
    if "context_type" in page and page["context_type"] not in {action.context.context_type, action.context.context_type.title()}:
        raise ValueError("Canvas returned a conflicting page context type")
    if "context_id" in page:
        _identity(page, "context_id", action.context.course_id if isinstance(action.context, CourseContext) else action.context.group_id)
    if _access(canvas, action)["user_id"] != actor:
        raise ValueError("The authenticated page viewer changed during the native view")
    metadata = {key: page[key] for key in _PAGE_FIELDS if key in page}
    lock = page.get("locked_for_user")
    lock_info = page.get("lock_info")
    if lock_info is not None and not isinstance(lock_info, dict):
        raise ValueError("Canvas returned an invalid native page lock")
    locked = lock is True or lock_info is not None
    status = "locked" if locked else "available" if lock is False else "unavailable"
    content = {}
    if status == "available":
        content = {key: page[key] for key in _CONTENT_FIELDS if key in page}
    if content.get("block_editor_data") is not None:
        content_format = "external_block_data"
        complete = isinstance(content["block_editor_data"], (dict, list))
    elif content.get("block_editor_attributes") is not None:
        content_format = "block_editor_attributes"
        complete = isinstance(content["block_editor_attributes"], dict)
    elif "body" in content:
        content_format = "html"
        complete = isinstance(content["body"], str)
    else:
        content_format, complete = "unavailable", False
    if "body" in content and content["body"] is not None and not isinstance(content["body"], str):
        raise ValueError("Canvas returned an invalid native HTML body")
    safe, links, redacted = _safe_content({"page": metadata, "content": content}, canvas.config.base_url)
    complete = complete and not redacted
    return {"verified": True, "verification_scope": "native_response_page_identity", "user_id": actor,
            "context": action.context.model_dump(), "page_id": action.page_id, **safe,
            "access": {"status": status, "native_permission_enforced": True},
            "content_source": "native_page_show", "content_format": content_format,
            "content_complete": complete, "content_redacted": redacted, "links": links,
            "html_url": f"{canvas.config.base_url}/{context_path(action.context)}/pages/page_id:{action.page_id}",
            "coverage": {"scope": "returned_native_page_content", "complete": complete,
                         "rendered_page_complete": False, "progress_verified": False},
            "progress_verified": False, "native_view_dispatched": True,
            "warning": "Native API content only; embedded services and rendered-page completeness are not verified. "
                       "Private or unsupported resource links may be omitted. This response does not authorize page editing.",
            "effect": _EFFECT}
