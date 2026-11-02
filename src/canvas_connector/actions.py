"""Prepared student actions with immutable payloads and conservative readback.

Hosted receipts use encrypted persistence and atomic one-use claims. Local
callers may omit persistence; a missing receipt is never evidence of no write.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Callable, Literal, Union
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator
from canvas_py import Canvas
from canvas_py._errors import CanvasError
from canvas_py._request import CanvasRequest
from canvas_py.endpoints.file_uploads import UploadContext
from canvas_py.endpoints.submissions import SubmitFileUpload

from .responses import dump

PositiveId = Annotated[int, Field(gt=0, strict=True)]
Text = Annotated[str, Field(min_length=1, max_length=100_000)]


class ActionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AssignmentTarget(ActionModel):
    course_id: PositiveId
    assignment_id: PositiveId


class SubmitText(AssignmentTarget):
    kind: Literal["submit_text"]
    body: Text


class SubmitURL(AssignmentTarget):
    kind: Literal["submit_url"]
    url: Annotated[str, Field(max_length=4096)]

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Use a public HTTP(S) submission URL without embedded credentials")
        return value


class SubmitFiles(AssignmentTarget):
    kind: Literal["submit_files"]
    upload_action_ids: Annotated[list[str], Field(min_length=1, max_length=20)]


class SubmissionComment(AssignmentTarget):
    kind: Literal["submission_comment"]
    text: Text


class DiscussionPost(ActionModel):
    kind: Literal["discussion_post"]
    course_id: PositiveId
    topic_id: PositiveId
    message: Text


class DiscussionReply(ActionModel):
    kind: Literal["discussion_reply"]
    course_id: PositiveId
    topic_id: PositiveId
    entry_id: PositiveId
    message: Text


class InboxSend(ActionModel):
    kind: Literal["inbox_send"]
    recipients: Annotated[list[PositiveId], Field(min_length=1, max_length=20)]
    subject: Annotated[str, Field(min_length=1, max_length=255)]
    body: Text
    group_conversation: bool = False


class InboxReply(ActionModel):
    kind: Literal["inbox_reply"]
    conversation_id: PositiveId
    body: Text


class InboxUpdate(ActionModel):
    kind: Literal["inbox_update"]
    conversation_id: PositiveId
    workflow_state: Literal["read", "unread", "archived"]


class PlannerCreate(ActionModel):
    kind: Literal["planner_create"]
    title: Annotated[str, Field(min_length=1, max_length=255)]
    details: Annotated[str, Field(max_length=100_000)] = ""
    todo_date: str
    course_id: PositiveId | None = None

    @field_validator("todo_date")
    @classmethod
    def valid_date(cls, value: str) -> str:
        if datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("todo_date requires an ISO datetime with timezone")
        return value


class PlannerUpdate(PlannerCreate):
    kind: Literal["planner_update"]
    note_id: PositiveId


class PlannerDelete(ActionModel):
    kind: Literal["planner_delete"]
    note_id: PositiveId


class PlannerOverride(ActionModel):
    kind: Literal["planner_override"]
    plannable_type: Literal["announcement", "assignment", "discussion_topic", "quiz", "wiki_page", "planner_note", "calendar_event", "assessment_request", "sub_assignment", "peer_review_sub_assignment"]
    plannable_id: PositiveId
    marked_complete: bool = False
    dismissed: bool = False
    override_id: PositiveId | None = None


class Upload(ActionModel):
    kind: Literal["upload"]
    filename: Annotated[str, Field(min_length=1, max_length=240)]
    content_base64: Annotated[str, Field(min_length=1, max_length=11_184_812)]
    destination: Literal["user", "assignment"] = "user"
    course_id: PositiveId | None = None
    assignment_id: PositiveId | None = None

    @field_validator("filename")
    @classmethod
    def valid_filename(cls, value: str) -> str:
        if value in {".", ".."} or any(c in value for c in "/\\\x00\r\n"):
            raise ValueError("filename must be a plain filename without path separators")
        return value

    @model_validator(mode="after")
    def valid_target(self) -> Upload:
        if self.destination == "assignment" and (self.course_id is None or self.assignment_id is None):
            raise ValueError("Assignment upload requires course_id and assignment_id")
        if self.destination == "user" and (self.course_id is not None or self.assignment_id is not None):
            raise ValueError("Personal uploads cannot have an assignment target")
        raw = base64.b64decode(self.content_base64, validate=True)
        if not raw or len(raw) > 8 * 1024 * 1024:
            raise ValueError("Upload must contain 1 byte to 8 MiB")
        return self


ActionRequest = Annotated[Union[SubmitText, SubmitURL, SubmitFiles, SubmissionComment,
    DiscussionPost, DiscussionReply, InboxSend, InboxReply, InboxUpdate,
    PlannerCreate, PlannerUpdate, PlannerDelete, PlannerOverride, Upload], Field(discriminator="kind")]
ACTION_ADAPTER = TypeAdapter(ActionRequest)
ACTION_KINDS = ["upload", "submit_files", "submit_text", "submit_url", "submission_comment",
                "discussion_post", "discussion_reply", "inbox_send", "inbox_reply", "inbox_update",
                "planner_create", "planner_update", "planner_delete", "planner_override"]
EXECUTE_ROUTES = {"upload": "canvas_upload_file", "submission_comment": "canvas_add_submission_comment",
    **{kind: "canvas_submit_assignment" for kind in ("submit_files", "submit_text", "submit_url")},
    **{kind: "canvas_post_discussion" for kind in ("discussion_post", "discussion_reply")},
    **{kind: "canvas_send_message" for kind in ("inbox_send", "inbox_reply")},
    "inbox_update": "canvas_update_inbox",
    **{kind: "canvas_update_planner" for kind in ("planner_create", "planner_update", "planner_delete", "planner_override")}}


class Request(CanvasRequest[Any]):
    """Internal fixed-route request; never exposed as a generic MCP proxy."""
    def __init__(self, path: str, *, method: str = "GET", body: dict | None = None, query: list | None = None):
        self._path, self.method, self.body, self.query = path, method, body, query or []

    @property
    def path(self) -> str:
        return self._path

    def json_body(self) -> dict | None:
        return self.body

    def query_pairs(self) -> list:
        return self.query


def account_key(canvas: Canvas) -> str:
    return hashlib.sha256((canvas.config.base_url + "\0" + canvas.config.token).encode()).hexdigest()


def _assignment(canvas: Canvas, p: dict, submission_type: str | None) -> dict:
    a = dump(canvas.assignments.get(p["course_id"], p["assignment_id"],
             include=["submission", "can_submit", "all_dates", "overrides"]))
    s = dump(canvas.submissions.get(p["course_id"], p["assignment_id"]))
    if a.get("id") != p["assignment_id"] or s.get("assignment_id") not in {None, p["assignment_id"]}:
        raise ValueError("Canvas returned a different assignment")
    if submission_type:
        if a.get("locked_for_user") or a.get("published") is False or a.get("can_submit") is False:
            raise ValueError("Canvas reports that the assignment is not currently submittable")
        if a.get("group_category_id"):
            raise ValueError("Group submissions require review in Canvas")
        if submission_type not in (a.get("submission_types") or []):
            raise ValueError("The assignment does not accept this submission type")
        now = datetime.now(timezone.utc)
        for field in ("unlock_at", "lock_at"):
            if a.get(field):
                date = datetime.fromisoformat(a[field].replace("Z", "+00:00"))
                if date.tzinfo is None:
                    raise ValueError("Canvas availability date has no timezone")
                if (field == "unlock_at" and now < date) or (field == "lock_at" and now >= date):
                    raise ValueError("Assignment is outside its submission availability dates")
        allowed, attempt = a.get("allowed_attempts"), s.get("attempt") or 0
        if a.get("can_submit") is not True and allowed and allowed > 0 and attempt >= allowed + (s.get("extra_attempts") or 0):
            raise ValueError("No submission attempts remain")
    return {"course_id": p["course_id"], "assignment_id": p["assignment_id"],
            "name": a.get("name"), "submission_types": a.get("submission_types"),
            "allowed_extensions": a.get("allowed_extensions") or [],
            "due_at": a.get("due_at"), "lock_at": a.get("lock_at"), "unlock_at": a.get("unlock_at"),
            "can_submit": a.get("can_submit"), "allowed_attempts": a.get("allowed_attempts"),
            "attempt": s.get("attempt"), "submitted_at": s.get("submitted_at"), "user_id": s.get("user_id"),
            "workflow_state": s.get("workflow_state"),
            "comment_ids": [c.get("id") for c in s.get("submission_comments") or []]}


class ActionStore:
    def __init__(self, *, ttl: float = 600, capacity: int = 256, max_bytes: int = 64 * 1024 * 1024,
                 bind_credentials: bool = True, persistence: Any = None, retention: float = 7 * 86400,
                 per_owner_capacity: int = 32):
        self.ttl, self.capacity, self.max_bytes = ttl, capacity, max_bytes
        self.bind_credentials = bind_credentials
        self.persistence, self.retention, self.per_owner_capacity = persistence, retention, per_owner_capacity
        self.instance = uuid.uuid4().hex
        self.records: dict[str, dict] = {}
        self.lock = threading.RLock()

    @contextmanager
    def _preparation_transaction(self):
        """Serialize deduplication and preparation across SQLite connections."""
        if self.persistence is None:
            yield
            return
        with self.persistence.lock:
            self.persistence.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.persistence.db.execute("COMMIT")
            except BaseException:
                self.persistence.db.execute("ROLLBACK")
                raise

    def _save(self, record: dict) -> None:
        if self.persistence is not None:
            saved = dict(record)
            saved["payload"] = ""
            if record["state"] in {"prepared", "executing"} and record["payload"]:
                self.persistence.put("canvas_action_payload", record["id"], {"payload": record["payload"]},
                                     expires=record["expires"], owner=record["tenant"])
            elif record["state"] not in {"prepared", "executing"}:
                self.persistence.delete("canvas_action_payload", record["id"])
                self.persistence.delete("canvas_action_pending", record["id"])
            self.persistence.put("canvas_action", record["id"], saved,
                                 expires=record["retain_until"], owner=record["tenant"])
        # Avoid a second unbounded in-memory cache in persistent deployments.
        else:
            self.records[record["id"]] = record

    def _fingerprint_record(self, fingerprint: str) -> dict | None:
        if self.persistence is not None:
            index = self.persistence.get("canvas_action_index", fingerprint)
            return self.persistence.get("canvas_action", index["action_id"]) if index else None
        return next((r for r in self.records.values() if r["fingerprint"] == fingerprint), None)

    def _preparation_capacity(self, owner: str, encoded: str) -> None:
        now = time.time()
        if self.persistence is not None:
            # The encrypted payload is never logged. Bound live preparations
            # separately from retained terminal receipts, which have no bytes.
            rows = self.persistence.db.execute(
                "SELECT owner,payload FROM records WHERE kind='canvas_action_pending' AND expires>?", (now,)).fetchall()
            if len(rows) >= self.capacity or sum(row[0] == owner for row in rows) >= self.per_owner_capacity:
                raise ValueError("Too many pending actions. Cancel unneeded preparations or wait for expiry.")
            used = sum(json.loads(self.persistence.cipher.decrypt(row[1]))["bytes"] for row in rows)
            if used + len(encoded) > self.max_bytes:
                raise ValueError("Pending action byte capacity reached; cancel unneeded preparations or wait for expiry.")
            return
        else:
            self.records = {k: r for k, r in self.records.items() if r["retain_until"] > now}
            records = list(self.records.values())
        active = [r for r in records if r["state"] in {"prepared", "executing"} and r["expires"] > now]
        owned = [r for r in active if r["tenant"] == owner]
        if len(active) >= self.capacity or len(owned) >= self.per_owner_capacity:
            raise ValueError("Too many pending actions. Cancel unneeded preparations or wait for expiry.")
        if sum(len(r["payload"]) for r in active) + len(encoded) > self.max_bytes:
            raise ValueError("Pending action byte capacity reached; cancel unneeded preparations or wait for expiry.")
        if self.persistence is None and len(records) >= 20000:
            raise ValueError("Action history capacity reached; use encrypted persistent storage for sustained service.")

    def _identity(self, canvas: Canvas, owner: str) -> str:
        # Hosted OAuth uses an unforgeable grant identity whose Canvas user and
        # institution are fixed by auth. Token refresh must preserve receipts.
        account = account_key(canvas) if self.bind_credentials else canvas.config.base_url
        return hashlib.sha256((owner + "\0" + account).encode()).hexdigest()

    def _get(self, canvas: Canvas, owner: str, action_id: str) -> dict:
        record = (self.persistence.get("canvas_action", action_id) if self.persistence is not None
                  else self.records.get(action_id))
        if record is None or record["owner"] != self._identity(canvas, owner):
            raise ValueError("Action not found for this account. After a restart, inspect Canvas before another write.")
        if self.persistence is not None and record["state"] == "prepared":
            staged = self.persistence.get("canvas_action_payload", action_id)
            record["payload"] = staged["payload"] if staged else ""
        if record["state"] == "prepared" and time.time() >= record["expires"]:
            record["state"] = "expired"
            record["payload"] = ""
            self._save(record)
        if self.persistence is not None and record["state"] == "prepared" and (
                not record["payload"] or self.persistence.get("canvas_action_claim", action_id) is None):
            record["state"], record["payload"] = "uncertain", ""
            record["result"] = {"verified": False, "message": "Action claim was consumed or staging is missing. Inspect Canvas before retrying."}
            self._save(record)
        if record["state"] == "executing" and record.get("instance") != self.instance:
            record["state"], record["payload"] = "uncertain", ""
            record["result"] = {"verified": False, "upstream_write_attempted": True,
                                "message": "Execution was interrupted. Inspect Canvas before another write."}
            self._save(record)
        return record

    @staticmethod
    def public(record: dict) -> dict:
        return copy.deepcopy({"action_id": record["id"], "kind": record["kind"], "state": record["state"],
                "review": record["review"], "result": record.get("result"),
                "execute": {"tool": EXECUTE_ROUTES[record["kind"]], "action_id": record["id"]},
                "expires_in_seconds": max(0, round(record["expires"] - time.time())),
                "requires_user_authorization": True, "server_attests_user_consent": False,
                "receipt_storage": "encrypted_persistent" if record.get("persistent") else "process_local",
                "retry": "Inspect Canvas before repeating an uncertain or unknown action."})

    def status(self, canvas: Canvas, owner: str, action_id: str) -> dict:
        with self.lock:
            return self.public(self._get(canvas, owner, action_id))

    def cancel(self, canvas: Canvas, owner: str, action_id: str) -> dict:
        with self.lock:
            r = self._get(canvas, owner, action_id)
            if r["state"] == "executing":
                raise ValueError("Action is already executing; inspect the result")
            if r["state"] == "prepared":
                r["state"], r["payload"] = "cancelled", ""
                self._save(r)
            return self.public(r)

    def _snapshot(self, canvas: Canvas, owner: str, p: dict) -> dict:
        kind = p["kind"]
        if kind in {"submit_text", "submit_url", "submit_files", "submission_comment"} or (kind == "upload" and p["destination"] == "assignment"):
            stype = {"submit_text": "online_text_entry", "submit_url": "online_url", "submit_files": "online_upload", "upload": "online_upload"}.get(kind)
            target = _assignment(canvas, p, stype)
            if kind == "upload":
                names = [p["filename"]]
            elif kind == "submit_files":
                files = self._uploaded(canvas, owner, p)
                target["files"] = files
                names = [f["filename"] for f in files]
            else:
                names = []
            extensions = [str(e).lower().lstrip(".") for e in target["allowed_extensions"]]
            if extensions and any(Path(name).suffix.lower().lstrip(".") not in extensions for name in names):
                raise ValueError("A file extension is not allowed by the assignment")
            return target
        if kind.startswith("discussion_"):
            topic = dump(canvas.discussions.get(p["course_id"], p["topic_id"]))
            if topic.get("locked") or topic.get("locked_for_user") or (topic.get("permissions") or {}).get("reply") is False:
                raise ValueError("Discussion does not permit this reply")
            return {k: topic.get(k) for k in ("id", "title", "assignment_id", "locked", "locked_for_user", "permissions")}
        if kind in {"inbox_reply", "inbox_update"}:
            thread = dump(canvas.inbox.get(p["conversation_id"], auto_mark_as_read=False))
            return {k: thread.get(k) for k in ("id", "subject", "audience", "participants", "workflow_state")}
        if kind in {"planner_update", "planner_delete"}:
            return dump(canvas.send(Request(f"planner_notes/{p['note_id']}")))
        if kind == "planner_override" and p.get("override_id"):
            existing = canvas.send(Request(f"planner/overrides/{p['override_id']}"))
            normalized_type = str(existing.get("plannable_type", "")).replace("_", "").lower()
            if existing.get("plannable_id") != p["plannable_id"] or normalized_type != p["plannable_type"].replace("_", "").lower():
                raise ValueError("Planner override belongs to another item")
            return existing
        return {}

    def _uploaded(self, canvas: Canvas, owner: str, p: dict) -> list[dict]:
        ids = p["upload_action_ids"]
        if len(set(ids)) != len(ids):
            raise ValueError("Upload receipt IDs must be distinct")
        files = []
        with self.lock:
            for action_id in ids:
                r = self._get(canvas, owner, action_id)
                if r["kind"] != "upload" or r["state"] != "succeeded":
                    raise ValueError("Each file must have a verified completed upload receipt")
                target = r["review"]
                if target.get("destination") != "assignment" or any(target.get(k) != p[k] for k in ("course_id", "assignment_id")):
                    raise ValueError("Upload receipt belongs to another assignment")
                files.append({"id": r["result"]["id"], "filename": target["filename"],
                              "bytes": target["bytes"], "sha256": target["sha256"]})
        if len({f["id"] for f in files}) != len(files):
            raise ValueError("Receipts must reference distinct Canvas files")
        return files

    def prepare(self, canvas: Canvas, owner: str, action: ActionRequest | dict) -> dict:
        parsed = ACTION_ADAPTER.validate_python(action)
        p = parsed.model_dump(mode="json")
        encoded = json.dumps(p, sort_keys=True, separators=(",", ":"))
        identity = self._identity(canvas, owner)
        fingerprint = hashlib.sha256((identity + "\0" + encoded).encode()).hexdigest()
        with self.lock:
            r = self._fingerprint_record(fingerprint)
            if r:
                r = self._get(canvas, owner, r["id"])
                if r["state"] not in {"cancelled", "expired", "rejected"}:
                    return self.public(r)
        snapshot = self._snapshot(canvas, owner, p)
        review = {k: v for k, v in p.items() if k != "content_base64"}
        review.update({"canvas_origin": canvas.config.base_url, "target": snapshot})
        if p["kind"] == "upload":
            raw = base64.b64decode(p["content_base64"], validate=True)
            review.update({"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "effect": "Upload file only; does not submit coursework"})
        with self.lock, self._preparation_transaction():
            # Recheck after reads so concurrent preparations collapse to one receipt.
            previous = self._fingerprint_record(fingerprint)
            if previous:
                previous = self._get(canvas, owner, previous["id"])
                if previous["state"] not in {"cancelled", "expired", "rejected"}:
                    return self.public(previous)
            self._preparation_capacity(owner, encoded)
            action_id = uuid.uuid4().hex
            r = {"id": action_id, "owner": identity, "fingerprint": fingerprint, "payload": encoded,
                 "kind": p["kind"], "state": "prepared", "review": review, "snapshot": snapshot,
                 "expires": time.time() + self.ttl, "retain_until": time.time() + self.retention,
                 "tenant": owner, "persistent": self.persistence is not None, "instance": self.instance}
            self._save(r)
            if self.persistence is not None:
                self.persistence.put("canvas_action_index", fingerprint, {"action_id": action_id},
                                     expires=r["retain_until"], owner=owner)
                self.persistence.put("canvas_action_claim", action_id, {"owner": identity},
                                     expires=r["retain_until"], owner=owner)
                self.persistence.put("canvas_action_pending", action_id, {"bytes": len(encoded)},
                                     expires=r["expires"], owner=owner)
            return self.public(r)

    def execute(self, canvas: Canvas, owner: str, action_id: str, authorize_write: Callable[[], None]) -> dict:
        authorize_write()
        with self.lock:
            r = self._get(canvas, owner, action_id)
            if r["state"] != "prepared":
                return self.public(r)
            if self.persistence is not None:
                claim = self.persistence.consume("canvas_action_claim", action_id)
                if claim is None or claim.get("owner") != r["owner"]:
                    r["state"], r["payload"] = "uncertain", ""
                    r["result"] = {"verified": False, "message": "Action was already claimed. Inspect Canvas; do not retry."}
                    self._save(r)
                    return self.public(r)
            # Claim before any I/O: concurrent executes cannot both dispatch.
            r["state"], r["instance"] = "executing", self.instance
            p = json.loads(r["payload"])
            self._save(r)
        try:
            snapshot = self._snapshot(canvas, owner, p)
            if snapshot != r["snapshot"]:
                raise ValueError("Target changed after preparation; prepare a fresh review")
            if p["kind"] == "submit_files":
                self._verify_files(canvas, snapshot["files"])
            if time.time() >= r["expires"]:
                raise ValueError("Prepared action expired before dispatch")
            authorize_write()
        except Exception:
            with self.lock:
                r["state"], r["payload"] = "rejected", ""
                r["result"] = {"verified": False, "upstream_write_attempted": False,
                               "message": "Preflight failed or target changed. Re-read the target before preparing again."}
                self._save(r)
                return self.public(r)
        try:
            result = self._perform(canvas, p, snapshot)
            state = "succeeded"
        except Exception:
            # A timeout or failed readback cannot establish that the write failed.
            result, state = {"verified": False, "upstream_write_attempted": True,
                             "message": "Outcome uncertain. Inspect Canvas; do not automatically retry."}, "uncertain"
        with self.lock:
            r["state"], r["result"], r["payload"] = state, result, ""
            self._save(r)
            canvas.navigation.clear_cache()
            return self.public(r)

    @staticmethod
    def _verify_files(canvas: Canvas, files: list[dict]) -> None:
        with tempfile.TemporaryDirectory(prefix="canvas-verify-") as directory:
            for index, expected in enumerate(files):
                f = canvas.files.get(expected["id"])
                if f.size != expected["bytes"]:
                    raise ValueError("Uploaded file size changed")
                path = Path(directory) / str(index)
                canvas.files.download(f, path, max_bytes=expected["bytes"])
                if hashlib.sha256(path.read_bytes()).hexdigest() != expected["sha256"]:
                    raise ValueError("Uploaded file content changed")

    def _perform(self, canvas: Canvas, p: dict, target: dict) -> dict:
        kind = p["kind"]
        if kind == "upload":
            raw = base64.b64decode(p["content_base64"], validate=True)
            context = UploadContext.user() if p["destination"] == "user" else UploadContext.assignment_submission(p["course_id"], p["assignment_id"])
            with tempfile.TemporaryDirectory(prefix="canvas-upload-") as directory:
                path = Path(directory) / p["filename"]
                path.write_bytes(raw)
                uploaded = dump(canvas.files.upload(path, context=context, name=p["filename"], on_duplicate="rename",
                           expected_size=len(raw), expected_sha256=hashlib.sha256(raw).hexdigest(), verify=True))
            if not uploaded.get("id"):
                raise ValueError("Canvas did not return a file identity")
            self._verify_files(canvas, [{"id": uploaded["id"], "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}])
            return {"id": uploaded["id"], "filename": uploaded.get("display_name"), "size": len(raw), "verified": True, "submitted": False}
        if kind.startswith("submit_") or kind == "submission_comment":
            c, a = p["course_id"], p["assignment_id"]
            if kind == "submit_text":
                posted = dump(canvas.submissions.submit_text(c, a, p["body"]))
            elif kind == "submit_url":
                posted = dump(canvas.submissions.submit_url(c, a, p["url"]))
            elif kind == "submit_files":
                posted = dump(canvas.send(SubmitFileUpload(c, a, [f["id"] for f in target["files"]])))
            else:
                posted = dump(canvas.submissions.add_comment(c, a, p["text"]))
            result = dump(canvas.submissions.get(c, a))
            if result.get("assignment_id") != a or result.get("user_id") != target.get("user_id"):
                raise ValueError("Submission identity changed")
            if kind == "submission_comment":
                comments = result.get("submission_comments") or []
                if not any(x.get("id") not in target["comment_ids"] and x.get("comment") == p["text"] for x in comments):
                    raise ValueError("Comment not found on readback")
            else:
                stype = {"submit_text": "online_text_entry", "submit_url": "online_url", "submit_files": "online_upload"}[kind]
                if result.get("submission_type") != stype or result.get("workflow_state") not in {"submitted", "graded", "pending_review"} or not result.get("submitted_at"):
                    raise ValueError("Submission not confirmed")
                if result.get("attempt") != (target.get("attempt") or 0) + 1:
                    raise ValueError("Submission attempt not confirmed")
                for key in ("attempt", "submitted_at", "assignment_id", "user_id"):
                    if posted.get(key) is not None and posted[key] != result.get(key):
                        raise ValueError("Submission changed before readback")
                if kind == "submit_text" and result.get("body") != p["body"]:
                    raise ValueError("Submitted text differs from review")
                if kind == "submit_url" and result.get("url") != p["url"]:
                    raise ValueError("Submitted URL differs from review")
                if kind == "submit_files" and sorted(x["id"] for x in result.get("attachments") or []) != sorted(x["id"] for x in target["files"]):
                    raise ValueError("Submitted files differ from review")
            return {"verified": True, "submission": result}
        if kind.startswith("discussion_"):
            args = [p["course_id"], p["topic_id"]]
            posted = dump(canvas.discussions.post(*args, p["message"]) if kind == "discussion_post" else canvas.discussions.reply(*args, p["entry_id"], p["message"]))
            base = f"courses/{p['course_id']}/discussion_topics/{p['topic_id']}"
            # entry_list reads explicit IDs, including replies, independent of cached /view.
            entries = canvas.send(Request(base + "/entry_list", query=[("ids[]", posted["id"])]))
            found = next((x for x in entries if x.get("id") == posted["id"]), None)
            if not found or found.get("message") != posted.get("message") or found.get("deleted"):
                raise ValueError("Discussion entry not confirmed")
            return {"verified": True, "entry": found}
        if kind in {"inbox_send", "inbox_reply"}:
            if kind == "inbox_send":
                posted = canvas.send(Request("conversations", method="POST", body={"recipients": p["recipients"], "subject": p["subject"],
                    "body": p["body"], "force_new": True, "group_conversation": p["group_conversation"], "mode": "sync"}))
                threads = posted if isinstance(posted, list) else [posted]
            else:
                threads = [canvas.send(Request(f"conversations/{p['conversation_id']}/add_message", method="POST", body={"body": p["body"]}))]
            if not threads:
                raise ValueError("No conversation returned")
            verified = []
            for thread in threads:
                readback = dump(canvas.inbox.get(thread["id"], auto_mark_as_read=False))
                returned_ids = {m["id"] for m in thread.get("messages", []) if m.get("body") == p["body"]}
                if not returned_ids or not any(m.get("id") in returned_ids and m.get("body") == p["body"] for m in readback.get("messages", [])):
                    raise ValueError("Message not found in conversation readback")
                verified.append(readback)
            return {"verified": True, "conversations": verified}
        if kind == "inbox_update":
            canvas.send(Request(f"conversations/{p['conversation_id']}", method="PUT", body={"conversation": {"workflow_state": p["workflow_state"]}}))
            readback = dump(canvas.inbox.get(p["conversation_id"], auto_mark_as_read=False))
            if readback.get("workflow_state") != p["workflow_state"]:
                raise ValueError("Inbox state not confirmed")
            return {"verified": True, "conversation": readback}
        if kind in {"planner_create", "planner_update"}:
            fields = {k: p[k] for k in ("title", "details", "todo_date", "course_id")}
            posted = dump(canvas.planner.create_note(**fields) if kind == "planner_create" else canvas.planner.update_note(p["note_id"], **fields))
            readback = canvas.send(Request(f"planner_notes/{posted['id']}"))
            if any(readback.get(k) != posted.get(k) for k in fields) or readback.get("title") != p["title"] or readback.get("details") != p["details"]:
                raise ValueError("Planner note not confirmed")
            return {"verified": True, "note": readback}
        if kind == "planner_delete":
            canvas.planner.delete_note(p["note_id"])
            try:
                note = canvas.send(Request(f"planner_notes/{p['note_id']}"))
            except CanvasError as error:
                if error.status != 404:
                    raise
            else:
                if note.get("workflow_state") != "deleted":
                    raise ValueError("Planner deletion not confirmed")
            return {"verified": True, "deleted_note_id": p["note_id"]}
        if kind == "planner_override":
            if p["override_id"]:
                posted = canvas.planner.update_override(p["override_id"], marked_complete=p["marked_complete"], dismissed=p["dismissed"])
            else:
                posted = canvas.planner.create_override(p["plannable_type"], p["plannable_id"], marked_complete=p["marked_complete"], dismissed=p["dismissed"])
            readback = canvas.send(Request(f"planner/overrides/{posted['id']}"))
            if any(readback.get(k) != p[k] for k in ("marked_complete", "dismissed")):
                raise ValueError("Planner override not confirmed")
            return {"verified": True, "override": readback,
                    "meaning": "Personal planner state only; does not submit an assignment or complete a course module."}
        raise ValueError("Unsupported action")
