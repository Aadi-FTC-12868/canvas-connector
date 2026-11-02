"""Group discussion actions over native Canvas HTTP; all data is synthetic."""
import copy
import hashlib
import json
import re

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError
from test_group_content import MockCanvas

from canvas_connector.actions import ACTION_ADAPTER, ActionStore
from canvas_connector.storage import Store

TARGET = {"context": {"context_type": "group", "group_id": 2}, "topic_id": 10}
CASES = [
    ("post", {"message": "<p>Reviewed post.</p>"}, "POST", "/entries"),
    ("reply", {"entry_id": 50, "message": "<p>Reviewed reply.</p>"}, "POST", "/entries/50/replies"),
    ("edit", {"entry_id": 50, "message": "<p>Reviewed edit.</p>"}, "PUT", "/entries/50"),
    ("delete", {"entry_id": 50}, "DELETE", "/entries/50"),
    ("subscribe", {}, "PUT", "/subscribed"),
    ("unsubscribe", {}, "DELETE", "/subscribed"),
    ("read", {}, "PUT", "/read"),
    ("read", {"read": False}, "DELETE", "/read"),
    ("read", {"entry_id": 50}, "PUT", "/entries/50/read"),
    ("read", {"entry_id": 50, "read": False}, "DELETE", "/entries/50/read"),
    ("rate", {"entry_id": 50, "rating": 1}, "POST", "/entries/50/rating"),
    ("rate", {"entry_id": 50, "rating": 0}, "POST", "/entries/50/rating"),
]


def payload(operation, fields=None):
    return {"kind": "group_discussion_" + operation, **copy.deepcopy(TARGET), **(fields or {})}


class DiscussionHTTP(MockCanvas):
    def __init__(self):
        super().__init__()
        self.topic.update(locked=False, subscribed=False, allow_rating=True, only_graders_can_rate=False,
                          permissions={"reply": True, "update": False, "delete": False})
        self.entries = {50: {"id": 50, "user_id": 7, "parent_id": None, "message": "<p>Original</p>",
                             "read_state": "unread", "updated_at": "2026-11-01T12:00:00Z"}}
        self.ratings = {}
        self.ignore_write = False
        self.visible_in_view = True
        self.view_override = None
        self.entry_list_override = None
        self.post_result = None

    def handle(self, request):
        path = request.url.path.removeprefix("/api/v1/")
        match = re.fullmatch(r"(?:groups/2|courses/1)/discussion_topics/10(/.*)", path)
        if match is None:
            return super().handle(request)
        suffix = match[1]
        self.seen.append(request)
        assert request.url.host == "school.example"
        assert request.headers["authorization"] == "Bearer synthetic-token"
        assert not any(key in {"as_user_id", "student_id", "user_id", "mark_read"} for key in request.url.params)
        if (request.method, path) in self.errors:
            status, body = self.errors[request.method, path]
            return httpx.Response(status, json=body)
        if request.method == "GET":
            assert request.content == b""
            if suffix == "/entry_list":
                assert set(request.url.params) == {"ids[]"}
                if self.topic.get("require_initial_post") and not self.topic.get("user_can_see_posts"):
                    return httpx.Response(403, json="require_initial_post")
                ids = [int(value) for value in request.url.params.get_list("ids[]")]
                body = self.entry_list_override if self.entry_list_override is not None else [self.entries[key] for key in ids if key in self.entries]
                return httpx.Response(200, json=body)
            if suffix == "/view":
                assert request.url.params.get("include_new_entries") == "true"
                body = self.view_override if self.view_override is not None else {
                    "entry_ratings": self.ratings, "view": list(self.entries.values()) if self.visible_in_view else [], "new_entries": []}
                return httpx.Response(200, json=body)
            raise AssertionError((request.method, path))
        body = json.loads(request.content) if request.content else None
        if body is not None:
            assert request.headers["content-type"] == "application/json"
        if suffix == "/entries" or suffix.endswith("/replies"):
            assert request.method == "POST" and set(body) == {"message"}
            parent_id = None if suffix == "/entries" else int(suffix.split("/")[2])
            created = {"id": 60, "user_id": 7, "parent_id": parent_id, "message": body["message"], "read_state": "unread"}
            if not self.ignore_write:
                self.entries[60] = copy.deepcopy(created)
                self.topic["user_can_see_posts"] = True
                self.topic.pop("subscription_hold", None)
            response = httpx.Response(201, json=self.post_result if self.post_result is not None else created)
        elif suffix == "/entries/50":
            assert request.method in {"PUT", "DELETE"}
            if not self.ignore_write:
                if request.method == "PUT":
                    assert set(body) == {"message"}
                    self.entries[50]["message"] = body["message"]
                else:
                    assert body is None
                    self.entries[50] = {"id": 50, "parent_id": self.entries[50]["parent_id"], "deleted": True}
            response = httpx.Response(200, json=self.entries[50])
        elif suffix == "/subscribed":
            assert request.method in {"PUT", "DELETE"} and body is None
            if not self.ignore_write:
                self.topic["subscribed"] = request.method == "PUT"
            response = httpx.Response(204)
        elif suffix in {"/read", "/entries/50/read"}:
            assert request.method in {"PUT", "DELETE"} and body is None
            if not self.ignore_write:
                (self.topic if suffix == "/read" else self.entries[50])["read_state"] = "read" if request.method == "PUT" else "unread"
            response = httpx.Response(204)
        elif suffix == "/entries/50/rating":
            assert request.method == "POST" and set(body) == {"rating"}
            if not self.ignore_write:
                if body["rating"]:
                    self.ratings["50"] = body["rating"]
                else:
                    self.ratings.pop("50", None)
            response = httpx.Response(204)
        else:
            raise AssertionError((request.method, path))
        if self.after_write:
            self.after_write(self)
        return response


@pytest.fixture
def upstream():
    state = DiscussionHTTP()
    yield state
    state.close()


def prepare_state(state, operation, fields):
    if operation == "unsubscribe":
        state.topic["subscribed"] = True
    if operation == "read" and fields.get("read") is False:
        state.topic["read_state"] = state.entries[50]["read_state"] = "read"
    if operation == "rate" and fields["rating"] == 0:
        state.ratings["50"] = 1


@pytest.mark.parametrize("operation,fields,method,suffix", CASES)
def test_group_actions_prepare_and_verify_exact_http_operation_once(upstream, operation, fields, method, suffix):
    prepare_state(upstream, operation, fields)
    store, p = ActionStore(), payload(operation, fields)
    prepared = store.prepare(upstream.canvas, "alice", p)
    route = "canvas_post_group_discussion" if operation in {"post", "reply"} else "canvas_manage_group_discussion"
    assert prepared["execute"]["tool"] == route
    target = prepared["review"]["target"]
    assert target["user_id"] == 7 and target["context"] == TARGET["context"]
    assert target["membership"]["workflow_state"] == "accepted"
    assert not upstream.writes
    result = store.execute(upstream.canvas, "alice", prepared["action_id"], lambda: None)
    assert result["state"] == "succeeded", result
    assert result["result"]["verified"] is True and result["result"]["context"] == TARGET["context"]
    assert [(request.method, request.url.path) for request in upstream.writes] == [(method, "/api/v1/groups/2/discussion_topics/10" + suffix)]
    assert upstream.seen[-1].method == "GET"
    assert store.execute(upstream.canvas, "alice", prepared["action_id"], lambda: None)["state"] == "succeeded"
    assert store.prepare(upstream.canvas, "alice", p)["action_id"] == prepared["action_id"]
    assert len(upstream.writes) == 1


@pytest.mark.parametrize("operation,fields,method,suffix", CASES)
def test_group_success_without_saved_state_is_uncertain_without_retry(upstream, operation, fields, method, suffix):
    prepare_state(upstream, operation, fields)
    store = ActionStore()
    prepared = store.prepare(upstream.canvas, "alice", payload(operation, fields))
    upstream.ignore_write = True
    result = store.execute(upstream.canvas, "alice", prepared["action_id"], lambda: None)
    assert result["state"] == "uncertain", result
    assert store.execute(upstream.canvas, "alice", prepared["action_id"], lambda: None)["state"] == "uncertain"
    assert len(upstream.writes) == 1


@pytest.mark.parametrize("operation,fields", [(op, fields) for op, fields, _, _ in CASES])
@pytest.mark.parametrize("change", ["membership", "group_name", "topic_title"])
def test_group_preflight_rejects_changed_audience_or_membership(upstream, operation, fields, change):
    store = ActionStore()
    prepared = store.prepare(upstream.canvas, "alice", payload(operation, fields))
    if change == "membership":
        upstream.member["workflow_state"] = "invited"
    elif change == "group_name":
        upstream.group["name"] = "Different audience"
    else:
        upstream.topic["title"] = "Changed discussion"
    result = store.execute(upstream.canvas, "alice", prepared["action_id"], lambda: None)
    assert result["state"] == "rejected" and result["result"]["upstream_write_attempted"] is False
    assert not upstream.writes


@pytest.mark.parametrize("operation,fields", [("post", {"message": "x"}), ("reply", {"entry_id": 50, "message": "x"})])
def test_posting_requires_reply_permission_not_create_topic_permission(upstream, operation, fields):
    upstream.group["permissions"]["create_discussion_topic"] = False
    store = ActionStore()
    assert store.prepare(upstream.canvas, "alice", payload(operation, fields))["state"] == "prepared"
    upstream.topic["permissions"]["reply"] = False
    with pytest.raises(ValueError, match="permit"):
        ActionStore().prepare(upstream.canvas, "alice", payload(operation, fields))
    assert not upstream.writes


def test_fresh_reply_permission_revocation_rejects_before_dispatch(upstream):
    store = ActionStore()
    prepared = store.prepare(upstream.canvas, "alice", payload("post", {"message": "x"}))
    upstream.topic["permissions"]["reply"] = False
    result = store.execute(upstream.canvas, "alice", prepared["action_id"], lambda: None)
    assert result["state"] == "rejected" and not upstream.writes


@pytest.mark.parametrize("operation,fields", [("reply", {"entry_id": 50, "message": "x"}),
    ("read", {"entry_id": 50}), ("rate", {"entry_id": 50, "rating": 1})])
def test_reply_and_personal_state_allow_another_members_entry(upstream, operation, fields):
    upstream.entries[50]["user_id"] = 8
    store = ActionStore()
    prepared = store.prepare(upstream.canvas, "alice", payload(operation, fields))
    assert store.execute(upstream.canvas, "alice", prepared["action_id"], lambda: None)["state"] == "succeeded"


@pytest.mark.parametrize("source,change", [("member", {"user_id": 8}), ("member", {"group_id": 3}),
    ("topic", {"id": 11}), ("topic", {"group_id": 3}), ("topic", {"context_type": "Course", "context_id": 2})])
def test_group_action_refuses_mismatched_membership_or_topic_context(upstream, source, change):
    getattr(upstream, source).update(change)
    with pytest.raises(ValueError):
        ActionStore().prepare(upstream.canvas, "alice", payload("post", {"message": "x"}))
    assert not upstream.writes


@pytest.mark.parametrize("operation,fields", [("edit", {"entry_id": 50, "message": "x"}), ("delete", {"entry_id": 50})])
@pytest.mark.parametrize("author", [None, 8, "7", True])
def test_own_entry_guards_reject_other_and_unidentifiable_authors(upstream, operation, fields, author):
    upstream.entries[50]["user_id"] = author
    with pytest.raises(ValueError):
        ActionStore().prepare(upstream.canvas, "alice", payload(operation, fields))
    assert not upstream.writes


@pytest.mark.parametrize("operation,fields,permission", [("edit", {"entry_id": 50, "message": "x"}, "update"),
    ("delete", {"entry_id": 50}, "delete"), ("rate", {"entry_id": 50, "rating": 1}, "rate")])
def test_returned_entry_permission_denial_is_respected(upstream, operation, fields, permission):
    upstream.entries[50]["permissions"] = {permission: False}
    with pytest.raises(ValueError, match="permit"):
        ActionStore().prepare(upstream.canvas, "alice", payload(operation, fields))
    assert not upstream.writes


@pytest.mark.parametrize("field", ["attachment", "attachments"])
def test_attached_entry_edits_stay_refused(upstream, field):
    upstream.entries[50][field] = {"id": 90} if field == "attachment" else [{"id": 90}]
    with pytest.raises(ValueError, match="preserve the attachment"):
        ActionStore().prepare(upstream.canvas, "alice", payload("edit", {"entry_id": 50, "message": "x"}))
    assert not upstream.writes


@pytest.mark.parametrize("operation,fields", [("reply", {"entry_id": 50, "message": "x"}),
    ("edit", {"entry_id": 50, "message": "x"}), ("delete", {"entry_id": 50}),
    ("subscribe", {}), ("read", {"entry_id": 50}), ("rate", {"entry_id": 50, "rating": 1})])
def test_initial_post_gate_blocks_targeted_actions_without_any_write(upstream, operation, fields):
    upstream.topic.update(require_initial_post=True, user_can_see_posts=False, subscription_hold="initial_post_required")
    with pytest.raises(ValueError, match="initial post"):
        ActionStore().prepare(upstream.canvas, "alice", payload(operation, fields))
    assert not upstream.writes and not any(request.url.path.endswith("/entry_list") for request in upstream.seen)


@pytest.mark.parametrize("operation,fields", [("post", {"message": "<p>My authorized first post</p>"}), ("read", {}), ("unsubscribe", {})])
def test_initial_post_gate_still_allows_authorized_first_post_topic_read_and_unsubscribe(upstream, operation, fields):
    upstream.topic.update(require_initial_post=True, user_can_see_posts=False, subscription_hold="initial_post_required", subscribed=True)
    store = ActionStore()
    prepared = store.prepare(upstream.canvas, "alice", payload(operation, fields))
    assert not upstream.writes
    assert store.execute(upstream.canvas, "alice", prepared["action_id"], lambda: None)["state"] == "succeeded"
    assert len(upstream.writes) == 1


@pytest.mark.parametrize("operation,fields", [("post", {"message": "x"}), ("reply", {"entry_id": 50, "message": "x"}),
    ("edit", {"entry_id": 50, "message": "x"}), ("delete", {"entry_id": 50}), ("rate", {"entry_id": 50, "rating": 1})])
def test_closed_topic_blocks_content_changes(upstream, operation, fields):
    upstream.topic["locked"] = True
    with pytest.raises(ValueError, match="closed"):
        ActionStore().prepare(upstream.canvas, "alice", payload(operation, fields))
    assert not upstream.writes


@pytest.mark.parametrize("operation", ["subscribe", "unsubscribe", "read"])
def test_closed_topic_still_allows_personal_subscription_and_read_state(upstream, operation):
    upstream.topic["locked"] = True
    assert ActionStore().prepare(upstream.canvas, "alice", payload(operation))["state"] == "prepared"


@pytest.mark.parametrize("change", [{"allow_rating": False}, {"only_graders_can_rate": True}])
def test_rating_policy_remains_student_scoped(upstream, change):
    upstream.topic.update(change)
    with pytest.raises(ValueError, match="student ratings"):
        ActionStore().prepare(upstream.canvas, "alice", payload("rate", {"entry_id": 50, "rating": 1}))


def test_missing_rating_requires_entry_in_materialized_view(upstream):
    upstream.visible_in_view = False
    with pytest.raises(ValueError, match="did not confirm"):
        ActionStore().prepare(upstream.canvas, "alice", payload("rate", {"entry_id": 50, "rating": 0}))
    assert not upstream.writes


def test_unrate_missing_stale_view_is_not_false_success(upstream):
    upstream.ratings["50"] = 1
    store = ActionStore()
    prepared = store.prepare(upstream.canvas, "alice", payload("rate", {"entry_id": 50, "rating": 0}))
    upstream.after_write = lambda state: setattr(state, "visible_in_view", False)
    assert store.execute(upstream.canvas, "alice", prepared["action_id"], lambda: None)["state"] == "uncertain"
    assert len(upstream.writes) == 1


@pytest.mark.parametrize("operation,fields", [("post", {"message": "<p>x</p>"}), ("reply", {"entry_id": 50, "message": "<p>x</p>"})])
@pytest.mark.parametrize("change", [{"id": 61}, {"user_id": 8}, {"message": "different"}, {"parent_id": 99},
                                     {"deleted": True}, {"discussion_topic_id": 99}])
def test_post_readback_requires_exact_entry_author_parent_and_text(upstream, operation, fields, change):
    store = ActionStore()
    prepared = store.prepare(upstream.canvas, "alice", payload(operation, fields))
    upstream.after_write = lambda state: state.entries[60].update(change)
    assert store.execute(upstream.canvas, "alice", prepared["action_id"], lambda: None)["state"] == "uncertain"
    assert len(upstream.writes) == 1


@pytest.mark.parametrize("change", [{"user_id": 8}, {"parent_id": 99}, {"message": "different"}, {"deleted": True}])
def test_edit_readback_preserves_owner_and_parent(upstream, change):
    store = ActionStore()
    prepared = store.prepare(upstream.canvas, "alice", payload("edit", {"entry_id": 50, "message": "<p>x</p>"}))
    upstream.after_write = lambda state: state.entries[50].update(change)
    assert store.execute(upstream.canvas, "alice", prepared["action_id"], lambda: None)["state"] == "uncertain"


@pytest.mark.parametrize("operation,fields", [("read", {"entry_id": 50}), ("rate", {"entry_id": 50, "rating": 1})])
@pytest.mark.parametrize("change", [{"id": 51}, {"user_id": 8}, {"parent_id": 99}, {"deleted": True}])
def test_personal_entry_state_readback_confirms_same_author_and_parent(upstream, operation, fields, change):
    store = ActionStore()
    prepared = store.prepare(upstream.canvas, "alice", payload(operation, fields))
    upstream.after_write = lambda state: state.entries[50].update(change)
    assert store.execute(upstream.canvas, "alice", prepared["action_id"], lambda: None)["state"] == "uncertain"
    assert len(upstream.writes) == 1


def test_delete_initial_post_readback_gate_is_uncertain_and_never_retried(upstream):
    store = ActionStore()
    prepared = store.prepare(upstream.canvas, "alice", payload("delete", {"entry_id": 50}))
    upstream.after_write = lambda state: state.topic.update(require_initial_post=True, user_can_see_posts=False)
    assert store.execute(upstream.canvas, "alice", prepared["action_id"], lambda: None)["state"] == "uncertain"
    assert store.execute(upstream.canvas, "alice", prepared["action_id"], lambda: None)["state"] == "uncertain"
    assert len(upstream.writes) == 1


@pytest.mark.parametrize("extra", [{"course_id": 1}, {"group_id": 2}, {"user_id": 9}, {"as_user_id": 9},
    {"attachment": "x"}, {"attachment_file_ids": [90]}, {"is_announcement": True}, {"url": "https://elsewhere.example"}])
def test_group_action_contract_forbids_ambiguous_contexts_and_extra_writes(extra):
    with pytest.raises(ValidationError):
        ACTION_ADAPTER.validate_python({**payload("post", {"message": "x"}), **extra})


@pytest.mark.parametrize("context", [{"context_type": "course", "course_id": 1},
    {"context_type": "group", "group_id": "2"}, {"context_type": "group", "group_id": True},
    {"context_type": "group", "group_id": 0}, {"context_type": "group", "group_id": 2**63}])
def test_group_target_is_explicit_strict_and_bounded(context):
    with pytest.raises(ValidationError):
        ACTION_ADAPTER.validate_python({**payload("post", {"message": "x"}), "context": context})


def legacy_snapshot(state, operation, fields):
    """Recorded v0.6 shape; deliberately independent of the current handler."""
    if operation in {"post", "reply"}:
        return {key: state.topic.get(key) for key in ("id", "title", "assignment_id", "locked", "locked_for_user", "permissions")}
    result = {"user_id": 7, "course_id": 1, "topic_id": 10, "title": state.topic["title"],
              "locked": False, "locked_for_user": False}
    if fields.get("entry_id") is not None:
        result["entry"] = {key: state.entries[50].get(key) for key in
            ("id", "user_id", "parent_id", "message", "updated_at", "attachment", "attachments", "permissions")}
    if operation in {"subscribe", "unsubscribe"}:
        result["subscribed"] = state.topic["subscribed"]
    elif operation == "read":
        result["read_state"] = state.entries[50]["read_state"] if fields.get("entry_id") else state.topic["read_state"]
    elif operation == "rate":
        result["rating"] = state.ratings.get("50", 0)
    return result


@pytest.mark.parametrize("operation,fields,method,suffix", CASES)
def test_persisted_v06_course_receipt_keeps_fingerprint_snapshot_and_replay_behavior(upstream, tmp_path, operation, fields, method, suffix):
    prepare_state(upstream, operation, fields)
    p = {"kind": "discussion_" + operation, "course_id": 1, "topic_id": 10, **fields}
    historical_payload = {**p, **({"entry_id": fields.get("entry_id"), "read": fields.get("read", True)} if operation == "read" else {})}
    old_target = legacy_snapshot(upstream, operation, fields)

    class LegacySnapshotStore(ActionStore):
        def _snapshot(self, canvas, owner, payload):
            return copy.deepcopy(old_target)

    db = Store(tmp_path / "legacy-course.db", Fernet.generate_key())
    try:
        old_store = LegacySnapshotStore(persistence=db, bind_credentials=False)
        prepared = old_store.prepare(upstream.canvas, "alice", p)
        action_id = prepared["action_id"]
        encoded = json.dumps(historical_payload, sort_keys=True, separators=(",", ":"))
        assert db.get("canvas_action_payload", action_id)["payload"] == encoded
        identity = old_store._identity(upstream.canvas, "alice")
        expected_fingerprint = hashlib.sha256((identity + "\0" + encoded).encode()).hexdigest()
        assert db.get("canvas_action", action_id)["fingerprint"] == expected_fingerprint
        assert db.get("canvas_action", action_id)["snapshot"] == old_target
        current = ActionStore(persistence=db, bind_credentials=False)
        assert current.prepare(upstream.canvas, "alice", p)["action_id"] == action_id
        assert not upstream.seen  # existing fingerprint reused before source reads
        result = current.execute(upstream.canvas, "alice", action_id, lambda: None)
        assert result["state"] == "succeeded", result
        assert result["execute"]["tool"] == ("canvas_post_discussion" if operation in {"post", "reply"} else "canvas_manage_discussion")
        assert upstream.writes[0].url.path == "/api/v1/courses/1/discussion_topics/10" + suffix
        restarted = ActionStore(persistence=db, bind_credentials=False)
        assert restarted.execute(upstream.canvas, "alice", action_id, lambda: None)["state"] == "succeeded"
        assert len(upstream.writes) == 1
    finally:
        db.close()
