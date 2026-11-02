"""Selected self profile fields and narrowly reviewed personal preferences.

Contracts: https://developerdocs.instructure.com/services/canvas/resources/users
https://github.com/instructure/canvas-lms/blob/master/app/controllers/users_controller.rb
https://github.com/instructure/canvas-lms/blob/master/lib/api/v1/user_profile.rb
https://github.com/instructure/canvas-lms/blob/master/app/models/user.rb

Names use Canvas's native override_sis_stickiness=true behavior, after its
explicit can_update_name permission. Stickiness preserves previous manual edits
against SIS imports; it is not an institution's rename permission. This module
does not expose a stickiness switch or arbitrary user/profile attributes.
Locale/time zone are read-only here; accepted-value discovery and canonicalized
readback need a separate design. Avatar selection/moderation is also separate.
"""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal

from canvas_py import Canvas
from canvas_py._request import CanvasRequest
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from .responses import dump, envelope

Id = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
Name = Annotated[str, Field(strict=True, min_length=1, max_length=255)]
Setting = Literal["manual_mark_as_read", "collapse_global_nav", "collapse_course_nav",
                  "hide_dashcard_color_overlays", "release_notes_badge_disabled"]
_ID = TypeAdapter(Id)
_NAMES = ("name", "short_name", "sortable_name")
_PROFILE_FIELDS = (*_NAMES, "title", "bio", "pronunciation", "pronouns", "primary_email", "time_zone", "locale", "effective_locale")
SETTINGS = ("manual_mark_as_read", "collapse_global_nav", "collapse_course_nav",
            "hide_dashcard_color_overlays", "release_notes_badge_disabled")
_EFFECTS = {
    "manual_mark_as_read": "Choose whether Canvas requires manually marking discussion posts read. Existing read markers are not changed by this preference.",
    "collapse_global_nav": "Choose whether Canvas loads global navigation collapsed.",
    "collapse_course_nav": "Choose whether Canvas loads course navigation collapsed.",
    "hide_dashcard_color_overlays": "Choose whether course card images appear without the course color overlay.",
    "release_notes_badge_disabled": "Choose whether Canvas hides the new release notes badge.",
}


class _Action(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SelfNamesSet(_Action):
    kind: Literal["self_names_set"]
    name: Name
    short_name: Name
    sortable_name: Name

    @field_validator("name", "short_name", "sortable_name")
    @classmethod
    def visible_name(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or 127 <= ord(char) <= 159
                                    or 0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError("Names must contain visible text without control characters")
        # Preserve the reviewed spelling, spacing and Unicode exactly.
        return value

    @model_validator(mode="after")
    def native_defaults(self) -> SelfNamesSet:
        # User#infer_defaults treats this exact full-name sentinel as unset.
        if self.name == "User" and self.short_name != "User":
            raise ValueError("Canvas replaces the full name 'User' with the short name; choose an exact name Canvas can preserve")
        return self


class SelfSettingSet(_Action):
    kind: Literal["self_setting_set"]
    setting: Setting
    enabled: Annotated[bool, Field(strict=True)]


SelfProfileActionTypes = SelfNamesSet | SelfSettingSet
KINDS = ["self_names_set", "self_setting_set"]
ROUTES = {"self_names_set": "canvas_update_profile", "self_setting_set": "canvas_update_user_settings"}
_ADAPTER = TypeAdapter(Annotated[SelfProfileActionTypes, Field(discriminator="kind")])


class _Request(CanvasRequest[Any]):
    def __init__(self, path: str, *, method: str = "GET", body: dict | None = None):
        self._path, self.method, self.body = path, method, body
        self.allow_retries = method == "GET"
        self.allow_redirects = method == "GET"

    @property
    def path(self) -> str:
        return self._path

    def json_body(self) -> dict | None:
        return self.body


def _object(canvas: Canvas, request: CanvasRequest) -> dict:
    row = dump(canvas.send(request))
    if not isinstance(row, dict):
        raise ValueError("Canvas did not return the requested self profile object")
    return row


def _text_fields(row: dict, keys: tuple[str, ...]) -> dict:
    selected = {key: row[key] for key in keys if key in row}
    if any(value is not None and (not isinstance(value, str) or len(value) > 32768)
           for value in selected.values()):
        raise ValueError("Canvas did not return bounded profile text fields")
    return selected


def _self(canvas: Canvas) -> dict:
    # Profile JSON deliberately drops permissions and adds a secret calendar
    # feed capability. Never return either whole upstream object.
    profile = _object(canvas, _Request("users/self/profile"))
    actor = _ID.validate_python(profile.get("id"))
    user = _object(canvas, _Request("users/self"))
    if _ID.validate_python(user.get("id")) != actor:
        raise ValueError("Canvas returned a different self profile identity")
    for key in _NAMES:
        if (not isinstance(profile.get(key), str) or len(profile[key]) > 255
                or profile[key] != user.get(key)):
            raise ValueError("Canvas self profile names are missing or changed between reads")
    selected = _text_fields(profile, _PROFILE_FIELDS)
    for key in ("locale", "effective_locale", "pronouns"):
        if key in selected and key in user and selected[key] != user[key]:
            raise ValueError("Canvas self profile changed between reads")
    permissions = user.get("permissions")
    # Missing/invalid permission is unknown on reads, never an invented denial.
    selected_permissions = {}
    for key in ("can_update_name", "can_update_avatar"):
        value = permissions.get(key) if isinstance(permissions, dict) else None
        selected_permissions[key] = value if type(value) is bool else None
    return {"user_id": actor, "profile": selected,
            "permissions": selected_permissions,
            "user_version": _text_fields(user, ("created_at", "updated_at"))}


def _settings(canvas: Canvas) -> dict:
    # No include[]=mobile_settings: that include exposes private analytics keys.
    row = _object(canvas, _Request("users/self/settings"))
    if (not row or len(row) > 100 or any(not isinstance(key, str) or len(key) > 128
                                      or type(value) is not bool for key, value in row.items())
            or any(key not in row for key in SETTINGS)):
        raise ValueError("Canvas did not return the supported boolean settings")
    return row


def _version(settings: dict) -> str:
    return hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_profile(canvas: Canvas) -> dict:
    context = _self(canvas)
    return envelope({"user_id": context["user_id"], **context["profile"],
        "permissions": context["permissions"], "html_url": f"{canvas.config.base_url}/profile/settings"},
        origin=canvas.config.base_url, coverage={"complete": True, "scope": "selected_self_profile_fields"})


def read_settings(canvas: Canvas) -> dict:
    context, settings = _self(canvas), _settings(canvas)
    return envelope({"user_id": context["user_id"], "settings": {key: settings[key] for key in SETTINGS}},
        origin=canvas.config.base_url, coverage={"complete": True, "scope": "five_supported_self_boolean_settings"})


def snapshot(canvas: Canvas, p: dict) -> dict:
    action = _ADAPTER.validate_python(p)
    context = _self(canvas)
    if isinstance(action, SelfNamesSet):
        if context["permissions"]["can_update_name"] is not True:
            raise ValueError("Canvas must explicitly permit changing your name")
        return {**context, "override_sis_stickiness": True,
            "effect": "Update your full, display and sortable names across Canvas, including grading lists, discussions and messages. This follows Canvas's native manual-name/SIS-stickiness behavior; institution rename permission remains required."}
    settings = _settings(canvas)
    return {**context, "setting": action.setting, "settings": {key: settings[key] for key in SETTINGS},
            "settings_version": _version(settings),
            "other_settings_version": _version({key: value for key, value in settings.items() if key != action.setting}),
            "effect": _EFFECTS[action.setting]}


def _same_actor_and_version(before: dict, after: dict) -> None:
    if _ID.validate_python(before.get("user_id")) != after["user_id"]:
        raise ValueError("Canvas self identity changed during verification")
    # A write may advance updated_at when an installation returns it. The
    # immutable creation identity, selected profile and permissions must match.
    versions = [{key: value for key, value in row["user_version"].items() if key != "updated_at"}
                for row in (before, after)]
    if versions[0] != versions[1]:
        raise ValueError("Canvas self profile identity version changed during verification")


def perform(canvas: Canvas, p: dict, target: dict) -> dict:
    action = _ADAPTER.validate_python(p)
    _ID.validate_python(target.get("user_id"))
    if isinstance(action, SelfNamesSet):
        if target.get("permissions", {}).get("can_update_name") is not True or target.get("override_sis_stickiness") is not True:
            raise ValueError("Prepared self name permission or behavior is unknown")
        desired = {key: getattr(action, key) for key in _NAMES}
        ack = _object(canvas, _Request("users/self", method="PUT", body={
            "user": desired, "override_sis_stickiness": True}))
        if _ID.validate_python(ack.get("id")) != target["user_id"]:
            raise ValueError("Canvas acknowledged a different self profile")
        after = _self(canvas)
        _same_actor_and_version(target, after)
        if after["profile"] != {**target["profile"], **desired} or after["permissions"] != target["permissions"]:
            raise ValueError("Canvas did not independently confirm the exact names and unchanged selected profile fields")
        return {"verified": True, "user_id": after["user_id"], **desired,
                "other_selected_profile_fields_unchanged": True, "submitted": False}
    if target.get("setting") != action.setting or action.setting not in target.get("settings", {}):
        raise ValueError("Prepared self setting does not match the requested preference")
    ack = _object(canvas, _Request("users/self/settings", method="PUT", body={action.setting: action.enabled}))
    if ack.get(action.setting) is not action.enabled:
        raise ValueError("Canvas did not acknowledge the requested self setting")
    after, settings = _self(canvas), _settings(canvas)
    _same_actor_and_version(target, after)
    if after["profile"] != target["profile"] or after["permissions"] != target["permissions"]:
        raise ValueError("Canvas self profile changed during preference verification")
    other = {key: value for key, value in settings.items() if key != action.setting}
    if settings[action.setting] is not action.enabled or _version(other) != target["other_settings_version"]:
        raise ValueError("Canvas did not independently confirm the exact setting and unchanged other boolean preferences")
    return {"verified": True, "user_id": after["user_id"], "setting": action.setting,
            "enabled": action.enabled, "other_returned_boolean_settings_unchanged": True, "submitted": False}
