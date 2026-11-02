import base64
import copy
import threading
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
    after = {**before, "submission_comments": [{"id": 90, "comment": "Thank you"}]}
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
    c.files.get.return_value = SimpleNamespace(size=len(data))
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
    assert two.execute(c, "grant", r["action_id"], lambda: None)["state"] == "uncertain"
    finish.set()
    t.join(3)
    c.submissions.submit_text.assert_called_once()


def test_verified_file_upload_then_final_submission_are_separate():
    c, store = client(), ActionStore()
    raw = b"My paper"
    upload = store.prepare(c, "a", {"kind": "upload", "filename": "paper.txt", "content_base64": base64.b64encode(raw).decode(),
        "destination": "assignment", "course_id": 1, "assignment_id": 2})
    c.files.upload.return_value = {"id": 55, "display_name": "paper.txt"}
    c.files.get.return_value = SimpleNamespace(size=len(raw))
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
