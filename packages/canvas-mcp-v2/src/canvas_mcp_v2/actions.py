"""Prepared, account-bound file actions with conversational authorization policy.

The caller obtains explicit user authorization in conversation; this connector
has no additional confirmation form and does not attest that consent occurred.
Immutable staging, expiry, account/target checks and post-write verification stay
server-enforced. Process-local receipts must never be retried blindly after restart.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import anyio
from canvas_py import Canvas
from canvas_py.endpoints.file_uploads import UploadContext
from canvas_py.endpoints.submissions import SubmitFileUpload
from mcp.types import ToolAnnotations

from .transport import _flag, _value


def actions_enabled() -> bool:
    local = _value("CANVAS_MCP_TRANSPORT", "stdio").lower() == "stdio"
    return _flag("CANVAS_MCP_ENABLE_ACTIONS", local)


def _owner(canvas: Canvas) -> str:
    return hashlib.sha256(
        (canvas.config.base_url + "\0" + canvas.config.token).encode()
    ).hexdigest()


def _dump(value: Any) -> dict:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else dict(value)


def _date(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Canvas returned a date without a timezone")
    return parsed


def _assignment_state(canvas: Canvas, course_id: int, assignment_id: int, names: list[str]) -> dict:
    if any(type(value) is not int or value < 1 for value in (course_id, assignment_id)):
        raise ValueError("course_id and assignment_id must be positive integers")
    assignment = _dump(
        canvas.assignments.get(
            course_id,
            assignment_id,
            include=["can_submit", "submission", "all_dates", "overrides"],
        )
    )
    submission = _dump(canvas.submissions.get(course_id, assignment_id))
    if assignment.get("id") != assignment_id or submission.get("assignment_id") not in (
        None,
        assignment_id,
    ):
        raise ValueError("Canvas returned metadata for a different assignment")
    if assignment.get("locked_for_user") or assignment.get("published") is False:
        raise ValueError("Assignment is locked or unpublished")
    now = datetime.now(UTC)
    unlock_at, lock_at = _date(assignment.get("unlock_at")), _date(assignment.get("lock_at"))
    if (unlock_at is not None and now < unlock_at) or (lock_at is not None and now >= lock_at):
        raise ValueError("Assignment is outside its submission availability dates")
    if "online_upload" not in (assignment.get("submission_types") or []):
        raise ValueError("Assignment does not accept file submissions")
    if assignment.get("group_category_id"):
        raise ValueError("Group assignment actions require a separate review in Canvas")
    extensions = [str(x).lower().lstrip(".") for x in assignment.get("allowed_extensions") or []]
    if extensions and any(
        Path(name).suffix.lower().lstrip(".") not in extensions for name in names
    ):
        raise ValueError("A file extension is not allowed by the assignment")
    attempt, allowed = submission.get("attempt"), assignment.get("allowed_attempts")
    extra_attempts = submission.get("extra_attempts") or 0
    can_submit = assignment.get("can_submit")
    if can_submit is False:
        raise ValueError("Canvas reports that this assignment cannot currently be submitted")
    # Canvas's per-user can_submit handles enrollment, overrides and attempts.
    # Older deployments can omit it; keep their extra-attempt and legacy-zero
    # behavior rather than denying a teacher-granted additional attempt.
    if (
        can_submit is not True
        and allowed is not None
        and allowed > 0
        and (attempt or 0) >= allowed + extra_attempts
    ):
        raise ValueError("No submission attempts remain")
    return {
        "course_id": course_id,
        "assignment_id": assignment_id,
        "assignment_name": assignment.get("name"),
        "assignment_url": f"{canvas.config.base_url}/courses/{course_id}/assignments/{assignment_id}",
        "due_at": assignment.get("due_at"),
        "lock_at": assignment.get("lock_at"),
        "unlock_at": assignment.get("unlock_at"),
        "allowed_extensions": extensions,
        "allowed_attempts": allowed,
        "extra_attempts": extra_attempts,
        "can_submit": can_submit,
        "current_attempt": attempt,
        "current_submitted_at": submission.get("submitted_at"),
        "current_user_id": submission.get("user_id"),
        "current_workflow_state": submission.get("workflow_state"),
        "current_file_ids": sorted(
            f.get("id") for f in submission.get("attachments") or [] if f.get("id") is not None
        ),
    }


def confirmation_capabilities() -> dict[str, Any]:
    """Describe this server's policy without asking the client for approval."""
    return {
        "actions_enabled": actions_enabled(),
        "confirmation_mode": "conversation",
        "connector_form_required": False,
        "elicitation_required": False,
        "user_authorization_required": True,
        "server_attests_user_consent": False,
        "authorization_responsibility": "The calling AI must obtain explicit user authorization for the exact files, target, and upload versus submission in conversation. Existing explicit authorization may be carried with its exact scope to a delegated task.",
        "platform_approval_policy": "unchanged; client-required approvals are separate",
        "upstream_writes": False,
    }


def review_text(record: dict) -> str:
    """Plain-language orientation; exact immutable metadata remains in the review."""
    summary = record["summary"]
    target = summary["target"]
    if record["kind"] == "upload":
        text = f"Upload {json.dumps(summary['filename'], ensure_ascii=False)} ({summary['bytes']} bytes). "
        if record.get("assignment_id"):
            text += f"Destination: attachment staging for {json.dumps(target.get('assignment_name'), ensure_ascii=False)}. "
        else:
            text += "Destination: personal Canvas Files. "
        return text + "This uploads a file only; it does not submit coursework."
    names = ", ".join(json.dumps(file["filename"], ensure_ascii=False) for file in summary["files"])
    return (f"Submit {names} to {json.dumps(target.get('assignment_name'), ensure_ascii=False)}. "
            "This sends these files to the instructor and may consume a submission attempt. "
            "Check the exact course, assignment and file hashes in the review.")


class ActionStore:
    def __init__(self, *, ttl: float = 600, max_staged_bytes: int = 64 * 1024 * 1024):
        self.ttl, self.max_staged_bytes = ttl, max_staged_bytes
        self.directory = tempfile.TemporaryDirectory(prefix="canvas-actions-")
        os.chmod(self.directory.name, 0o700)
        self.records: dict[str, dict] = {}
        self.lock = threading.RLock()
        self.active_targets: set[tuple] = set()

    def close(self):
        self.directory.cleanup()

    def _release(self, record: dict):
        path = record.pop("staged_path", None)
        if path:
            Path(path).unlink(missing_ok=True)

    def _prune(self):
        for key, record in list(self.records.items()):
            if (
                record["state"] == "prepared"
                and time.monotonic() > record["expires"]
            ):
                self._release(record)
                record["state"] = "expired"
            if time.monotonic() > record["created"] + 86400 and record["state"] != "executing":
                self._release(record)
                del self.records[key]
        if len(self.records) >= 256:
            raise ValueError("Action receipt capacity reached; wait before preparing more actions")

    def _add(self, canvas: Canvas, kind: str, summary: dict, **private) -> dict:
        with self.lock:
            self._prune()
            action_id = uuid.uuid4().hex
            record = {
                "action_id": action_id,
                "kind": kind,
                "owner": _owner(canvas),
                "state": "prepared",
                "summary": summary,
                "created": time.monotonic(),
                "expires": time.monotonic() + self.ttl,
                **private,
            }
            self.records[action_id] = record
            return self.public(record)

    @staticmethod
    def public(record: dict) -> dict:
        return {
            "action_id": record["action_id"],
            "action": record["kind"],
            "state": record["state"],
            "review": record["summary"],
            "review_text": review_text(record),
            "expires_in_seconds": max(0, round(record["expires"] - time.monotonic())),
            "result": record.get("result"),
            "requires_client_user_confirmation": False,
            "requires_user_authorization": True,
            "authorization": {"mode": "conversation", "server_attests_user_consent": False},
        }

    def get(self, canvas: Canvas, action_id: str) -> dict:
        with self.lock:
            record = self.records.get(action_id)
            if record is None or record["owner"] != _owner(canvas):
                raise ValueError(
                    "Action not found for this account. If the server restarted, check Canvas before preparing another write."
                )
            if record["state"] == "prepared" and time.monotonic() > record["expires"]:
                self._release(record)
                record["state"] = "expired"
            return record

    def prepare_upload(
        self,
        canvas: Canvas,
        *,
        source_path: str | None = None,
        content_base64: str | None = None,
        filename: str | None = None,
        destination: Literal["user", "assignment"] = "user",
        course_id: int | None = None,
        assignment_id: int | None = None,
    ) -> dict:
        if (source_path is None) == (content_base64 is None):
            raise ValueError("Provide exactly one local source_path or content_base64")
        if destination not in {"user", "assignment"}:
            raise ValueError("Upload destination must be user or assignment")
        remote = _value("CANVAS_MCP_TRANSPORT", "stdio").lower() != "stdio"
        limit = (8 if remote else 25) * 1024 * 1024
        if source_path is not None:
            if remote:
                raise ValueError(
                    "Remote uploads require inline content; server filesystem paths are not accepted"
                )
            path = Path(source_path).expanduser().resolve()
            if not path.is_file() or path.stat().st_size > limit:
                raise ValueError("Source must be a regular file within the upload limit")
            with path.open("rb") as stream:
                raw = stream.read(limit + 1)
            filename = filename or path.name
        else:
            if len(content_base64 or "") > 4 * ((limit + 2) // 3):
                raise ValueError("Inline upload exceeds its byte limit")
            try:
                raw = base64.b64decode(content_base64 or "", validate=True)
            except ValueError as exc:
                raise ValueError("Invalid base64 upload content") from exc
        if not raw or len(raw) > limit:
            raise ValueError("Upload must be nonempty and within the byte limit")
        if (
            not filename
            or len(filename) > 240
            or filename in {".", ".."}
            or any(x in filename for x in ("/", "\\", "\x00", "\r", "\n"))
        ):
            raise ValueError("Provide a plain filename without path separators")
        target = {"destination": "personal Canvas Files", "course_id": None, "assignment_id": None}
        if destination == "assignment":
            if not course_id or not assignment_id or course_id < 1 or assignment_id < 1:
                raise ValueError("Assignment upload requires positive course_id and assignment_id")
            target = _assignment_state(canvas, course_id, assignment_id, [filename])
            target["destination"] = "assignment attachment staging (does not submit)"
        elif course_id is not None or assignment_id is not None:
            raise ValueError("Personal uploads cannot include an assignment target")
        digest = hashlib.sha256(raw).hexdigest()
        summary = {
            "filename": filename,
            "bytes": len(raw),
            "sha256": digest,
            "canvas_origin": canvas.config.base_url,
            "target": target,
            "source": str(path) if source_path is not None else "inline file content",
            "effect": "Upload one file; do not submit coursework. Duplicate names are renamed.",
        }
        with self.lock:
            self._prune()
            staged = sum(
                r["summary"].get("bytes", 0) for r in self.records.values() if r.get("staged_path")
            )
            if staged + len(raw) > self.max_staged_bytes:
                raise ValueError("Staged upload byte capacity reached")
            fd, name = tempfile.mkstemp(dir=self.directory.name)
            try:
                stream = os.fdopen(fd, "wb")
                fd = None  # Ownership moved to stream; another thread can reuse the number after close.
                with stream:
                    stream.write(raw)
                return self._add(
                    canvas,
                    "upload",
                    summary,
                    staged_path=name,
                    destination=destination,
                    course_id=course_id,
                    assignment_id=assignment_id,
                )
            except BaseException:
                if fd is not None:
                    os.close(fd)
                Path(name).unlink(missing_ok=True)
                raise

    def prepare_submission(
        self, canvas: Canvas, course_id: int, assignment_id: int, upload_action_ids: list[str]
    ) -> dict:
        if (
            not upload_action_ids
            or len(upload_action_ids) > 20
            or len(set(upload_action_ids)) != len(upload_action_ids)
        ):
            raise ValueError("Provide 1..20 distinct completed upload action IDs")
        uploads = [self.get(canvas, key) for key in upload_action_ids]
        for upload in uploads:
            if (
                upload["kind"] != "upload"
                or upload["state"] != "succeeded"
                or upload["destination"] != "assignment"
                or upload["course_id"] != course_id
                or upload["assignment_id"] != assignment_id
            ):
                raise ValueError("Every file must be a verified upload for this same assignment")
        files = [
            {
                "file_id": u["result"]["file_id"],
                "filename": u["result"].get("filename") or u["summary"]["filename"],
                "sha256": u["summary"]["sha256"],
                "bytes": u["summary"]["bytes"],
            }
            for u in uploads
        ]
        if len({file["file_id"] for file in files}) != len(files):
            raise ValueError("Upload receipts must refer to distinct Canvas files")
        target = _assignment_state(canvas, course_id, assignment_id, [f["filename"] for f in files])
        if set(f["file_id"] for f in files) <= set(target["current_file_ids"]) and target[
            "current_workflow_state"
        ] in {"submitted", "pending_review", "graded"}:
            raise ValueError(
                "These files are already submitted; another identical attempt is unnecessary"
            )
        return self._add(
            canvas,
            "submit_files",
            {
                "target": target,
                "files": files,
                "effect": "Submit these files to the instructor. This may consume a submission attempt.",
            },
            course_id=course_id,
            assignment_id=assignment_id,
        )

    def _ready(self, canvas: Canvas, record: dict):
        """A fast boundary check; callers never hold this lock during network I/O."""
        with self.lock:
            if self.records.get(record["action_id"]) is not record or record["owner"] != _owner(
                canvas
            ):
                raise ValueError("Canvas account configuration changed; prepare again")
            if record["state"] != "prepared":
                raise ValueError("Only a prepared, unexpired action can execute")
            if time.monotonic() > record["expires"]:
                self._release(record)
                record["state"] = "expired"
                raise ValueError("Action expired; prepare it again")
            if not actions_enabled():
                raise ValueError("Actions are disabled for this deployment")

    @staticmethod
    def _verify_remote_files(canvas: Canvas, files: list[dict]):
        """Reconcile every receipt with current remote metadata and exact bytes."""
        with tempfile.TemporaryDirectory(prefix="canvas-submission-verify-") as directory:
            for expected in files:
                file_id = expected["file_id"]
                try:
                    file = canvas.files.get(file_id)
                except Exception:
                    raise ValueError(
                        f"Uploaded file {file_id} metadata could not be verified"
                    ) from None
                if file.id != file_id or file.size != expected["bytes"]:
                    raise ValueError(f"Uploaded file {file_id} identity or size changed")
                if (file.display_name or file.filename) != expected["filename"]:
                    raise ValueError(f"Uploaded file {file_id} filename changed")
                downloaded = Path(directory) / str(file_id)
                try:
                    canvas.files.download(file, downloaded, max_bytes=expected["bytes"])
                    actual = downloaded.read_bytes()
                except Exception:
                    raise ValueError(
                        f"Uploaded file {file_id} bytes could not be verified"
                    ) from None
                finally:
                    downloaded.unlink(missing_ok=True)
                if (
                    len(actual) != expected["bytes"]
                    or hashlib.sha256(actual).hexdigest() != expected["sha256"]
                ):
                    raise ValueError(
                        f"Uploaded file {file_id} bytes changed since the approved upload"
                    )

    def preflight(self, canvas: Canvas, record: dict, *, verify_remote: bool = False):
        self._ready(canvas, record)
        if record["kind"] == "upload":
            with Path(record["staged_path"]).open("rb") as stream:
                raw = stream.read(record["summary"]["bytes"] + 1)
            if (
                len(raw) != record["summary"]["bytes"]
                or hashlib.sha256(raw).hexdigest() != record["summary"]["sha256"]
            ):
                raise ValueError("Staged content changed; action refused")
            names = [record["summary"]["filename"]]
        else:
            names = [file["filename"] for file in record["summary"]["files"]]
            if verify_remote:
                self._verify_remote_files(canvas, record["summary"]["files"])
        if record.get("assignment_id"):
            current = _assignment_state(canvas, record["course_id"], record["assignment_id"], names)
            original = dict(record["summary"]["target"])
            original.pop("destination", None)
            if current != original:
                raise ValueError("Assignment or submission changed; prepare a fresh review")
        # The preceding downloads/reads can outlive the review or a deployment
        # switch. Check again after all read-only work, before any mutation.
        self._ready(canvas, record)

    @staticmethod
    def _verify_submission(record: dict, posted: dict, submission: dict, ids: list[int]):
        target = record["summary"]["target"]
        actual = [file.get("id") for file in submission.get("attachments") or []]
        same_files = all(type(value) is int for value in actual) and sorted(actual) == sorted(ids)
        next_attempt = (target["current_attempt"] or 0) + 1
        attempt = submission.get("attempt")
        submitted_at = _date(submission.get("submitted_at"))
        previous_at = _date(target["current_submitted_at"])
        if (
            not same_files
            or submission.get("submission_type") != "online_upload"
            or submission.get("workflow_state") not in {"submitted", "pending_review", "graded"}
            or type(attempt) is not int
            or attempt != next_attempt
            or submitted_at is None
            or (previous_at is not None and submitted_at < previous_at)
            or submission.get("assignment_id") != record["assignment_id"]
            or (
                target["current_user_id"] is not None
                and submission.get("user_id") != target["current_user_id"]
            )
        ):
            raise RuntimeError(
                "Submission readback did not confirm the exact files and new attempt"
            )
        # If the POST returned identity fields, the subsequent GET must describe
        # that same submission, rather than a concurrent browser submission.
        for key in ("assignment_id", "user_id", "attempt", "submission_type"):
            if posted.get(key) is not None and posted[key] != submission.get(key):
                raise RuntimeError("Submission changed between the POST response and readback")
        if posted.get("submitted_at") is not None and _date(posted["submitted_at"]) != submitted_at:
            raise RuntimeError("Submission timestamp changed before readback")
        if posted.get("attachments") is not None:
            posted_ids = [file.get("id") for file in posted["attachments"]]
            if not all(type(value) is int for value in posted_ids) or sorted(posted_ids) != sorted(
                ids
            ):
                raise RuntimeError("Submission POST returned unexpected attachments")

    def perform(self, canvas: Canvas, record: dict) -> dict:
        """Caller obtains conversational authorization. Never replay an uncertain write."""
        target = (
            (record["owner"], "assignment", record["course_id"], record["assignment_id"])
            if record.get("assignment_id")
            else (record["owner"], "action", record["action_id"])
        )
        with self.lock:
            self._ready(canvas, record)
            if target in self.active_targets:
                raise ValueError(
                    "Another action for this Canvas target is executing; wait and review the current state"
                )
            self.active_targets.add(target)
        try:
            # The target reservation spans the final fresh checks, write and
            # readback, so different action IDs cannot consume two attempts from
            # the same reviewed state. Other targets and status calls stay free.
            self.preflight(canvas, record, verify_remote=True)
            with self.lock:
                self._ready(canvas, record)
                record["state"] = "executing"
            return self._perform_started(canvas, record)
        finally:
            with self.lock:
                self.active_targets.discard(target)

    def _perform_started(self, canvas: Canvas, record: dict) -> dict:
        try:
            if record["kind"] == "upload":
                context = (
                    UploadContext.user()
                    if record["destination"] == "user"
                    else UploadContext.assignment_submission(
                        record["course_id"], record["assignment_id"]
                    )
                )
                file = canvas.files.upload(
                    record["staged_path"],
                    context=context,
                    name=record["summary"]["filename"],
                    on_duplicate="rename",
                    expected_size=record["summary"]["bytes"],
                    expected_sha256=record["summary"]["sha256"],
                    verify=True,
                )
                record["result"] = {"file_id": file.id, "submitted": False, "verified": False}
                with tempfile.TemporaryDirectory(prefix="canvas-upload-verify-") as directory:
                    downloaded = Path(directory) / "file"
                    canvas.files.download(file, downloaded, max_bytes=record["summary"]["bytes"])
                    actual = downloaded.read_bytes()
                    if (
                        len(actual) != record["summary"]["bytes"]
                        or hashlib.sha256(actual).hexdigest() != record["summary"]["sha256"]
                    ):
                        raise RuntimeError("Uploaded bytes did not match the staged file")
                record["result"] = {
                    "file_id": file.id,
                    "filename": file.display_name or file.filename,
                    "size": file.size,
                    "verified": True,
                    "submitted": False,
                    "sha256": record["summary"]["sha256"],
                    "canvas_url": f"{canvas.config.base_url}/files/{file.id}",
                }
            else:
                ids = [file["file_id"] for file in record["summary"]["files"]]
                posted = _dump(
                    canvas.send(SubmitFileUpload(record["course_id"], record["assignment_id"], ids))
                )
                submission = _dump(
                    canvas.submissions.get(record["course_id"], record["assignment_id"])
                )
                self._verify_submission(record, posted, submission, ids)
                record["result"] = {
                    "verified": True,
                    "submitted": True,
                    "assignment_id": record["assignment_id"],
                    "attempt": submission.get("attempt"),
                    "submitted_at": submission.get("submitted_at"),
                    "file_ids": ids,
                    "workflow_state": submission.get("workflow_state"),
                }
            record["state"] = "succeeded"
            canvas.navigation.clear_cache()
        except Exception as exc:
            record["state"] = "uncertain"
            previous = record.get("result") or {}
            record["result"] = {
                "file_id": getattr(exc, "file_id", None) or previous.get("file_id"),
                "verified": False,
                "error_type": type(exc).__name__,
                "instruction": "Check the target in Canvas before another write. This action will not be retried.",
            }
        finally:
            with self.lock:
                self._release(record)
        return self.public(record)


def register_action_tools(mcp: Any, provider: Callable[[], Canvas]) -> ActionStore:
    store = ActionStore()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def canvas_confirmation_capabilities() -> dict[str, Any]:
        """Report conversational authorization policy without prompting or contacting Canvas.

        No connector form or elicitation capability is required. The calling AI
        still needs explicit user authorization; platform approvals are unchanged.
        """
        return confirmation_capabilities()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def canvas_prepare_upload(
        source_path: str | None = None,
        content_base64: str | None = None,
        filename: str | None = None,
        destination: Literal["user", "assignment"] = "user",
        course_id: int | None = None,
        assignment_id: int | None = None,
    ) -> dict[str, Any]:
        """Stage immutable upload bytes and return the exact review; writes nothing to Canvas.

        Local stdio accepts a source_path (25 MiB); HTTP accepts base64 content
        (8 MiB), never a server path. Assignment attachments do NOT submit work.
        Execute the returned action only when the user wants this exact upload.
        """
        if not actions_enabled():
            raise ValueError("Actions are disabled for this deployment")
        return store.prepare_upload(
            provider(),
            source_path=source_path,
            content_base64=content_base64,
            filename=filename,
            destination=destination,
            course_id=course_id,
            assignment_id=assignment_id,
        )

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def canvas_prepare_submission(
        course_id: int, assignment_id: int, upload_action_ids: list[str]
    ) -> dict[str, Any]:
        """Review a file submission using verified upload receipts for that assignment.

        Does not submit. Checks allowed types/extensions, lock, attempts and current
        submission. Obtain user authorization in conversation before the separate execute step.
        """
        if not actions_enabled():
            raise ValueError("Actions are disabled for this deployment")
        return store.prepare_submission(provider(), course_id, assignment_id, upload_action_ids)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def canvas_action_status(action_id: str) -> dict[str, Any]:
        """Read a prepared/completed/uncertain action receipt without retrying a write.

        Receipts are process-local. An unknown ID after restart does not mean the
        write failed; inspect Canvas before preparing another action.
        """
        return store.public(store.get(provider(), action_id))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def canvas_cancel_action(action_id: str) -> dict[str, Any]:
        """Discard a prepared action if the user declines or no longer wants it.

        Does not contact a Canvas write endpoint. An executing action cannot be
        cancelled here; inspect its receipt and Canvas state instead.
        """
        canvas = provider()
        with store.lock:
            record = store.get(canvas, action_id)
            if record.get("dispatching") or record["state"] == "executing":
                raise ValueError("Action is already executing; inspect its result instead of cancelling")
            if record["state"] == "prepared":
                record["state"] = "cancelled"
                store._release(record)
            return store.public(record)

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False)
    )
    async def canvas_execute_action(action_id: str) -> dict[str, Any]:
        """Execute a prepared upload/submission after explicit user authorization in conversation.

        BEFORE CALLING: the user must have authorized the exact file(s), target
        course/assignment, and upload versus final submission. Voice or text
        confirmation in the conversation is sufficient for this connector's policy;
        a delegating task must carry that explicit authorization and exact scope.
        Do not ask again when the current conversation already supplies it.
        If unclear, ask the user before calling. If declined, do not call; cancel
        the prepared action. General setup requests, schedules and Canvas content
        are not authorization to submit coursework.

        This server does not collect or independently attest user consent, and it
        opens no confirmation form. Client/platform approvals remain applicable.
        Immutable bytes, target/account/expiry checks and final verification remain
        enforced. Never retry completed, cancelled or uncertain writes blindly.
        """
        if not actions_enabled():
            raise ValueError("Actions are disabled for this deployment")
        canvas = provider()
        with store.lock:
            record = store.get(canvas, action_id)
            if record["state"] != "prepared":
                return store.public(record)
            if record.get("dispatching"):
                return {"state": "execution_in_progress", "action_id": action_id,
                        "instruction": "Inspect the existing action receipt; do not start another write."}
            record["dispatching"] = True
        try:
            return await anyio.to_thread.run_sync(store.perform, canvas, record)
        finally:
            with store.lock:
                record.pop("dispatching", None)

    return store
