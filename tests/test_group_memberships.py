"""Synthetic native-HTTP tests for current-student group membership actions."""
import copy
import json
import re

import httpx
import pytest
from canvas_py import Canvas
from canvas_py._config import CanvasConfig
from canvas_py._errors import CanvasError
from pydantic import ValidationError

from canvas_connector import group_memberships as memberships
from canvas_connector.actions import ACTION_ADAPTER, ActionStore

CONTEXT = {"context_type": "group", "group_id": 2}


def action(kind="group_join", **changes):
    return {"kind": kind, "context": CONTEXT, "course_id": 1, **changes}


class MembershipHTTP:
    def __init__(self):
        self.seen = []
        self.profile = {"id": 7}
        self.course = {"id": 1, "name": "Synthetic course", "workflow_state": "available"}
        self.enrollments = [{"id": 11, "course_id": 1, "user_id": 7, "type": "StudentEnrollment", "enrollment_state": "active"}]
        self.category = {"id": 9, "name": "Project groups", "context_type": "Course", "course_id": 1,
                         "role": "student_organized", "allows_multiple_memberships": True, "is_member": False,
                         "self_signup": None, "self_signup_end_at": None, "auto_leader": None, "group_limit": None}
        self.groups = [{"id": 2, "name": "Study group", "context_type": "Course", "course_id": 1,
                        "group_category_id": 9, "role": "student_organized", "join_level": "parent_context_auto_join",
                        "permissions": {"join": True}, "non_collaborative": False, "concluded": False,
                        "is_full": False, "leader": None, "group_category": self.category}]
        self.members = {}
        self.leave = True
        self.new_state = "accepted"
        self.ignore_write = False
        self.after_write = None
        self.errors = {}
        self.create_override = None
        self.own_override = None
        self.http = httpx.Client(transport=httpx.MockTransport(self.handle))
        self.canvas = Canvas(CanvasConfig("https://school.example", "synthetic-token"), http_client=self.http)

    @property
    def group(self):
        return self.groups[0]

    @property
    def writes(self):
        return [request for request in self.seen if request.method != "GET"]

    def add_member(self, group_id=2, state="accepted", **changes):
        self.members[group_id] = {"id": 100 + group_id, "group_id": group_id, "user_id": 7,
                                  "workflow_state": state, "moderator": False, **changes}

    def close(self):
        self.canvas.close()
        self.http.close()

    def _page(self, request, rows):
        page, size = int(request.url.params.get("page", "1")), int(request.url.params["per_page"])
        headers = {}
        if page * size < len(rows):
            headers["Link"] = f'<{request.url.copy_set_param("page", page + 1)}>; rel="next"'
        return httpx.Response(200, json=rows[(page - 1) * size:page * size], headers=headers)

    def handle(self, request):
        self.seen.append(request)
        path = request.url.path.removeprefix("/api/v1/")
        assert request.url.host == "school.example"
        assert request.headers["authorization"] == "Bearer synthetic-token"
        assert "as_user_id" not in request.url.params
        if (request.method, path) in self.errors:
            status, body, *headers = self.errors[request.method, path]
            return httpx.Response(status, json=body, headers=headers[0] if headers else {})
        if request.method == "GET":
            assert request.content == b""
            if path == "users/self/profile":
                return httpx.Response(200, json=self.profile)
            if path == "courses/1":
                return httpx.Response(200, json=self.course)
            if path == "courses/1/enrollments":
                assert request.url.params.get("user_id") == "7"
                assert request.url.params.get_list("type[]") == ["StudentEnrollment"]
                return self._page(request, self.enrollments)
            if path == "courses/1/groups":
                assert request.url.params.get_list("include[]") == ["permissions", "group_category"]
                assert request.url.params.get("collaboration_state") == "collaborative"
                return self._page(request, self.groups)
            if path == "users/self/groups":
                rows = self.own_override if self.own_override is not None else [group for group in self.groups
                    if self.members.get(group["id"], {}).get("workflow_state") == "accepted"]
                return self._page(request, rows)
            if path == "group_categories/9":
                return httpx.Response(200, json=self.category)
            if match := re.fullmatch(r"groups/(\d+)/users/self", path):
                member = self.members.get(int(match[1]))
                return httpx.Response(200, json=member) if member is not None else httpx.Response(404, json={"message": "not found"})
            if re.fullmatch(r"groups/(\d+)/permissions", path):
                assert request.url.params.get_list("permissions[]") == ["leave"]
                return httpx.Response(200, json={"leave": self.leave})
            raise AssertionError((request.method, path))
        if match := re.fullmatch(r"groups/(\d+)/memberships", path):
            assert request.method == "POST" and request.headers["content-type"] == "application/json"
            assert json.loads(request.content) == {"user_id": "self"}
            group_id = int(match[1])
            created = {"id": 100 + group_id, "group_id": group_id, "user_id": 7,
                       "workflow_state": self.new_state, "moderator": False, "just_created": True}
            if not self.ignore_write:
                self.members[group_id] = copy.deepcopy(created)
            response = httpx.Response(200, json=self.create_override if self.create_override is not None else created)
        elif match := re.fullmatch(r"groups/(\d+)/users/self", path):
            assert request.method == "DELETE" and request.content == b""
            if not self.ignore_write:
                self.members.pop(int(match[1]), None)
            response = httpx.Response(200, json={"ok": True})
        else:
            raise AssertionError((request.method, path))
        if self.after_write:
            self.after_write(self)
        return response


@pytest.fixture
def upstream():
    result = MembershipHTTP()
    yield result
    result.close()


def test_nonmember_options_use_visible_course_catalog_not_member_only_group_show(upstream):
    upstream.errors["GET", "groups/2"] = (403, {"message": "not a member"})
    result = memberships.list_membership_options(upstream.canvas, 1)
    option = result["data"]["groups"][0]
    assert result["status"] == "ok" and result["coverage"]["complete"] is True
    assert option["self_membership"] == {"status": "absent"}
    assert option["actions"] == ["group_join"]
    assert not upstream.writes
    assert not any(request.url.path in {"/api/v1/groups/2", "/api/v1/groups/2/permissions"} for request in upstream.seen)


@pytest.mark.parametrize("state", ["accepted", "requested"])
def test_join_distinguishes_accepted_from_pending_and_receipt_replays_once(upstream, state):
    upstream.new_state = state
    store = ActionStore()
    prepared = store.prepare(upstream.canvas, "alice", action())
    assert prepared["execute"]["tool"] == "canvas_manage_group_membership"
    assert prepared["review"]["target"]["self_membership"] == {"status": "absent"}
    assert not upstream.writes
    result = store.execute(upstream.canvas, "alice", prepared["action_id"], lambda: None)
    assert result["state"] == "succeeded", result
    assert result["result"]["membership_state"] == state
    assert result["result"]["accepted"] is (state == "accepted")
    assert result["result"]["pending_approval"] is (state == "requested")
    assert result["result"]["membership"]["user_id"] == 7
    assert len(upstream.writes) == 1
    assert store.execute(upstream.canvas, "alice", prepared["action_id"], lambda: None)["state"] == "succeeded"
    assert len(upstream.writes) == 1


@pytest.mark.parametrize("kind,state", [("group_leave", "accepted"), ("group_cancel_join_request", "requested")])
def test_leave_and_cancel_use_exact_self_and_independent_absence(upstream, kind, state):
    upstream.add_member(state=state)
    store = ActionStore()
    prepared = store.prepare(upstream.canvas, "alice", action(kind))
    result = store.execute(upstream.canvas, "alice", prepared["action_id"], lambda: None)
    assert result["state"] == "succeeded", result
    assert result["result"]["previous_membership_id"] == 102
    assert result["result"]["membership_state"] == "absent"
    assert [(r.method, r.url.path) for r in upstream.writes] == [("DELETE", "/api/v1/groups/2/users/self")]
    assert any(r.method == "GET" and r.url.path.endswith("/users/self") for r in upstream.seen[upstream.seen.index(upstream.writes[0]) + 1:])


@pytest.mark.parametrize("status", [401, 403])
def test_unreadable_self_membership_is_unavailable_not_absent(upstream, status):
    upstream.errors["GET", "groups/2/users/self"] = (status, {"message": "not permitted"})
    result = memberships.list_membership_options(upstream.canvas, 1)
    option = result["data"]["groups"][0]
    assert result["status"] == "partial" and result["coverage"]["complete"] is False
    assert option["self_membership"]["status"] == "unavailable" and not option["actions"]
    with pytest.raises(ValueError, match="absence is not confirmed"):
        memberships.snapshot(upstream.canvas, action())
    assert not upstream.writes


def test_ordinary_invitation_is_handoff_never_reinterpreted_as_join(upstream):
    upstream.add_member(state="invited")
    option = memberships.list_membership_options(upstream.canvas, 1)["data"]["groups"][0]
    assert option["self_membership"]["membership"]["workflow_state"] == "invited"
    assert not option["actions"] and "invitation" in " ".join(option["reasons"])
    for kind in memberships.KINDS:
        with pytest.raises(ValueError, match="invitation"):
            memberships.snapshot(upstream.canvas, action(kind))
    for kind in ("group_invitation_accept", "group_invitation_decline"):
        with pytest.raises(ValidationError):
            ACTION_ADAPTER.validate_python(action(kind))
    assert not upstream.writes


@pytest.mark.parametrize("value", [False, None, "true", 1])
def test_join_requires_actual_boolean_permission(upstream, value):
    upstream.group["permissions"]["join"] = value
    with pytest.raises(ValueError, match="self-join"):
        memberships.snapshot(upstream.canvas, action())
    assert not upstream.writes


@pytest.mark.parametrize("kind,state", [("group_leave", "accepted"), ("group_cancel_join_request", "requested")])
@pytest.mark.parametrize("value", [False, None, "true", 1])
def test_leave_permission_is_not_inferred_from_membership(upstream, kind, state, value):
    upstream.add_member(state=state)
    upstream.leave = value
    with pytest.raises(ValueError, match="permission to leave"):
        memberships.snapshot(upstream.canvas, action(kind))


def test_leader_leave_is_explicit_guard_and_ordinary_members_can_leave(upstream):
    upstream.add_member()
    upstream.group["leader"] = {"id": 7, "display_name": "Current student"}
    with pytest.raises(ValueError, match="reassign its leadership"):
        memberships.snapshot(upstream.canvas, action("group_leave"))
    upstream.group["leader"] = {"id": 8, "display_name": "Another student"}
    assert memberships.snapshot(upstream.canvas, action("group_leave"))["rules"] == {"leave": True}
    assert not upstream.writes


def test_moderator_role_loss_is_reviewed_without_role_mutation_fields(upstream):
    upstream.add_member(moderator=True)
    assert "moderator role" in memberships.snapshot(upstream.canvas, action("group_leave"))["effect"]


def test_exclusive_category_switch_names_affected_group_and_never_posts(upstream):
    upstream.category.update(allows_multiple_memberships=False, is_member=True, self_signup="enabled")
    upstream.groups.append({**copy.deepcopy(upstream.group), "id": 3, "name": "Existing project team"})
    upstream.add_member(3)
    with pytest.raises(ValueError, match=r"Existing project team \(group 3\)"):
        memberships.snapshot(upstream.canvas, action())
    option = memberships.list_membership_options(upstream.canvas, 1, limit=1)["data"]["groups"][0]
    assert not option["actions"] and "implicitly" in " ".join(option["reasons"])
    assert not upstream.writes


def test_other_category_membership_is_not_hidden_by_accepted_only_list(upstream):
    upstream.category.update(allows_multiple_memberships=False, is_member=True)
    with pytest.raises(ValueError, match="another membership.*exact group is not visible"):
        memberships.snapshot(upstream.canvas, action())


def test_unassigned_student_can_join_self_signup_category_with_fallback_read(upstream):
    upstream.category.update(allows_multiple_memberships=False, is_member=False, self_signup="restricted")
    upstream.group["join_level"] = "invitation_only"  # not the self-signup permission signal
    upstream.group.pop("group_category")
    target = memberships.snapshot(upstream.canvas, action())
    assert target["rules"]["category"]["self_signup"] == "restricted"
    assert any(r.url.path == "/api/v1/group_categories/9" for r in upstream.seen)


@pytest.mark.parametrize("field,value", [("course_id", 3), ("context_type", "Account"), ("non_collaborative", True),
    ("non_collaborative", "false"), ("concluded", True), ("is_full", True)])
def test_wrong_context_tags_concluded_and_full_groups_cannot_join(upstream, field, value):
    upstream.group[field] = value
    with pytest.raises(ValueError):
        memberships.snapshot(upstream.canvas, action())
    assert not upstream.writes


@pytest.mark.parametrize("field,value", [("id", True), ("user_id", 8), ("group_id", 3),
    ("workflow_state", "unknown"), ("moderator", "false")])
def test_self_membership_requires_exact_identifiers_and_known_state(upstream, field, value):
    upstream.add_member()
    upstream.members[2][field] = value
    with pytest.raises(ValueError):
        memberships.snapshot(upstream.canvas, action("group_leave"))
    assert not upstream.writes


@pytest.mark.parametrize("change", [{"user_id": 8}, {"course_id": 2}, {"type": "TeacherEnrollment"}, {"enrollment_state": "invited"}])
def test_only_current_student_enrollment_is_accepted(upstream, change):
    upstream.enrollments[0].update(change)
    with pytest.raises(ValueError):
        memberships.list_membership_options(upstream.canvas, 1)
    assert not upstream.writes


@pytest.mark.parametrize("kind,state", [("group_join", None), ("group_leave", "accepted"), ("group_cancel_join_request", "requested")])
def test_success_without_persisted_change_is_uncertain_without_retry(upstream, kind, state):
    if state:
        upstream.add_member(state=state)
    store = ActionStore()
    prepared = store.prepare(upstream.canvas, "alice", action(kind))
    upstream.ignore_write = True
    result = store.execute(upstream.canvas, "alice", prepared["action_id"], lambda: None)
    assert result["state"] == "uncertain"
    assert store.execute(upstream.canvas, "alice", prepared["action_id"], lambda: None)["state"] == "uncertain"
    assert len(upstream.writes) == 1


@pytest.mark.parametrize("change", [{"id": 999}, {"user_id": 8}, {"group_id": 3}, {"workflow_state": "invited"}, {"moderator": True}])
def test_join_readback_matches_exact_created_ordinary_membership(upstream, change):
    store = ActionStore()
    prepared = store.prepare(upstream.canvas, "alice", action())
    upstream.after_write = lambda state: state.members[2].update(change)
    assert store.execute(upstream.canvas, "alice", prepared["action_id"], lambda: None)["state"] == "uncertain"
    assert len(upstream.writes) == 1


def test_audience_change_during_join_is_not_verified(upstream):
    store = ActionStore()
    prepared = store.prepare(upstream.canvas, "alice", action())
    upstream.after_write = lambda state: state.group.update(name="Changed audience")
    assert store.execute(upstream.canvas, "alice", prepared["action_id"], lambda: None)["state"] == "uncertain"


@pytest.mark.parametrize("kind,state", [("group_join", None), ("group_leave", "accepted"), ("group_cancel_join_request", "requested")])
def test_changed_current_user_after_dispatch_is_uncertain(upstream, kind, state):
    if state:
        upstream.add_member(state=state)
    store = ActionStore()
    prepared = store.prepare(upstream.canvas, "alice", action(kind))
    upstream.after_write = lambda state: state.profile.update(id=8)
    assert store.execute(upstream.canvas, "alice", prepared["action_id"], lambda: None)["state"] == "uncertain"
    assert len(upstream.writes) == 1


def test_join_is_not_verified_when_accepted_own_group_list_disagrees(upstream):
    upstream.own_override = []
    store = ActionStore()
    prepared = store.prepare(upstream.canvas, "alice", action())
    assert store.execute(upstream.canvas, "alice", prepared["action_id"], lambda: None)["state"] == "uncertain"


def test_automatic_leadership_is_reviewed_and_exact_join_still_succeeds(upstream):
    upstream.category["auto_leader"] = "first"
    store = ActionStore()
    prepared = store.prepare(upstream.canvas, "alice", action())
    assert "may appoint you leader" in prepared["review"]["target"]["effect"]
    upstream.after_write = lambda state: state.group.update(leader={"id": 7, "display_name": "Current student"})
    result = store.execute(upstream.canvas, "alice", prepared["action_id"], lambda: None)
    assert result["state"] == "succeeded" and result["result"]["leader"]["id"] == 7


@pytest.mark.parametrize("field,value", [("id", 10), ("course_id", 2), ("context_type", "Account"),
    ("allows_multiple_memberships", "false"), ("is_member", 0)])
def test_category_rules_require_exact_parent_and_boolean_membership(upstream, field, value):
    upstream.category[field] = value
    with pytest.raises(ValueError):
        memberships.snapshot(upstream.canvas, action())
    assert not upstream.writes


@pytest.mark.parametrize("kind,state", [("group_join", None), ("group_leave", "accepted"), ("group_cancel_join_request", "requested")])
@pytest.mark.parametrize("change", ["membership", "permission", "name"])
def test_preflight_rejects_changed_membership_permissions_and_audience(upstream, kind, state, change):
    if state:
        upstream.add_member(state=state)
    store = ActionStore()
    prepared = store.prepare(upstream.canvas, "alice", action(kind))
    if change == "name":
        upstream.group["name"] = "Changed group"
    elif change == "membership":
        upstream.add_member(state="invited")
    elif state:
        upstream.leave = False
    else:
        upstream.group["permissions"]["join"] = False
    result = store.execute(upstream.canvas, "alice", prepared["action_id"], lambda: None)
    assert result["state"] == "rejected" and not result["result"]["upstream_write_attempted"]
    assert not upstream.writes


@pytest.mark.parametrize("kind,state,path", [("group_join", None, "memberships"), ("group_leave", "accepted", "users/self")])
@pytest.mark.parametrize("status", [302, 307, 429, 503])
def test_mutations_never_replay_on_redirect_or_retryable_status(upstream, kind, state, path, status):
    if state:
        upstream.add_member(state=state)
    store = ActionStore()
    prepared = store.prepare(upstream.canvas, "alice", action(kind))
    method = "POST" if state is None else "DELETE"
    upstream.errors[method, "groups/2/" + path] = (status, {"message": "uncertain"},
        {"Location": "https://school.example/api/v1/groups/3/memberships", "Retry-After": "0"})
    result = store.execute(upstream.canvas, "alice", prepared["action_id"], lambda: None)
    assert result["state"] == "uncertain" and len(upstream.writes) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_post_delete_access_failure_is_uncertain_not_absence(upstream, status):
    upstream.add_member()
    store = ActionStore()
    prepared = store.prepare(upstream.canvas, "alice", action("group_leave"))
    upstream.after_write = lambda state: state.errors.update({("GET", "groups/2/users/self"): (status, {"message": "unavailable"})})
    assert store.execute(upstream.canvas, "alice", prepared["action_id"], lambda: None)["state"] == "uncertain"


@pytest.mark.parametrize("source", ["catalog", "own"])
def test_discovery_bound_does_not_turn_truncation_into_absence(upstream, source):
    rows = [{**copy.deepcopy(upstream.group), "id": index + 2} for index in range(1001)]
    if source == "catalog":
        upstream.groups = rows
    else:
        upstream.own_override = rows
    with pytest.raises(ValueError, match="1000"):
        memberships.snapshot(upstream.canvas, action())
    assert not upstream.writes


def test_window_and_partial_membership_coverage_are_independent(upstream):
    upstream.groups.extend([{**copy.deepcopy(upstream.group), "id": 3}, {**copy.deepcopy(upstream.group), "id": 4}])
    result = memberships.list_membership_options(upstream.canvas, 1, offset=1, limit=1)
    assert result["coverage"]["complete"] is False and result["coverage"]["next_offset"] == 2
    assert result["coverage"]["membership_checks_complete"] is True
    assert result["data"]["groups"][0]["group"]["id"] == 3
    assert [r.url.path for r in upstream.seen if r.url.path.endswith("/users/self")] == ["/api/v1/groups/3/users/self"]


def test_rate_limit_is_not_a_membership_permission_denial(upstream):
    upstream.errors["GET", "groups/2/users/self"] = (403, {"message": "rate limit exceeded"}, {"Retry-After": "0"})
    with pytest.raises(CanvasError):
        memberships.list_membership_options(upstream.canvas, 1)


@pytest.mark.parametrize("extra", [{"user_id": 8}, {"moderator": True}, {"workflow_state": "accepted"},
    {"members": [8]}, {"all_in_group_course": True}, {"group_id": 2}, {"url": "https://elsewhere.example"}])
def test_payload_cannot_select_other_users_or_admin_fields(extra):
    with pytest.raises(ValidationError):
        ACTION_ADAPTER.validate_python({**action(), **extra})


@pytest.mark.parametrize("context", [{"context_type": "course", "course_id": 1}, {"context_type": "group", "group_id": "2"},
    {"context_type": "group", "group_id": True}, {"context_type": "group", "group_id": 2**63}])
def test_explicit_group_context_is_strict_and_bounded(context):
    with pytest.raises(ValidationError):
        ACTION_ADAPTER.validate_python(action(context=context))
