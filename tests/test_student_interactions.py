"""Stateful synthetic Canvas fixtures: no network and no real account writes."""
import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from canvas_py._config import CanvasConfig
from canvas_py._errors import CanvasError
from pydantic import ValidationError

from canvas_connector.actions import ACTION_ADAPTER, ActionStore


class StudentCanvas:
    def __init__(self):
        self.config = CanvasConfig("https://school.example", "synthetic-token")
        self.navigation = Mock()
        self.actor = 7
        self.topic = {"id": 4, "title": "Class discussion", "locked": False,
                      "locked_for_user": False, "allow_rating": True,
                      "subscribed": False, "read_state": "unread"}
        self.entry = {"id": 5, "user_id": 7, "message": "Original", "read_state": "unread"}
        self.ratings = {}
        self.submission = {"assignment_id": 2, "user_id": 7,
            "submission_comments": [{"id": 9, "author_id": 7, "comment": "Original"}]}
        self.event = {"id": 12, "context_code": "user_7", "title": "Study",
            "description": "Plan", "start_at": "2026-11-03T15:00:00Z", "end_at": "2026-11-03T16:00:00Z",
            "location_name": "Library", "location_address": "", "all_day": False, "workflow_state": "active"}
        self.discussions = SimpleNamespace(get=lambda *args: copy.deepcopy(self.topic))
        self.submissions = SimpleNamespace(get=lambda *args: copy.deepcopy(self.submission))
        self.requests = []
        self.writes = []
        self.ignore_writes = False
        self.delete_status = None
        self.write_error = None

    def send(self, request):
        method, path, body = request.method, request.path, request.json_body()
        self.requests.append((method, path, copy.deepcopy(body), request.query_pairs()))
        if method != "GET":
            self.writes.append((method, path, copy.deepcopy(body)))
            if self.write_error:
                raise self.write_error
        if path == "users/self/profile":
            assert method == "GET"
            return {"id": self.actor}
        if path.endswith("/entry_list"):
            assert request.query_pairs() == [("ids[]", 5)]
            return [copy.deepcopy(self.entry)]
        if path.endswith("/view"):
            return {"entry_ratings": copy.deepcopy(self.ratings)}
        if path.endswith("/entries/5"):
            if not self.ignore_writes:
                if method == "PUT":
                    self.entry["message"] = body["message"]
                elif method == "DELETE":
                    self.entry = {"id": 5, "deleted": True}
            return copy.deepcopy(self.entry)
        if path.endswith("/subscribed"):
            if not self.ignore_writes:
                self.topic["subscribed"] = method == "PUT"
            return None
        if path.endswith("/read"):
            if not self.ignore_writes:
                target = self.entry if "/entries/" in path else self.topic
                target["read_state"] = "read" if method == "PUT" else "unread"
            return None
        if path.endswith("/rating"):
            if not self.ignore_writes:
                if body["rating"]:
                    self.ratings["5"] = body["rating"]
                else:
                    self.ratings.pop("5", None)
            return None
        if path.endswith("/comments/9"):
            assert "/submissions/7/comments/" in path
            result = copy.deepcopy(self.submission["submission_comments"][0])
            if not self.ignore_writes:
                if method == "PUT":
                    self.submission["submission_comments"][0]["comment"] = body["comment"]
                    result["comment"] = body["comment"]
                else:
                    self.submission["submission_comments"] = []
            return result
        if path in {"calendar_events", "calendar_events/12"}:
            if method == "GET" and self.event["workflow_state"] == "deleted" and self.delete_status:
                raise CanvasError("readback", status=self.delete_status)
            if not self.ignore_writes:
                if method in {"POST", "PUT"}:
                    self.event.update(body["calendar_event"])
                elif method == "DELETE":
                    self.event["workflow_state"] = "deleted"
            return copy.deepcopy(self.event)
        raise AssertionError((method, path, body))


DISCUSSION = {"course_id": 1, "topic_id": 4}
COMMENT = {"course_id": 1, "assignment_id": 2, "comment_id": 9}
CREATE = {"kind": "calendar_create", "title": "Review chapter", "description": "Bring notes",
          "start_at": "2026-11-04T15:00:00Z", "end_at": "2026-11-04T16:00:00Z"}
CASES = [
    ({"kind": "discussion_edit", **DISCUSSION, "entry_id": 5, "message": "Revised"}, "PUT", "/entries/5", "canvas_manage_discussion"),
    ({"kind": "discussion_delete", **DISCUSSION, "entry_id": 5}, "DELETE", "/entries/5", "canvas_manage_discussion"),
    ({"kind": "discussion_subscribe", **DISCUSSION}, "PUT", "/subscribed", "canvas_manage_discussion"),
    ({"kind": "discussion_unsubscribe", **DISCUSSION}, "DELETE", "/subscribed", "canvas_manage_discussion"),
    ({"kind": "discussion_read", **DISCUSSION}, "PUT", "/read", "canvas_manage_discussion"),
    ({"kind": "discussion_read", **DISCUSSION, "entry_id": 5, "read": False}, "DELETE", "/entries/5/read", "canvas_manage_discussion"),
    ({"kind": "discussion_rate", **DISCUSSION, "entry_id": 5, "rating": 1}, "POST", "/rating", "canvas_manage_discussion"),
    ({"kind": "submission_comment_edit", **COMMENT, "comment": "Corrected"}, "PUT", "/submissions/7/comments/9", "canvas_manage_submission_comments"),
    ({"kind": "submission_comment_delete", **COMMENT}, "DELETE", "/submissions/7/comments/9", "canvas_manage_submission_comments"),
    (CREATE, "POST", "calendar_events", "canvas_manage_calendar"),
    ({"kind": "calendar_update", "event_id": 12, "title": "Review"}, "PUT", "calendar_events/12", "canvas_manage_calendar"),
    ({"kind": "calendar_delete", "event_id": 12}, "DELETE", "calendar_events/12", "canvas_manage_calendar"),
]


@pytest.mark.parametrize("payload,method,suffix,route", CASES)
def test_student_mutation_prepares_exact_review_then_verifies_once(payload, method, suffix, route):
    c, store = StudentCanvas(), ActionStore()
    r = store.prepare(c, "alice", payload)
    assert r["state"] == "prepared"
    assert r["execute"]["tool"] == route
    assert r["review"]["target"]["user_id"] == 7
    assert not c.writes
    result = store.execute(c, "alice", r["action_id"], lambda: None)
    assert result["state"] == "succeeded", result
    assert result["result"]["verified"] is True
    assert c.writes[0][0] == method and c.writes[0][1].endswith(suffix)
    assert store.execute(c, "alice", r["action_id"], lambda: None)["state"] == "succeeded"
    assert len(c.writes) == 1


@pytest.mark.parametrize("payload,method,suffix,route", [case for case in CASES if case[0]["kind"] not in {"discussion_unsubscribe"} and not (case[0]["kind"] == "discussion_read" and case[0].get("read") is False)])
def test_unconfirmed_saved_state_is_uncertain_and_not_retried(payload, method, suffix, route):
    c, store = StudentCanvas(), ActionStore()
    r = store.prepare(c, "alice", payload)
    c.ignore_writes = True
    result = store.execute(c, "alice", r["action_id"], lambda: None)
    assert result["state"] == "uncertain", result
    assert store.execute(c, "alice", r["action_id"], lambda: None)["state"] == "uncertain"
    assert len(c.writes) == 1


@pytest.mark.parametrize("owner", [None, 8])
@pytest.mark.parametrize("kind", ["discussion_edit", "discussion_delete"])
def test_discussion_owner_must_be_current_identifiable_user(owner, kind):
    c = StudentCanvas()
    c.entry["user_id"] = owner
    payload = {"kind": kind, **DISCUSSION, "entry_id": 5}
    if kind == "discussion_edit": payload["message"] = "Edit"
    with pytest.raises(ValueError, match="own identifiable"):
        ActionStore().prepare(c, "alice", payload)
    assert not c.writes


@pytest.mark.parametrize("change,match", [({"attachment": {"id": 3}}, "preserve"), ({"permissions": {"update": False}}, "permit")])
def test_discussion_attachment_and_policy_are_preserved(change, match):
    c = StudentCanvas()
    c.entry.update(change)
    with pytest.raises(ValueError, match=match):
        ActionStore().prepare(c, "alice", CASES[0][0])
    assert not c.writes


@pytest.mark.parametrize("kind", ["discussion_edit", "discussion_delete", "discussion_rate"])
def test_locked_discussion_cannot_be_changed(kind):
    c = StudentCanvas()
    c.topic["locked_for_user"] = True
    payload = next(case[0] for case in CASES if case[0]["kind"] == kind)
    with pytest.raises(ValueError, match="locked"):
        ActionStore().prepare(c, "alice", payload)
    assert not c.writes


def test_stale_content_and_changed_identity_reject_before_dispatch():
    for change in (lambda c: c.entry.update(message="Edited elsewhere"), lambda c: setattr(c, "actor", 8)):
        c, store = StudentCanvas(), ActionStore()
        r = store.prepare(c, "alice", CASES[0][0])
        change(c)
        result = store.execute(c, "alice", r["action_id"], lambda: None)
        assert result["state"] == "rejected"
        assert result["result"]["upstream_write_attempted"] is False
        assert not c.writes


@pytest.mark.parametrize("change", [{"allow_rating": False}, {"only_graders_can_rate": True}])
def test_student_rating_honors_topic_policy(change):
    c = StudentCanvas()
    c.topic.update(change)
    with pytest.raises(ValueError, match="ratings"):
        ActionStore().prepare(c, "alice", {"kind": "discussion_rate", **DISCUSSION, "entry_id": 5, "rating": 1})


def test_unrate_verifies_absence_from_current_user_ratings():
    c, store = StudentCanvas(), ActionStore()
    c.ratings = {"5": 1}
    r = store.prepare(c, "alice", {"kind": "discussion_rate", **DISCUSSION, "entry_id": 5, "rating": 0})
    assert store.execute(c, "alice", r["action_id"], lambda: None)["state"] == "succeeded"
    assert c.ratings == {}


@pytest.mark.parametrize("mutate", [lambda c: c.submission.update(user_id=8), lambda c: c.submission["submission_comments"][0].update(author_id=8), lambda c: c.submission["submission_comments"][0].update(permissions={"delete": False})])
def test_comment_rejects_foreign_author_or_wrong_submission_or_denied_policy(mutate):
    c = StudentCanvas()
    mutate(c)
    with pytest.raises(ValueError):
        ActionStore().prepare(c, "alice", {"kind": "submission_comment_delete", **COMMENT})
    assert not c.writes


@pytest.mark.parametrize("change", [{"context_code": "course_1"}, {"context_code": "user_8"}, {"appointment_group_id": 3}, {"parent_event_id": 3}, {"child_events_count": 1}, {"rrule": "FREQ=DAILY;COUNT=2"}, {"series_uuid": "series"}, {"workflow_state": "locked"}, {"permissions": {"delete": False}}])
def test_calendar_rejects_other_calendars_and_shared_or_recurring_events(change):
    c = StudentCanvas()
    c.event.update(change)
    with pytest.raises(ValueError):
        ActionStore().prepare(c, "alice", {"kind": "calendar_delete", "event_id": 12})
    assert not c.writes


def test_calendar_create_binds_server_identity_and_update_preserves_other_fields():
    c, store = StudentCanvas(), ActionStore()
    r = store.prepare(c, "alice", CREATE)
    assert store.execute(c, "alice", r["action_id"], lambda: None)["state"] == "succeeded"
    assert c.writes[0][2]["calendar_event"]["context_code"] == "user_7"
    assert c.writes[0][2]["calendar_event"]["all_day"] is False
    c, store = StudentCanvas(), ActionStore()
    r = store.prepare(c, "alice", {"kind": "calendar_update", "event_id": 12, "title": "New title"})
    assert store.execute(c, "alice", r["action_id"], lambda: None)["state"] == "succeeded"
    assert c.writes[0][2] == {"calendar_event": {"title": "New title"}, "which": "one"}
    assert c.event["description"] == "Plan"


def test_partial_calendar_date_patch_must_not_reverse_current_range():
    c = StudentCanvas()
    with pytest.raises(ValueError, match="reversed"):
        ActionStore().prepare(c, "alice", {"kind": "calendar_update", "event_id": 12, "start_at": "2026-11-03T18:00:00Z"})


@pytest.mark.parametrize("status,state", [(404, "succeeded"), (403, "uncertain"), (500, "uncertain")])
def test_calendar_delete_only_accepts_gone_or_verified_deleted(status, state):
    c, store = StudentCanvas(), ActionStore()
    c.delete_status = status
    r = store.prepare(c, "alice", {"kind": "calendar_delete", "event_id": 12})
    assert store.execute(c, "alice", r["action_id"], lambda: None)["state"] == state


def test_all_day_edit_is_rejected_but_own_single_event_delete_allowed():
    c, store = StudentCanvas(), ActionStore()
    c.event["all_day"] = True
    with pytest.raises(ValueError, match="timed"):
        store.prepare(c, "alice", {"kind": "calendar_update", "event_id": 12, "title": "Changed"})
    r = store.prepare(c, "alice", {"kind": "calendar_delete", "event_id": 12})
    assert store.execute(c, "alice", r["action_id"], lambda: None)["state"] == "succeeded"


@pytest.mark.parametrize("payload", [
    {"kind": "discussion_edit", **DISCUSSION, "entry_id": "../../1", "message": "x"},
    {"kind": "discussion_rate", **DISCUSSION, "entry_id": 5, "rating": 2},
    {"kind": "discussion_read", **DISCUSSION, "read": "false"},
    {"kind": "submission_comment_delete", **COMMENT, "user_id": 8},
    {**CREATE, "context_code": "course_1"},
    {**CREATE, "start_at": "2026-11-04T15:00:00"},
    {**CREATE, "end_at": "2026-11-01T15:00:00Z"},
    {**CREATE, "time_zone_edited": "Invalid/Zone"},
    {"kind": "calendar_update", "event_id": 12},
    {"kind": "calendar_delete", "event_id": 12, "which": "all"},
])
def test_actions_reject_unbounded_or_ambiguous_payloads(payload):
    with pytest.raises(ValidationError):
        ACTION_ADAPTER.validate_python(payload)


def test_scope_denial_and_canvas_forbidden_are_never_reported_as_success():
    c, store = StudentCanvas(), ActionStore()
    r = store.prepare(c, "alice", CASES[0][0])
    def deny(): raise PermissionError("read-only")
    with pytest.raises(PermissionError):
        store.execute(c, "alice", r["action_id"], deny)
    assert not c.writes
    c.write_error = CanvasError("forbidden", status=403)
    assert store.execute(c, "alice", r["action_id"], lambda: None)["state"] == "uncertain"


def test_durable_student_action_survives_storage_restart(tmp_path):
    from cryptography.fernet import Fernet
    from canvas_connector.storage import Store
    path, key = tmp_path / "actions.sqlite", Fernet.generate_key().decode()
    c = StudentCanvas()
    first = Store(path, key)
    r = ActionStore(persistence=first).prepare(c, "alice", CASES[0][0])
    first.close()
    second = Store(path, key)
    result = ActionStore(persistence=second).execute(c, "alice", r["action_id"], lambda: None)
    assert result["state"] == "succeeded"
    second.close()


@pytest.mark.parametrize("payload,method,suffix,route", CASES)
def test_real_canvas_transport_and_model_contract(payload, method, suffix, route):
    import json
    import httpx
    from canvas_py import Canvas
    from canvas_connector.student_actions import StudentRequest

    state = StudentCanvas()
    def handler(request):
        assert request.url.host == "school.example"
        assert request.headers["authorization"] == "Bearer synthetic-token"
        path = request.url.path.removeprefix("/api/v1/")
        if request.method == "GET" and path == "courses/1/discussion_topics/4":
            return httpx.Response(200, json=state.topic)
        if request.method == "GET" and path == "courses/1/assignments/2/submissions/self":
            assert "submission_comments" in request.url.params.get_list("include[]")
            return httpx.Response(200, json=state.submission)
        query = [(key, int(value) if key == "ids[]" else value) for key, value in request.url.params.multi_items()]
        value = state.send(StudentRequest(path, method=request.method,
            body=json.loads(request.content) if request.content else None, query=query))
        return httpx.Response(204) if value is None else httpx.Response(200, json=value)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with Canvas(state.config, http_client=http) as canvas:
            store = ActionStore()
            r = store.prepare(canvas, "alice", payload)
            result = store.execute(canvas, "alice", r["action_id"], lambda: None)
            assert result["state"] == "succeeded", result
    assert len(state.writes) == 1
