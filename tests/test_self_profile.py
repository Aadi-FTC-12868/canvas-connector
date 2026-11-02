"""Native self profile HTTP and prepared-action boundaries, with no live writes."""
import asyncio
import copy
import json

import httpx
import pytest
from canvas_py import Canvas
from canvas_py._config import CanvasConfig
from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from canvas_connector import self_profile as p
from canvas_connector.actions import ActionStore
from canvas_connector.tools import register_tools


NAMES = {"kind": "self_names_set", "name": "Alex Rivera", "short_name": "Alex", "sortable_name": "Rivera, Alex"}
SET = {"kind": "self_setting_set", "setting": "manual_mark_as_read", "enabled": True}


class API:
    def __init__(self):
        self.calls = []
        names = {"id": 7, "name": "Old Name", "short_name": "Old", "sortable_name": "Name, Old"}
        self.profile = {**names, "title": "Student", "bio": "A short bio.", "pronunciation": None,
            "pronouns": "they/them", "time_zone": "America/Chicago", "locale": None, "effective_locale": "en",
            "calendar": {"ics": "https://school.example/feeds/calendars/PRIVATE_FEED.ics"},
            "lti_user_id": "PRIVATE_LTI", "sis_user_id": "PRIVATE_SIS", "sis_login_id": "PRIVATE_LOGIN",
            "integration_id": "PRIVATE_INTEGRATION", "login_id": "PRIVATE_LOGIN", "primary_email": "student@school.example",
            "avatar_url": "https://cdn.example/opaque/PRIVATE_AVATAR", "uuid": "PRIVATE_UUID"}
        self.user = {**names, "created_at": "2020-01-01T00:00:00Z", "locale": None, "effective_locale": "en",
            "pronouns": "they/them", "permissions": {"can_update_name": True, "can_update_avatar": True},
            "email": "PRIVATE_EMAIL", "lti_id": "PRIVATE_LTI", "avatar_url": "https://cdn.example/PRIVATE_AVATAR"}
        self.settings = {key: False for key in p.SETTINGS}
        self.settings.update(comment_library_suggestions_enabled=True, elementary_dashboard_disabled=False,
            default_to_block_editor=False, widget_dashboard_user_preference=True, widget_dashboard_dark_mode=False)
        self.skip_update = False
        self.after_write = lambda: None
        self.status = None
        self.ack = None
        self.hide_after = False
        self.denied_reads = set()

    @property
    def writes(self):
        return [request for request in self.calls if request.method == "PUT"]

    def handle(self, request):
        self.calls.append(request)
        assert request.url.host == "school.example"
        assert request.headers["authorization"] == "Bearer synthetic-token"
        assert not request.url.query  # Includes never request mobile keys or extra identifiers.
        path = request.url.path
        if request.method == "GET":
            if path in self.denied_reads or (self.hide_after and self.writes):
                return httpx.Response(403, json={"error": "denied"})
            rows = {"/api/v1/users/self/profile": self.profile, "/api/v1/users/self": self.user,
                    "/api/v1/users/self/settings": self.settings}
            if path in rows:
                return httpx.Response(200, json=copy.deepcopy(rows[path]))
        if request.method == "PUT":
            if self.status is not None:
                return httpx.Response(self.status, json={"error": "rate limit" if self.status in {403, 429} else "failed"},
                    headers={"location": "https://school.example/replay", "retry-after": "0"})
            body = json.loads(request.content)
            assert request.headers["content-type"] == "application/json"
            if path == "/api/v1/users/self":
                assert set(body) == {"user", "override_sis_stickiness"}
                assert body["override_sis_stickiness"] is True
                assert set(body["user"]) == {"name", "short_name", "sortable_name"}
                if not self.skip_update:
                    self.profile.update(body["user"])
                    self.user.update(body["user"])
                result = copy.deepcopy(self.user)
            elif path == "/api/v1/users/self/settings":
                assert len(body) == 1 and set(body) <= set(p.SETTINGS)
                assert all(type(value) is bool for value in body.values())
                if not self.skip_update:
                    self.settings.update(body)
                # An optimistic acknowledgement is deliberately independent of
                # persisted state so tests cannot mistake the response for proof.
                result = {**self.settings, **body}
            else:
                raise AssertionError(f"Unexpected write {path}")
            self.after_write()
            return httpx.Response(200, json=result if self.ack is None else self.ack)
        raise AssertionError(f"Unexpected {request.method} {path}")


@pytest.fixture
def api():
    instance = API()
    with Canvas(CanvasConfig("https://school.example", "synthetic-token"),
                http_client=httpx.Client(transport=httpx.MockTransport(instance.handle))) as canvas:
        instance.canvas = canvas
        yield instance


def execute(api, action):
    store = ActionStore()
    prepared = store.prepare(api.canvas, "alice", action)
    return store, prepared, store.execute(api.canvas, "alice", prepared["action_id"], lambda: None)


def test_selected_profile_includes_primary_email_but_excludes_feed_identifiers_and_avatar(api):
    result = p.read_profile(api.canvas)
    assert result["data"]["user_id"] == 7
    assert result["data"]["name"] == "Old Name"
    assert result["data"]["time_zone"] == "America/Chicago"
    assert result["data"]["locale"] is None
    assert result["data"]["primary_email"] == "student@school.example"
    assert "email" not in result["data"]
    assert result["data"]["permissions"] == {"can_update_name": True, "can_update_avatar": True}
    assert result["coverage"] == {"complete": True, "scope": "selected_self_profile_fields"}
    assert "PRIVATE_" not in json.dumps(result)
    assert not api.writes
    assert [request.url.path for request in api.calls] == ["/api/v1/users/self/profile", "/api/v1/users/self"]


@pytest.mark.parametrize("primary_email", [None, "student@school.example"])
def test_primary_email_is_preserved_by_reviewed_name_changes(api, primary_email):
    api.profile["primary_email"] = primary_email
    _, prepared, result = execute(api, NAMES)
    assert prepared["review"]["target"]["profile"]["primary_email"] == primary_email
    assert result["state"] == "succeeded"
    assert api.profile["primary_email"] == primary_email
    assert "primary_email" not in json.loads(api.writes[0].content)["user"]


def test_missing_primary_email_remains_missing_in_profile(api):
    del api.profile["primary_email"]
    assert "primary_email" not in p.read_profile(api.canvas)["data"]


def test_settings_reader_reports_only_selected_five_without_changing_markers(api):
    before = copy.deepcopy(api.settings)
    result = p.read_settings(api.canvas)
    assert result["data"] == {"user_id": 7, "settings": {key: False for key in p.SETTINGS}}
    assert result["coverage"]["complete"]
    assert api.settings == before and not api.writes


@pytest.mark.parametrize("permissions", [None, {}, {"can_update_name": False}, {"can_update_name": 1}, {"can_update_name": "true"}])
def test_unknown_name_permission_remains_unknown_but_cannot_authorize_rename(api, permissions):
    api.user["permissions"] = permissions
    value = p.read_profile(api.canvas)["data"]["permissions"]["can_update_name"]
    assert value is (False if permissions == {"can_update_name": False} else None)
    with pytest.raises(ValueError, match="explicitly permit"):
        p.snapshot(api.canvas, NAMES)
    # Rename policy does not invent a restriction on native self preferences.
    assert p.snapshot(api.canvas, SET)["setting"] == "manual_mark_as_read"
    assert not api.writes


@pytest.mark.parametrize("row,changes", [
    ("profile", {"id": True}), ("profile", {"id": "7"}), ("user", {"id": 8}),
    ("user", {"id": None}), ("user", {"short_name": "Different"}),
    ("profile", {"name": None}), ("profile", {"bio": {"url": "PRIVATE_DATA"}}),
    ("profile", {"bio": "x" * 32769}), ("user", {"locale": "de"}),
    ("profile", {"primary_email": {"address": "student@school.example"}}),
])
def test_missing_or_conflicting_profile_identity_blocks_reads_and_prepare(api, row, changes):
    getattr(api, row).update(changes)
    with pytest.raises(ValueError):
        p.read_profile(api.canvas)
    with pytest.raises(ValueError):
        p.snapshot(api.canvas, NAMES)
    assert not api.writes


@pytest.mark.parametrize("change", [
    {"user_id": 9}, {"name": None}, {"name": 4}, {"name": ""}, {"name": "  "},
    {"name": "x" * 256}, {"short_name": "Line\nBreak"}, {"sortable_name": "A\x00B"},
    {"name": "A\x85B"}, {"name": "\ud800"}, {"override_sis_stickiness": False},
    {"bio": "New bio"}, {"locale": "en"}, {"time_zone": "America/Chicago"}, {"name": "User"},
])
def test_names_are_exact_strict_three_fields_with_no_arbitrary_update(api, change):
    with pytest.raises(ValidationError):
        p._ADAPTER.validate_python({**NAMES, **change})
    assert not api.calls


@pytest.mark.parametrize("missing", ["name", "short_name", "sortable_name"])
def test_all_three_names_are_required_to_avoid_automatic_recalculation(api, missing):
    payload = {key: value for key, value in NAMES.items() if key != missing}
    with pytest.raises(ValidationError):
        p._ADAPTER.validate_python(payload)
    assert not api.calls


@pytest.mark.parametrize("change", [
    {"enabled": "true"}, {"enabled": 1}, {"enabled": None}, {"setting": "default_to_block_editor"},
    {"setting": "mobile_settings"}, {"user_id": 9}, {"settings": {"manual_mark_as_read": True}},
])
def test_settings_model_is_one_allowlisted_strict_boolean(api, change):
    with pytest.raises(ValidationError):
        p._ADAPTER.validate_python({**SET, **change})
    assert not api.calls


@pytest.mark.parametrize("action", [NAMES, {**NAMES, "name": "User", "short_name": "User"},
    {**NAMES, "name": " Élise O’Connor ", "short_name": "E\u0301lise", "sortable_name": "O’Connor, Élise"}])
def test_names_exact_readback_review_sis_default_and_no_receipt_replay(api, action):
    store, prepared, result = execute(api, action)
    assert prepared["execute"]["tool"] == "canvas_update_profile"
    assert prepared["review"]["target"]["override_sis_stickiness"] is True
    assert "across Canvas" in prepared["review"]["target"]["effect"]
    assert "PRIVATE_" not in json.dumps(prepared)
    assert result["state"] == "succeeded"
    assert result["result"]["other_selected_profile_fields_unchanged"]
    for key in ("name", "short_name", "sortable_name"):
        assert result["result"][key] == action[key]
    assert len(api.writes) == 1
    assert store.execute(api.canvas, "alice", prepared["action_id"], lambda: None) == result
    assert store.prepare(api.canvas, "alice", action)["action_id"] == prepared["action_id"]
    assert len(api.writes) == 1


@pytest.mark.parametrize("setting", p.SETTINGS)
@pytest.mark.parametrize("enabled", [True, False])
def test_each_setting_exact_top_level_wire_and_other_preferences_preserved(api, setting, enabled):
    api.settings[setting] = not enabled
    before = copy.deepcopy(api.settings)
    store, prepared, result = execute(api, {**SET, "setting": setting, "enabled": enabled})
    assert prepared["execute"]["tool"] == "canvas_update_user_settings"
    assert result["state"] == "succeeded" and result["result"]["enabled"] is enabled
    assert result["result"]["other_returned_boolean_settings_unchanged"]
    assert api.settings == {**before, setting: enabled}
    assert json.loads(api.writes[0].content) == {setting: enabled}
    assert store.execute(api.canvas, "alice", prepared["action_id"], lambda: None) == result
    assert len(api.writes) == 1


@pytest.mark.parametrize("where,key,value", [
    ("profile", "bio", "Changed bio"), ("user", "created_at", "2021-01-01T00:00:00Z"),
    ("user", "permissions", {"can_update_name": False}), ("profile", "id", 8),
    ("user", "updated_at", "2026-02-01T00:00:00Z"),
])
def test_stale_profile_and_permission_reject_before_write(api, where, key, value):
    store = ActionStore()
    prepared = store.prepare(api.canvas, "alice", NAMES)
    getattr(api, where)[key] = value
    result = store.execute(api.canvas, "alice", prepared["action_id"], lambda: None)
    assert result["state"] == "rejected" and not api.writes


@pytest.mark.parametrize("key", ["manual_mark_as_read", "collapse_course_nav", "widget_dashboard_dark_mode"])
def test_stale_selected_or_other_native_setting_rejects_before_write(api, key):
    store = ActionStore()
    prepared = store.prepare(api.canvas, "alice", SET)
    api.settings[key] = not api.settings[key]
    result = store.execute(api.canvas, "alice", prepared["action_id"], lambda: None)
    assert result["state"] == "rejected" and not api.writes


@pytest.mark.parametrize("action", [NAMES, SET])
def test_ignored_write_with_optimistic_ack_is_uncertain_and_never_replayed(api, action):
    api.skip_update = True
    store, prepared, result = execute(api, action)
    assert result["state"] == "uncertain" and not result["result"]["verified"]
    assert store.execute(api.canvas, "alice", prepared["action_id"], lambda: None) == result
    assert len(api.writes) == 1


@pytest.mark.parametrize("where,key,value", [
    ("profile", "name", "Normalized Name"), ("profile", "short_name", "Old"),
    ("profile", "bio", "Concurrent bio"), ("user", "created_at", "2022-01-01T00:00:00Z"),
    ("profile", "primary_email", "changed@school.example"),
    ("user", "id", 8), ("user", "permissions", {"can_update_name": False}),
])
def test_partial_names_normalization_identity_or_unreviewed_change_is_uncertain(api, where, key, value):
    def changed():
        getattr(api, where)[key] = value
        if key in {"name", "short_name"}:
            api.user[key] = value
    api.after_write = changed
    _, _, result = execute(api, NAMES)
    assert result["state"] == "uncertain" and len(api.writes) == 1


def test_changed_mutable_timestamp_is_not_a_false_failure(api):
    api.user["updated_at"] = "2026-01-01T00:00:00Z"
    api.after_write = lambda: api.user.update(updated_at="2026-01-02T00:00:00Z")
    _, _, result = execute(api, NAMES)
    assert result["state"] == "succeeded"


@pytest.mark.parametrize("key", ["collapse_global_nav", "widget_dashboard_dark_mode", "new_native_preference"])
def test_settings_postwrite_other_boolean_change_or_new_field_is_uncertain(api, key):
    api.after_write = lambda: api.settings.update({key: True})
    _, _, result = execute(api, SET)
    assert result["state"] == "uncertain" and len(api.writes) == 1


@pytest.mark.parametrize("action", [NAMES, SET])
@pytest.mark.parametrize("status", [302, 307, 308, 403, 429, 500])
def test_mutations_do_not_redirect_or_retry(api, action, status):
    api.status = status
    _, _, result = execute(api, action)
    assert result["state"] == "uncertain"
    assert len(api.writes) == 1
    assert all(request.url.path != "/replay" for request in api.calls)


@pytest.mark.parametrize("action", [NAMES, SET])
def test_postwrite_read_denial_is_uncertain(api, action):
    api.hide_after = True
    _, _, result = execute(api, action)
    assert result["state"] == "uncertain" and len(api.writes) == 1


@pytest.mark.parametrize("settings", [{}, {key: False for key in p.SETTINGS if key != "manual_mark_as_read"},
    {**{key: False for key in p.SETTINGS}, "manual_mark_as_read": "false"},
    {**{key: False for key in p.SETTINGS}, "pendo_mobile_student_api_key": "PRIVATE_API_KEY"}])
def test_missing_nonboolean_or_private_mobile_settings_are_unavailable(api, settings):
    api.settings = settings
    with pytest.raises(ValueError, match="boolean settings"):
        p.read_settings(api.canvas)
    with pytest.raises(ValueError, match="boolean settings"):
        p.snapshot(api.canvas, SET)
    assert not api.writes


def test_name_reply_wrong_actor_is_not_accepted(api):
    api.ack = {"id": 8}
    _, _, result = execute(api, NAMES)
    assert result["state"] == "uncertain" and len(api.writes) == 1


@pytest.mark.parametrize("action,route,wrong", [(NAMES, "canvas_update_profile", "canvas_update_user_settings"),
    (SET, "canvas_update_user_settings", "canvas_update_profile")])
def test_actual_mcp_profile_settings_prepare_route_execute_and_receipt(api, action, route, wrong):
    mcp = FastMCP("profile-test")
    register_tools(mcp, lambda: api.canvas, lambda: None, owner=lambda: "alice")

    async def check():
        listed = {tool.name: tool for tool in await mcp.list_tools()}
        assert listed["canvas_profile"].annotations.readOnlyHint
        assert listed["canvas_user_settings"].annotations.readOnlyHint
        assert not listed[route].annotations.readOnlyHint
        for read in ("canvas_profile", "canvas_user_settings"):
            result = (await mcp.call_tool(read, {}))[1]
            assert result["status"] == "ok" and result["coverage"]["complete"]
            assert "PRIVATE_" not in json.dumps(result)
        prepared = (await mcp.call_tool("canvas_prepare_action", {"action": action}))[1]["data"]
        assert not api.writes
        refused = (await mcp.call_tool(wrong, {"action_id": prepared["action_id"]}))[1]
        assert refused["status"] == "invalid_request" and not api.writes
        result = (await mcp.call_tool(route, {"action_id": prepared["action_id"]}))[1]
        assert result["status"] == "succeeded" and result["data"]["result"]["verified"]
        replay = (await mcp.call_tool(route, {"action_id": prepared["action_id"]}))[1]
        assert replay["data"] == result["data"] and len(api.writes) == 1

    asyncio.run(check())


def test_actual_mcp_profile_forbidden_is_not_an_empty_complete_profile(api):
    api.denied_reads.add("/api/v1/users/self")
    mcp = FastMCP("profile-denied")
    register_tools(mcp, lambda: api.canvas, lambda: None)

    async def check():
        result = (await mcp.call_tool("canvas_profile", {}))[1]
        assert result["status"] == "forbidden" and result["coverage"]["complete"] is False
        assert not api.writes

    asyncio.run(check())
