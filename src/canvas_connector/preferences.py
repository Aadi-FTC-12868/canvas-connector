"""Narrow, current-account notification and dashboard preferences.

Contracts: https://developerdocs.instructure.com/services/canvas/resources/
notification_preferences, communication_channels, favorites, users, groups,
and enrollments. Read helpers issue GET only. Canvas's notification GET may
materialize missing default policy rows internally (NotificationPolicy.find_all_for);
it does not change the effective preference. We never call a setter from a read.

Group include[]=favorites is supported by the official group JSON serializer,
although not listed in the group-show documentation. Missing is_favorite fails
closed. Favorite lists can contain default selections; list membership alone
is never evidence of an explicit favorite or a successful removal.
"""
from __future__ import annotations

from itertools import islice
import re
from typing import Annotated, Any, Literal, Union

from canvas_py import Canvas
from canvas_py._request import CanvasRequest, QueryPair
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from .responses import dump, envelope, window

Id = Annotated[int, Field(gt=0, le=2**63 - 1, strict=True)]
Frequency = Literal["immediately", "daily", "weekly", "never"]
_FREQUENCIES = {"immediately", "daily", "weekly", "never"}
_MAX_DISCOVERY = 1000


class PreferenceAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NotificationPreferenceUpdate(PreferenceAction):
    kind: Literal["notification_preference_update"]
    channel_id: Id
    notification: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,127}$", strict=True)]
    frequency: Frequency


class CourseTarget(PreferenceAction):
    course_id: Id


class CourseFavoriteAdd(CourseTarget):
    kind: Literal["course_favorite_add"]


class CourseFavoriteRemove(CourseTarget):
    kind: Literal["course_favorite_remove"]


class GroupTarget(PreferenceAction):
    group_id: Id


class GroupFavoriteAdd(GroupTarget):
    kind: Literal["group_favorite_add"]


class GroupFavoriteRemove(GroupTarget):
    kind: Literal["group_favorite_remove"]


class CourseNicknameSet(CourseTarget):
    kind: Literal["course_nickname_set"]
    nickname: Annotated[str, Field(min_length=1, max_length=59, strict=True)]

    @field_validator("nickname")
    @classmethod
    def visible_name(cls, value: str) -> str:
        if not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("nickname must contain visible text without control characters")
        return value


class CourseNicknameDelete(CourseTarget):
    kind: Literal["course_nickname_delete"]


class CourseColorSet(CourseTarget):
    kind: Literal["course_color_set"]
    hexcode: Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$", strict=True)]

    @field_validator("hexcode")
    @classmethod
    def canonical_color(cls, value: str) -> str:
        return value.lower()


PreferenceActionTypes = Union[NotificationPreferenceUpdate, CourseFavoriteAdd,
    CourseFavoriteRemove, GroupFavoriteAdd, GroupFavoriteRemove,
    CourseNicknameSet, CourseNicknameDelete, CourseColorSet]
KINDS = ["notification_preference_update", "course_favorite_add", "course_favorite_remove",
         "group_favorite_add", "group_favorite_remove", "course_nickname_set",
         "course_nickname_delete", "course_color_set"]
ROUTES = {kind: ("canvas_update_notifications" if kind == "notification_preference_update"
                 else "canvas_update_preferences") for kind in KINDS}
_ADAPTER = TypeAdapter(Annotated[PreferenceActionTypes, Field(discriminator="kind")])
_ID = TypeAdapter(Id)


class _Request(CanvasRequest[Any]):
    """Private fixed-route dispatch; no caller-supplied URL or target user."""
    def __init__(self, path: str, *, method: str = "GET", body: dict | None = None,
                 query: list[QueryPair] | None = None):
        self._path, self.method, self.body, self.query = path, method, body, query or []

    @property
    def path(self) -> str:
        return self._path

    def json_body(self) -> dict | None:
        return self.body

    def query_pairs(self) -> list[QueryPair]:
        return self.query


def _object(canvas: Canvas, request: CanvasRequest) -> dict:
    result = dump(canvas.send(request))
    if not isinstance(result, dict):
        raise ValueError("Canvas did not return the requested preference object")
    return result


def _identity(value: dict, key: str, expected: int) -> None:
    if type(value.get(key)) is not int or value[key] != expected:
        raise ValueError(f"Canvas returned a different or unknown {key}")


def _self(canvas: Canvas) -> dict:
    profile = _object(canvas, _Request("users/self/profile"))
    if type(profile.get("id")) is not int or not 0 < profile["id"] < 2**63:
        raise ValueError("Canvas did not confirm the current user's identity")
    return profile


def _items(canvas: Canvas, request: CanvasRequest, offset: int, limit: int) -> tuple[list, bool]:
    iterator = canvas.paginate(request)
    try:
        result = [dump(v) for v in islice(iterator, offset, offset + limit + 1)]
    except TypeError as error:
        raise ValueError("Canvas did not return a preference object list") from error
    finally:
        iterator.close()
    if any(not isinstance(v, dict) for v in result):
        raise ValueError("Canvas did not return a preference object list")
    return result[:limit], len(result) > limit


def _all(canvas: Canvas, request: CanvasRequest) -> list[dict]:
    result, more = _items(canvas, request, 0, _MAX_DISCOVERY)
    if more:
        raise ValueError("Canvas preference ownership discovery exceeded its safe limit; open Canvas")
    return result


def _coverage(offset: int, limit: int, count: int, more: bool) -> dict:
    return {"scope": "requested_list", "complete": offset == 0 and not more,
            "offset": offset, "returned": count, "next_offset": offset + limit if more else None,
            "pagination_note": "Offset windows may shift if Canvas changes between reads."}


def _channel_fields(channel: dict, actor: int) -> dict:
    _identity(channel, "user_id", actor)
    _ID.validate_python(channel.get("id"))
    result = {k: channel[k] for k in ("id", "user_id", "type", "workflow_state", "position",
        "permissions", "locked", "read_only") if k in channel}
    # Push addresses may be device identifiers; channel ID is sufficient here.
    if channel.get("type") != "push" and "address" in channel:
        result["address"] = channel["address"]
    return result


def communication_channels(canvas: Canvas, offset: int = 0, limit: int = 30) -> dict:
    """List only the connected user's existing channels; never create channels."""
    window([], offset, limit)
    actor = _self(canvas)["id"]
    items, more = _items(canvas, _Request("users/self/communication_channels"), offset, limit)
    result = [_channel_fields(v, actor) for v in items]
    return envelope({"user_id": actor, "communication_channels": result}, origin=canvas.config.base_url,
                    coverage=_coverage(offset, limit, len(result), more))


def _channel(canvas: Canvas, actor: int, channel_id: int) -> dict:
    items = _all(canvas, _Request("users/self/communication_channels"))
    for item in items:
        _channel_fields(item, actor)
    matches = [v for v in items if v.get("id") == channel_id]
    if len(matches) != 1:
        raise ValueError("The exact communication channel is not uniquely available in your account")
    return _channel_fields(matches[0], actor)


def _notification_path(channel_id: int) -> str:
    return f"users/self/communication_channels/{channel_id}/notification_preferences"


def _notifications(canvas: Canvas, actor: int, channel_id: int) -> list[dict]:
    # Real Canvas wraps the list despite the generated docs saying list. GET
    # list is used both before and after dispatch; no setter or bulk update.
    value = dump(canvas.send(_Request(_notification_path(channel_id))))
    if isinstance(value, dict):
        for key, expected in (("user_id", actor), ("communication_channel_id", channel_id)):
            if key in value:
                _identity(value, key, expected)
        value = value.get("notification_preferences")
    if not isinstance(value, list) or len(value) > _MAX_DISCOVERY:
        raise ValueError("Canvas did not return a complete bounded notification preference list")
    result, seen = [], set()
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("notification"), str):
            raise ValueError("Canvas did not identify a notification preference")
        name = item["notification"]
        if name in seen:
            raise ValueError("Canvas returned duplicate notification preferences")
        seen.add(name)
        for key, expected in (("user_id", actor), ("communication_channel_id", channel_id)):
            if key in item:
                _identity(item, key, expected)
        result.append({k: item[k] for k in ("notification", "category", "frequency", "permissions",
            "locked", "read_only", "available_frequencies") if k in item})
    return sorted(result, key=lambda v: v["notification"])


def notification_preferences(canvas: Canvas, channel_id: int, offset: int = 0, limit: int = 30) -> dict:
    """Read effective frequencies for an existing current-user channel."""
    _ID.validate_python(channel_id)
    window([], offset, limit)
    actor = _self(canvas)["id"]
    channel = _channel(canvas, actor, channel_id)
    items, coverage = window(_notifications(canvas, actor, channel_id), offset, limit)
    return envelope({"user_id": actor, "channel": channel, "notification_preferences": items},
                    origin=canvas.config.base_url, coverage=coverage)


def favorites(canvas: Canvas, context_type: Literal["courses", "groups"] = "courses",
              offset: int = 0, limit: int = 30) -> dict:
    """Read Canvas's favorites/default dashboard selection for this account."""
    if context_type not in {"courses", "groups"}:
        raise ValueError("context_type must be courses or groups")
    window([], offset, limit)
    actor = _self(canvas)["id"]
    items, more = _items(canvas, _Request(f"users/self/favorites/{context_type}",
                         query=[("include[]", "favorites")]), offset, limit)
    result = []
    for item in items:
        _ID.validate_python(item.get("id"))
        result.append({k: item[k] for k in ("id", "name", "course_code", "course_id",
            "workflow_state", "is_favorite", "concluded") if k in item})
    return envelope({"user_id": actor, "context_type": context_type, "favorites": result,
        "selection_note": "Canvas can return default selections when no explicit favorites exist; membership in this list does not prove an explicit favorite."},
        origin=canvas.config.base_url, coverage=_coverage(offset, limit, len(result), more))


def course_nicknames(canvas: Canvas, offset: int = 0, limit: int = 30) -> dict:
    window([], offset, limit)
    actor = _self(canvas)["id"]
    items, more = _items(canvas, _Request("users/self/course_nicknames"), offset, limit)
    result = []
    for item in items:
        _ID.validate_python(item.get("course_id"))
        result.append({k: item[k] for k in ("course_id", "name", "nickname") if k in item})
    return envelope({"user_id": actor, "course_nicknames": result}, origin=canvas.config.base_url,
                    coverage=_coverage(offset, limit, len(result), more))


def _colors(canvas: Canvas) -> dict:
    value = _object(canvas, _Request("users/self/colors")).get("custom_colors")
    if not isinstance(value, dict):
        raise ValueError("Canvas did not return the current user's custom colors")
    if any(not isinstance(v, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", v) for v in value.values()):
        raise ValueError("Canvas returned an unknown custom color representation")
    return {k: v.lower() for k, v in value.items()}


def course_colors(canvas: Canvas, offset: int = 0, limit: int = 30) -> dict:
    window([], offset, limit)
    actor = _self(canvas)["id"]
    colors = _colors(canvas)
    entries = [{"course_id": int(k.removeprefix("course_")), "hexcode": v} for k, v in sorted(colors.items())
               if re.fullmatch(r"course_[1-9][0-9]*", k)]
    selected, coverage = window(entries, offset, limit)
    return envelope({"user_id": actor, "course_colors": selected}, origin=canvas.config.base_url, coverage=coverage)


def _policy(value: dict, *, permissions: tuple[str, ...] = ("read",)) -> dict:
    for field in ("locked", "read_only", "access_restricted_by_date"):
        if field in value and value[field] is not False:
            raise ValueError("Canvas reports a restricted or unknown preference access state")
    returned = value.get("permissions")
    if returned is not None:
        if not isinstance(returned, dict):
            raise ValueError("Canvas returned an unknown permissions object")
        if any(key in returned and returned[key] is not True for key in permissions):
            raise ValueError("Canvas does not permit this preference action")
    return {k: value[k] for k in ("permissions", "locked", "read_only", "access_restricted_by_date") if k in value}


def _context(canvas: Canvas, action: PreferenceActionTypes, actor: int) -> dict:
    if isinstance(action, GroupTarget):
        group = _object(canvas, _Request(f"groups/{action.group_id}",
                        query=[("include[]", "favorites"), ("include[]", "permissions")]))
        _identity(group, "id", action.group_id)
        policy = _policy(group)
        if "workflow_state" in group and group["workflow_state"] != "available":
            # Group models use "available"; normally omitted by the serializer.
            raise ValueError("Canvas reports an unavailable group")
        if group.get("concluded") is not False or group.get("non_collaborative") is not False:
            raise ValueError("Canvas did not confirm an active collaborative group")
        member = _object(canvas, _Request(f"groups/{action.group_id}/users/self"))
        _identity(member, "group_id", action.group_id)
        _identity(member, "user_id", actor)
        if member.get("workflow_state") != "accepted":
            raise ValueError("The current user is not an accepted group member")
        return {"group_id": action.group_id, "name": group.get("name"),
                "is_favorite": _favorite(group), "membership": {k: member.get(k) for k in
                ("id", "group_id", "user_id", "workflow_state")}, "policy": policy}
    course = _object(canvas, _Request(f"courses/{action.course_id}",
                     query=[("include[]", "favorites"), ("include[]", "permissions")]))
    _identity(course, "id", action.course_id)
    policy = _policy(course)
    if course.get("workflow_state") not in {"available", "completed"}:
        raise ValueError("Canvas did not confirm an available or completed course")
    enrollments = _all(canvas, _Request(f"courses/{action.course_id}/enrollments", query=[
        ("user_id", actor), ("state[]", "active"), ("state[]", "completed")]))
    if not enrollments:
        raise ValueError("No enrollment for the current user was confirmed")
    for enrollment in enrollments:
        _identity(enrollment, "user_id", actor)
        _identity(enrollment, "course_id", action.course_id)
        if enrollment.get("enrollment_state") not in {"active", "completed"}:
            raise ValueError("The current user's enrollment is not active or completed")
    result = {"course_id": action.course_id, "name": course.get("original_name", course.get("name")),
        "workflow_state": course["workflow_state"], "policy": policy,
        "enrollments": sorted([{k: e.get(k) for k in ("id", "user_id", "course_id", "type", "enrollment_state")}
                               for e in enrollments], key=lambda e: str(e["id"]))}
    if isinstance(action, (CourseFavoriteAdd, CourseFavoriteRemove)):
        result["is_favorite"] = _favorite(course)
    return result


def _favorite(value: dict) -> bool:
    if type(value.get("is_favorite")) is not bool:
        raise ValueError("Canvas did not confirm the exact favorite flag; open Canvas")
    return value["is_favorite"]


def _nickname(canvas: Canvas, course_id: int) -> dict:
    value = _object(canvas, _Request(f"users/self/course_nicknames/{course_id}"))
    _identity(value, "course_id", course_id)
    if "nickname" not in value or (value["nickname"] is not None and not isinstance(value["nickname"], str)):
        raise ValueError("Canvas did not confirm the exact nickname state")
    return {k: value.get(k) for k in ("course_id", "name", "nickname")}


def snapshot(canvas: Canvas, p: dict) -> dict:
    """Prepare only exact owned settings; repeated by ActionStore before dispatch."""
    action = _ADAPTER.validate_python(p)
    profile = _self(canvas)
    actor = profile["id"]
    if isinstance(action, NotificationPreferenceUpdate):
        channel = _channel(canvas, actor, action.channel_id)
        if channel.get("workflow_state") != "active" or channel.get("type") not in {"email", "sms", "push"}:
            raise ValueError("The communication channel is not active or its type is unknown")
        _policy(channel, permissions=("read", "update"))
        preferences = _notifications(canvas, actor, action.channel_id)
        matches = [v for v in preferences if v["notification"] == action.notification]
        if len(matches) != 1 or matches[0].get("frequency") not in _FREQUENCIES:
            raise ValueError("The exact notification and its current frequency were not confirmed")
        preference = matches[0]
        _policy(preference, permissions=("read", "update"))
        if "available_frequencies" in preference and (not isinstance(preference["available_frequencies"], list)
                or action.frequency not in preference["available_frequencies"]):
            raise ValueError("Canvas does not allow that frequency for this notification")
        result = {"user_id": actor, "channel_id": action.channel_id, "notification": action.notification,
                  "channel": channel, "preference": preference, "channel_preferences": preferences}
        if action.frequency == "never":
            result["warning"] = "This disables this notification on this channel. You may miss course updates; it does not disable other notifications or channels."
        return result
    result = {"user_id": actor, **_context(canvas, action, actor)}
    if isinstance(action, (CourseNicknameSet, CourseNicknameDelete)):
        result["nickname"] = _nickname(canvas, action.course_id)
        if isinstance(action, CourseNicknameDelete) and result["nickname"]["nickname"] is None:
            raise ValueError("This course has no nickname to delete")
    elif isinstance(action, CourseColorSet):
        permissions = profile.get("permissions")
        if isinstance(permissions, dict) and all(key in permissions and permissions[key] is not True
                                               for key in ("manage", "manage_user_details")):
            raise ValueError("Canvas does not permit custom color changes for the current user")
        result["custom_colors"] = _colors(canvas)
        result["hexcode"] = result["custom_colors"].get(f"course_{action.course_id}")
        result["user_permissions"] = permissions
    return result


def perform(canvas: Canvas, p: dict, target: dict) -> dict:
    """One exact mutation followed by independent reads, never automatic retries."""
    action = _ADAPTER.validate_python(p)
    actor = _ID.validate_python(target.get("user_id"))
    result = {"verified": True, "user_id": actor}
    if isinstance(action, NotificationPreferenceUpdate):
        _identity(target, "channel_id", action.channel_id)
        if target.get("notification") != action.notification:
            raise ValueError("Prepared notification does not match the action")
        _identity(target.get("channel") or {}, "user_id", actor)
        _identity(target["channel"], "id", action.channel_id)
        before = target.get("channel_preferences")
        if not isinstance(before, list) or not any(v.get("notification") == action.notification for v in before):
            raise ValueError("Prepared action has no confirmed existing notification")
        canvas.send(_Request(_notification_path(action.channel_id) + f"/{action.notification}", method="PUT",
                             body={"notification_preferences": {"frequency": action.frequency}}))
        after = _notifications(canvas, actor, action.channel_id)
        expected = [{**v, "frequency": action.frequency} if v["notification"] == action.notification else v for v in before]
        if after != expected:
            raise ValueError("Exact notification update and preservation of other preferences were not confirmed")
        return {**result, "channel_id": action.channel_id, "notification": action.notification,
                "frequency": action.frequency, "other_preferences_preserved": True}
    key = "group_id" if isinstance(action, GroupTarget) else "course_id"
    context_id = getattr(action, key)
    _identity(target, key, context_id)
    if isinstance(action, (CourseFavoriteAdd, CourseFavoriteRemove, GroupFavoriteAdd, GroupFavoriteRemove)):
        _favorite(target)
        group = isinstance(action, GroupTarget)
        context_type = "groups" if group else "courses"
        expected = isinstance(action, (CourseFavoriteAdd, GroupFavoriteAdd))
        canvas.send(_Request(f"users/self/favorites/{context_type}/{context_id}", method="POST" if expected else "DELETE"))
        saved = _object(canvas, _Request(f"{context_type}/{context_id}", query=[("include[]", "favorites")]))
        _identity(saved, "id", context_id)
        if _favorite(saved) is not expected:
            raise ValueError("Exact favorite state was not confirmed")
        return {**result, key: context_id, "is_favorite": expected}
    if isinstance(action, (CourseNicknameSet, CourseNicknameDelete)):
        _identity(target.get("nickname") or {}, "course_id", action.course_id)
        expected = action.nickname if isinstance(action, CourseNicknameSet) else None
        canvas.send(_Request(f"users/self/course_nicknames/{action.course_id}",
            method="PUT" if isinstance(action, CourseNicknameSet) else "DELETE",
            body={"nickname": expected} if isinstance(action, CourseNicknameSet) else None))
        saved = _nickname(canvas, action.course_id)
        if saved["nickname"] != expected:
            raise ValueError("Exact course nickname state was not confirmed")
        return {**result, "course_id": action.course_id, "nickname": expected}
    before = target.get("custom_colors")
    if not isinstance(before, dict):
        raise ValueError("Prepared action has no confirmed current colors")
    canvas.send(_Request(f"users/self/colors/course_{action.course_id}", method="PUT", body={"hexcode": action.hexcode}))
    after = _colors(canvas)
    if after != {**before, f"course_{action.course_id}": action.hexcode}:
        raise ValueError("Exact course color and preservation of other colors were not confirmed")
    return {**result, "course_id": action.course_id, "hexcode": action.hexcode, "other_colors_preserved": True}
