"""Synthetic current-account preference tests. Never write to real Canvas."""
import copy
import json
from typing import Annotated
from unittest.mock import Mock

import httpx
import pytest
from canvas_py import Canvas
from canvas_py._config import CanvasConfig
from canvas_py._errors import CanvasError
from canvas_py.models import Course
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from canvas_connector import preferences as pref
from canvas_connector.actions import ActionStore


ADAPTER = TypeAdapter(Annotated[pref.PreferenceActionTypes, Field(discriminator="kind")])
NOTIFY = {"kind": "notification_preference_update", "channel_id": 2,
          "notification": "new_announcement", "frequency": "weekly"}
CASES = [
    (NOTIFY, "PUT", "users/self/communication_channels/2/notification_preferences/new_announcement",
     {"notification_preferences": {"frequency": "weekly"}}),
    ({"kind": "course_favorite_add", "course_id": 1}, "POST", "users/self/favorites/courses/1", None),
    ({"kind": "course_favorite_remove", "course_id": 1}, "DELETE", "users/self/favorites/courses/1", None),
    ({"kind": "group_favorite_add", "group_id": 3}, "POST", "users/self/favorites/groups/3", None),
    ({"kind": "group_favorite_remove", "group_id": 3}, "DELETE", "users/self/favorites/groups/3", None),
    ({"kind": "course_nickname_set", "course_id": 1, "nickname": "Physics"}, "PUT", "users/self/course_nicknames/1", {"nickname": "Physics"}),
    ({"kind": "course_nickname_delete", "course_id": 1}, "DELETE", "users/self/course_nicknames/1", None),
    ({"kind": "course_color_set", "course_id": 1, "hexcode": "#ABC123"}, "PUT", "users/self/colors/course_1", {"hexcode": "#abc123"}),
]


class NativeObject(BaseModel):
    model_config = ConfigDict(extra="allow")


class PreferencesCanvas:
    def __init__(self):
        self.config = CanvasConfig("https://school.example", "synthetic-token")
        self.navigation = Mock()
        self.profile = {"id": 7}
        self.channels = [{"id": 2, "user_id": 7, "type": "email", "address": "student@example.test", "workflow_state": "active"}]
        self.preferences = [
            {"notification": "new_announcement", "category": "announcement", "frequency": "daily"},
            {"notification": "submission_graded", "category": "grading", "frequency": "immediately"}]
        self.course = {"id": 1, "name": "Science", "workflow_state": "available", "is_favorite": False}
        self.group = {"id": 3, "name": "Study group", "concluded": False, "non_collaborative": False, "is_favorite": False}
        self.membership = {"id": 10, "group_id": 3, "user_id": 7, "workflow_state": "accepted"}
        self.enrollments = [{"id": 11, "user_id": 7, "course_id": 1, "type": "StudentEnrollment", "enrollment_state": "active"}]
        self.nickname = {"course_id": 1, "name": "Science", "nickname": "Old nickname"}
        self.colors = {"course_1": "#111111", "course_9": "#999999", "group_3": "#333333"}
        self.requests, self.writes = [], []
        self.ignore_write = False
        self.after_write = None
        self.readback_error = None
        self.native = False

    def paginate(self, request):
        yield from self.send(request)

    def send(self, request):
        method, path, body, query = request.method, request.path, request.json_body(), request.query_pairs()
        self.requests.append((method, path, body, query))
        if method != "GET":
            self.writes.append((method, path, body))
            if not self.ignore_write:
                if "/notification_preferences/" in path:
                    self.preferences[0]["frequency"] = body["notification_preferences"]["frequency"]
                elif "/favorites/courses/" in path:
                    self.course["is_favorite"] = method == "POST"
                elif "/favorites/groups/" in path:
                    self.group["is_favorite"] = method == "POST"
                elif "/course_nicknames/" in path:
                    self.nickname["nickname"] = body["nickname"] if method == "PUT" else None
                elif "/colors/course_" in path:
                    self.colors["course_1"] = body["hexcode"]
                else:
                    raise AssertionError((method, path, body))
            if self.after_write:
                self.after_write(self)
            return {"success": True}  # Deliberately unusable as verification.
        assert body is None
        if self.writes and self.readback_error:
            raise self.readback_error
        if path == "users/self/profile":
            result = self.profile
        elif path == "users/self/communication_channels":
            result = self.channels
        elif path == "users/self/communication_channels/2/notification_preferences":
            result = {"notification_preferences": self.preferences}
        elif path == "courses/1":
            assert ("include[]", "favorites") in query
            result = self.course
        elif path == "courses/1/enrollments":
            assert ("user_id", 7) in query
            result = self.enrollments
        elif path == "groups/3":
            assert ("include[]", "favorites") in query
            result = self.group
        elif path == "groups/3/users/self":
            result = self.membership
        elif path == "users/self/course_nicknames/1":
            result = self.nickname
        elif path == "users/self/course_nicknames":
            result = [self.nickname]
        elif path == "users/self/colors":
            result = {"custom_colors": self.colors}
        elif path == "users/self/favorites/courses":
            result = [self.course]  # May be a default, even when not explicit.
        elif path == "users/self/favorites/groups":
            result = [self.group]
        else:
            raise AssertionError((method, path, body, query))
        result = copy.deepcopy(result)
        if self.native:
            if path == "courses/1":
                return Course.model_validate(result)
            if isinstance(result, list):
                return [NativeObject.model_validate(v) for v in result]
            return NativeObject.model_validate(result)
        return result


@pytest.mark.parametrize("payload,method,path,body", CASES)
@pytest.mark.parametrize("native", [False, True])
def test_exact_single_write_and_independent_readback(payload, method, path, body, native):
    canvas = PreferencesCanvas()
    canvas.native = native
    if payload["kind"].endswith("remove"):
        canvas.course["is_favorite"] = canvas.group["is_favorite"] = True
    target = pref.snapshot(canvas, payload)
    assert target["user_id"] == 7
    assert not canvas.writes
    result = pref.perform(canvas, payload, target)
    assert result["verified"] is True
    assert canvas.writes == [(method, path, body)]
    assert canvas.requests[-1][0] == "GET"
    assert pref.ROUTES[payload["kind"]] == ("canvas_update_notifications" if payload == NOTIFY else "canvas_update_preferences")
    assert canvas.preferences[1]["frequency"] == "immediately"
    assert canvas.colors["course_9"] == "#999999"


@pytest.mark.parametrize("payload,method,path,body", CASES)
def test_success_response_without_saved_state_is_not_verified(payload, method, path, body):
    canvas = PreferencesCanvas()
    if payload["kind"].endswith("remove"):
        canvas.course["is_favorite"] = canvas.group["is_favorite"] = True
    target = pref.snapshot(canvas, payload)
    canvas.ignore_write = True
    with pytest.raises(ValueError, match="not confirmed"):
        pref.perform(canvas, payload, target)
    assert len(canvas.writes) == 1


@pytest.mark.parametrize("value", [True, 0, -1, "1", 1.0, 2**63, None])
@pytest.mark.parametrize("payload,key", [(NOTIFY, "channel_id"), (CASES[1][0], "course_id"), (CASES[3][0], "group_id")])
def test_strict_bounded_ids(payload, key, value):
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({**payload, key: value})


@pytest.mark.parametrize("extra", [{"user_id": 9}, {"as_user_id": 9}, {"category": "announcement"},
    {"url": "https://evil.example"}, {"preferences": {}}, {"notification_preferences": {}}])
def test_no_arbitrary_user_url_category_or_settings_dictionary(extra):
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({**NOTIFY, **extra})


@pytest.mark.parametrize("name", ["../other", "a/b", "a?x=1", "a#x", "New Announcement", "", "a\n", "a%2fb"])
def test_notification_must_be_exact_safe_name(name):
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({**NOTIFY, "notification": name})


@pytest.mark.parametrize("frequency", ["hourly", "off", "Never", None, True, 1])
def test_frequency_enum(frequency):
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({**NOTIFY, "frequency": frequency})


@pytest.mark.parametrize("name", ["", " ", "a" * 60, "has\nnewline", "nul\x00value"])
def test_nickname_constraints(name):
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({"kind": "course_nickname_set", "course_id": 1, "nickname": name})


@pytest.mark.parametrize("color", ["red", "#abc", "abc123", "#fffffff", "#zzzzzz", None])
def test_color_is_exact_six_digit_hex(color):
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({"kind": "course_color_set", "course_id": 1, "hexcode": color})


@pytest.mark.parametrize("location,change", [
    ("profile", {"id": True}), ("profile", {"id": "7"}), ("profile", {"id": None}),
    ("channel", {"user_id": 8}), ("channel", {"user_id": None}), ("channel", {"id": True}),
    ("channel", {"workflow_state": "unconfirmed"}), ("channel", {"workflow_state": None}),
    ("channel", {"type": "unknown"}), ("channel", {"locked": True}),
    ("channel", {"permissions": {"update": False}}),
    ("preference", {"notification": "other"}), ("preference", {"frequency": "unknown"}),
    ("preference", {"locked": True}), ("preference", {"read_only": None}),
    ("preference", {"communication_channel_id": 9}), ("preference", {"user_id": 9}),
    ("preference", {"permissions": {"update": False}}),
    ("preference", {"available_frequencies": ["immediately", "never"]}),
])
def test_notification_ownership_availability_and_policy_fail_closed(location, change):
    canvas = PreferencesCanvas()
    obj = canvas.channels[0] if location == "channel" else canvas.preferences[0] if location == "preference" else canvas.profile
    obj.update(change)
    with pytest.raises(ValueError):
        pref.snapshot(canvas, NOTIFY)
    assert not canvas.writes


def test_missing_or_duplicate_channel_and_notification_are_rejected():
    for change in (lambda c: c.channels.clear(), lambda c: c.channels.append(copy.deepcopy(c.channels[0])),
                   lambda c: c.preferences.clear(), lambda c: c.preferences.append(copy.deepcopy(c.preferences[0]))):
        canvas = PreferencesCanvas()
        change(canvas)
        with pytest.raises(ValueError):
            pref.snapshot(canvas, NOTIFY)
        assert not canvas.writes


@pytest.mark.parametrize("location,change", [
    ("course", {"id": 9}), ("course", {"is_favorite": None}), ("course", {"is_favorite": 0}),
    ("course", {"workflow_state": "deleted"}), ("course", {"access_restricted_by_date": True}),
    ("course", {"permissions": {"read": False}}),
    ("enrollment", {"user_id": 9}), ("enrollment", {"user_id": None}),
    ("enrollment", {"course_id": 9}), ("enrollment", {"enrollment_state": "invited"}),
])
def test_course_favorite_requires_real_current_enrollment(location, change):
    canvas = PreferencesCanvas()
    (canvas.enrollments[0] if location == "enrollment" else canvas.course).update(change)
    with pytest.raises(ValueError):
        pref.snapshot(canvas, CASES[1][0])
    assert not canvas.writes


@pytest.mark.parametrize("location,change", [
    ("group", {"id": 9}), ("group", {"is_favorite": None}), ("group", {"concluded": True}),
    ("group", {"workflow_state": "deleted"}),
    ("group", {"non_collaborative": True}), ("group", {"non_collaborative": None}),
    ("membership", {"user_id": 9}), ("membership", {"user_id": None}),
    ("membership", {"group_id": 9}), ("membership", {"workflow_state": "invited"}),
])
def test_group_favorite_requires_own_accepted_membership(location, change):
    canvas = PreferencesCanvas()
    getattr(canvas, location).update(change)
    with pytest.raises(ValueError):
        pref.snapshot(canvas, CASES[3][0])
    assert not canvas.writes


def test_no_enrollment_blocks_nickname_and_color_too():
    for payload in [CASES[1][0], CASES[5][0], CASES[6][0], CASES[7][0]]:
        canvas = PreferencesCanvas()
        canvas.enrollments = []
        with pytest.raises(ValueError, match="enrollment"):
            pref.snapshot(canvas, payload)
        assert not canvas.writes


def test_never_has_explicit_suppression_warning():
    target = pref.snapshot(PreferencesCanvas(), {**NOTIFY, "frequency": "never"})
    assert "disables this notification" in target["warning"]
    assert "miss course updates" in target["warning"]


@pytest.mark.parametrize("payload,change", [
    (NOTIFY, lambda c: c.preferences[1].update(frequency="never")),
    (NOTIFY, lambda c: c.preferences[0].update(notification="wrong_notification")),
    (NOTIFY, lambda c: c.preferences[0].update(communication_channel_id=9)),
    (CASES[1][0], lambda c: c.course.update(id=9)),
    (CASES[3][0], lambda c: c.group.update(id=9)),
    (CASES[5][0], lambda c: c.nickname.update(course_id=9)),
    (CASES[7][0], lambda c: c.colors.update(course_9="#000000")),
])
def test_wrong_readback_or_unrelated_changes_are_not_verified(payload, change):
    canvas = PreferencesCanvas()
    target = pref.snapshot(canvas, payload)
    canvas.after_write = change
    with pytest.raises(ValueError):
        pref.perform(canvas, payload, target)
    assert len(canvas.writes) == 1


@pytest.mark.parametrize("payload,method,path,body", CASES)
def test_readback_error_propagates_without_repeating_write(payload, method, path, body):
    canvas = PreferencesCanvas()
    target = pref.snapshot(canvas, payload)
    canvas.readback_error = CanvasError("synthetic read error", status=503)
    with pytest.raises(CanvasError):
        pref.perform(canvas, payload, target)
    assert len(canvas.writes) == 1


@pytest.mark.parametrize("reader,args,key", [
    (pref.communication_channels, {}, "communication_channels"),
    (pref.notification_preferences, {"channel_id": 2}, "notification_preferences"),
    (pref.favorites, {}, "favorites"),
    (pref.favorites, {"context_type": "groups"}, "favorites"),
    (pref.course_nicknames, {}, "course_nicknames"),
    (pref.course_colors, {}, "course_colors"),
])
def test_read_helpers_are_bounded_and_issue_gets_only(reader, args, key):
    canvas = PreferencesCanvas()
    before = copy.deepcopy((canvas.preferences, canvas.colors))
    result = reader(canvas, **args, limit=1)
    assert result["data"]["user_id"] == 7
    assert len(result["data"][key]) == 1
    assert (canvas.preferences, canvas.colors) == before
    assert not canvas.writes
    assert all(method == "GET" for method, *_ in canvas.requests)
    assert result["source"]["checked_at"]
    assert "complete" in result["coverage"]


def test_push_address_is_not_disclosed_and_default_favorites_are_not_asserted():
    canvas = PreferencesCanvas()
    canvas.channels[0].update(type="push", address="synthetic-device-token")
    assert "address" not in pref.communication_channels(canvas)["data"]["communication_channels"][0]
    result = pref.favorites(canvas)
    assert result["data"]["favorites"][0]["is_favorite"] is False
    assert "default selections" in result["data"]["selection_note"]


def test_discovery_is_bounded_and_never_treats_error_as_empty():
    canvas = PreferencesCanvas()
    canvas.channels *= pref._MAX_DISCOVERY + 1
    with pytest.raises(ValueError, match="safe limit"):
        pref.snapshot(canvas, NOTIFY)
    canvas = PreferencesCanvas()
    canvas.send = Mock(side_effect=CanvasError("denied", status=403))
    with pytest.raises(CanvasError):
        pref.communication_channels(canvas)


def test_http_channel_discovery_follows_pages_and_reports_window_coverage():
    state = PreferencesCanvas()
    seen = []
    first = {**state.channels[0], "id": 5}

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=state.profile)
        assert request.method == "GET"
        assert request.url.path.endswith("/communication_channels")
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=state.channels)
        return httpx.Response(200, json=[first], headers={
            "Link": '<https://school.example/api/v1/users/self/communication_channels?page=2>; rel="next"'})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with Canvas(CanvasConfig("https://school.example", "synthetic-token"), http_client=http) as canvas:
            result = pref.communication_channels(canvas, limit=1)
            assert result["coverage"]["complete"] is False
            assert result["coverage"]["next_offset"] == 1
            assert result["data"]["communication_channels"][0]["id"] == 5
            assert pref._channel(canvas, 7, 2)["id"] == 2
    assert any(request.url.params.get("page") == "2" for request in seen)


def test_malformed_http_list_fails_closed():
    def handler(request):
        return httpx.Response(200, json={"id": 7} if request.url.path.endswith("/profile") else {"not": "a list"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with Canvas(CanvasConfig("https://school.example", "synthetic-token"), http_client=http) as canvas:
            with pytest.raises(ValueError, match="object list"):
                pref.communication_channels(canvas)


@pytest.mark.parametrize("payload,key", [(NOTIFY, "channel_id"), (CASES[1][0], "course_id"), (CASES[3][0], "group_id")])
def test_prepared_target_cannot_change_id(payload, key):
    canvas = PreferencesCanvas()
    target = pref.snapshot(canvas, payload)
    target[key] = 99
    with pytest.raises(ValueError):
        pref.perform(canvas, payload, target)
    assert not canvas.writes


def test_missing_nickname_is_known_null_and_color_can_be_first_custom_color():
    canvas = PreferencesCanvas()
    canvas.nickname["nickname"] = None
    with pytest.raises(ValueError, match="no nickname"):
        pref.snapshot(canvas, CASES[6][0])
    target = pref.snapshot(canvas, CASES[5][0])
    assert pref.perform(canvas, CASES[5][0], target)["nickname"] == "Physics"
    canvas.colors.pop("course_1")
    target = pref.snapshot(canvas, CASES[7][0])
    assert target["hexcode"] is None
    assert pref.perform(canvas, CASES[7][0], target)["hexcode"] == "#abc123"


@pytest.mark.parametrize("payload,method,path,body", CASES)
def test_real_http_client_uses_fixed_paths_and_json(payload, method, path, body):
    state = PreferencesCanvas()
    if payload["kind"].endswith("remove"):
        state.course["is_favorite"] = state.group["is_favorite"] = True
    seen = []

    def handler(request):
        seen.append(request)
        assert request.headers["authorization"] == "Bearer synthetic-token"
        query = [(k, int(v) if k == "user_id" else v) for k, v in request.url.params.multi_items()]
        request_body = json.loads(request.content) if request.content else None
        bridge = pref._Request(request.url.path.removeprefix("/api/v1/"), method=request.method, body=request_body, query=query)
        return httpx.Response(200, json=state.send(bridge))

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with Canvas(CanvasConfig("https://school.example", "synthetic-token"), http_client=http) as canvas:
            target = pref.snapshot(canvas, payload)
            assert pref.perform(canvas, payload, target)["verified"] is True
    assert state.writes == [(method, path, body)]
    assert all("as_user_id" not in request.url.params for request in seen)
    assert seen[-1].method == "GET"


@pytest.mark.parametrize("payload,method,path,body", CASES)
def test_action_store_routes_and_claims_once(payload, method, path, body):
    canvas, store = PreferencesCanvas(), ActionStore()
    prepared = store.prepare(canvas, "alice", payload)
    assert prepared["execute"]["tool"] == pref.ROUTES[payload["kind"]]
    assert not canvas.writes
    result = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
    assert result["state"] == "succeeded"
    assert result["result"]["verified"] is True
    assert store.execute(canvas, "alice", prepared["action_id"], lambda: None) == result
    assert len(canvas.writes) == 1


@pytest.mark.parametrize("payload,change", [
    (NOTIFY, lambda c: c.channels[0].update(user_id=9)),
    (NOTIFY, lambda c: c.preferences[0].update(frequency="never")),
    (NOTIFY, lambda c: c.preferences[1].update(frequency="never")),
    (CASES[1][0], lambda c: c.enrollments.clear()),
    (CASES[1][0], lambda c: c.course.update(is_favorite=True)),
    (CASES[3][0], lambda c: c.membership.update(workflow_state="invited")),
    (CASES[5][0], lambda c: c.nickname.update(nickname="Changed")),
    (CASES[7][0], lambda c: c.colors.update(course_1="#000000")),
])
def test_action_store_rejects_stale_ownership_availability_and_state(payload, change):
    canvas, store = PreferencesCanvas(), ActionStore()
    prepared = store.prepare(canvas, "alice", payload)
    change(canvas)
    result = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
    assert result["state"] == "rejected"
    assert result["result"]["upstream_write_attempted"] is False
    assert not canvas.writes


def test_action_store_readback_failure_is_uncertain_not_retryable():
    canvas, store = PreferencesCanvas(), ActionStore()
    prepared = store.prepare(canvas, "alice", NOTIFY)
    canvas.ignore_write = True
    result = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
    assert result["state"] == "uncertain"
    assert result["result"]["upstream_write_attempted"] is True
    assert store.execute(canvas, "alice", prepared["action_id"], lambda: None) == result
    assert len(canvas.writes) == 1
