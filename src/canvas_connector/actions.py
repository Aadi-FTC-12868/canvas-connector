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
from . import module_progress, student_actions, personal_files, preferences, group_content, enrollments, bookmarks, what_if_grades, mastery_paths, group_memberships, discussion_attachments, feedback, appointments, self_profile, dashboard_cards, inbox_state, inbox_messages, inbox_attachments

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
    upload_action_ids: Annotated[list[Annotated[str, Field(min_length=1, max_length=128)]], Field(max_length=20)] = Field(default_factory=list)


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
    destination: Literal["user", "assignment", "submission_comment"] = "user"
    course_id: PositiveId | None = None
    assignment_id: PositiveId | None = None
    parent_folder_id: PositiveId | None = None

    @field_validator("filename")
    @classmethod
    def valid_filename(cls, value: str) -> str:
        if value in {".", ".."} or any(c in value for c in "/\\\x00\r\n"):
            raise ValueError("filename must be a plain filename without path separators")
        return value

    @model_validator(mode="after")
    def valid_target(self) -> Upload:
        if self.destination in {"assignment", "submission_comment"} and (self.course_id is None or self.assignment_id is None):
            raise ValueError("Assignment/comment upload requires course_id and assignment_id")
        if self.destination == "user" and (self.course_id is not None or self.assignment_id is not None):
            raise ValueError("Personal uploads cannot have an assignment target")
        if self.parent_folder_id is not None and self.destination != "user":
            raise ValueError("A parent folder is supported only for personal uploads")
        raw = base64.b64decode(self.content_base64, validate=True)
        if not raw or len(raw) > 8 * 1024 * 1024:
            raise ValueError("Upload must contain 1 byte to 8 MiB")
        return self


ActionRequest = Annotated[Union[SubmitText, SubmitURL, SubmitFiles, SubmissionComment,
    DiscussionPost, DiscussionReply, InboxSend, InboxReply, InboxUpdate,
    PlannerCreate, PlannerUpdate, PlannerDelete, PlannerOverride, Upload,
    student_actions.StudentActionTypes, module_progress.ModuleProgressActionTypes,
    personal_files.PersonalFileActionTypes, preferences.PreferenceActionTypes,
    group_content.GroupContentActionTypes, enrollments.InvitationActionTypes,
    bookmarks.BookmarkActionTypes, what_if_grades.WhatIfActionTypes, mastery_paths.MasteryPathActionTypes,
    group_memberships.GroupMembershipActionTypes, discussion_attachments.DiscussionAttachmentActionTypes,
    feedback.FeedbackActionTypes, appointments.AppointmentActionTypes, self_profile.SelfProfileActionTypes,
    dashboard_cards.DashboardCardActionTypes, inbox_state.InboxStateActionTypes,
    inbox_messages.InboxMessageActionTypes, inbox_attachments.InboxAttachmentActionTypes], Field(discriminator="kind")]
ACTION_ADAPTER = TypeAdapter(ActionRequest)
ACTION_KINDS = ["upload", "submit_files", "submit_text", "submit_url", "submission_comment",
                "discussion_post", "discussion_reply", "inbox_send", "inbox_reply", "inbox_update",
                "planner_create", "planner_update", "planner_delete", "planner_override", *student_actions.KINDS, *module_progress.KINDS,
                *personal_files.KINDS, *preferences.KINDS, *group_content.KINDS, *enrollments.KINDS,
                *bookmarks.KINDS, *what_if_grades.KINDS, *mastery_paths.KINDS, *group_memberships.KINDS,
                *discussion_attachments.KINDS, *feedback.KINDS, *appointments.KINDS, *self_profile.KINDS, *dashboard_cards.KINDS,
                *inbox_state.KINDS, *inbox_messages.KINDS, *inbox_attachments.KINDS]
EXECUTE_ROUTES = {"upload": "canvas_upload_file", "submission_comment": "canvas_add_submission_comment",
    **{kind: "canvas_submit_assignment" for kind in ("submit_files", "submit_text", "submit_url")},
    **{kind: "canvas_post_discussion" for kind in ("discussion_post", "discussion_reply")},
    **{kind: "canvas_send_message" for kind in ("inbox_send", "inbox_reply")},
    "inbox_update": "canvas_update_inbox",
    **{kind: "canvas_update_planner" for kind in ("planner_create", "planner_update", "planner_delete", "planner_override")},
    **student_actions.ROUTES, **module_progress.ROUTES, **personal_files.ROUTES,
    **preferences.ROUTES, **group_content.ROUTES, **enrollments.ROUTES, **bookmarks.ROUTES, **what_if_grades.ROUTES, **mastery_paths.ROUTES, **group_memberships.ROUTES,
    **discussion_attachments.ROUTES, **feedback.ROUTES, **appointments.ROUTES, **self_profile.ROUTES, **dashboard_cards.ROUTES,
    **inbox_state.ROUTES, **inbox_messages.ROUTES, **inbox_attachments.ROUTES}


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


def _send_inbox(canvas: Canvas, p: dict) -> dict:
    """Verify native new-thread sends without changing legacy prepared payloads.

    ConversationsController#create requires string recipients. force_new enters
    ConversationBatch, whose deliver method creates one thread per recipient;
    a real group send must use direct non-private initiation instead. Batch
    acknowledgements contain summaries, so independent thread GETs establish
    the new message ID, author, audience and exact attachment-free content.
    Sources: app/controllers/conversations_controller.rb and
    app/models/conversation_batch.rb in instructure/canvas-lms.
    """
    def request(path: str, *, method: str = "GET", body: dict | None = None, query: list | None = None) -> Any:
        operation = Request(path, method=method, body=body, query=query)
        operation.allow_retries = method == "GET"
        operation.allow_redirects = False
        return dump(canvas.send(operation))

    def identity(value: Any) -> int:
        if type(value) is not int or value <= 0:
            raise ValueError("Canvas did not provide an exact Inbox identity")
        return value

    def identities(value: Any, *, maximum: int = 1000) -> set[int]:
        if not isinstance(value, list) or len(value) > maximum:
            raise ValueError("Canvas did not provide bounded Inbox identities")
        result = {identity(item) for item in value}
        if len(result) != len(value):
            raise ValueError("Canvas returned duplicate Inbox identities")
        return result

    profile = request("users/self/profile")
    actor = identity(profile.get("id") if isinstance(profile, dict) else None)
    recipients = identities(p["recipients"], maximum=20)
    if actor in recipients and len(recipients) > 1:
        raise ValueError("Canvas omits self from mixed recipient lists; review recipients without self")
    groups = [frozenset(recipients)] if p["group_conversation"] else [frozenset([recipient]) for recipient in recipients]
    expected_audiences = {group - {actor} for group in groups}
    posted = request("conversations", method="POST", body={
        "recipients": [str(recipient) for recipient in p["recipients"]],
        "subject": p["subject"], "body": p["body"],
        "force_new": not p["group_conversation"],
        "group_conversation": p["group_conversation"], "mode": "sync"})
    if not isinstance(posted, list) or len(posted) != len(groups) or any(not isinstance(row, dict) for row in posted):
        raise ValueError("Canvas did not return the complete requested conversation set")
    identities([row.get("id") for row in posted], maximum=20)
    verified, actual_audiences = [], set()
    for thread in posted:
        thread_id = identity(thread.get("id"))
        readback = request(f"conversations/{thread_id}", query=[("auto_mark_as_read", False)])
        if not isinstance(readback, dict) or identity(readback.get("id")) != thread_id:
            raise ValueError("Canvas returned a different conversation during verification")
        audience = frozenset(identities(readback.get("audience"), maximum=20))
        if audience not in expected_audiences or audience in actual_audiences:
            raise ValueError("Canvas did not confirm the exact reviewed recipient mapping")
        actual_audiences.add(audience)
        if readback.get("subject") != p["subject"] or readback.get("private") is not False:
            raise ValueError("Canvas did not confirm a new conversation with the reviewed subject")
        participants = readback.get("participants")
        if (not isinstance(participants, list) or any(not isinstance(row, dict) for row in participants)
                or identities([row.get("id") for row in participants], maximum=21) != audience | {actor}):
            raise ValueError("Canvas did not confirm the exact conversation participants")
        messages = readback.get("messages")
        if not isinstance(messages, list) or len(messages) > 1000 or any(not isinstance(row, dict) for row in messages):
            raise ValueError("Canvas did not provide complete bounded conversation messages")
        identities([message.get("id") for message in messages])
        authored = [message for message in messages if identity(message.get("author_id")) == actor]
        if len(authored) != 1:
            raise ValueError("Canvas did not independently identify exactly one new authored message")
        message = authored[0]
        if (message.get("body") != p["body"] or message.get("generated") is not False
                or message.get("attachments") != [] or message.get("forwarded_messages") != []
                or "media_comment" not in message or message["media_comment"] is not None
                or identities(message.get("participating_user_ids"), maximum=21) != audience | {actor}):
            raise ValueError("Canvas did not confirm the exact authored message, recipients and empty attachments")
        # Direct group creation supplies the posted message ID. Native batch
        # creation omits messages; its fresh, non-private threads are read above.
        if "messages" in thread:
            acknowledged = thread["messages"]
            if (not isinstance(acknowledged, list) or len(acknowledged) != 1
                    or not isinstance(acknowledged[0], dict)
                    or identity(acknowledged[0].get("id")) != message["id"]):
                raise ValueError("Canvas message acknowledgement and independent readback disagree")
        verified.append(readback)
    after = request("users/self/profile")
    if not isinstance(after, dict) or identity(after.get("id")) != actor or actual_audiences != expected_audiences:
        raise ValueError("Canvas actor or recipient set changed during message verification")
    return {"verified": True, "conversations": verified}


def _assignment(canvas: Canvas, p: dict, submission_type: str | None, *, require_own_submission: bool = False) -> dict:
    a = dump(canvas.assignments.get(p["course_id"], p["assignment_id"],
             include=["submission", "can_submit", "all_dates", "overrides"]))
    s = dump(canvas.submissions.get(p["course_id"], p["assignment_id"]))
    if a.get("id") != p["assignment_id"] or s.get("assignment_id") not in {None, p["assignment_id"]}:
        raise ValueError("Canvas returned a different assignment")
    if require_own_submission and (s.get("assignment_id") != p["assignment_id"] or
            type(s.get("user_id")) is not int or s["user_id"] < 1 or
            not isinstance(s.get("submission_comments"), list)):
        raise ValueError("Canvas did not identify your own submission and comments")
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
    """Transactional prepared-action state machine, shared safely by workers.

    A claimed action never becomes runnable again. Heartbeats extend an execution
    lease; an expired lease becomes uncertain, including after a process crash.
    All mutations compare the current durable state under a storage transaction.
    """
    def __init__(self, *, ttl: float = 600, capacity: int = 256, max_bytes: int = 64 * 1024 * 1024,
                 bind_credentials: bool = True, persistence: Any = None, retention: float = 7 * 86400,
                 per_owner_capacity: int = 32, execution_lease: float = 120,
                 heartbeat_interval: float | None = None, max_execution_seconds: float = 1800):
        if min(ttl, retention, execution_lease, max_execution_seconds) <= 0:
            raise ValueError("Action timeouts must be positive")
        heartbeat_interval = heartbeat_interval or min(30.0, execution_lease / 3)
        if not 0 < heartbeat_interval < execution_lease:
            raise ValueError("heartbeat_interval must be shorter than execution_lease")
        if retention <= ttl + max_execution_seconds or max_execution_seconds < execution_lease:
            raise ValueError("Retention must outlive preparation plus execution; execution deadline must cover its lease")
        self.ttl, self.capacity, self.max_bytes = ttl, capacity, max_bytes
        self.bind_credentials = bind_credentials
        self.persistence, self.retention, self.per_owner_capacity = persistence, retention, per_owner_capacity
        self.execution_lease = execution_lease
        self.heartbeat_interval = heartbeat_interval
        self.max_execution_seconds = max_execution_seconds
        self.records: dict[str, dict] = {}
        self.lock = threading.RLock()

    @contextmanager
    def _atomic(self):
        """One cross-worker transaction; never hold this around Canvas I/O."""
        with self.lock:
            if self.persistence is None:
                yield
            else:
                with self.persistence.transaction():
                    yield

    def _revoked(self, owner: str) -> bool:
        return self.persistence is not None and self.persistence.is_owner_revoked(owner)

    def _load(self, action_id: str) -> dict | None:
        record = (self.persistence.get("canvas_action", action_id) if self.persistence is not None
                  else self.records.get(action_id))
        return copy.deepcopy(record) if record is not None else None

    def _save(self, record: dict) -> None:
        # Every caller holds _atomic, including expiry, cancellation and heartbeat.
        if self.persistence is not None:
            saved = dict(record)
            saved["payload"] = ""
            if record["state"] in {"prepared", "executing"} and record["payload"]:
                self.persistence.put("canvas_action_payload", record["id"], {"payload": record["payload"]},
                                     expires=record["expires"], owner=record["tenant"])
            elif record["state"] not in {"prepared", "executing"}:
                for kind in ("canvas_action_payload", "canvas_action_pending", "canvas_action_claim"):
                    self.persistence.delete(kind, record["id"])
            self.persistence.put("canvas_action", record["id"], saved,
                                 expires=record["retain_until"], owner=record["tenant"])
        else:
            self.records[record["id"]] = copy.deepcopy(record)

    def _fingerprint_record(self, fingerprint: str) -> dict | None:
        if self.persistence is not None:
            index = self.persistence.get("canvas_action_index", fingerprint)
            return self._load(index["action_id"]) if index else None
        return next((copy.deepcopy(r) for r in reversed(list(self.records.values()))
                     if r["fingerprint"] == fingerprint), None)

    def _intent_record(self, identity: str, intent_id: str) -> dict | None:
        key = hashlib.sha256((identity + "\0" + intent_id).encode()).hexdigest()
        if self.persistence is not None:
            index = self.persistence.get("canvas_action_intent", key)
            return self._load(index["action_id"]) if index else None
        return next((copy.deepcopy(r) for r in self.records.values()
                     if r["owner"] == identity and r.get("intent_id") == intent_id), None)

    def _preparation_capacity(self, owner: str, encoded: str) -> None:
        now = time.time()
        if self.persistence is not None:
            pending = self.persistence.scan("canvas_action_pending")
            if len(pending) >= self.capacity or sum(r.owner == owner for r in pending) >= self.per_owner_capacity:
                raise ValueError("Too many pending actions. Cancel unneeded preparations or wait for expiry.")
            if sum(r.value["bytes"] for r in pending) + len(encoded) > self.max_bytes:
                raise ValueError("Pending action byte capacity reached; cancel unneeded preparations or wait for expiry.")
            return
        self.records = {k: r for k, r in self.records.items() if r["retain_until"] > now}
        active = [r for r in self.records.values() if
                  (r["state"] == "prepared" and r["expires"] > now) or
                  (r["state"] == "executing" and r.get("lease_expires", 0) > now)]
        if len(active) >= self.capacity or sum(r["tenant"] == owner for r in active) >= self.per_owner_capacity:
            raise ValueError("Too many pending actions. Cancel unneeded preparations or wait for expiry.")
        if sum(r.get("payload_bytes", len(r["payload"])) for r in active) + len(encoded) > self.max_bytes:
            raise ValueError("Pending action byte capacity reached; cancel unneeded preparations or wait for expiry.")
        if len(self.records) >= 20000:
            raise ValueError("Action history capacity reached; use encrypted persistent storage for sustained service.")

    def _identity(self, canvas: Canvas, owner: str) -> str:
        # Hosted grants bind institution + Canvas user; token refresh is not a
        # new identity. Local deployments default to stricter credential binding.
        account = account_key(canvas) if self.bind_credentials else canvas.config.base_url
        return hashlib.sha256((owner + "\0" + account).encode()).hexdigest()

    def _get(self, canvas: Canvas, owner: str, action_id: str) -> dict:
        record = self._load(action_id)
        if record is None or record["owner"] != self._identity(canvas, owner) or self._revoked(owner):
            raise ValueError("Action not found for this account. Inspect Canvas before repeating an unknown write.")
        if self.persistence is not None and record["state"] == "prepared":
            staged = self.persistence.get("canvas_action_payload", action_id)
            record["payload"] = staged["payload"] if staged else ""
        now = time.time()
        if record["state"] == "prepared" and now >= record["expires"]:
            record["state"], record["payload"] = "expired", ""
            self._save(record)
        if self.persistence is not None and record["state"] == "prepared" and (
                not record["payload"] or self.persistence.get("canvas_action_claim", action_id) is None):
            # Also covers a crash in the earlier nontransactional implementation.
            record["state"], record["payload"] = "uncertain", ""
            record["result"] = {"verified": False, "message": "Action staging or claim is missing. Inspect Canvas before retrying."}
            self._save(record)
        if record["state"] == "executing" and now >= record.get("lease_expires", 0):
            record["state"], record["payload"] = "uncertain", ""
            record["result"] = {"verified": False, "upstream_write_attempted": record.get("phase") == "dispatching",
                                "message": "Execution lease expired. The old operation may have reached Canvas; inspect before a new intent."}
            self._save(record)
        return record

    @staticmethod
    def public(record: dict) -> dict:
        return copy.deepcopy({"action_id": record["id"], "kind": record["kind"], "state": record["state"],
                "review": record["review"], "result": record.get("result"),
                "intent_id": record.get("intent_id"),
                "execute": {"tool": EXECUTE_ROUTES[record["kind"]], "action_id": record["id"]},
                "expires_in_seconds": max(0, round(record["expires"] - time.time())),
                "execution_lease_seconds": max(0, round(record.get("lease_expires", 0) - time.time())) if record["state"] == "executing" else None,
                "requires_user_authorization": True, "server_attests_user_consent": False,
                "receipt_storage": "encrypted_persistent" if record.get("persistent") else "process_local",
                "retry": "Retry this action ID to read its result. A new intent_id permits a repeat only after fresh user authorization and Canvas inspection for uncertain outcomes."})

    def status(self, canvas: Canvas, owner: str, action_id: str) -> dict:
        with self._atomic():
            return self.public(self._get(canvas, owner, action_id))

    def cancel(self, canvas: Canvas, owner: str, action_id: str) -> dict:
        with self._atomic():
            r = self._get(canvas, owner, action_id)
            if r["state"] == "executing":
                raise ValueError("Action is already executing; inspect the result")
            if r["state"] == "prepared":
                r["state"], r["payload"] = "cancelled", ""
                self._save(r)
            return self.public(r)

    def _snapshot(self, canvas: Canvas, owner: str, p: dict) -> dict:
        kind = p["kind"]
        if kind in inbox_messages.KINDS:
            return inbox_messages.snapshot(canvas, p, files=self._conversation_uploaded(canvas, owner, p))
        for module in (personal_files, preferences, group_content, enrollments, bookmarks, what_if_grades, mastery_paths, group_memberships, discussion_attachments, feedback, appointments, self_profile, dashboard_cards, inbox_state, inbox_attachments):
            if kind in module.KINDS:
                return module.snapshot(canvas, p)
        if kind == "upload" and p.get("parent_folder_id") is not None:
            return personal_files.validated_personal_folder(canvas, p["parent_folder_id"])
        if kind in module_progress.KINDS:
            return module_progress.snapshot(canvas, p)
        if kind in student_actions.KINDS:
            return student_actions.snapshot(canvas, p)
        if kind in {"submit_text", "submit_url", "submit_files", "submission_comment"} or (kind == "upload" and p["destination"] in {"assignment", "submission_comment"}):
            is_comment = kind == "submission_comment" or (kind == "upload" and p["destination"] == "submission_comment")
            stype = None if is_comment else {"submit_text": "online_text_entry", "submit_url": "online_url", "submit_files": "online_upload", "upload": "online_upload"}.get(kind)
            target = _assignment(canvas, p, stype, require_own_submission=is_comment)
            if kind == "upload" and not is_comment:
                names = [p["filename"]]
            elif kind == "submit_files":
                files = self._uploaded(canvas, owner, p)
                target["files"] = files
                names = [f["filename"] for f in files]
            elif kind == "submission_comment":
                if p.get("upload_action_ids"):
                    target["files"] = self._uploaded(canvas, owner, p, destination="submission_comment", user_id=target["user_id"])
                names = []
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

    def _uploaded(self, canvas: Canvas, owner: str, p: dict, *, destination: str = "assignment", user_id: int | None = None) -> list[dict]:
        ids = p["upload_action_ids"]
        if len(set(ids)) != len(ids):
            raise ValueError("Upload receipt IDs must be distinct")
        files = []
        with self._atomic():
            for action_id in ids:
                r = self._get(canvas, owner, action_id)
                if r["kind"] != "upload" or r["state"] != "succeeded":
                    raise ValueError("Each file must have a verified completed upload receipt")
                target = r["review"]
                if target.get("destination") != destination or any(target.get(k) != p[k] for k in ("course_id", "assignment_id")):
                    raise ValueError("Upload receipt belongs to another assignment or upload context")
                if user_id is not None and (target.get("target") or {}).get("user_id") != user_id:
                    raise ValueError("Upload receipt belongs to another Canvas user")
                result = r.get("result") or {}
                if result.get("verified") is not True or type(result.get("id")) is not int or result["id"] < 1 or result.get("size") != target.get("bytes"):
                    raise ValueError("Upload receipt does not confirm the expected file identity")
                files.append({"id": r["result"]["id"], "filename": target["filename"],
                              "bytes": target["bytes"], "sha256": target["sha256"]})
        if len({f["id"] for f in files}) != len(files):
            raise ValueError("Receipts must reference distinct Canvas files")
        return files

    def _conversation_uploaded(self, canvas: Canvas, owner: str, p: dict) -> list[dict]:
        ids = p.get("upload_action_ids", [])
        if len(ids) > 20 or len(set(ids)) != len(ids):
            raise ValueError("Choose at most 20 distinct conversation upload receipts")
        files = []
        keys = ("id", "actor_id", "folder_id", "filename", "stored_filename", "bytes", "sha256", "destination")
        with self._atomic():
            for action_id in ids:
                record = self._get(canvas, owner, action_id)
                if record["kind"] != "conversation_attachment_upload" or record["state"] != "succeeded":
                    raise ValueError("Each Inbox file needs a verified conversation attachment upload receipt")
                review = record["review"]
                target, result = review.get("target", {}), record.get("result") or {}
                attachment = target.get("attachment", {})
                if (result.get("verified") is not True or result.get("message_sent") is not False
                        or result.get("destination") != "conversation_attachment"
                        or result.get("actor_id") != target.get("actor_id")
                        or result.get("bytes") != attachment.get("bytes")
                        or result.get("sha256") != attachment.get("sha256")
                        or result.get("requested_filename") != review.get("filename")
                        or target.get("folder_id") is not None and result.get("folder_id") != target["folder_id"]
                        or any(key not in result for key in keys)):
                    raise ValueError("Conversation upload receipt does not match the reviewed attachment")
                files.append({key: result[key] for key in keys})
        if len({file["id"] for file in files}) != len(files):
            raise ValueError("Conversation receipts must reference distinct files")
        return files

    def _existing_preparation(self, canvas: Canvas, owner: str, identity: str,
                              fingerprint: str, intent_id: str | None) -> tuple[dict | None, dict | None]:
        if self._revoked(owner):
            raise PermissionError("This Canvas connection was revoked")
        if intent_id is not None:
            existing = self._intent_record(identity, intent_id)
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise ValueError("intent_id was already used for different content or a different target")
                # Explicit intent keys never silently reset after cancellation or
                # rejection; a new user intent needs a new key.
                return self._get(canvas, owner, existing["id"]), None
        prior = self._fingerprint_record(fingerprint)
        if prior:
            prior = self._get(canvas, owner, prior["id"])
            if intent_id is None and prior["state"] not in {"cancelled", "expired", "rejected"}:
                return prior, None
            if intent_id is not None and prior["state"] in {"prepared", "executing"}:
                raise ValueError("An identical action is still prepared or executing; finish or cancel that action before creating a new intent")
        return None, prior

    def prepare(self, canvas: Canvas, owner: str, action: ActionRequest | dict,
                *, intent_id: str | None = None) -> dict:
        """Stage immutable content. intent_id is a user-authorized intent key.

        Default calls deduplicate identical content. Reuse the same explicit key
        for retries; use a new key only for a fresh, separately authorized action.
        A key cannot be rebound to different content during receipt retention.
        """
        if intent_id is not None:
            import re
            if not isinstance(intent_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", intent_id):
                raise ValueError("intent_id must be 1..128 letters, digits, dots, underscores, colons or hyphens")
        parsed = ACTION_ADAPTER.validate_python(action)
        p = parsed.model_dump(mode="json")
        # Preserve the original text-only comment fingerprint across upgrades.
        # Adding an optional attachment field must not turn a retry into a new
        # comment or invalidate an already-prepared text-only snapshot.
        if p["kind"] == "submission_comment" and not p.get("upload_action_ids"):
            p.pop("upload_action_ids", None)
        # Preserve fingerprints of uploads prepared before folder targeting existed.
        if p["kind"] == "upload" and p.get("parent_folder_id") is None:
            p.pop("parent_folder_id", None)
        encoded = json.dumps(p, sort_keys=True, separators=(",", ":"))
        identity = self._identity(canvas, owner)
        fingerprint = hashlib.sha256((identity + "\0" + encoded).encode()).hexdigest()
        with self._atomic():
            existing, _ = self._existing_preparation(canvas, owner, identity, fingerprint, intent_id)
            if existing:
                return self.public(existing)
        snapshot = self._snapshot(canvas, owner, p)
        review = {k: v for k, v in p.items() if k != "content_base64"}
        review.update({"canvas_origin": canvas.config.base_url, "target": snapshot})
        if intent_id is not None:
            review["intent"] = {"intent_id": intent_id, "requires_fresh_user_authorization": True,
                "warning": "This key represents a distinct user-authorized action, not an automatic retry. If an earlier receipt is unknown or uncertain, inspect Canvas first and explicitly authorize the risk of repeating it."}
        if p["kind"] == "upload":
            raw = base64.b64decode(p["content_base64"], validate=True)
            effect = ("Upload attachment only; does not send a submission comment or submit coursework"
                      if p["destination"] == "submission_comment" else "Upload file only; does not submit coursework")
            review.update({"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "effect": effect})
        with self._atomic():
            existing, prior = self._existing_preparation(canvas, owner, identity, fingerprint, intent_id)
            if existing:
                return self.public(existing)
            self._preparation_capacity(owner, encoded)
            if intent_id is not None and prior and prior["state"] in {"succeeded", "uncertain"}:
                review["repeat_intent"] = {"intent_id": intent_id, "previous_action_id": prior["id"],
                    "previous_state": prior["state"], "requires_fresh_user_authorization": True,
                    "warning": "This repeats identical content. The earlier action may already have reached Canvas. Inspect Canvas and obtain fresh user authorization that explicitly covers another submission/message/upload before executing."}
            action_id = uuid.uuid4().hex
            now = time.time()
            r = {"id": action_id, "owner": identity, "fingerprint": fingerprint, "intent_id": intent_id,
                 "payload": encoded, "payload_bytes": len(encoded), "kind": p["kind"], "state": "prepared",
                 "review": review, "snapshot": snapshot, "created_at": now,
                 "expires": now + self.ttl, "retain_until": now + self.retention,
                 "tenant": owner, "persistent": self.persistence is not None}
            self._save(r)
            if self.persistence is not None:
                self.persistence.put("canvas_action_index", fingerprint, {"action_id": action_id},
                                     expires=r["retain_until"], owner=owner)
                if intent_id is not None:
                    intent_key = hashlib.sha256((identity + "\0" + intent_id).encode()).hexdigest()
                    self.persistence.put("canvas_action_intent", intent_key, {"action_id": action_id},
                                         expires=r["retain_until"], owner=owner)
                self.persistence.put("canvas_action_claim", action_id, {"owner": identity},
                                     expires=r["retain_until"], owner=owner)
                self.persistence.put("canvas_action_pending", action_id, {"bytes": len(encoded)},
                                     expires=r["expires"], owner=owner)
            return self.public(r)

    def _pending_lease(self, record: dict) -> None:
        if self.persistence is not None:
            self.persistence.put("canvas_action_pending", record["id"], {"bytes": record["payload_bytes"]},
                                 expires=record["lease_expires"], owner=record["tenant"])

    def _renew_execution(self, canvas: Canvas, owner: str, action_id: str, execution_id: str) -> bool:
        with self._atomic():
            try:
                r = self._get(canvas, owner, action_id)
            except ValueError:
                return False
            if r["state"] != "executing" or r.get("execution_id") != execution_id:
                return False
            now = time.time()
            if now >= r["execution_deadline"]:
                r["state"], r["payload"] = "uncertain", ""
                r["result"] = {"verified": False, "upstream_write_attempted": r.get("phase") == "dispatching",
                               "message": "Execution exceeded its deadline. Inspect Canvas before another action."}
                self._save(r)
                return False
            r["lease_expires"] = min(now + self.execution_lease, r["execution_deadline"])
            self._save(r)
            self._pending_lease(r)
            return True

    @contextmanager
    def _heartbeat(self, canvas: Canvas, owner: str, action_id: str, execution_id: str):
        stopped = threading.Event()
        def run():
            while not stopped.wait(self.heartbeat_interval):
                try:
                    if not self._renew_execution(canvas, owner, action_id, execution_id):
                        return
                except Exception:
                    # A storage failure must never make an expired lease reusable.
                    # The current executor is fenced again immediately before dispatch.
                    return
        heartbeat = threading.Thread(target=run, name="canvas-action-lease", daemon=True)
        heartbeat.start()
        try:
            yield
        finally:
            stopped.set()
            heartbeat.join(timeout=min(5.0, self.execution_lease))

    def _missing_completion(self, original: dict) -> dict:
        r = dict(original)
        r["state"], r["payload"] = ("revoked" if self._revoked(r["tenant"]) else "unknown"), ""
        r["result"] = {"verified": False, "upstream_write_attempted": original.get("phase") == "dispatching",
                       "message": "The action record or connection is no longer available. No receipt was recreated; inspect Canvas."}
        return self.public(r)

    def _finish(self, canvas: Canvas, owner: str, original: dict, state: str, result: dict) -> dict:
        """Conditional terminal transition: never resurrect a removed/revoked row."""
        with self._atomic():
            try:
                current = self._get(canvas, owner, original["id"])
            except ValueError:
                return self._missing_completion(original)
            if current["state"] != "executing" or current.get("execution_id") != original["execution_id"]:
                return self.public(current)
            current["state"], current["result"], current["payload"] = state, result, ""
            self._save(current)
            return self.public(current)

    def execute(self, canvas: Canvas, owner: str, action_id: str, authorize_write: Callable[[], None]) -> dict:
        authorize_write()
        with self._atomic():
            r = self._get(canvas, owner, action_id)
            if r["state"] != "prepared":
                return self.public(r)
            # The prepared->executing transition and claim deletion are one
            # transaction. Cancellation cannot win after this durable claim.
            p = json.loads(r["payload"])
            now = time.time()
            r.update(state="executing", phase="preflight", execution_id=uuid.uuid4().hex,
                     execution_deadline=now + self.max_execution_seconds,
                     lease_expires=now + self.execution_lease)
            if self.persistence is not None:
                self.persistence.delete("canvas_action_claim", action_id)
            self._save(r)
            self._pending_lease(r)
        with self._heartbeat(canvas, owner, action_id, r["execution_id"]):
            try:
                snapshot = self._snapshot(canvas, owner, p)
                if snapshot != r["snapshot"]:
                    raise ValueError("Target changed after preparation; prepare a fresh review")
                if p["kind"] == "submit_files" or (p["kind"] == "submission_comment" and snapshot.get("files")):
                    self._verify_files(canvas, snapshot["files"])
                authorize_write()
            except Exception:
                return self._finish(canvas, owner, r, "rejected", {"verified": False, "upstream_write_attempted": False,
                    "message": "Preflight failed or target changed. Re-read the target before preparing again."})
            with self._atomic():
                try:
                    current = self._get(canvas, owner, action_id)
                except ValueError:
                    return self._missing_completion(r)
                if current["state"] != "executing" or current.get("execution_id") != r["execution_id"]:
                    return self.public(current)
                if time.time() >= current["expires"]:
                    return self._finish(canvas, owner, r, "rejected", {"verified": False, "upstream_write_attempted": False,
                        "message": "Prepared action expired before dispatch; prepare again."})
                current["phase"] = r["phase"] = "dispatching"
                current["dispatch_started_at"] = time.time()
                self._save(current)
            try:
                result = self._perform(canvas, p, snapshot)
                state = "succeeded"
            except Exception:
                result, state = {"verified": False, "upstream_write_attempted": True,
                    "message": "Outcome uncertain. Inspect Canvas; do not automatically retry."}, "uncertain"
            receipt = self._finish(canvas, owner, r, state, result)
            canvas.navigation.clear_cache()
            return receipt

    @staticmethod
    def _verify_files(canvas: Canvas, files: list[dict]) -> None:
        with tempfile.TemporaryDirectory(prefix="canvas-verify-") as directory:
            for index, expected in enumerate(files):
                f = canvas.files.get(expected["id"])
                if f.id != expected["id"]:
                    raise ValueError("Canvas returned a different uploaded file")
                if f.size != expected["bytes"]:
                    raise ValueError("Uploaded file size changed")
                path = Path(directory) / str(index)
                canvas.files.download(f, path, max_bytes=expected["bytes"])
                if hashlib.sha256(path.read_bytes()).hexdigest() != expected["sha256"]:
                    raise ValueError("Uploaded file content changed")

    def _perform(self, canvas: Canvas, p: dict, target: dict) -> dict:
        kind = p["kind"]
        for module in (personal_files, preferences, group_content, enrollments, bookmarks, what_if_grades, mastery_paths, group_memberships, discussion_attachments, feedback, appointments, self_profile, dashboard_cards, inbox_state, inbox_messages, inbox_attachments):
            if kind in module.KINDS:
                return module.perform(canvas, p, target)
        if kind in module_progress.KINDS:
            return module_progress.perform(canvas, p, target)
        if kind in student_actions.KINDS:
            return student_actions.perform(canvas, p, target)
        if kind == "upload":
            raw = base64.b64decode(p["content_base64"], validate=True)
            if p["destination"] == "user":
                context = UploadContext.user()
            elif p["destination"] == "submission_comment":
                context = UploadContext.submission_comment(p["course_id"], p["assignment_id"])
            else:
                context = UploadContext.assignment_submission(p["course_id"], p["assignment_id"])
            with tempfile.TemporaryDirectory(prefix="canvas-upload-") as directory:
                path = Path(directory) / p["filename"]
                path.write_bytes(raw)
                options = {"parent_folder_id": p["parent_folder_id"]} if p.get("parent_folder_id") is not None else {}
                uploaded = dump(canvas.files.upload(path, context=context, name=p["filename"], on_duplicate="rename", **options,
                           expected_size=len(raw), expected_sha256=hashlib.sha256(raw).hexdigest(), verify=True))
            if not uploaded.get("id"):
                raise ValueError("Canvas did not return a file identity")
            self._verify_files(canvas, [{"id": uploaded["id"], "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}])
            if p.get("parent_folder_id") is not None:
                fresh = dump(canvas.files.get(uploaded["id"]))
                if (uploaded.get("folder_id") != p["parent_folder_id"] or
                        fresh.get("id") != uploaded["id"] or fresh.get("folder_id") != p["parent_folder_id"]):
                    raise ValueError("Upload destination folder was not confirmed")
            result = {"id": uploaded["id"], "filename": uploaded.get("display_name"), "size": len(raw), "verified": True, "submitted": False}
            if p.get("parent_folder_id") is not None:
                result["folder_id"] = p["parent_folder_id"]
            if p["destination"] == "submission_comment":
                result["comment_sent"] = False
            return result
        if kind.startswith("submit_") or kind == "submission_comment":
            c, a = p["course_id"], p["assignment_id"]
            if kind == "submit_text":
                posted = dump(canvas.submissions.submit_text(c, a, p["body"]))
            elif kind == "submit_url":
                posted = dump(canvas.submissions.submit_url(c, a, p["url"]))
            elif kind == "submit_files":
                posted = dump(canvas.send(SubmitFileUpload(c, a, [f["id"] for f in target["files"]])))
            else:
                posted = dump(canvas.submissions.add_comment(c, a, p["text"],
                              attachment_file_ids=[f["id"] for f in target.get("files", [])]))
            result = dump(canvas.submissions.get(c, a))
            if result.get("assignment_id") != a or result.get("user_id") != target.get("user_id"):
                raise ValueError("Submission identity changed")
            if kind == "submission_comment":
                comments = result.get("submission_comments") or []
                expected_files = sorted(f["id"] for f in target.get("files", []))
                def matches(comment: dict) -> bool:
                    return (type(comment.get("id")) is int and comment["id"] > 0 and
                            comment["id"] not in target["comment_ids"] and
                            comment.get("author_id") == target["user_id"] and comment.get("comment") == p["text"] and
                            sorted(f.get("id") for f in comment.get("attachments") or []) == expected_files)
                returned_ids = {x["id"] for x in posted.get("submission_comments") or [] if matches(x)}
                confirmed = [x for x in comments if matches(x) and x["id"] in returned_ids]
                if len(confirmed) != 1:
                    raise ValueError("Exact new comment and attachments were not confirmed")
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
        if kind == "inbox_send":
            return _send_inbox(canvas, p)
        if kind == "inbox_reply":
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
