import base64
import copy
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from canvas_py._config import CanvasConfig
from canvas_py._errors import CanvasError

from canvas_connector.actions import ACTION_ADAPTER, ActionStore


def client():
    canvas = SimpleNamespace(config=CanvasConfig("https://school.example", "token-one"), navigation=Mock())
    canvas.assignments = Mock()
    canvas.submissions = Mock()
    canvas.discussions = Mock()
    canvas.inbox = Mock()
    canvas.planner = Mock()
    canvas.files = Mock()
    canvas.send = Mock()
    canvas.assignments.get.return_value = {"id": 2, "name": "Essay", "submission_types": ["online_text_entry", "online_url", "online_upload"], "can_submit": True}
    canvas.submissions.get.return_value = {"assignment_id": 2, "user_id": 7, "attempt": None,
        "submitted_at": None, "workflow_state": "unsubmitted", "submission_comments": []}
    return canvas


TEXT = {"kind": "submit_text", "course_id": 1, "assignment_id": 2, "body": "My essay."}


def test_prepare_is_immutable_does_not_write_and_is_deduplicated():
    c, store = client(), ActionStore()
    payload = dict(TEXT)
    receipt = store.prepare(c, "alice", payload)
    payload["body"] = "changed"
    assert receipt["review"]["body"] == "My essay."
    receipt["review"]["body"] = "changed public copy"
    assert store.status(c, "alice", receipt["action_id"])["review"]["body"] == "My essay."
    assert store.prepare(c, "alice", TEXT)["action_id"] == receipt["action_id"]
    c.submissions.submit_text.assert_not_called()
    c.send.assert_not_called()


def test_owner_and_credentials_isolate_receipts():
    c, store = client(), ActionStore()
    r = store.prepare(c, "alice", TEXT)
    with pytest.raises(ValueError, match="not found"):
        store.status(c, "bob", r["action_id"])
    c.config = CanvasConfig("https://school.example", "different-token")
    with pytest.raises(ValueError, match="not found"):
        store.status(c, "alice", r["action_id"])


def test_oauth_refresh_preserves_trusted_grant_but_not_other_grants():
    c, store = client(), ActionStore(bind_credentials=False)
    r = store.prepare(c, "grant-alice", TEXT)
    c.config = CanvasConfig("https://school.example", "refreshed-token")
    assert store.status(c, "grant-alice", r["action_id"])["state"] == "prepared"
    with pytest.raises(ValueError):
        store.status(c, "grant-bob", r["action_id"])
    c.config = CanvasConfig("https://other.example", "refreshed-token")
    with pytest.raises(ValueError):
        store.status(c, "grant-alice", r["action_id"])


def test_expiry_cancels_and_does_not_dispatch():
    c, store = client(), ActionStore()
    r = store.prepare(c, "a", TEXT)
    store.records[r["action_id"]]["expires"] = 0
    assert store.execute(c, "a", r["action_id"], lambda: None)["state"] == "expired"
    c.submissions.submit_text.assert_not_called()
    assert store.prepare(c, "a", TEXT)["action_id"] != r["action_id"]


def test_write_scope_denial_cannot_dispatch():
    c, store = client(), ActionStore()
    r = store.prepare(c, "a", TEXT)
    def denied():
        raise PermissionError("read-only grant")
    with pytest.raises(PermissionError):
        store.execute(c, "a", r["action_id"], denied)
    c.submissions.submit_text.assert_not_called()
    assert store.status(c, "a", r["action_id"])["state"] == "prepared"


def test_changed_assignment_rejects_before_dispatch():
    c, store = client(), ActionStore()
    r = store.prepare(c, "a", TEXT)
    c.assignments.get.return_value["name"] = "Changed assignment"
    result = store.execute(c, "a", r["action_id"], lambda: None)
    assert result["state"] == "rejected"
    assert result["result"]["upstream_write_attempted"] is False
    c.submissions.submit_text.assert_not_called()


@pytest.mark.parametrize("kind,field,value", [("submit_text", "body", "My essay."), ("submit_url", "url", "https://example.org/essay")])
def test_submission_requires_exact_new_attempt_and_readback(kind, field, value):
    c, store = client(), ActionStore()
    p = {"kind": kind, "course_id": 1, "assignment_id": 2, field: value}
    r = store.prepare(c, "a", p)
    before = copy.deepcopy(c.submissions.get.return_value)
    after = {**before, "attempt": 1, "submitted_at": "2026-11-02T01:00:00Z", "workflow_state": "submitted",
             "submission_type": "online_text_entry" if kind == "submit_text" else "online_url", field: value}
    c.submissions.get.side_effect = [before, after]
    getattr(c.submissions, kind).return_value = after
    result = store.execute(c, "a", r["action_id"], lambda: None)
    assert result["state"] == "succeeded"
    assert result["result"]["verified"]
    assert store.execute(c, "a", r["action_id"], lambda: None)["state"] == "succeeded"
    getattr(c.submissions, kind).assert_called_once()


def test_unconfirmed_write_is_uncertain_and_never_replayed():
    c, store = client(), ActionStore()
    r = store.prepare(c, "a", TEXT)
    c.submissions.submit_text.side_effect = TimeoutError("possibly sent")
    assert store.execute(c, "a", r["action_id"], lambda: None)["state"] == "uncertain"
    assert store.execute(c, "a", r["action_id"], lambda: None)["state"] == "uncertain"
    assert store.prepare(c, "a", TEXT)["state"] == "uncertain"
    c.submissions.submit_text.assert_called_once()


def test_parallel_execute_claims_once():
    c, store = client(), ActionStore()
    r = store.prepare(c, "a", TEXT)
    started, finish = threading.Event(), threading.Event()
    def slow(*args):
        started.set()
        finish.wait(3)
        raise TimeoutError()
    c.submissions.submit_text.side_effect = slow
    t = threading.Thread(target=lambda: store.execute(c, "a", r["action_id"], lambda: None))
    t.start()
    assert started.wait(2)
    assert store.execute(c, "a", r["action_id"], lambda: None)["state"] == "executing"
    finish.set()
    t.join(3)
    c.submissions.submit_text.assert_called_once()


def test_comment_verified_by_new_comment_id():
    c, store = client(), ActionStore()
    r = store.prepare(c, "a", {"kind": "submission_comment", "course_id": 1, "assignment_id": 2, "text": "Thank you"})
    before = copy.deepcopy(c.submissions.get.return_value)
    after = {**before, "submission_comments": [{"id": 90, "author_id": 7, "comment": "Thank you"}]}
    c.submissions.get.side_effect = [before, after]
    c.submissions.add_comment.return_value = after
    assert store.execute(c, "a", r["action_id"], lambda: None)["state"] == "succeeded"


@pytest.mark.parametrize("kind", ["discussion_post", "discussion_reply"])
def test_discussion_readback_uses_returned_entry_id(kind):
    c, store = client(), ActionStore()
    c.discussions.get.return_value = {"id": 4, "title": "Discuss", "locked_for_user": False}
    p = {"kind": kind, "course_id": 1, "topic_id": 4, "message": "Hello"}
    if kind == "discussion_reply":
        p["entry_id"] = 5
    r = store.prepare(c, "a", p)
    c.discussions.post.return_value = c.discussions.reply.return_value = {"id": 6, "message": "<p>Hello</p>"}
    c.send.return_value = [{"id": 6, "message": "<p>Hello</p>"}]
    assert store.execute(c, "a", r["action_id"], lambda: None)["state"] == "succeeded"
    request = c.send.call_args.args[0]
    assert request.path.endswith("/entry_list")
    assert request.query_pairs() == [("ids[]", 6)]


@pytest.mark.parametrize("kind", ["inbox_send", "inbox_reply"])
def test_inbox_messages_verified_without_mark_read(kind):
    c, store = client(), ActionStore()
    existing = {"id": 4, "subject": "Hello", "audience": [8], "participants": [{"id": 8}], "workflow_state": "unread"}
    c.inbox.get.return_value = existing
    p = {"kind": kind, "body": "Message"}
    p.update({"recipients": [8], "subject": "Hello"} if kind == "inbox_send" else {"conversation_id": 4})
    r = store.prepare(c, "a", p)
    after = {**existing, "messages": [{"id": 100, "body": "Message"}]}
    c.send.return_value = [after] if kind == "inbox_send" else after
    c.inbox.get.side_effect = [after] if kind == "inbox_send" else [existing, after]
    assert store.execute(c, "a", r["action_id"], lambda: None)["state"] == "succeeded"
    assert all(x.kwargs["auto_mark_as_read"] is False for x in c.inbox.get.call_args_list)


def test_upload_has_no_host_path_and_does_not_count_as_submission(tmp_path):
    c, store = client(), ActionStore()
    data = b"Hello document"
    r = store.prepare(c, "a", {"kind": "upload", "filename": "paper.txt", "content_base64": base64.b64encode(data).decode()})
    assert "content_base64" not in r["review"]
    c.files.upload.return_value = {"id": 21, "display_name": "paper.txt"}
    c.files.get.return_value = SimpleNamespace(id=21, size=len(data))
    c.files.download.side_effect = lambda file, path, **kwargs: path.write_bytes(data)
    result = store.execute(c, "a", r["action_id"], lambda: None)
    assert result["state"] == "succeeded"
    assert result["result"]["submitted"] is False
    c.submissions.submit_text.assert_not_called()


def test_file_submission_rejects_other_assignment_receipt():
    c, store = client(), ActionStore()
    r = store.prepare(c, "a", {"kind": "upload", "filename": "paper.txt", "content_base64": "aGk="})
    store.records[r["action_id"]].update(state="succeeded", result={"id": 4})
    with pytest.raises(ValueError, match="another assignment"):
        store.prepare(c, "a", {"kind": "submit_files", "course_id": 1, "assignment_id": 2, "upload_action_ids": [r["action_id"]]})


@pytest.mark.parametrize("change", [{"course_id": True}, {"assignment_id": -1}, {"arbitrary_path": "users/self"}])
def test_action_validation_rejects_bad_ids_and_extra_fields(change):
    with pytest.raises(ValueError):
        ACTION_ADAPTER.validate_python({**TEXT, **change})


def test_planner_override_uses_documented_lowercase_type_and_verifies():
    c, store = client(), ActionStore()
    r = store.prepare(c, "a", {"kind": "planner_override", "plannable_type": "assignment", "plannable_id": 2, "marked_complete": True})
    c.planner.create_override.return_value = {"id": 5}
    c.send.return_value = {"id": 5, "marked_complete": True, "dismissed": False}
    assert store.execute(c, "a", r["action_id"], lambda: None)["state"] == "succeeded"
    c.planner.create_override.assert_called_once_with("assignment", 2, marked_complete=True, dismissed=False)


def persistent(tmp_path, **kwargs):
    from cryptography.fernet import Fernet
    from canvas_connector.storage import Store
    db = Store(tmp_path / "actions.db", Fernet.generate_key())
    return db, ActionStore(persistence=db, bind_credentials=False, **kwargs)


def test_persistent_action_survives_restart_and_token_refresh(tmp_path):
    c = client()
    db, store = persistent(tmp_path)
    receipt = store.prepare(c, "grant", TEXT)
    assert b"My essay" not in (tmp_path / "actions.db").read_bytes()
    restarted = ActionStore(persistence=db, bind_credentials=False)
    c.config = CanvasConfig("https://school.example", "new-oauth-token")
    assert restarted.status(c, "grant", receipt["action_id"])["state"] == "prepared"
    assert restarted.prepare(c, "grant", TEXT)["action_id"] == receipt["action_id"]
    c.submissions.submit_text.side_effect = TimeoutError()
    assert restarted.execute(c, "grant", receipt["action_id"], lambda: None)["state"] == "uncertain"
    again = ActionStore(persistence=db, bind_credentials=False)
    assert again.execute(c, "grant", receipt["action_id"], lambda: None)["state"] == "uncertain"
    c.submissions.submit_text.assert_called_once()
    assert db.get("canvas_action_payload", receipt["action_id"]) is None


def test_consumed_persistent_claim_with_prepared_record_cannot_replay(tmp_path):
    c = client()
    db, store = persistent(tmp_path)
    r = store.prepare(c, "a", TEXT)
    db.consume("canvas_action_claim", r["action_id"])
    restarted = ActionStore(persistence=db, bind_credentials=False)
    assert restarted.status(c, "a", r["action_id"])["state"] == "uncertain"
    assert restarted.execute(c, "a", r["action_id"], lambda: None)["state"] == "uncertain"
    c.submissions.submit_text.assert_not_called()


def test_interrupted_persistent_execution_becomes_uncertain(tmp_path):
    c = client()
    db, store = persistent(tmp_path)
    r = store.prepare(c, "a", TEXT)
    saved = db.get("canvas_action", r["action_id"])
    saved["state"] = "executing"
    db.put("canvas_action", r["action_id"], saved, expires=saved["retain_until"], owner="a")
    restarted = ActionStore(persistence=db, bind_credentials=False)
    assert restarted.status(c, "a", r["action_id"])["state"] == "uncertain"


def test_pending_limits_are_per_tenant_and_cancellation_releases_capacity(tmp_path):
    c = client()
    db, store = persistent(tmp_path, per_owner_capacity=1)
    first = store.prepare(c, "a", TEXT)
    with pytest.raises(ValueError, match="Too many"):
        store.prepare(c, "a", {**TEXT, "body": "Second"})
    assert store.prepare(c, "b", TEXT)["state"] == "prepared"
    store.cancel(c, "a", first["action_id"])
    assert store.prepare(c, "a", {**TEXT, "body": "Second"})["state"] == "prepared"


def test_revocation_removes_all_persistent_action_material(tmp_path):
    c = client()
    db, store = persistent(tmp_path)
    r = store.prepare(c, "grant", TEXT)
    db.revoke_owner("grant")
    with pytest.raises(ValueError, match="not found"):
        store.status(c, "grant", r["action_id"])


def test_completed_persistent_receipt_survives_restart_without_replay(tmp_path):
    c = client()
    db, store = persistent(tmp_path)
    r = store.prepare(c, "grant", TEXT)
    before = copy.deepcopy(c.submissions.get.return_value)
    after = {**before, "body": TEXT["body"], "attempt": 1, "submitted_at": "2026-11-02T01:00:00Z",
             "workflow_state": "submitted", "submission_type": "online_text_entry"}
    c.submissions.get.side_effect = [before, after]
    c.submissions.submit_text.return_value = after
    assert store.execute(c, "grant", r["action_id"], lambda: None)["state"] == "succeeded"
    restarted = ActionStore(persistence=db, bind_credentials=False)
    receipt = restarted.execute(c, "grant", r["action_id"], lambda: None)
    assert receipt["state"] == "succeeded" and receipt["result"]["verified"]
    c.submissions.submit_text.assert_called_once()


def test_two_store_instances_cannot_dispatch_same_claim_twice(tmp_path):
    c = client()
    db, one = persistent(tmp_path)
    two = ActionStore(persistence=db, bind_credentials=False)
    r = one.prepare(c, "grant", TEXT)
    started, finish = threading.Event(), threading.Event()
    def blocked(*args):
        started.set()
        finish.wait(3)
        raise TimeoutError()
    c.submissions.submit_text.side_effect = blocked
    t = threading.Thread(target=lambda: one.execute(c, "grant", r["action_id"], lambda: None))
    t.start()
    assert started.wait(2)
    assert two.execute(c, "grant", r["action_id"], lambda: None)["state"] == "executing"
    finish.set()
    t.join(3)
    c.submissions.submit_text.assert_called_once()


def test_verified_file_upload_then_final_submission_are_separate():
    c, store = client(), ActionStore()
    raw = b"My paper"
    upload = store.prepare(c, "a", {"kind": "upload", "filename": "paper.txt", "content_base64": base64.b64encode(raw).decode(),
        "destination": "assignment", "course_id": 1, "assignment_id": 2})
    c.files.upload.return_value = {"id": 55, "display_name": "paper.txt"}
    c.files.get.return_value = SimpleNamespace(id=55, size=len(raw))
    c.files.download.side_effect = lambda file, path, **kwargs: path.write_bytes(raw)
    assert store.execute(c, "a", upload["action_id"], lambda: None)["result"]["submitted"] is False
    c.send.assert_not_called()
    submit = store.prepare(c, "a", {"kind": "submit_files", "course_id": 1, "assignment_id": 2,
                                   "upload_action_ids": [upload["action_id"]]})
    before = copy.deepcopy(c.submissions.get.return_value)
    after = {**before, "attempt": 1, "submitted_at": "2026-11-02T01:00:00Z", "workflow_state": "submitted",
             "submission_type": "online_upload", "attachments": [{"id": 55}]}
    c.submissions.get.side_effect = [before, after]
    c.send.return_value = after
    assert store.execute(c, "a", submit["action_id"], lambda: None)["state"] == "succeeded"
    request = c.send.call_args.args[0]
    assert request.json_body() == {"submission": {"submission_type": "online_upload", "file_ids": [55]}}


@pytest.mark.parametrize("kind", ["planner_create", "planner_update", "planner_delete", "inbox_update"])
def test_remaining_action_kinds_confirm_readback(kind):
    c, store = client(), ActionStore()
    note = {"id": 1, "title": "Study", "details": "Review", "todo_date": "2026-11-03T17:00:00Z", "course_id": None}
    c.send.return_value = note
    if kind == "inbox_update":
        c.inbox.get.return_value = {"id": 4, "workflow_state": "unread"}
        payload = {"kind": kind, "conversation_id": 4, "workflow_state": "archived"}
    elif kind == "planner_delete":
        payload = {"kind": kind, "note_id": 1}
    else:
        payload = {"kind": kind, **{k: v for k, v in note.items() if k != "id"}}
        if kind == "planner_update":
            payload["note_id"] = 1
    r = store.prepare(c, "a", payload)
    if kind == "planner_delete":
        c.send.side_effect = [note, CanvasError("gone", status=404)]
    elif kind == "planner_create":
        c.planner.create_note.return_value = note
    elif kind == "planner_update":
        c.planner.update_note.return_value = note
    else:
        c.inbox.get.side_effect = [{"id": 4, "workflow_state": "unread"}, {"id": 4, "workflow_state": "archived"}]
    assert store.execute(c, "a", r["action_id"], lambda: None)["state"] == "succeeded"


def test_fresh_intent_allows_repeat_but_same_intent_remains_idempotent(tmp_path):
    c = client()
    db, store = persistent(tmp_path)
    first = store.prepare(c, "grant", TEXT)
    c.submissions.submit_text.side_effect = TimeoutError()
    assert store.execute(c, "grant", first["action_id"], lambda: None)["state"] == "uncertain"
    assert store.prepare(c, "grant", TEXT)["action_id"] == first["action_id"]
    repeated = store.prepare(c, "grant", TEXT, intent_id="after-canvas-inspection-1")
    assert repeated["action_id"] != first["action_id"]
    assert repeated["review"]["repeat_intent"]["previous_state"] == "uncertain"
    assert repeated["review"]["repeat_intent"]["requires_fresh_user_authorization"] is True
    assert "already have reached Canvas" in repeated["review"]["repeat_intent"]["warning"]
    restarted = ActionStore(persistence=db, bind_credentials=False)
    assert restarted.prepare(c, "grant", TEXT, intent_id="after-canvas-inspection-1")["action_id"] == repeated["action_id"]
    assert restarted.prepare(c, "grant", TEXT)["action_id"] == repeated["action_id"]
    c.submissions.submit_text.assert_called_once()


def test_intent_key_cannot_be_rebound_or_silently_reset(tmp_path):
    c = client()
    db, store = persistent(tmp_path)
    first = store.prepare(c, "grant", TEXT, intent_id="student-intent-1")
    with pytest.raises(ValueError, match="different content"):
        store.prepare(c, "grant", {**TEXT, "body": "Another essay"}, intent_id="student-intent-1")
    with pytest.raises(ValueError, match="still prepared"):
        store.prepare(c, "grant", TEXT, intent_id="student-intent-2")
    store.cancel(c, "grant", first["action_id"])
    assert store.prepare(c, "grant", TEXT, intent_id="student-intent-1")["state"] == "cancelled"
    assert store.prepare(c, "grant", TEXT, intent_id="student-intent-2")["state"] == "prepared"
    assert store.prepare(c, "different-grant", TEXT, intent_id="student-intent-1")["state"] == "prepared"


@pytest.mark.parametrize("intent", ["", "x" * 129, "new intent", "new\nintent", True])
def test_intent_id_validation(intent):
    with pytest.raises(ValueError, match="intent_id"):
        ActionStore().prepare(client(), "grant", TEXT, intent_id=intent)


def test_other_worker_observes_renewed_live_execution_lease(tmp_path):
    c = client()
    db, store = persistent(tmp_path, execution_lease=1, heartbeat_interval=0.1, max_execution_seconds=10)
    other = ActionStore(persistence=db, bind_credentials=False)
    receipt = store.prepare(c, "grant", TEXT)
    started, finish = threading.Event(), threading.Event()
    results = []
    def perform(*args):
        started.set()
        assert finish.wait(5)
        return {"verified": True}
    store._perform = perform
    worker = threading.Thread(target=lambda: results.append(store.execute(c, "grant", receipt["action_id"], lambda: None)))
    worker.start()
    assert started.wait(2)
    try:
        time.sleep(1.3)
        assert other.status(c, "grant", receipt["action_id"])["state"] == "executing"
        with pytest.raises(ValueError, match="already executing"):
            other.cancel(c, "grant", receipt["action_id"])
    finally:
        finish.set()
        worker.join(5)
    assert results[0]["state"] == "succeeded"


def test_expired_execution_lease_is_never_reclaimed_or_overwritten_by_late_success(tmp_path):
    c = client()
    db, store = persistent(tmp_path)
    other = ActionStore(persistence=db, bind_credentials=False)
    receipt = store.prepare(c, "grant", TEXT)
    started, finish = threading.Event(), threading.Event()
    results = []
    def perform(*args):
        started.set()
        assert finish.wait(5)
        return {"verified": True}
    store._perform = perform
    worker = threading.Thread(target=lambda: results.append(store.execute(c, "grant", receipt["action_id"], lambda: None)))
    worker.start()
    assert started.wait(2)
    db.update("canvas_action", receipt["action_id"], lambda r: {**r, "lease_expires": 0})
    assert other.status(c, "grant", receipt["action_id"])["state"] == "uncertain"
    assert other.execute(c, "grant", receipt["action_id"], lambda: None)["state"] == "uncertain"
    finish.set()
    worker.join(5)
    assert results[0]["state"] == "uncertain"
    assert db.get("canvas_action", receipt["action_id"])["state"] == "uncertain"


def test_revocation_during_dispatch_cannot_resurrect_receipt(tmp_path):
    c = client()
    db, store = persistent(tmp_path)
    receipt = store.prepare(c, "grant", TEXT)
    started, finish = threading.Event(), threading.Event()
    results = []
    def perform(*args):
        started.set()
        assert finish.wait(5)
        return {"verified": True}
    store._perform = perform
    worker = threading.Thread(target=lambda: results.append(store.execute(c, "grant", receipt["action_id"], lambda: None)))
    worker.start()
    assert started.wait(2)
    db.revoke_owner("grant")
    finish.set()
    worker.join(5)
    assert results[0]["state"] == "revoked"
    assert db.get("canvas_action", receipt["action_id"]) is None
    assert not db.scan("canvas_action_payload", owner="grant")
    assert not db.scan("canvas_action_pending", owner="grant")


def test_revocation_during_prepare_prevents_late_insertion(tmp_path):
    c = client()
    db, store = persistent(tmp_path)
    started, finish = threading.Event(), threading.Event()
    errors = []
    assignment = copy.deepcopy(c.assignments.get.return_value)
    def read(*args, **kwargs):
        started.set()
        assert finish.wait(5)
        return assignment
    c.assignments.get.side_effect = read
    def prepare():
        try:
            store.prepare(c, "grant", TEXT)
        except Exception as error:
            errors.append(error)
    worker = threading.Thread(target=prepare)
    worker.start()
    assert started.wait(2)
    db.revoke_owner("grant")
    finish.set()
    worker.join(5)
    assert len(errors) == 1 and isinstance(errors[0], PermissionError)
    assert not db.scan("canvas_action", owner="grant")


def test_lease_loss_during_preflight_fences_the_actual_write(tmp_path):
    c = client()
    db, store = persistent(tmp_path)
    receipt = store.prepare(c, "grant", TEXT)
    started, finish = threading.Event(), threading.Event()
    results = []
    assignment = copy.deepcopy(c.assignments.get.return_value)
    def preflight(*args, **kwargs):
        started.set()
        assert finish.wait(5)
        return assignment
    c.assignments.get.side_effect = preflight
    worker = threading.Thread(target=lambda: results.append(store.execute(c, "grant", receipt["action_id"], lambda: None)))
    worker.start()
    assert started.wait(2)
    db.update("canvas_action", receipt["action_id"], lambda r: {**r, "lease_expires": 0})
    finish.set()
    worker.join(5)
    assert results[0]["state"] == "uncertain"
    c.submissions.submit_text.assert_not_called()


def _process_action_job(job, db_path, key, barrier, output, action_id=None, counter=None, intent_id=None):
    """Independent-process contract test: fake dispatch increments a counter only."""
    from canvas_connector.storage import Store
    db = Store(db_path, key)
    store = ActionStore(persistence=db, bind_credentials=False)
    c = client()
    def perform(*args):
        with counter.get_lock():
            counter.value += 1
        time.sleep(0.15)
        return {"verified": True, "id": 9, "submitted": False}
    store._perform = perform
    try:
        barrier.wait(15)
        if job == "prepare":
            result = store.prepare(c, "grant", {"kind": "upload", "filename": "paper.txt", "content_base64": "aGk="}, intent_id=intent_id)
        elif job == "execute":
            result = store.execute(c, "grant", action_id, lambda: None)
        else:
            result = store.cancel(c, "grant", action_id)
        output.put(("ok", result["state"], result["action_id"]))
    except Exception as error:
        output.put(("error", type(error).__name__, str(error)))
    finally:
        db.close()


@pytest.mark.parametrize("intent_id", [None, "student-explicit-intent"])
def test_independent_process_preparations_share_one_id(tmp_path, intent_id):
    import multiprocessing
    from cryptography.fernet import Fernet
    from canvas_connector.storage import Store
    ctx = multiprocessing.get_context("spawn")
    path, key = str(tmp_path / "shared.db"), Fernet.generate_key()
    db = Store(path, key)
    barrier, output = ctx.Barrier(3), ctx.Queue()
    workers = [ctx.Process(target=_process_action_job, args=("prepare", path, key, barrier, output), kwargs={"intent_id": intent_id}) for _ in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait(15)
    results = [output.get(timeout=15) for _ in workers]
    for worker in workers:
        worker.join(15)
        assert worker.exitcode == 0
    assert all(r[0] == "ok" for r in results), results
    assert results[0][2] == results[1][2]
    assert len(db.scan("canvas_action", owner="grant")) == 1
    db.close()


@pytest.mark.parametrize("second_job", ["execute", "cancel"])
def test_independent_process_claim_and_cancel_are_atomic(tmp_path, second_job):
    import multiprocessing
    from cryptography.fernet import Fernet
    from canvas_connector.storage import Store
    ctx = multiprocessing.get_context("spawn")
    path, key = str(tmp_path / "shared.db"), Fernet.generate_key()
    db = Store(path, key)
    store = ActionStore(persistence=db, bind_credentials=False)
    receipt = store.prepare(client(), "grant", {"kind": "upload", "filename": "paper.txt", "content_base64": "aGk="})
    barrier, output, counter = ctx.Barrier(3), ctx.Queue(), ctx.Value("i", 0)
    workers = [ctx.Process(target=_process_action_job, args=(job, path, key, barrier, output, receipt["action_id"], counter))
               for job in ("execute", second_job)]
    for worker in workers:
        worker.start()
    barrier.wait(15)
    results = [output.get(timeout=15) for _ in workers]
    for worker in workers:
        worker.join(15)
        assert worker.exitcode == 0
    final = store.status(client(), "grant", receipt["action_id"])
    if final["state"] == "cancelled":
        assert second_job == "cancel" and counter.value == 0
    else:
        assert final["state"] == "succeeded" and counter.value == 1, (results, final)
    assert all(r[0] == "ok" or "already executing" in r[2] for r in results), results
    db.close()
