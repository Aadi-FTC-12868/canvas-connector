"""Stable, readable MCP results. Course content is data, never instructions."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import threading
import time
import uuid
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

from bs4 import BeautifulSoup
from canvas_py._errors import CanvasError

_PRIVATE_QUERY = {"verifier", "access_token", "token", "signature", "sig", "key"}
_DECORATIVE = {"avatar_url", "avatar_image_url", "image_download_url"}
_HTML_FIELDS = {"body", "description", "message", "syllabus_body", "instructions"}
_TOOL_NAMES = {"planner_items": "canvas_planner", "list_inbox": "canvas_inbox",
               "get_assignment": "canvas_get_assignment", "get_my_submission": "canvas_get_submission",
               "get_file": "canvas_get_file", "get_inbox_thread": "canvas_inbox_thread"}


def dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_unset=True)
    if isinstance(value, dict):
        return {str(k): dump(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [dump(v) for v in value]
    return value


def public_url(value: str) -> str:
    """Remove credential-bearing query parameters from displayed URLs."""
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            return ""
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if k.lower() not in _PRIVATE_QUERY and not k.lower().startswith(("x-amz-", "x-goog-"))]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    except ValueError:
        return ""


def external_link(url: str, canvas_origin: str = "") -> dict[str, Any]:
    url = public_url(url)
    host = (urlsplit(url).hostname or "").lower()
    canvas_host = (urlsplit(canvas_origin).hostname or "").lower()
    result: dict[str, Any] = {"url": url}
    if host == canvas_host:
        result.update({"provider": "canvas", "next_action": "Use canvas_open_url with this URL."})
    elif host in {"docs.google.com", "drive.google.com"}:
        result.update({"provider": "google_drive", "access": "external_account_required",
                       "next_action": "Use an available Google Drive connector. If unavailable, ask the user to connect their school Google account; if forbidden, check the connected account, then use their browser or request the source."})
    elif host == "onedrive.live.com" or host.endswith((".sharepoint.com", ".office.com")):
        result.update({"provider": "microsoft", "access": "external_account_required",
                       "next_action": "Use an available Microsoft/SharePoint connector with the school account. If unavailable, ask the user to connect it; if still forbidden, use their browser or request the source."})
    else:
        result.update({"provider": "external", "access": "browser_or_connector_required",
                       "next_action": "Use a relevant connected service or the user's browser. Canvas credentials cannot unlock external services."})
    return result


def normalize(value: Any, *, canvas_origin: str = "", include_html: bool = False,
              links: list[dict[str, Any]] | None = None) -> Any:
    """Preserve all substantive fields, including false, zero, and unknown/null."""
    links = links if links is not None else []
    value = dump(value)
    if isinstance(value, list):
        return [normalize(v, canvas_origin=canvas_origin, include_html=include_html, links=links) for v in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in _DECORATIVE:
            continue
        if key in _HTML_FIELDS and isinstance(item, str) and "<" in item and ">" in item:
            soup = BeautifulSoup(item, "html.parser")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            for tag in soup.find_all(True):
                for attr in list(tag.attrs):
                    if attr.lower().startswith("on") or attr.lower() in {"style", "srcdoc", "srcset"}:
                        del tag.attrs[attr]
                    elif attr.lower() in {"href", "src", "action", "poster", "data"}:
                        safe = public_url(urljoin(canvas_origin + "/", str(tag.attrs[attr])))
                        if safe:
                            tag.attrs[attr] = safe
                        else:
                            del tag.attrs[attr]
            for anchor in soup.find_all(["a", "iframe", "img"]):
                href = anchor.get("href") or anchor.get("src")
                if href:
                    url = public_url(urljoin(canvas_origin + "/", str(href)))
                    if url:
                        link = external_link(url, canvas_origin)
                        link["label"] = anchor.get_text(" ", strip=True) or anchor.get("title") or anchor.get("alt") or url
                        if link not in links:
                            links.append(link)
            result[key] = soup.get_text("\n", strip=True)
            if include_html:
                result[key + "_html"] = str(soup)
        elif isinstance(item, str) and (key == "tool" or key.endswith("_tool")):
            result[key] = _TOOL_NAMES.get(item, item)
        elif isinstance(item, str) and (key.endswith("url") or key in {"url", "href"}) and item.startswith(("https://", "http://")):
            result[key] = public_url(item)
        else:
            result[key] = normalize(item, canvas_origin=canvas_origin, include_html=include_html, links=links)
    return result


def envelope(value: Any = None, *, origin: str = "", status: str = "ok",
             coverage: dict[str, Any] | None = None, include_html: bool = False,
             errors: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    links: list[dict[str, Any]] = []
    value = dump(value)
    source = {"canvas_origin": origin, "checked_at": datetime.now(timezone.utc).isoformat(),
              "content_is_untrusted": True}
    context = {}
    if isinstance(value, dict) and "status" in value and "source" in value:
        status = value["status"]
        source.update(value["source"])
        # Keep routing, access details and partial-failure information alongside data.
        context = {k: v for k, v in value.items() if k not in {"source", "status", "data"}}
        value = value.get("data")
    data = normalize(value, canvas_origin=origin, include_html=include_html, links=links)
    coverage = dict(coverage) if coverage is not None else {"scope": "requested_resource", "complete": status == "ok"}
    # Navigation can contain nested pages and explicitly partial access results.
    def incomplete(node: Any) -> bool:
        if isinstance(node, dict):
            return ((node.get("next_offset") is not None) or bool(node.get("next_read"))
                    or node.get("status") in {"forbidden", "unavailable", "locked", "external", "not_found_or_disabled"}
                    or any(incomplete(v) for v in node.values()))
        return isinstance(node, list) and any(incomplete(v) for v in node)
    if incomplete(data) or status not in {"ok", "complete", "succeeded"}:
        coverage["complete"] = False
    result = {"schema_version": "1", "status": status, "data": data,
            "links": links, "coverage": coverage or {"scope": "requested_resource", "complete": status == "ok"},
            "errors": errors or [], "source": source}
    if context:
        result["context"] = normalize(context, canvas_origin=origin, include_html=include_html, links=links)
    return result


def failure(error: Exception, *, origin: str = "") -> dict[str, Any]:
    if isinstance(error, CanvasError):
        code = error.status
        status = {401: "unauthorized", 403: "forbidden", 404: "not_found", 429: "rate_limited"}.get(code, "unavailable")
        message = "Canvas did not provide this resource. This does not mean it is empty or completed."
    else:
        code, status, message = None, "invalid_request", str(error)
    return envelope(origin=origin, status=status, coverage={"complete": False},
                    errors=[{"code": status, "http_status": code, "message": message}])


def window(items: list[Any], offset: int, limit: int) -> tuple[list[Any], dict[str, Any]]:
    if type(offset) is not int or type(limit) is not int or not 0 <= offset <= 10000 or not 1 <= limit <= 100:
        raise ValueError("offset must be 0..10000 and limit must be 1..100")
    selected = items[offset:offset + limit]
    next_offset = offset + limit if offset + limit < len(items) else None
    return selected, {"complete": next_offset is None and offset == 0, "total": len(items),
                      "offset": offset, "returned": len(selected), "next_offset": next_offset}


class ResponseStore:
    """Tenant-bound, bounded continuations for unusually large structured results."""
    def __init__(self, *, ttl: float = 600, budget: int = 16 * 1024 * 1024, inline_chars: int = 60000):
        self.ttl, self.budget, self.inline_chars = ttl, budget, inline_chars
        self.records: dict[str, dict] = {}
        self.lock = threading.RLock()

    def compact(self, result: dict, owner: str) -> dict:
        if isinstance(result.get("data"), dict) and result["data"].get("encoding") == "json_text":
            # Already a bounded character window. Re-encoding its escaped JSON
            # can be longer than inline_chars; never recursively paginate it.
            return result
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) <= self.inline_chars:
            return result
        byte_count = len(encoded.encode())
        with self.lock:
            now = time.monotonic()
            self.records = {k: v for k, v in self.records.items() if v["expires"] > now}
            used = sum(r["bytes"] for r in self.records.values())
            if byte_count > self.budget or used + byte_count > self.budget:
                return envelope(status="result_too_large", coverage={"complete": False},
                    errors=[{"code": "result_too_large", "message": "Narrow the request or reduce list/document limits; result was not presented as complete."}])
            key = uuid.uuid4().hex
            self.records[key] = {"owner": owner, "text": encoded, "bytes": byte_count,
                                 "sha256": hashlib.sha256(encoded.encode()).hexdigest(), "expires": now + self.ttl}
        return self.read(owner, key, 0, 40000)

    def read(self, owner: str, result_id: str, offset: int = 0, max_chars: int = 40000) -> dict:
        if type(offset) is not int or type(max_chars) is not int or offset < 0 or not 1 <= max_chars <= 40000:
            raise ValueError("offset must be nonnegative; max_chars must be 1..40000")
        with self.lock:
            r = self.records.get(result_id)
            if not r or r["owner"] != owner or r["expires"] <= time.monotonic():
                raise ValueError("Result continuation expired or is unavailable for this account; repeat the source read")
            total = len(r["text"])
            if offset > total:
                raise ValueError("offset exceeds result length")
            end = min(total, offset + max_chars)
            next_read = {"tool": "canvas_read_result", "result_id": result_id, "offset": end, "max_chars": max_chars} if end < total else None
            return envelope({"result_id": result_id, "encoding": "json_text", "text": r["text"][offset:end],
                             "sha256": r["sha256"], "next_read": next_read},
                            status="partial" if next_read else "ok",
                            coverage={"complete": offset == 0 and next_read is None, "scope": "result_character_window",
                                      "total_chars": total, "offset": offset, "returned_chars": end - offset,
                                      "all_windows_required": True})
