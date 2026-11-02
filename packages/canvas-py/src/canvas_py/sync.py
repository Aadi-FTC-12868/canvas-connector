"""Bounded, read-only Canvas discovery and versioned local course material sync.

No scheduling or Canvas mutations live here. Only a marked ``canvas-managed``
subtree is written; content-addressed originals and previous manifests survive
changes, failed downloads, partial visibility, and interrupted runs.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ._errors import CanvasAuthError, CanvasError
from .endpoints.assignments import ListAssignments
from .endpoints.courses import ListCourses
from .endpoints.discussions import ListDiscussionTopics
from .endpoints.files import ListCourseFiles
from .endpoints.modules import GetModuleItems, ListModules
from .endpoints.navigation import ListPages

_MARKER = "Canvas Connector managed materials v1\n"
_SCHEMA = 1


@dataclass(frozen=True)
class SyncLimits:
    max_courses: int = 30
    max_operations: int = 250
    max_items: int = 2500
    max_files: int = 100
    max_file_bytes: int = 25 * 1024 * 1024
    max_total_bytes: int = 250 * 1024 * 1024
    max_text_chars: int = 500_000

    def __post_init__(self) -> None:
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in asdict(self).values()):
            raise ValueError("sync limits must be positive integers")


def _dump(value: Any) -> Any:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_url(raw: str, base: str) -> str | None:
    try:
        url = urlsplit(urljoin(base + "/", raw))
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            return None
        return urlunsplit((url.scheme, url.netloc, url.path, "", ""))
    except ValueError:
        return None


def _safe_name(value: str) -> str:
    # The ID and hash, not the teacher-supplied name, choose file identity.
    value = value.replace("\\", "/").split("/")[-1]
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return (value[:120] or "material")


class _Budget:
    def __init__(self, canvas: Any, limits: SyncLimits):
        self.canvas, self.limits = canvas, limits
        self.operations = self.items = self.files = self.downloaded_bytes = 0
        self.transfer_budget_used = 0

    def call(self, fn: Callable[[], Any]) -> Any:
        if self.operations >= self.limits.max_operations:
            raise _Limit("operation_limit")
        self.operations += 1
        return fn()

    def collect(self, request: Any) -> tuple[list[dict[str, Any]], bool]:
        remaining = self.limits.max_items - self.items
        if remaining < 1:
            raise _Limit("item_limit")
        iterator = self.call(lambda: self.canvas.paginate(request))
        values = []
        try:
            for value in iterator:
                values.append(_dump(value))
                self.items += 1
                if len(values) >= remaining:
                    # Conservatively incomplete at the cap; no unbounded peek.
                    return values, False
        finally:
            close = getattr(iterator, "close", None)
            if close:
                close()
        return values, True


class _Limit(Exception):
    pass


def _issue(error: Exception, source: str) -> dict[str, Any]:
    # Error bodies and URLs may contain tokens, signed links, or private payloads.
    if isinstance(error, CanvasAuthError) or getattr(error, "status", None) == 401:
        raise CanvasAuthError("Canvas authentication failed; no sync manifests were committed", status=401)
    status = getattr(error, "status", None)
    reason = str(error) if isinstance(error, _Limit) else {
        403: "forbidden", 404: "not_found_or_disabled",
    }.get(status, "read_failed")
    return {"source": source, "reason": reason, "http_status": status}


def _discover(budget: _Budget, course_id: int, now: datetime) -> dict[str, Any]:
    canvas, limits = budget.canvas, budget.limits
    base = canvas.config.base_url.rstrip("/")
    result: dict[str, Any] = {"course_id": course_id, "name": f"Course {course_id}",
                             "items": {}, "coverage": {}, "issues": [], "attention": []}
    files: dict[int, set[str]] = {}
    pages: set[str] = set()
    assignments: set[int] = set()
    known_assignments: set[int] = set()
    result["file_models"] = {}

    def issue(source: str, error: Exception) -> None:
        result["issues"].append(_issue(error, source))
        result["coverage"][source] = "incomplete"

    def read(source: str, fn: Callable[[], Any]) -> dict[str, Any] | None:
        try:
            value = _dump(budget.call(fn))
            result["coverage"][source] = "complete"
            return value
        except (CanvasError, _Limit, ValueError, TypeError) as error:
            issue(source, error)
            return None

    def collect(source: str, request: Any) -> list[dict[str, Any]]:
        try:
            values, complete = budget.collect(request)
            result["coverage"][source] = "complete" if complete else "incomplete"
            if not complete:
                issue(source, _Limit("item_limit"))
            return values
        except (CanvasError, _Limit, ValueError, TypeError) as error:
            issue(source, error)
            return []

    def file_ref(file_id: Any, source: str) -> None:
        if isinstance(file_id, int) and not isinstance(file_id, bool) and file_id > 0:
            files.setdefault(file_id, set()).add(source)

    def links(html: str, source: str) -> tuple[str, list[str]]:
        if len(html) > limits.max_text_chars:
            issue(source, _Limit("text_limit"))
            html = html[:limits.max_text_chars]
        soup = BeautifulSoup(html, "html.parser")
        found: set[str] = set()
        for tag in soup.find_all(True):
            for attr in ("href", "src", "data-api-endpoint"):
                raw = tag.get(attr)
                if not isinstance(raw, str):
                    continue
                url = _safe_url(raw, base)
                if not url:
                    continue
                found.add(url)
                parsed = urlsplit(url)
                if (parsed.scheme, parsed.netloc) != (urlsplit(base).scheme, urlsplit(base).netloc):
                    continue
                path = unquote(parsed.path)
                match = re.fullmatch(r"(?:/api/v1)?(?:/courses/\d+)?/files/(\d+)(?:/(?:download|preview))?/?", path)
                if match:
                    file_ref(int(match[1]), source)
                match = re.fullmatch(rf"(?:/api/v1)?/courses/{course_id}/pages/([^/]+)/?", path)
                if match:
                    pages.add(match[1])
                match = re.fullmatch(rf"(?:/api/v1)?/courses/{course_id}/assignments/(\d+)/?", path)
                if match:
                    assignments.add(int(match[1]))
        # Save inert text rather than executable, credential-bearing source HTML.
        for tag in soup(["script", "style"]):
            tag.decompose()
        return soup.get_text("\n", strip=True), sorted(found)

    def content(kind: str, identity: Any, raw: dict[str, Any], body_key: str) -> None:
        key = f"{kind}:{identity}"
        locked = bool(raw.get("locked_for_user"))
        if locked:
            result["issues"].append({"source": key, "reason": "locked"})
            result["coverage"][key] = "incomplete"
        text, urls = links("" if locked else raw.get(body_key) or "", key)
        entry = {"kind": kind, "id": identity, "title": raw.get("name") or raw.get("title") or kind,
                 "updated_at": raw.get("updated_at"), "text": text, "links": urls,
                 "source_url": _safe_url(raw.get("html_url") or f"/courses/{course_id}", base)}
        if kind == "assignment":
            sub = raw.get("submission") or {}
            entry.update({"due_at": raw.get("due_at"), "locked_for_user": raw.get("locked_for_user"),
                          "submission_state": sub.get("workflow_state"), "missing": sub.get("missing"),
                          "late": sub.get("late"), "submitted_at": sub.get("submitted_at")})
            due = raw.get("due_at")
            due_date = None
            if due:
                try:
                    due_date = datetime.fromisoformat(due.replace("Z", "+00:00"))
                    if due_date.tzinfo is None:
                        due_date = due_date.replace(tzinfo=UTC)
                except (ValueError, AttributeError):
                    pass
            reason = "missing" if sub.get("missing") else (
                "due_within_seven_days" if due_date and now <= due_date <= now + timedelta(days=7)
                and not sub.get("submitted_at") and sub.get("workflow_state") not in {"submitted", "graded"} else None)
            if reason:
                result["attention"].append({"item": key, "reason": reason, "title": entry["title"],
                                            "due_at": due, "source_url": entry["source_url"]})
            file_ref(raw.get("annotatable_attachment_id"), key)
        for attachment in ([] if locked else raw.get("attachments") or []):
            if isinstance(attachment, dict):
                file_ref(attachment.get("id"), key)
        if not locked and isinstance(raw.get("attachment"), dict):
            file_ref(raw["attachment"].get("id"), key)
        entry["fingerprint"] = _sha(_bytes(entry))
        result["items"][key] = entry

    course = read("syllabus", lambda: canvas.courses.get(course_id, include=["syllabus_body"]))
    if course:
        result["name"] = course.get("display_name") or course.get("name") or result["name"]
        content("syllabus", course_id, {**course, "html_url": f"{base}/courses/{course_id}/assignments/syllabus"}, "syllabus_body")
    for module in collect("modules", ListModules(course_id, include=[])):
        for item in collect(f"module:{module['id']}", GetModuleItems(course_id, module["id"])):
            source = f"module_item:{item['id']}"
            if (item.get("content_details") or {}).get("locked_for_user") or module.get("state") == "locked":
                result["issues"].append({"source": source, "reason": "locked"})
                result["coverage"][source] = "incomplete"
                continue
            if item.get("type") == "File":
                file_ref(item.get("content_id"), source)
            elif item.get("type") == "Page" and item.get("page_url"):
                pages.add(item["page_url"])
            elif item.get("type") == "Assignment" and item.get("content_id"):
                assignments.add(item["content_id"])
            elif item.get("type") in {"ExternalUrl", "ExternalTool"}:
                result["attention"].append({"item": source, "reason": "external_resource_not_downloaded",
                                            "title": item.get("title"),
                                            "source_url": _safe_url(item.get("external_url") or item.get("html_url") or "", base)})
    for assignment in collect("assignments", ListAssignments(course_id, include=["submission"])):
        known_assignments.add(assignment["id"])
        content("assignment", assignment["id"], assignment, "description")
    for page in collect("pages", ListPages(course_id)):
        if page.get("url"):
            pages.add(page["url"])
    # Unlike the cross-course announcements endpoint, this has no implicit
    # recent-date window, so old announcements and their attachments are included.
    for announcement in collect("announcements", ListDiscussionTopics(course_id, only_announcements=True)):
        content("announcement", announcement["id"], announcement, "message")
    seen_pages: set[str] = set()
    while (pages - seen_pages or assignments - known_assignments) and budget.operations < limits.max_operations:
        for slug in sorted(pages - seen_pages):
            seen_pages.add(slug)
            page = read(f"page:{slug}", lambda slug=slug: canvas.pages.get(course_id, slug))
            if page:
                content("page", slug, page, "body")
        for identity in sorted(assignments - known_assignments):
            known_assignments.add(identity)
            assignment = read(f"assignment:{identity}", lambda identity=identity: canvas.assignments.get(course_id, identity, include=["submission"]))
            if assignment:
                content("assignment", identity, assignment, "description")
    if pages - seen_pages or assignments - known_assignments:
        issue("linked_content", _Limit("operation_limit"))
    # Supplement modules/HTML discovery. A student 403 is explicit partial
    # visibility and never means a course has no files.
    for metadata in collect("files_catalog", ListCourseFiles(course_id)):
        file_ref(metadata["id"], "files_catalog")
    for identity in sorted(files):
        if budget.files >= limits.max_files:
            issue("files", _Limit("file_limit"))
            break
        budget.files += 1
        model = None
        try:
            model = budget.call(lambda identity=identity: canvas.files.get(identity))
            metadata = _dump(model)
        except (CanvasError, _Limit, ValueError, TypeError) as error:
            issue(f"file:{identity}", error)
            continue
        key = f"file:{identity}"
        entry = {"kind": "file", "id": identity,
                 "title": metadata.get("display_name") or metadata.get("filename") or f"file-{identity}",
                 "filename": _safe_name(metadata.get("filename") or metadata.get("display_name") or "material"),
                 "size": metadata.get("size"), "content_type": metadata.get("content_type") or metadata.get("content-type"),
                 "updated_at": metadata.get("updated_at"), "modified_at": metadata.get("modified_at"),
                 "source_url": f"{base}/courses/{course_id}/files/{identity}",
                 "discovered_via": sorted(files[identity]),
                 "hidden_for_user": metadata.get("hidden_for_user"),
                 "access": "locked" if metadata.get("locked_for_user") else "available"}
        entry["fingerprint"] = _sha(_bytes(entry))
        entry["download_fingerprint"] = _sha(_bytes({field: entry.get(field) for field in
                                                    ("id", "size", "updated_at", "modified_at", "content_type")}))
        result["items"][key] = entry
        result["file_models"][key] = model
    result["coverage_complete"] = bool(result["coverage"]) and all(v == "complete" for v in result["coverage"].values())
    return result


def _path(root: Path, relative: str) -> Path:
    candidate = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("unsafe managed path")
    for part in [root, *candidate.parents, candidate]:
        if part.is_symlink():
            raise ValueError("sync refuses symlinks in managed output paths")
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError("managed path escapes output root")
    return candidate


def _atomic(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".canvas-sync-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def _lock(root: Path, apply: bool):
    if not apply:
        yield
        return
    import fcntl
    root.mkdir(parents=True, exist_ok=True)
    with _path(root, ".canvas-sync.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("another sync is already writing this output root") from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _load_manifest(root: Path, cid: int, base: str) -> tuple[Path, dict[str, Any]]:
    managed = _path(root, f"course-{cid}/canvas-managed")
    marker = _path(managed, ".managed-by-canvas-connector")
    if managed.exists() and (not marker.is_file() or marker.read_text() != _MARKER):
        raise ValueError("existing output folder is not marked as Canvas Connector managed")
    path = _path(managed, "manifest.json")
    if not path.exists():
        return managed, {"schema_version": _SCHEMA, "course_id": cid, "canvas_origin": base, "items": {}}
    previous = json.loads(path.read_text())
    if previous.get("schema_version") != _SCHEMA or previous.get("course_id") != cid or previous.get("canvas_origin") != base:
        raise ValueError("manifest belongs to another course, Canvas origin, or unsupported schema")
    if not isinstance(previous.get("items"), dict):
        raise ValueError("invalid sync manifest")
    return managed, previous


def _valid_original(managed: Path, entry: dict[str, Any]) -> bool:
    relative, digest = entry.get("original_path"), entry.get("sha256")
    if not isinstance(relative, str) or not isinstance(digest, str):
        return False
    path = _path(managed, relative)
    return path.is_file() and _file_sha(path) == digest


def _put(managed: Path, relative: str, raw: bytes) -> str:
    target = _path(managed, relative)
    if target.exists():
        if target.stat().st_size == len(raw) and _file_sha(target) == _sha(raw):
            return relative
        # Preserve even a user-edited managed original; never overwrite it.
        target = target.with_name(target.stem + "-recovered-" + os.urandom(5).hex() + target.suffix)
        relative = str(target.relative_to(managed))
    _atomic(target, raw)
    return relative


def _markdown_text(value: Any, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[:limit] + "…"
    return re.sub(r"([\\`*_{}\[\]<>#!|])", r"\\\1", text)


def _course_readme(managed: Path, manifest: dict[str, Any], course: dict[str, Any], checked_at: str) -> bytes:
    lines = [f"# {_markdown_text(course['name'])}", "", f"Course ID: {course['course_id']}",
             f"Last checked: {_markdown_text(checked_at)}", ""]
    if course["status"] == "partial":
        lines += ["**Partial coverage. Some sources or files were inaccessible or exceeded this run's limits.**",
                  "Previously saved material is retained; this folder is not proof that every current course item was collected.", ""]
    else:
        lines += ["Status: bounded scan complete. Coverage applies to the supported Canvas sources checked in this run.", ""]
    lines += ["Original files are preserved below. Course text and bounded document extraction previews live in",
              "[manifest.json](manifest.json), together with hashes, source links, dates and extraction warnings.",
              "A preview may be partial or require OCR/conversion; the original file remains authoritative.", "",
              "This generated folder is managed by Canvas Connector. Keep drafts and hand-edited notes outside it.", "",
              "## Coverage and issues", ""]
    complete = sum(value == "complete" for value in course["coverage"].values())
    lines += [f"{complete} of {len(course['coverage'])} checked discovery sources completed.", ""]
    for issue in course["issues"][:20]:
        lines.append(f"- {_markdown_text(issue['source'])}: {_markdown_text(issue['reason'])}")
    if len(course["issues"]) > 20:
        lines.append("- Additional issues are listed in the root canvas-sync-last-run.json report.")
    if not course["issues"]:
        lines.append("No source issues were reported in this bounded run.")
    lines += ["", "## Attention", ""]
    for item in course["attention"][:20]:
        lines.append(f"- {_markdown_text(item.get('title') or item['item'])}: {_markdown_text(item['reason'])}"
                     + (f"; due {_markdown_text(item['due_at'])}" if item.get("due_at") else ""))
    if len(course["attention"]) > 20:
        lines.append("- Additional attention items are listed in the root canvas-sync-last-run.json report.")
    if not course["attention"]:
        lines.append("No attention items were reported by the successfully checked sources.")
    lines += ["", "## Preserved originals", ""]
    originals = [(key, value) for key, value in sorted(manifest["items"].items())
                 if isinstance(value.get("original_path"), str) and _path(managed, value["original_path"]).is_file()]
    for key, item in originals[:50]:
        state = item.get("remote_state") if item.get("remote_state") == "removed" else item.get("sync_state", "retained")
        label = _markdown_text(item.get("title") or key)
        lines.append(f"- [{label}]({quote(item['original_path'], safe='/')}) — {_markdown_text(state)}")
    if len(originals) > 50:
        lines.append(f"- {len(originals) - 50} more originals are indexed by original_path in [manifest.json](manifest.json).")
    if not originals:
        lines.append("No original files have been saved for this course yet.")
    lines += ["", "Older original versions remain under originals/; previous manifest versions remain under history/.", ""]
    body = "\n".join(lines).encode()
    return f"<!-- canvas-connector-sync-readme:v1 sha256={_sha(body)} -->\n".encode() + body


def _write_readme(path: Path, raw: bytes) -> bool:
    if path.exists():
        # Preserve a pre-existing student README or a manually edited generated
        # one. The embedded checksum lets us distinguish it from our last output.
        previous = path.read_bytes()
        header, separator, body = previous.partition(b"\n")
        marker = re.fullmatch(rb"<!-- canvas-connector-sync-readme:v1 sha256=([0-9a-f]{64}) -->", header)
        if not separator or not marker or marker[1].decode() != _sha(body):
            return False
    _atomic(path, raw)
    return True


def run_sync(canvas: Any, output_root: str | Path, *, course_ids: list[int] | None = None,
             all_active: bool = False, apply: bool = False, limits: SyncLimits | None = None,
             now: datetime | None = None) -> dict[str, Any]:
    """Plan or apply a private local sync using the maintained Canvas library.

    Supply explicit positive course IDs OR ``all_active=True``. The default
    reads metadata/content without downloading originals or writing anything.
    ``apply=True`` stores originals and extraction previews. This function never
    writes to Canvas and never treats inaccessible content as deleted content.
    """
    limits = limits or SyncLimits()
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    root = Path(output_root).expanduser()
    if not root.is_absolute():
        raise ValueError("output_root must be an absolute local path")
    _path(root, "")
    source = Path(__file__).resolve()
    if source.parent.parent.name == "src":
        connector = source.parents[3]
        if root.resolve().is_relative_to(connector):
            raise ValueError("coursework output must be outside Canvas Connector source")
    if bool(course_ids) == all_active:
        raise ValueError("choose explicit course_ids or all_active=True")
    if course_ids and any(isinstance(cid, bool) or not isinstance(cid, int) or cid < 1 for cid in course_ids):
        raise ValueError("course_ids must contain positive integers")
    budget = _Budget(canvas, limits)
    if all_active:
        courses, complete = budget.collect(ListCourses(enrollment_state="active"))
        if not complete:
            raise ValueError("active course discovery hit the item limit; use explicit course_ids")
        course_ids = [course["id"] for course in courses]
    course_ids = sorted(set(course_ids or []))
    if len(course_ids) > limits.max_courses:
        raise ValueError("course selection exceeds max_courses")
    base = canvas.config.base_url.rstrip("/")
    report_path = _path(root, "canvas-sync-last-run.json")
    if report_path.exists():
        previous_report = json.loads(report_path.read_text())
        if not isinstance(previous_report, dict) or previous_report.get("managed_by") != "canvas-connector-sync":
            raise ValueError("existing run report is not managed by Canvas Connector")
    report: dict[str, Any] = {"schema_version": _SCHEMA, "managed_by": "canvas-connector-sync",
                              "mode": "apply" if apply else "dry_run", "checked_at": now.isoformat(),
                              "upstream_writes": False, "output_root": str(root), "courses": [],
                              "limits": asdict(limits), "status": "complete"}
    # Commit no course manifests until discovery AND downloads for every selected
    # course have survived authentication. Temporary data is discarded on 401.
    with _lock(root, apply), tempfile.TemporaryDirectory(prefix="canvas-sync-") as temporary:
        pending = []
        for cid in course_ids:
            managed, previous = _load_manifest(root, cid, base)
            discovery = _discover(budget, cid, now)
            old_items = previous["items"]
            items = dict(old_items)
            changes: dict[str, list[str]] = {key: [] for key in ("new", "changed", "removed", "inaccessible", "not_seen", "downloads_planned")}
            staged: list[tuple[str, Path]] = []
            for key, found in sorted(discovery["items"].items()):
                old = old_items.get(key, {})
                changed = old.get("fingerprint") != found["fingerprint"] or old.get("remote_state") == "removed"
                state = "new" if not old else "changed" if changed else None
                if state:
                    changes[state].append(key)
                entry = {**old, **found, "remote_state": "present"}
                entry.pop("observed", None)
                if found["kind"] == "file":
                    if found["access"] != "available":
                        changes["inaccessible"].append(key)
                        entry["sync_state"] = "inaccessible"
                    elif (old.get("download_fingerprint") != found.get("download_fingerprint")
                          or not (found.get("updated_at") or found.get("modified_at"))
                          or not _valid_original(managed, old)):
                        changes["downloads_planned"].append(key)
                        if apply:
                            remaining = limits.max_total_bytes - budget.transfer_budget_used
                            bound = min(limits.max_file_bytes, remaining)
                            try:
                                if bound < 1 or (found.get("size") or 0) > bound:
                                    raise _Limit("download_byte_limit")
                                destination = Path(temporary) / f"{cid}-{found['id']}"
                                # Failed transfers may already have consumed
                                # bytes: reserve their worst-case cap as well.
                                budget.transfer_budget_used += bound
                                budget.call(lambda model=discovery["file_models"][key], dest=destination, cap=bound:
                                            canvas.files.download(model, dest, max_bytes=cap))
                                size = destination.stat().st_size
                                budget.downloaded_bytes += size
                                if size > bound:
                                    raise _Limit("download_byte_limit")
                                budget.transfer_budget_used -= bound - size
                                digest = _file_sha(destination)
                                if old.get("sha256") and old["sha256"] != digest and key not in changes["changed"]:
                                    changes["changed"].append(key)
                                relative = f"originals/{found['id']}/{digest}-{found['filename']}"
                                staged.append((relative, destination))
                                entry.update({"sha256": digest, "original_path": relative,
                                              "downloaded_bytes": size,
                                              "downloaded_at": old.get("downloaded_at") if old.get("sha256") == digest else now.isoformat(),
                                              "sync_state": "current"})
                                try:
                                    from .documents import extract_document
                                    entry["extraction"] = extract_document(destination.read_bytes(), found["filename"], found.get("content_type"), max_chars=40000, max_units=20)
                                except ImportError:
                                    entry["extraction"] = {"state": "pending", "reason": "document_extractor_unavailable"}
                                except Exception:
                                    entry["extraction"] = {"state": "failed", "reason": "document_extraction_failed"}
                            except (CanvasError, _Limit, OSError, ValueError) as error:
                                discovery["issues"].append(_issue(error, key))
                                changes["inaccessible"].append(key)
                                # Preserve previous fingerprint too: a failed
                                # changed download must retry on the next run.
                                entry = {**old, "observed": found, "sync_state": "download_failed"} if old else {**found, "sync_state": "download_failed"}
                        else:
                            entry["sync_state"] = "download_planned"
                    else:
                        entry["sync_state"] = "current"
                items[key] = entry
            for key in sorted(set(old_items) - set(discovery["items"])):
                if discovery["coverage_complete"]:
                    if old_items[key].get("remote_state") != "removed":
                        changes["removed"].append(key)
                    items[key] = {**old_items[key], "remote_state": "removed"}
                else:
                    changes["not_seen"].append(key)
            for issue in discovery["issues"]:
                if (issue["source"] in old_items or issue["source"].startswith("file:")) and issue["source"] not in changes["inaccessible"]:
                    changes["inaccessible"].append(issue["source"])
            course_report = {"course_id": cid, "name": discovery["name"], "changes": changes,
                             "coverage": discovery["coverage"], "issues": discovery["issues"],
                             "attention": discovery["attention"], "managed_folder": str(managed),
                             "status": "partial" if discovery["issues"] or changes["inaccessible"] else "complete"}
            report["courses"].append(course_report)
            if course_report["status"] == "partial":
                report["status"] = "partial"
            manifest = {"schema_version": _SCHEMA, "course_id": cid, "canvas_origin": base,
                        "name": discovery["name"], "items": items}
            pending.append((managed, manifest, staged, course_report))
        report["usage"] = {"operations": budget.operations, "discovered_items": budget.items,
                           "files_checked": budget.files, "downloaded_bytes": budget.downloaded_bytes,
                           "transfer_budget_used": budget.transfer_budget_used}
        if apply:
            for managed, manifest, staged, course_report in pending:
                if not managed.exists():
                    # Publish a fully marked folder in one rename. A crash
                    # cannot strand the canonical path without its marker.
                    managed.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.TemporaryDirectory(prefix=".canvas-managed-", dir=managed.parent) as staging:
                        staged_dir = Path(staging) / "ready"
                        staged_dir.mkdir()
                        _atomic(staged_dir / ".managed-by-canvas-connector", _MARKER.encode())
                        os.rename(staged_dir, managed)
                for relative, temporary_path in staged:
                    actual = _put(managed, relative, temporary_path.read_bytes())
                    if actual != relative:
                        for entry in manifest["items"].values():
                            if entry.get("original_path") == relative:
                                entry["original_path"] = actual
                path = _path(managed, "manifest.json")
                raw = _bytes(manifest)
                if not path.exists() or path.read_bytes() != raw:
                    if path.exists():
                        prior_raw = path.read_bytes()
                        _put(managed, f"history/manifest-{_sha(prior_raw)}.json", prior_raw)
                    _atomic(path, raw)
                readme = _course_readme(managed, manifest, course_report, report["checked_at"])
                if not _write_readme(_path(managed, "README.md"), readme):
                    course_report["issues"].append({"source": "README.md", "reason": "local_student_edits_preserved"})
                    course_report["status"] = report["status"] = "partial"
            _atomic(report_path, _bytes(report))
    return report
