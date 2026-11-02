"""Comment-specific upload receipts, isolation and exact byte/readback checks."""
import base64
import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from canvas_py._config import CanvasConfig
from canvas_py._errors import CanvasError
from canvas_py.models.file import CanvasFile
from pydantic import ValidationError

from canvas_connector.actions import ACTION_ADAPTER, ActionStore


class CommentCanvas:
    def __init__(self):
        self.config = CanvasConfig("https://school.example", "synthetic-token")
        self.navigation = Mock()
        self.bytes = b"Attached feedback"
        self.file = CanvasFile(id=55, size=len(self.bytes), display_name="notes.txt", filename="notes.txt")
        self.assignment = {"id": 2, "name": "Essay", "submission_types": ["online_upload"],
                           "can_submit": True, "allowed_extensions": []}
        self.submission = {"assignment_id": 2, "user_id": 7, "attempt": 1,
            "workflow_state": "submitted", "submitted_at": "2026-11-02T01:00:00Z", "submission_comments": []}
        self.assignments = SimpleNamespace(get=Mock(side_effect=lambda *a, **kw: copy.deepcopy(self.assignment)))
        self.submissions = SimpleNamespace(get=Mock(side_effect=lambda *a, **kw: copy.deepcopy(self.submission)),
            add_comment=Mock(side_effect=self.add_comment))
        self.files = SimpleNamespace(upload=Mock(side_effect=self.upload),
            get=Mock(side_effect=lambda file_id: self.file),
            download=Mock(side_effect=lambda file, path, **kw: path.write_bytes(self.bytes)))
        self.upload_contexts = []
        self.comment_change = None
        self.readback_change = None

    def upload(self, path, *, context, **kwargs):
        assert path.read_bytes() == self.bytes
        assert kwargs["verify"] is True
        assert kwargs["on_duplicate"] == "rename"
        self.upload_contexts.append(context)
        return self.file

    def add_comment(self, course_id, assignment_id, text, *, attachment_file_ids):
        assert (course_id, assignment_id) == (1, 2)
        comment = {"id": 90, "author_id": 7, "comment": text,
                   "attachments": [{"id": file_id} for file_id in attachment_file_ids]}
        if self.comment_change:
            self.comment_change(comment)
        self.submission["submission_comments"].append(comment)
        posted = copy.deepcopy(self.submission)
        if self.readback_change:
            self.readback_change(self.submission["submission_comments"][-1])
        return posted


def uploaded(c, store, *, owner="alice", destination="submission_comment"):
    payload = {"kind": "upload", "filename": "notes.txt", "content_base64": base64.b64encode(c.bytes).decode(),
               "destination": destination}
    if destination != "user":
        payload.update(course_id=1, assignment_id=2)
    review = store.prepare(c, owner, payload)
    receipt = store.execute(c, owner, review["action_id"], lambda: None)
    assert receipt["state"] == "succeeded", receipt
    return receipt


def comment_payload(*receipts):
    return {"kind": "submission_comment", "course_id": 1, "assignment_id": 2,
            "text": "Please see my attached notes.", "upload_action_ids": [r["action_id"] for r in receipts]}


def test_comment_upload_and_comment_delivery_are_separate_verified_actions():
    c, store = CommentCanvas(), ActionStore()
    upload = uploaded(c, store)
    assert upload["result"]["submitted"] is False
    assert upload["result"]["comment_sent"] is False
    assert "does not send a submission comment" in upload["review"]["effect"]
    c.submissions.add_comment.assert_not_called()
    context = c.upload_contexts[0]
    assert context.kind == "submission_comment"
    assert context.endpoint_path() == "courses/1/assignments/2/submissions/self/comments/files"
    r = store.prepare(c, "alice", comment_payload(upload))
    assert r["execute"]["tool"] == "canvas_add_submission_comment"
    assert r["review"]["target"]["files"] == [{"id": 55, "filename": "notes.txt", "bytes": len(c.bytes), "sha256": upload["review"]["sha256"]}]
    c.submissions.add_comment.assert_not_called()
    downloads_before = c.files.download.call_count
    receipt = store.execute(c, "alice", r["action_id"], lambda: None)
    assert receipt["state"] == "succeeded"
    assert receipt["result"]["verified"] is True
    assert c.files.download.call_count == downloads_before + 1
    c.submissions.add_comment.assert_called_once_with(1, 2, r["review"]["text"], attachment_file_ids=[55])
    assert store.execute(c, "alice", r["action_id"], lambda: None)["state"] == "succeeded"
    assert c.submissions.add_comment.call_count == 1
    assert c.submission["attempt"] == 1


def test_optional_attachments_preserve_legacy_text_comment_payload_and_deduplication():
    import json
    c, store = CommentCanvas(), ActionStore()
    original = {"kind": "submission_comment", "course_id": 1, "assignment_id": 2, "text": "Plain comment"}
    r = store.prepare(c, "alice", original)
    record = store.records[r["action_id"]]
    assert json.loads(record["payload"]) == original
    assert "files" not in record["snapshot"]
    assert store.prepare(c, "alice", {**original, "upload_action_ids": []})["action_id"] == r["action_id"]
    assert store.execute(c, "alice", r["action_id"], lambda: None)["state"] == "succeeded"


def test_comment_attachments_are_not_subject_to_assignment_submission_format_or_window():
    c, store = CommentCanvas(), ActionStore()
    c.assignment.update(submission_types=["online_text_entry"], allowed_extensions=["pdf"],
                        can_submit=False, locked_for_user=True)
    upload = uploaded(c, store)
    r = store.prepare(c, "alice", comment_payload(upload))
    assert store.execute(c, "alice", r["action_id"], lambda: None)["state"] == "succeeded"


@pytest.mark.parametrize("destination", ["assignment", "user"])
def test_comment_requires_comment_context_not_other_successful_upload(destination):
    c, store = CommentCanvas(), ActionStore()
    upload = uploaded(c, store, destination=destination)
    with pytest.raises(ValueError, match="upload context"):
        store.prepare(c, "alice", comment_payload(upload))
    c.submissions.add_comment.assert_not_called()


def test_comment_upload_receipt_cannot_be_submitted_as_assignment():
    c, store = CommentCanvas(), ActionStore()
    upload = uploaded(c, store)
    with pytest.raises(ValueError, match="upload context"):
        store.prepare(c, "alice", {"kind": "submit_files", "course_id": 1, "assignment_id": 2,
                                   "upload_action_ids": [upload["action_id"]]})


@pytest.mark.parametrize("change", ["owner", "token", "origin", "course", "assignment", "user"])
def test_receipt_cannot_cross_owner_origin_credentials_or_target(change):
    c, store = CommentCanvas(), ActionStore()
    upload = uploaded(c, store)
    payload, owner = comment_payload(upload), "alice"
    if change == "owner": owner = "bob"
    elif change == "token": c.config = CanvasConfig("https://school.example", "other-token")
    elif change == "origin": c.config = CanvasConfig("https://other.example", "synthetic-token")
    elif change == "course": payload["course_id"] = 3
    elif change == "assignment":
        payload["assignment_id"] = c.assignment["id"] = c.submission["assignment_id"] = 3
    elif change == "user": c.submission["user_id"] = 8
    with pytest.raises(ValueError):
        store.prepare(c, owner, payload)
    c.submissions.add_comment.assert_not_called()


def test_duplicate_receipts_and_duplicate_canvas_file_ids_rejected():
    c, store = CommentCanvas(), ActionStore()
    one = uploaded(c, store)
    with pytest.raises(ValueError, match="distinct"):
        store.prepare(c, "alice", comment_payload(one, one))
    payload = {"kind": "upload", "filename": "another.txt", "content_base64": base64.b64encode(c.bytes).decode(),
               "destination": "submission_comment", "course_id": 1, "assignment_id": 2}
    prepared = store.prepare(c, "alice", payload)
    two = store.execute(c, "alice", prepared["action_id"], lambda: None)
    with pytest.raises(ValueError, match="distinct Canvas files"):
        store.prepare(c, "alice", comment_payload(one, two))


@pytest.mark.parametrize("state", ["prepared", "uncertain", "rejected", "expired"])
def test_only_successful_upload_receipts_can_be_attached(state):
    c, store = CommentCanvas(), ActionStore()
    r = store.prepare(c, "alice", {"kind": "upload", "filename": "notes.txt", "content_base64": "aGk=",
        "destination": "submission_comment", "course_id": 1, "assignment_id": 2})
    store.records[r["action_id"]]["state"] = state
    with pytest.raises(ValueError, match="verified completed"):
        store.prepare(c, "alice", comment_payload(r))


@pytest.mark.parametrize("change", ["content", "size", "file_id", "gone"])
def test_preflight_rechecks_exact_file_identity_and_bytes_before_comment_dispatch(change):
    c, store = CommentCanvas(), ActionStore()
    upload = uploaded(c, store)
    r = store.prepare(c, "alice", comment_payload(upload))
    if change == "content": c.bytes = b"X" * len(c.bytes)
    elif change == "size": c.file = c.file.model_copy(update={"size": 1})
    elif change == "file_id": c.file = c.file.model_copy(update={"id": 56})
    elif change == "gone": c.files.get.side_effect = CanvasError("gone", status=404)
    result = store.execute(c, "alice", r["action_id"], lambda: None)
    assert result["state"] == "rejected"
    assert result["result"]["upstream_write_attempted"] is False
    c.submissions.add_comment.assert_not_called()


@pytest.mark.parametrize("change", [lambda x: x.update(author_id=8), lambda x: x.update(comment="Different"),
    lambda x: x.update(attachments=[]), lambda x: x.update(attachments=[{"id": 56}]),
    lambda x: x.update(attachments=[{"id": 55}, {"id": 56}]), lambda x: x.update(id=None)])
def test_unconfirmed_comment_author_text_or_attachment_set_never_succeeds(change):
    c, store = CommentCanvas(), ActionStore()
    upload = uploaded(c, store)
    r = store.prepare(c, "alice", comment_payload(upload))
    c.comment_change = change
    assert store.execute(c, "alice", r["action_id"], lambda: None)["state"] == "uncertain"
    assert store.execute(c, "alice", r["action_id"], lambda: None)["state"] == "uncertain"
    assert c.submissions.add_comment.call_count == 1


def test_readback_requires_the_exact_post_returned_comment_id():
    c, store = CommentCanvas(), ActionStore()
    upload = uploaded(c, store)
    r = store.prepare(c, "alice", comment_payload(upload))
    c.readback_change = lambda x: x.update(id=91)
    assert store.execute(c, "alice", r["action_id"], lambda: None)["state"] == "uncertain"


def test_denied_write_scope_leaves_comment_prepared_and_does_not_dispatch():
    c, store = CommentCanvas(), ActionStore()
    upload = uploaded(c, store)
    r = store.prepare(c, "alice", comment_payload(upload))
    def denied(): raise PermissionError("read only")
    with pytest.raises(PermissionError):
        store.execute(c, "alice", r["action_id"], denied)
    assert store.status(c, "alice", r["action_id"])["state"] == "prepared"
    c.submissions.add_comment.assert_not_called()


@pytest.mark.parametrize("payload", [
    {"kind": "submission_comment", "course_id": 1, "assignment_id": 2, "text": "", "upload_action_ids": ["receipt"]},
    {"kind": "submission_comment", "course_id": 1, "assignment_id": 2, "text": "x", "file_ids": [55]},
    {"kind": "submission_comment", "course_id": 1, "assignment_id": 2, "text": "x", "upload_action_ids": ["receipt"] * 21},
    {"kind": "upload", "filename": "notes.txt", "content_base64": "aGk=", "destination": "submission_comment"},
    {"kind": "upload", "filename": "notes.txt", "content_base64": "aGk=", "destination": "submission_comment", "course_id": 1, "assignment_id": 2, "user_id": 8},
])
def test_typed_payload_does_not_accept_raw_file_ids_unknown_users_or_empty_text(payload):
    with pytest.raises(ValidationError):
        ACTION_ADAPTER.validate_python(payload)


def test_encrypted_comment_receipts_survive_restart_and_hosted_token_refresh(tmp_path):
    from cryptography.fernet import Fernet
    from canvas_connector.storage import Store
    c, key, path = CommentCanvas(), Fernet.generate_key().decode(), tmp_path / "actions.sqlite"
    first = Store(path, key)
    one = ActionStore(persistence=first, bind_credentials=False)
    upload = uploaded(c, one)
    r = one.prepare(c, "alice", comment_payload(upload))
    first.close()
    c.config = CanvasConfig("https://school.example", "refreshed-token")
    second = Store(path, key)
    two = ActionStore(persistence=second, bind_credentials=False)
    assert two.execute(c, "alice", r["action_id"], lambda: None)["state"] == "succeeded"
    second.close()


def test_comment_attachment_uses_real_canvas_json_contract():
    import json
    import httpx
    from canvas_py import Canvas
    from canvas_connector.responses import dump
    c = CommentCanvas()
    writes = []
    def handler(request):
        path = request.url.path.removeprefix("/api/v1/")
        if path == "courses/1/assignments/2":
            return httpx.Response(200, json=c.assignment)
        if path == "courses/1/assignments/2/submissions/self":
            if request.method == "PUT":
                body = json.loads(request.content)
                assert body == {"comment": {"text_comment": "See attached.", "file_ids": [55]}}
                writes.append(body)
                c.add_comment(1, 2, "See attached.", attachment_file_ids=[55])
            return httpx.Response(200, json=c.submission)
        raise AssertionError((request.method, path))
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with Canvas(c.config, http_client=http) as canvas:
            # The file upload transport has dedicated existing tests; this
            # covers comment PUT shape and native Submission/Comment decoding.
            store = ActionStore()
            target = {"user_id": 7, "comment_ids": [], "files": [{"id": 55}]}
            result = store._perform(canvas, {"kind": "submission_comment", "course_id": 1,
                "assignment_id": 2, "text": "See attached."}, target)
            assert dump(result)["verified"] is True
    assert len(writes) == 1
