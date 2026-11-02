"""Synthetic module progress checks; never write to a real Canvas account."""
import copy
from typing import Annotated
from unittest.mock import Mock

import httpx
import pytest
from canvas_py import Canvas
from canvas_py._config import CanvasConfig
from canvas_py._errors import CanvasError
from pydantic import Field, TypeAdapter, ValidationError

from canvas_connector import module_progress as progress
from canvas_connector.actions import ActionStore


ADAPTER = TypeAdapter(Annotated[progress.ModuleProgressActionTypes, Field(discriminator="kind")])
BASE = {"course_id": 1, "module_id": 2, "item_id": 3}
DONE = {"kind": "module_mark_done", **BASE}
READ = {"kind": "module_mark_read", **BASE}
MODULE_PATH = "courses/1/modules/2"
ITEM_PATH = MODULE_PATH + "/items/3"
ITEMS_PATH = MODULE_PATH + "/items"


class ModuleCanvas:
    def __init__(self, requirement="must_mark_done"):
        self.config = CanvasConfig("https://school.example", "synthetic-token")
        self.navigation = Mock()
        self.actor = 7
        self.module = {"id": 2, "name": "Study", "state": "started",
                       "require_sequential_progress": False, "prerequisite_module_ids": []}
        self.item = {"id": 3, "module_id": 2, "title": "Reading", "type": "Page",
                     "page_url": "reading", "completion_requirement": {"type": requirement, "completed": False},
                     "content_details": {"locked_for_user": False}}
        self.requests, self.writes = [], []
        self.ignore_write = False
        self.after_write = None
        self.readback_error = None

    def send(self, request):
        method, path = request.method, request.path
        self.requests.append((method, path, request.query_pairs(), request.json_body()))
        assert request.json_body() is None
        assert not any(key in {"student_id", "as_user_id"} for key, _ in request.query_pairs())
        if method != "GET":
            self.writes.append((method, path))
            assert request.query_pairs() == []
            assert (method, path) in {("PUT", ITEM_PATH + "/done"),
                                      ("DELETE", ITEM_PATH + "/done"),
                                      ("POST", ITEM_PATH + "/mark_read")}
            if not self.ignore_write:
                self.item["completion_requirement"]["completed"] = method != "DELETE"
            if self.after_write:
                self.after_write(self)
            return {"message": "OK"}
        if path == "users/self/profile":
            return {"id": self.actor}
        if path == MODULE_PATH:
            return copy.deepcopy(self.module)
        if path == ITEMS_PATH:
            assert request.query_pairs() == [("include[]", "content_details"), ("per_page", 100)]
            if self.writes and self.readback_error:
                raise self.readback_error
            return [copy.deepcopy(self.item)]
        raise AssertionError((method, path))

    def paginate(self, request):
        yield from self.send(request)


@pytest.mark.parametrize("payload,requirement,method,endpoint,expected", [
    (DONE, "must_mark_done", "PUT", "done", True),
    ({**DONE, "done": False}, "must_mark_done", "DELETE", "done", False),
    (READ, "must_view", "POST", "mark_read", True),
])
def test_exact_progress_endpoint_and_readback(payload, requirement, method, endpoint, expected):
    canvas = ModuleCanvas(requirement)
    canvas.item["completion_requirement"]["completed"] = not expected
    target = progress.snapshot(canvas, payload)
    assert target["user_id"] == 7
    assert not canvas.writes
    result = progress.perform(canvas, payload, target)
    assert result["verified"] is True
    assert result["completion_requirement"] == {"type": requirement, "completed": expected}
    assert canvas.writes == [(method, ITEM_PATH + "/" + endpoint)]
    assert canvas.requests[-1][:2] == ("GET", ITEMS_PATH)
    assert progress.ROUTES[payload["kind"]] == "canvas_update_module_progress"


@pytest.mark.parametrize("field", ["course_id", "module_id", "item_id"])
@pytest.mark.parametrize("value", [0, -1, True, "2", 2.0, 2**63])
def test_ids_are_strict_positive_and_bounded(field, value):
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({**DONE, field: value})


@pytest.mark.parametrize("extra", [{"student_id": 9}, {"user_id": 9}, {"as_user_id": 9},
                                   {"url": "https://other.example"}, {"completion_requirement": {"type": "must_view"}}])
def test_no_target_user_or_arbitrary_mutation_fields(extra):
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({**DONE, **extra})


@pytest.mark.parametrize("value", [0, 1, "true", "false", None])
def test_done_requires_boolean(value):
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({**DONE, "done": value})
    assert ADAPTER.validate_python(DONE).done is True


def test_read_is_one_way_viewed_state():
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({**READ, "done": False})


@pytest.mark.parametrize("requirement", [None, {}, {"type": "must_view", "completed": False},
    {"type": "must_submit", "completed": False}, {"type": "must_contribute", "completed": False},
    {"type": "min_score", "completed": False}, {"type": "min_percentage", "completed": False},
    {"type": "future_requirement", "completed": False}, {"type": "must_mark_done"},
    {"type": "must_mark_done", "completed": 0}])
def test_done_cannot_complete_other_or_unknown_requirements(requirement):
    canvas = ModuleCanvas()
    canvas.item["completion_requirement"] = requirement
    with pytest.raises(ValueError):
        progress.snapshot(canvas, DONE)
    assert not canvas.writes


def test_read_cannot_complete_mark_done_requirement():
    canvas = ModuleCanvas()
    with pytest.raises(ValueError, match="must_view"):
        progress.snapshot(canvas, READ)
    assert not canvas.writes


@pytest.mark.parametrize("location,change", [
    ("module", {"id": 9}), ("item", {"id": 9}), ("item", {"module_id": 9}),
    ("item", {"module_id": None}), ("module", {"state": "locked"}),
    ("module", {"state": None}), ("module", {"state": "unknown"}),
    ("module", {"published": False}), ("item", {"published": False}),
    ("item", {"published": None}), ("module", {"workflow_state": "deleted"}),
    ("item", {"locked_for_user": True}), ("item", {"content_details": None}),
    ("item", {"content_details": {}}), ("item", {"content_details": {"locked_for_user": 0}}),
    ("item", {"content_details": {"locked_for_user": True}}),
    ("item", {"content_details": {"locked_for_user": False, "hidden": True}}),
])
def test_unknown_locked_unpublished_or_moved_items_rejected(location, change):
    canvas = ModuleCanvas()
    getattr(canvas, location).update(change)
    error = CanvasError if location == "item" and {"id", "module_id"} & change.keys() else ValueError
    with pytest.raises(error):
        progress.snapshot(canvas, DONE)
    assert not canvas.writes


@pytest.mark.parametrize("actor", [None, True, "7", 0])
def test_current_user_must_be_confirmed(actor):
    canvas = ModuleCanvas()
    canvas.actor = actor
    with pytest.raises(ValueError, match="identity"):
        progress.snapshot(canvas, DONE)


def test_unknown_lock_provides_exact_trusted_canvas_item_url():
    canvas = ModuleCanvas()
    canvas.item["content_details"] = {}
    canvas.item["html_url"] = "https://untrusted.example/ignore-me"
    with pytest.raises(ValueError, match="open https://school.example/courses/1/modules/items/3 in Canvas"):
        progress.snapshot(canvas, DONE)


def test_snapshot_ignores_incidental_fields_and_tracks_relevant_changes():
    canvas = ModuleCanvas()
    before = progress.snapshot(canvas, DONE)
    canvas.module.update(items_count=99, updated_at="later", completed_at="later")
    canvas.item.update(position=7, indent=2, html_url="https://school.example/new-link")
    canvas.item["content_details"].update(points_possible=10, due_at="tomorrow")
    assert progress.snapshot(canvas, DONE) == before
    canvas.item["completion_requirement"]["completed"] = True
    assert progress.snapshot(canvas, DONE) != before
    canvas.item["completion_requirement"]["completed"] = False
    canvas.item["page_url"] = "different-content"
    assert progress.snapshot(canvas, DONE) != before


@pytest.mark.parametrize("payload,requirement", [(DONE, "must_mark_done"),
    ({**DONE, "done": False}, "must_mark_done"), (READ, "must_view")])
def test_success_response_without_exact_saved_state_is_not_verified(payload, requirement):
    canvas = ModuleCanvas(requirement)
    canvas.item["completion_requirement"]["completed"] = not payload.get("done", True)
    target = progress.snapshot(canvas, payload)
    canvas.ignore_write = True
    with pytest.raises(ValueError, match="not confirmed"):
        progress.perform(canvas, payload, target)
    assert len(canvas.writes) == 1


@pytest.mark.parametrize("change", [
    {"id": 99}, {"module_id": 99},
    {"completion_requirement": {"type": "must_submit", "completed": True}},
    {"completion_requirement": {"type": "must_mark_done", "completed": 1}},
])
def test_wrong_readback_identity_or_requirement_is_not_verified(change):
    canvas = ModuleCanvas()
    target = progress.snapshot(canvas, DONE)
    canvas.after_write = lambda c: c.item.update(change)
    with pytest.raises(CanvasError if {"id", "module_id"} & change.keys() else ValueError):
        progress.perform(canvas, DONE, target)
    assert len(canvas.writes) == 1


def test_readback_failure_propagates_without_repeating_write():
    canvas = ModuleCanvas()
    target = progress.snapshot(canvas, DONE)
    canvas.readback_error = CanvasError("read failed", status=503)
    with pytest.raises(CanvasError):
        progress.perform(canvas, DONE, target)
    assert len(canvas.writes) == 1


def test_target_must_match_payload():
    canvas = ModuleCanvas()
    target = progress.snapshot(canvas, DONE)
    target["item_id"] = 9
    with pytest.raises(ValueError):
        progress.perform(canvas, DONE, target)
    assert not canvas.writes


@pytest.mark.parametrize("payload,requirement", [
    (DONE, "must_mark_done"), ({**DONE, "done": False}, "must_mark_done"), (READ, "must_view"),
])
def test_action_store_routes_and_verifies_progress_once(payload, requirement):
    canvas, store = ModuleCanvas(requirement), ActionStore()
    canvas.item["completion_requirement"]["completed"] = not payload.get("done", True)
    prepared = store.prepare(canvas, "alice", payload)
    assert prepared["state"] == "prepared"
    assert prepared["execute"]["tool"] == "canvas_update_module_progress"
    assert prepared["review"]["target"]["user_id"] == canvas.actor
    assert not canvas.writes
    result = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
    assert result["state"] == "succeeded"
    assert result["result"]["verified"] is True
    assert store.execute(canvas, "alice", prepared["action_id"], lambda: None)["state"] == "succeeded"
    assert len(canvas.writes) == 1


@pytest.mark.parametrize("payload,requirement", [(DONE, "must_mark_done"), (READ, "must_view")])
def test_action_store_rejects_changed_completion_before_dispatch(payload, requirement):
    canvas, store = ModuleCanvas(requirement), ActionStore()
    prepared = store.prepare(canvas, "alice", payload)
    canvas.item["completion_requirement"]["completed"] = True
    result = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
    assert result["state"] == "rejected"
    assert result["result"]["upstream_write_attempted"] is False
    assert not canvas.writes


def test_real_http_client_uses_fixed_paths_and_content_details():
    state = ModuleCanvas("must_view")
    seen = []

    def handler(request):
        seen.append(request)
        path = request.url.path.removeprefix("/api/v1/")
        assert request.headers["authorization"] == "Bearer synthetic-token"
        if path == "users/self/profile":
            body = {"id": state.actor}
        elif path == MODULE_PATH:
            body = state.module
        elif path == ITEMS_PATH:
            assert request.url.params.get_list("include[]") == ["content_details"]
            assert request.url.params["per_page"] == "100"
            body = [state.item]
        elif path == ITEM_PATH + "/mark_read":
            assert request.method == "POST" and request.content == b""
            state.item["completion_requirement"]["completed"] = True
            body = {"message": "OK"}
        else:
            raise AssertionError(request.url)
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with Canvas(CanvasConfig("https://school.example", "synthetic-token"), http_client=http) as canvas:
            target = progress.snapshot(canvas, READ)
            assert progress.perform(canvas, READ, target)["verified"] is True
    assert [request.method for request in seen] == ["GET", "GET", "GET", "POST", "GET"]
    assert all("student_id" not in request.url.params for request in seen)
