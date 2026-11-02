"""Reviewed current-student membership changes in collaborative course groups.

Contracts: https://developerdocs.instructure.com/services/canvas/resources/groups
The official group serializer accepts permissions/group_category includes on the
course list, which is accessible to legitimate nonmembers when group-show is not.
Membership PUT is a management operation, not a general self invitation response.
"""
from __future__ import annotations

from itertools import islice
from typing import Annotated, Any, Literal

from canvas_py import Canvas
from canvas_py._errors import CanvasError, CanvasRateLimitError
from canvas_py._request import CanvasRequest
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .group_content import GroupContext, Id
from .responses import dump, envelope, window

_ID = TypeAdapter(Id)
_MAX_RECORDS = 1000
_GROUP_FIELDS = ("id", "name", "context_type", "course_id", "group_category_id", "role", "join_level",
                 "max_membership", "is_full", "concluded", "non_collaborative", "leader")
_CATEGORY_FIELDS = ("id", "name", "context_type", "course_id", "role", "allows_multiple_memberships", "is_member", "self_signup",
                    "self_signup_end_at", "auto_leader", "group_limit")
_MEMBER_FIELDS = ("id", "group_id", "user_id", "workflow_state", "moderator")
_INVITATION_NOTE = ("Use Canvas to accept or decline this invitation. This connector does not use membership "
                    "management updates or interpret joining/leaving as an invitation response.")


class MembershipTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    context: GroupContext
    course_id: Id


class GroupJoin(MembershipTarget):
    kind: Literal["group_join"]


class GroupLeave(MembershipTarget):
    kind: Literal["group_leave"]


class GroupCancelJoinRequest(MembershipTarget):
    kind: Literal["group_cancel_join_request"]


GroupMembershipActionTypes = GroupJoin | GroupLeave | GroupCancelJoinRequest
KINDS = ["group_join", "group_leave", "group_cancel_join_request"]
ROUTES = {kind: "canvas_manage_group_membership" for kind in KINDS}
_ADAPTER = TypeAdapter(Annotated[GroupMembershipActionTypes, Field(discriminator="kind")])


class _Request(CanvasRequest[Any]):
    def __init__(self, path: str, *, method: str = "GET", query: list | None = None, body: dict | None = None):
        self._path, self.method, self._query, self._body = path, method, query or [], body
        self.allow_retries = self.allow_redirects = method == "GET"

    @property
    def path(self) -> str:
        return self._path

    def query_pairs(self) -> list:
        return self._query

    def json_body(self) -> dict | None:
        return self._body


class _List(_Request):
    response_is_paginated = True

    def decode(self, data: Any) -> list[dict]:
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise TypeError("Canvas did not return a group membership discovery list")
        return data


def _object(canvas: Canvas, path: str, **kwargs: Any) -> dict:
    result = dump(canvas.send(_Request(path, **kwargs)))
    if not isinstance(result, dict):
        raise TypeError("Canvas did not return the expected group membership object")
    return result


def _all(canvas: Canvas, path: str, *, query: list | None = None) -> list[dict]:
    iterator = canvas.paginate(_List(path, query=[*(query or []), ("per_page", 100)]))
    try:
        rows = list(islice(iterator, _MAX_RECORDS + 1))
    finally:
        iterator.close()
    if len(rows) > _MAX_RECORDS:
        raise ValueError("Group membership discovery exceeds 1000 records; inspect this group in Canvas")
    return rows


def _identity(value: dict, field: str, expected: int) -> None:
    if type(value.get(field)) is not int or value[field] != expected:
        raise ValueError(f"Canvas did not confirm the exact group membership {field}")


def _actor(canvas: Canvas) -> int:
    return _ID.validate_python(_object(canvas, "users/self/profile").get("id"))


def _course(canvas: Canvas, course_id: int) -> dict:
    course_id = _ID.validate_python(course_id)
    actor = _actor(canvas)
    course = _object(canvas, f"courses/{course_id}")
    _identity(course, "id", course_id)
    if course.get("workflow_state") != "available":
        raise ValueError("This membership workflow requires an available course")
    enrollments = _all(canvas, f"courses/{course_id}/enrollments", query=[
        ("user_id", actor), ("type[]", "StudentEnrollment")])
    for enrollment in enrollments:
        _identity(enrollment, "user_id", actor)
        _identity(enrollment, "course_id", course_id)
        if enrollment.get("type") != "StudentEnrollment":
            raise ValueError("Canvas did not confirm your student enrollment")
    active = [row for row in enrollments if row.get("enrollment_state") == "active"]
    if not active:
        raise ValueError("This membership workflow requires your active student enrollment")
    return {"user_id": actor, "course_id": course_id,
            "course": {key: course.get(key) for key in ("id", "name", "workflow_state")}}


def _group(group: dict, course_id: int) -> dict:
    _ID.validate_python(group.get("id"))
    _identity(group, "course_id", course_id)
    if group.get("context_type") != "Course":
        raise ValueError("Canvas returned a group outside the requested course")
    for field in ("non_collaborative", "is_full", "concluded"):
        if field in group and type(group[field]) is not bool:
            raise ValueError(f"Canvas returned unknown group {field}")
    if group.get("non_collaborative") is True:
        raise ValueError("Differentiation tags are outside self group membership")
    if not isinstance(group.get("name"), str) or not group["name"]:
        raise ValueError("Canvas did not identify the group name")
    result = {key: group[key] for key in _GROUP_FIELDS if key in group}
    if isinstance(result.get("leader"), dict):
        result["leader"] = {key: result["leader"][key] for key in ("id", "display_name") if key in result["leader"]}
    return result


def _catalog(canvas: Canvas, course_id: int) -> list[dict]:
    rows = _all(canvas, f"courses/{course_id}/groups", query=[("collaboration_state", "collaborative"),
        ("include[]", "permissions"), ("include[]", "group_category")])
    seen = set()
    for row in rows:
        group = _group(row, course_id)
        if group["id"] in seen:
            raise ValueError("Canvas returned duplicate groups during membership discovery")
        seen.add(group["id"])
    return rows


def _candidate(rows: list[dict], group_id: int) -> dict:
    matches = [row for row in rows if row["id"] == group_id]
    if len(matches) != 1:
        raise ValueError("The exact group is not visible in this course's group list")
    return matches[0]


def _membership(canvas: Canvas, group_id: int, actor: int) -> dict:
    try:
        member = _object(canvas, f"groups/{group_id}/users/self")
    except CanvasError as error:
        if isinstance(error, CanvasRateLimitError):
            raise
        if error.status == 404:
            return {"status": "absent"}
        if error.status in {401, 403}:
            return {"status": "unavailable", "http_status": error.status,
                    "reason": "Canvas did not permit reading your exact membership; absence is not confirmed."}
        raise
    _identity(member, "group_id", group_id)
    _identity(member, "user_id", actor)
    _ID.validate_python(member.get("id"))
    if member.get("workflow_state") not in {"accepted", "invited", "requested", "rejected", "deleted"}:
        raise ValueError("Canvas returned an unknown self membership state")
    if type(member.get("moderator")) is not bool:
        raise ValueError("Canvas did not identify your group moderator status")
    return {"status": "present", "membership": {key: member[key] for key in _MEMBER_FIELDS}}


def _category(canvas: Canvas, group: dict) -> dict:
    category_id = _ID.validate_python(group.get("group_category_id"))
    category = group.get("group_category")
    if category is None:
        category = _object(canvas, f"group_categories/{category_id}")
    if not isinstance(category, dict):
        raise TypeError("Canvas did not identify the group category")
    _identity(category, "id", category_id)
    _identity(category, "course_id", group["course_id"])
    if category.get("context_type") != "Course":
        raise ValueError("Canvas returned a group category outside the requested course")
    if type(category.get("allows_multiple_memberships")) is not bool or type(category.get("is_member")) is not bool:
        raise ValueError("Canvas did not confirm category membership and switching rules")
    return {key: category[key] for key in _CATEGORY_FIELDS if key in category}


def _own_groups(canvas: Canvas) -> list[dict]:
    rows = _all(canvas, "users/self/groups")
    seen, result = set(), []
    for row in rows:
        group_id = _ID.validate_python(row.get("id"))
        if group_id in seen:
            raise ValueError("Canvas returned duplicate own groups")
        seen.add(group_id)
        if "group_category_id" not in row:
            raise ValueError("Canvas did not identify an own group's category")
        if row["group_category_id"] is not None:
            _ID.validate_python(row["group_category_id"])
        result.append({key: row.get(key) for key in ("id", "name", "group_category_id")})
    return result


def _join_rules(canvas: Canvas, group: dict, own: list[dict]) -> dict:
    permissions = group.get("permissions")
    if not isinstance(permissions, dict) or permissions.get("join") is not True:
        raise ValueError("Canvas did not grant self-join for this group")
    if group.get("is_full") is True:
        raise ValueError("This group is full")
    category = _category(canvas, group)
    if not category["allows_multiple_memberships"]:
        peers = [row for row in own if row["group_category_id"] == category["id"] and row["id"] != group["id"]]
        if peers:
            affected = ", ".join(f"{row['name'] or 'Group'} (group {row['id']})" for row in peers)
            raise ValueError(f"Joining {group['name']} would remove your membership in {affected}. "
                             "This connector does not switch groups implicitly; review that membership in Canvas first.")
        if category["is_member"]:
            raise ValueError(f"Canvas reports another membership in category {category.get('name') or category['id']}, "
                             "but its exact group is not visible. Joining could remove it; inspect Canvas first.")
    return {"join": True, "category": category}


def _leave_rules(canvas: Canvas, group: dict, actor: int) -> dict:
    if "leader" not in group:
        raise ValueError("Canvas did not confirm whether leaving would change group leadership")
    leader = group["leader"]
    if leader is not None:
        if not isinstance(leader, dict):
            raise TypeError("Canvas returned unknown group leadership")
        leader_id = _ID.validate_python(leader.get("id"))
        if leader_id == actor:
            raise ValueError(f"You lead {group['name']}; leaving can reassign its leadership. "
                             "Review leadership in Canvas before leaving through this connector.")
    rights = _object(canvas, f"groups/{group['id']}/permissions", query=[("permissions[]", "leave")])
    if rights.get("leave") is not True:
        raise ValueError("Canvas did not grant permission to leave this group")
    return {"leave": True}


def _choices(canvas: Canvas, group: dict, actor: int, own: list[dict]) -> dict:
    member = _membership(canvas, group["id"], actor)
    result = {"group": _group(group, group["course_id"]), "self_membership": member,
              "actions": [], "reasons": [], "html_url": f"{canvas.config.base_url}/courses/{group['course_id']}/groups"}
    if member["status"] == "unavailable":
        result["reasons"].append(member["reason"])
        return result
    state = member.get("membership", {}).get("workflow_state")
    if state == "invited":
        result["reasons"].append(_INVITATION_NOTE)
        return result
    if state not in {None, "accepted", "requested"}:
        result["reasons"].append("Inspect this membership state in Canvas before changing it.")
        return result
    if group.get("concluded") is True:
        result["reasons"].append("This group belongs to a concluded context.")
        return result
    try:
        if state is None:
            result["rules"] = _join_rules(canvas, group, own)
            result["actions"] = ["group_join"]
        else:
            result["rules"] = _leave_rules(canvas, group, actor)
            result["actions"] = ["group_leave" if state == "accepted" else "group_cancel_join_request"]
    except CanvasError as error:
        if isinstance(error, CanvasRateLimitError) or error.status not in {401, 403, 404}:
            raise
        result["reasons"].append("Canvas did not make the required membership rules available.")
    except (ValueError, TypeError) as error:
        result["reasons"].append(str(error))
    return result


def list_membership_options(canvas: Canvas, course_id: int, offset: int = 0, limit: int = 30) -> dict:
    """Read visible course candidates and exact self states; never join on a GET."""
    window([], offset, limit)
    context = _course(canvas, course_id)
    groups = _catalog(canvas, course_id)
    selected, coverage = window(groups, offset, limit)
    own = _own_groups(canvas) if selected else []
    options = [_choices(canvas, group, context["user_id"], own) for group in selected]
    complete_memberships = all(row["self_membership"]["status"] != "unavailable" for row in options)
    coverage.update(scope="canvas_visible_course_groups", discovery_limit=_MAX_RECORDS,
                    group_list_complete=coverage["complete"], membership_checks_complete=complete_memberships,
                    complete=coverage["complete"] and complete_memberships,
                    invitations="Only individually readable invitations are shown; this is not an exhaustive invitation inbox.")
    return envelope({**context, "groups": options}, origin=canvas.config.base_url,
                    status="ok" if complete_memberships else "partial", coverage=coverage)


def snapshot(canvas: Canvas, p: dict) -> dict:
    action = _ADAPTER.validate_python(p)
    context = _course(canvas, action.course_id)
    group = _candidate(_catalog(canvas, action.course_id), action.context.group_id)
    own = _own_groups(canvas)
    options = _choices(canvas, group, context["user_id"], own)
    if action.kind not in options["actions"]:
        raise ValueError(" ".join(options["reasons"]) or "The requested action does not match your current group membership state")
    effect = ("Join this group. Canvas may accept membership immediately or record a pending approval request; "
              "the result distinguishes those states."
              if isinstance(action, GroupJoin) else
              "Remove your own accepted membership and access to this group's shared content."
              if isinstance(action, GroupLeave) else "Cancel your own pending group join request.")
    if isinstance(action, GroupJoin) and options["rules"]["category"].get("auto_leader") in {"first", "random"}:
        effect += " This category automatically assigns group leaders; joining may appoint you leader."
    if options["self_membership"].get("membership", {}).get("moderator"):
        effect += " Your moderator role in this group will also be removed."
    return {**context, "context": action.context.model_dump(), "group": options["group"],
            "self_membership": options["self_membership"], "rules": options["rules"], "effect": effect}


def perform(canvas: Canvas, p: dict, target: dict) -> dict:
    action = _ADAPTER.validate_python(p)
    actor = _ID.validate_python(target.get("user_id"))
    _identity(target, "course_id", action.course_id)
    if target.get("context") != action.context.model_dump():
        raise ValueError("Prepared group membership context does not match")
    group = target.get("group")
    if not isinstance(group, dict):
        raise TypeError("Prepared action lacks a group")
    _identity(group, "id", action.context.group_id)
    _identity(group, "course_id", action.course_id)
    before = target.get("self_membership") or {}
    result = {"verified": True, "user_id": actor, "context": action.context.model_dump(),
              "course_id": action.course_id, "submitted": False}
    if isinstance(action, GroupJoin):
        if before.get("status") != "absent" or target.get("rules", {}).get("join") is not True:
            raise ValueError("Prepared self-join is not permitted")
        created = _object(canvas, f"groups/{action.context.group_id}/memberships", method="POST", body={"user_id": "self"})
        _identity(created, "group_id", action.context.group_id)
        _identity(created, "user_id", actor)
        membership_id = _ID.validate_python(created.get("id"))
        if created.get("workflow_state") not in {"accepted", "requested"}:
            raise ValueError("Canvas did not confirm an accepted membership or pending join request")
        if _actor(canvas) != actor:
            raise ValueError("Current user changed during group join")
        saved = _membership(canvas, action.context.group_id, actor)
        if saved.get("status") != "present":
            raise ValueError("The new self membership could not be independently read back")
        member = saved["membership"]
        _identity(member, "id", membership_id)
        if member["workflow_state"] != created["workflow_state"] or member["moderator"] is not False:
            raise ValueError("Canvas did not confirm the exact ordinary membership state")
        current_group = _candidate(_catalog(canvas, action.course_id), action.context.group_id)
        if any(current_group.get(key) != group.get(key) for key in ("id", "name", "course_id", "context_type", "group_category_id")):
            raise ValueError("Group audience changed during join")
        accepted = member["workflow_state"] == "accepted"
        own = _own_groups(canvas)
        if any(row["id"] == action.context.group_id for row in own) is not accepted:
            raise ValueError("Canvas's own-group list did not confirm the membership state")
        return {**result, "membership": member, "membership_state": member["workflow_state"],
                "accepted": accepted, "pending_approval": not accepted,
                "leader": _group(current_group, action.course_id).get("leader"),
                "coverage": {"complete": True, "scope": "exact_self_membership"}}
    expected = "accepted" if isinstance(action, GroupLeave) else "requested"
    member = before.get("membership") or {}
    _identity(member, "user_id", actor)
    _identity(member, "group_id", action.context.group_id)
    _ID.validate_python(member.get("id"))
    if member.get("workflow_state") != expected or target.get("rules", {}).get("leave") is not True:
        raise ValueError("Prepared self membership removal is not permitted")
    response = _object(canvas, f"groups/{action.context.group_id}/users/self", method="DELETE")
    if response.get("ok") is not True:
        raise ValueError("Canvas did not acknowledge the self membership removal")
    if _actor(canvas) != actor:
        raise ValueError("Current user changed during group leave")
    saved = _membership(canvas, action.context.group_id, actor)
    if saved["status"] != "absent" or any(row["id"] == action.context.group_id for row in _own_groups(canvas)):
        raise ValueError("Canvas did not independently confirm the exact self membership was removed")
    return {**result, "previous_membership_id": member["id"], "membership_state": "absent", "accepted": False,
            "pending_approval": False, "coverage": {"complete": True, "scope": "exact_self_membership"}}
