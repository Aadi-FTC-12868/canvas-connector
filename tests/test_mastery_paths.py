"""Student Mastery Path selection against synthetic HTTP, never live Canvas."""
import copy
import json

import httpx
import pytest
from canvas_py import Canvas
from canvas_py._config import CanvasConfig
from canvas_py._errors import CanvasError
from pydantic import ValidationError

from canvas_connector import mastery_paths as mp

SELECT = {"kind": "mastery_path_select", "course_id": 1, "module_id": 2,
          "item_id": 3, "assignment_set_id": 40}


class MasteryHTTP:
    def __init__(self):
        self.profile = {"id": 7}
        self.course = {"id": 1, "name": "Synthetic course", "workflow_state": "available"}
        self.enrollments = [{"id": 11, "user_id": 7, "course_id": 1,
                             "type": "StudentEnrollment", "enrollment_state": "active"}]
        self.permissions = {"read": True, "participate_as_student": True}
        self.module = {"id": 2, "name": "Synthetic module", "state": "started", "require_sequential_progress": False}
        self.item = {"id": 3, "module_id": 2, "type": "Assignment", "content_id": 10,
                     "title": "Source lab", "content_details": {"locked_for_user": False}}
        self.current = {k: v for k, v in self.item.items() if k != "content_details"}
        self.sets = []
        self.assignments = {}
        for set_id, assignment_id, position in [(40, 101, 1), (50, 102, 2)]:
            model = {"id": assignment_id, "course_id": 1, "name": "Same title in different options",
                     "points_possible": 10, "published": True, "grading_type": "points",
                     "submission_types": ["online_text_entry"], "due_at": None,
                     "locked_for_user": False}
            self.assignments[assignment_id] = model
            self.sets.append({"id": set_id, "position": position,
                "assignment_set_associations": [{"id": set_id + 1, "assignment_set_id": set_id,
                    "assignment_id": assignment_id, "position": 1, "model": model}]})
        self.path = {"locked": False, "selected_set_id": None, "awaiting_choice": True, "is_student": True}
        self.requests, self.writes = [], []
        self.ignore_write = False
        self.cached_readback = False
        self.actual_selection = None
        self.after_write = None
        self.errors = {}
        self.write_status = None
        self.sequence_transform = None
        self.list_transform = None
        self.other_items = []
        self.page_size = 100
        self.canvas = Canvas(CanvasConfig("https://school.example", "synthetic-token"),
                             http_client=httpx.Client(transport=httpx.MockTransport(self.handle)))

    def state_path(self):
        return {**copy.deepcopy(self.path), "assignment_sets": copy.deepcopy(self.sets)}

    def handle(self, request):
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/v1/")
        if request.method != "GET":
            self.writes.append(request)
            assert request.method == "POST"
            assert path == "courses/1/modules/2/items/3/select_mastery_path"
            body = json.loads(request.content)
            assert set(body) == {"assignment_set_id"}
            if self.write_status:
                return httpx.Response(self.write_status, json={"message": "synthetic denial"},
                    headers={"Location": str(request.url), "Retry-After": "0"})
            if not self.ignore_write:
                self.actual_selection = body["assignment_set_id"]
                if not self.cached_readback:
                    self.path.update(selected_set_id=self.actual_selection, awaiting_choice=False)
            if self.after_write:
                self.after_write(self)
            return httpx.Response(200, json={"meta": {"primaryCollection": "assignments"},
                "assignments": [self.assignments[101]], "items": [], "selected_set_id": body["assignment_set_id"]})
        assert not request.content
        if path in self.errors:
            return httpx.Response(self.errors[path], json={"message": "private-upstream-body"})
        if path == "users/self/profile":
            result = self.profile
        elif path == "courses/1":
            result = self.course
        elif path == "courses/1/enrollments":
            assert request.url.params["user_id"] == str(self.profile["id"])
            assert request.url.params.get_list("type[]") == ["StudentEnrollment"]
            result = self.enrollments
        elif path == "courses/1/permissions":
            assert set(request.url.params.get_list("permissions[]")) == {"read", "participate_as_student"}
            result = self.permissions
        elif path == "courses/1/modules/2":
            result = self.module
        elif path == "courses/1/module_item_sequence":
            assert request.url.params["asset_type"] == "ModuleItem"
            assert request.url.params["asset_id"] == "3"
            assert "student_id" not in request.url.params
            path_data = self.state_path()
            # Sequence embeds raw AR model metadata. Deliberately not normalized
            # Assignment JSON: the reader must not depend on its field names.
            for option in path_data["assignment_sets"]:
                for association in option.get("assignment_set_associations", []):
                    association["model"] = {"id": association["assignment_id"], "context_id": 1,
                                             "context_type": "Course", "title": "Raw model title"}
            result = {"items": [{"current": self.current, "prev": None, "next": None,
                                 "mastery_path": path_data}], "modules": [self.module]}
            if self.sequence_transform:
                result = self.sequence_transform(result)
        elif path == "courses/1/modules/2/items":
            if request.url.params.get("page") is None:
                assert set(request.url.params.get_list("include[]")) == {"content_details", "mastery_paths"}
            path_data = self.state_path()
            for field in ("awaiting_choice", "still_processing", "is_student"):
                path_data.pop(field, None)
            rows = [*self.other_items, {**self.item, "mastery_paths": path_data}]
            if self.list_transform:
                rows = self.list_transform(rows)
            if not isinstance(rows, list):
                return httpx.Response(200, json=rows)
            start = (int(request.url.params.get("page", 1)) - 1) * self.page_size
            more = start + self.page_size < len(rows)
            headers = {"Link": '<https://school.example/api/v1/courses/1/modules/2/items?page=2>; rel="next"'} if more else {}
            return httpx.Response(200, json=rows[start:start + self.page_size], headers=headers)
        elif path.startswith("courses/1/assignments/"):
            assignment_id = int(path.rsplit("/", 1)[1])
            assert not request.url.query
            result = self.assignments[assignment_id]
        else:
            raise AssertionError((request.method, path))
        return httpx.Response(200, json=result)


def test_exact_offered_set_write_independent_readback_and_access():
    state = MasteryHTTP()
    before = mp.snapshot(state.canvas, SELECT)
    assert not state.writes
    assert before["chosen_set"]["assignment_set_associations"][0]["assignment_id"] == 101
    assert before["assignment_sets"][0]["assignment_set_associations"][0]["assignment"]["name"] == "Same title in different options"
    result = mp.perform(state.canvas, SELECT, before)
    assert len(state.writes) == 1
    assert json.loads(state.writes[0].content) == {"assignment_set_id": 40}
    assert result["verified"] and result["selection_verified"]
    assert result["selected_set_id"] == 40
    assert result["assignment_access"]["complete"] is True
    assert result["coverage"]["complete"] is True
    assert result["coursework_complete"] is None
    assert state.requests[-1].method == "GET"
    assert mp.ROUTES == {"mastery_path_select": "canvas_select_mastery_path"}
    assert not any(r.url.path.endswith("/items/3") or "/mark_read" in r.url.path or "/done" in r.url.path for r in state.requests)


def test_read_has_bounded_exact_options_and_never_selects_or_opens_conditional_assignments():
    state = MasteryHTTP()
    result = mp.read_mastery_paths(state.canvas, 1, 2, 3, limit=1)
    assert result["status"] == "ok"
    assert result["data"]["availability"]["can_select"] is True
    assert result["data"]["mastery_path"]["selected_set_id"] is None
    assert len(result["data"]["assignment_sets"]) == 1
    assert result["data"]["assignment_sets"][0]["id"] == 40
    assert result["coverage"]["next_offset"] == 1
    assert result["coverage"]["complete"] is False
    assert result["source"]["checked_at"]
    assert result["data"]["coursework_complete"] is None
    assert not any("/assignments/" in r.url.path for r in state.requests)
    assert not state.writes


@pytest.mark.parametrize("field", ["course_id", "module_id", "item_id", "assignment_set_id"])
@pytest.mark.parametrize("value", [True, 0, -1, "1", 1.0, 2**63, None])
def test_strict_positive_ids(field, value):
    with pytest.raises(ValidationError):
        mp.MasteryPathSelect.model_validate({**SELECT, field: value})


@pytest.mark.parametrize("extra", [{"student_id": 8}, {"user_id": 7}, {"as_user_id": 9},
    {"assignment_ids": [101]}, {"option_index": 1}, {"url": "https://evil.example"}, {"done": True}])
def test_no_actor_override_hidden_assignment_ids_index_url_or_completion_input(extra):
    with pytest.raises(ValidationError):
        mp.MasteryPathSelect.model_validate({**SELECT, **extra})


@pytest.mark.parametrize("target,key,value", [
    ("profile", "id", True), ("profile", "id", "7"), ("course", "id", 8),
    ("course", "workflow_state", "completed"), ("course", "access_restricted_by_date", True),
    ("enrollment", "user_id", 8), ("enrollment", "course_id", 8),
    ("enrollment", "type", "TeacherEnrollment"), ("enrollment", "enrollment_state", "completed"),
    ("permissions", "read", False), ("permissions", "participate_as_student", False),
    ("module", "id", 8), ("module", "state", "locked"), ("module", "published", False),
    ("module", "workflow_state", "deleted"),
    ("current", "id", 8), ("current", "module_id", 8), ("current", "content_id", 99),
    ("item", "module_id", 8), ("item", "published", False),
    ("item", "workflow_state", "deleted"),
    ("path", "locked", True), ("path", "awaiting_choice", False), ("path", "is_student", False),
    ("path", "still_processing", True), ("path", "still_processing", "false"),
])
def test_wrong_identity_unavailable_or_unknown_policy_rejected_before_write(target, key, value):
    state = MasteryHTTP()
    objects = {"profile": state.profile, "course": state.course, "enrollment": state.enrollments[0],
        "permissions": state.permissions, "module": state.module, "current": state.current, "item": state.item, "path": state.path}
    objects[target][key] = value
    with pytest.raises(ValueError):
        mp.snapshot(state.canvas, SELECT)
    assert not state.writes


def test_missing_selected_state_is_not_treated_as_an_unselected_path():
    state = MasteryHTTP()
    state.path.pop("selected_set_id")
    with pytest.raises(ValueError, match="omitted"):
        mp.snapshot(state.canvas, SELECT)


def test_previously_selected_path_has_explicit_current_boundary_and_readable_state():
    state = MasteryHTTP()
    state.path.update(selected_set_id=50, awaiting_choice=False)
    result = mp.read_mastery_paths(state.canvas, 1, 2, 3)
    assert result["data"]["mastery_path"]["selected_set_id"] == 50
    assert result["data"]["availability"]["can_select"] is False
    assert "API also supports changing" in result["data"]["availability"]["reasons"][0]
    with pytest.raises(ValueError, match="initial selection only"):
        mp.snapshot(state.canvas, SELECT)
    assert not state.writes


def test_singleton_is_automatic_not_an_offered_manual_choice():
    state = MasteryHTTP()
    state.sets = state.sets[:1]
    state.path.update(selected_set_id=40, awaiting_choice=False, still_processing=True)
    result = mp.read_mastery_paths(state.canvas, 1, 2, 3)
    assert result["data"]["mastery_path"]["still_processing"] is True
    assert result["data"]["availability"]["can_select"] is False
    assert result["data"]["coursework_complete"] is None
    with pytest.raises(ValueError):
        mp.snapshot(state.canvas, SELECT)


def test_no_rule_or_disabled_feature_is_unavailable_discovery_not_empty_success():
    state = MasteryHTTP()
    state.sequence_transform = lambda result: {**result, "items": [{**result["items"][0], "mastery_path": None}]}
    state.list_transform = lambda rows: [{**rows[0], "mastery_paths": None}]
    result = mp.read_mastery_paths(state.canvas, 1, 2, 3)
    assert result["status"] == "unavailable"
    assert result["coverage"]["complete"] is False
    assert result["data"]["availability"]["can_select"] is False
    assert not state.writes


def test_ungraded_source_is_locked_without_offered_sets():
    state = MasteryHTTP()
    state.sets = []
    state.path.update(locked=True, awaiting_choice=False)
    result = mp.read_mastery_paths(state.canvas, 1, 2, 3)
    assert result["data"]["mastery_path"]["locked"] is True
    assert result["data"]["availability"]["can_select"] is False
    with pytest.raises(ValueError):
        mp.snapshot(state.canvas, SELECT)


@pytest.mark.parametrize("mode", ["duplicate_set", "duplicate_association", "duplicate_assignment", "wrong_set",
    "missing_associations", "wrong_model_id", "wrong_model_course", "missing_model", "unpublished", "empty_set"])
def test_exact_association_metadata_is_required_not_inferred_from_labels(mode):
    state = MasteryHTTP()
    option, association = state.sets[0], state.sets[0]["assignment_set_associations"][0]
    if mode == "duplicate_set":
        state.sets.append(copy.deepcopy(option))
    elif mode == "duplicate_association":
        option["assignment_set_associations"].append(copy.deepcopy(association))
    elif mode == "duplicate_assignment":
        option["assignment_set_associations"].append({**association, "id": 999})
    elif mode == "wrong_set":
        association["assignment_set_id"] = 50
    elif mode == "missing_associations":
        option.pop("assignment_set_associations")
    elif mode == "wrong_model_id":
        association["model"]["id"] = 999
    elif mode == "wrong_model_course":
        association["model"]["course_id"] = 9
    elif mode == "missing_model":
        association.pop("model")
    elif mode == "unpublished":
        association["model"]["published"] = False
    else:
        option["assignment_set_associations"] = []
    with pytest.raises(ValueError):
        mp.snapshot(state.canvas, SELECT)
    assert not state.writes


def test_unknown_set_cannot_be_selected_even_with_matching_assignment_title():
    state = MasteryHTTP()
    with pytest.raises(ValueError, match="not an exact offered choice"):
        mp.snapshot(state.canvas, {**SELECT, "assignment_set_id": 999})
    assert not state.writes


def test_sequence_and_list_must_offer_identical_association_ids():
    state = MasteryHTTP()
    def alter(result):
        result["items"][0]["mastery_path"]["assignment_sets"][0]["assignment_set_associations"][0]["assignment_id"] = 999
        return result
    state.sequence_transform = alter
    with pytest.raises(ValueError, match="changed between"):
        mp.snapshot(state.canvas, SELECT)


def test_bounded_module_list_follows_pages_and_refuses_overflow(monkeypatch):
    state = MasteryHTTP()
    state.other_items = [{"id": 4, "module_id": 2, "title": "Other item"}]
    state.page_size = 1
    assert mp.snapshot(state.canvas, SELECT)["item_id"] == 3
    assert any(r.url.params.get("page") == "2" for r in state.requests)
    monkeypatch.setattr(mp, "_MAX_DISCOVERY", 1)
    with pytest.raises(ValueError, match="safe limit"):
        mp.snapshot(state.canvas, SELECT)
    assert not state.writes


@pytest.mark.parametrize("mode", ["ignore_write", "cached_readback"])
def test_success_echo_or_stale_server_cache_cannot_confirm_selection(mode):
    state = MasteryHTTP()
    before = mp.snapshot(state.canvas, SELECT)
    setattr(state, mode, True)
    with pytest.raises(ValueError, match="not confirmed"):
        mp.perform(state.canvas, SELECT, before)
    assert len(state.writes) == 1
    if mode == "cached_readback":
        assert state.actual_selection == 40


@pytest.mark.parametrize("field,value", [("selected_set_id", 50), ("locked", True)])
def test_different_readback_state_is_not_success(field, value):
    state = MasteryHTTP()
    before = mp.snapshot(state.canvas, SELECT)
    state.after_write = lambda state: state.path.update({field: value})
    with pytest.raises(ValueError, match="not confirmed"):
        mp.perform(state.canvas, SELECT, before)


def test_current_student_identity_change_on_readback_is_not_success():
    state = MasteryHTTP()
    before = mp.snapshot(state.canvas, SELECT)
    def change(state):
        state.profile["id"] = 8
        state.enrollments[0]["user_id"] = 8
    state.after_write = change
    with pytest.raises(ValueError, match="user_id"):
        mp.perform(state.canvas, SELECT, before)


def test_selected_association_content_change_is_not_verified():
    state = MasteryHTTP()
    before = mp.snapshot(state.canvas, SELECT)
    def change(state):
        a = state.sets[0]["assignment_set_associations"][0]
        a.update(assignment_id=102, model=state.assignments[102])
    state.after_write = change
    with pytest.raises(ValueError, match="contents changed"):
        mp.perform(state.canvas, SELECT, before)


@pytest.mark.parametrize("status", [403, 404])
def test_selected_choice_and_assignment_access_are_separate(status):
    state = MasteryHTTP()
    before = mp.snapshot(state.canvas, SELECT)
    state.errors["courses/1/assignments/101"] = status
    result = mp.perform(state.canvas, SELECT, before)
    assert result["selection_verified"] is True
    assert result["selected_set_id"] == 40
    assert result["assignment_access"]["complete"] is False
    assert result["assignment_access"]["assignments"][0]["accessible"] is None
    assert result["coverage"]["complete"] is False
    assert result["coursework_complete"] is None
    assert "private-upstream-body" not in json.dumps(result)


def test_assignment_lock_and_due_date_change_do_not_undo_verified_selection():
    state = MasteryHTTP()
    before = mp.snapshot(state.canvas, SELECT)
    state.after_write = lambda state: state.assignments[101].update(locked_for_user=True, due_at="2026-11-30T10:00:00Z")
    result = mp.perform(state.canvas, SELECT, before)
    assert result["selection_verified"] is True
    assert result["coverage"]["complete"] is False
    assert result["assignment_access"]["assignments"][0]["locked_for_user"] is True


def test_access_verification_is_bounded_and_reports_unchecked_items(monkeypatch):
    state = MasteryHTTP()
    state.sets[0]["assignment_set_associations"].append({"id": 42, "assignment_id": 102,
        "assignment_set_id": 40, "position": 2, "model": state.assignments[102]})
    monkeypatch.setattr(mp, "_MAX_ACCESS", 1)
    result = mp.perform(state.canvas, SELECT, mp.snapshot(state.canvas, SELECT))
    assert result["selection_verified"] is True
    assert result["assignment_access"]["checked"] == 1
    assert result["assignment_access"]["unchecked"] == 1
    assert result["coverage"]["complete"] is False


def test_readback_failure_is_not_treated_as_no_path_or_success():
    state = MasteryHTTP()
    before = mp.snapshot(state.canvas, SELECT)
    state.after_write = lambda state: state.errors.update({"courses/1/module_item_sequence": 403})
    with pytest.raises(CanvasError):
        mp.perform(state.canvas, SELECT, before)
    assert len(state.writes) == 1


@pytest.mark.parametrize("status", [403, 429, 503, 301, 302, 307, 308])
def test_write_denial_or_redirect_is_never_retried(status):
    state = MasteryHTTP()
    before = mp.snapshot(state.canvas, SELECT)
    state.write_status = status
    with pytest.raises(CanvasError):
        mp.perform(state.canvas, SELECT, before)
    assert len(state.writes) == 1


@pytest.mark.parametrize("args", [{"course_id": True}, {"module_id": "2"}, {"item_id": 0},
    {"offset": -1}, {"limit": 101}, {"limit": 0}])
def test_bad_read_args_fail_before_http(args):
    state = MasteryHTTP()
    with pytest.raises(ValueError):
        mp.read_mastery_paths(state.canvas, **{"course_id": 1, "module_id": 2, "item_id": 3, **args})
    assert not state.requests
