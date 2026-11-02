"""Prepared, current-student module progress actions.

Contract: https://canvas.instructure.com/doc/api/modules.html
Mark-read records that the student viewed the content; it is not a cosmetic
read/unread flag. Only explicit prepared actions may invoke these helpers.
There are no teacher requirement edits, target-user inputs, or arbitrary URLs.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from canvas_py import Canvas
from canvas_py._request import CanvasRequest, QueryPair
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .responses import dump

Id = Annotated[int, Field(gt=0, le=2**63 - 1, strict=True)]


class ModuleProgressTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    course_id: Id
    module_id: Id
    item_id: Id


class ModuleMarkDone(ModuleProgressTarget):
    kind: Literal["module_mark_done"]
    done: Annotated[bool, Field(strict=True)] = True


class ModuleMarkRead(ModuleProgressTarget):
    kind: Literal["module_mark_read"]


ModuleProgressActionTypes = Union[ModuleMarkDone, ModuleMarkRead]
KINDS = ["module_mark_done", "module_mark_read"]
ROUTES = {kind: "canvas_update_module_progress" for kind in KINDS}
_ADAPTER = TypeAdapter(Annotated[ModuleProgressActionTypes, Field(discriminator="kind")])


class _SelfProfile(CanvasRequest[dict[str, Any]]):
    @property
    def path(self) -> str:
        return "users/self/profile"


class _ModuleState(CanvasRequest[dict[str, Any]]):
    def __init__(self, action: ModuleProgressActionTypes):
        self.action = action

    @property
    def path(self) -> str:
        return f"courses/{self.action.course_id}/modules/{self.action.module_id}"


class _ItemState(_ModuleState):
    @property
    def path(self) -> str:
        return super().path + f"/items/{self.action.item_id}"

    def query_pairs(self) -> list[QueryPair]:
        return [("include[]", "content_details")]


class _SetProgress(_ItemState):
    @property
    def path(self) -> str:
        suffix = "done" if isinstance(self.action, ModuleMarkDone) else "mark_read"
        return super().path + "/" + suffix

    @property
    def method(self) -> str:
        if isinstance(self.action, ModuleMarkDone):
            return "PUT" if self.action.done else "DELETE"
        return "POST"

    def query_pairs(self) -> list[QueryPair]:
        return []


def _object(canvas: Canvas, request: CanvasRequest) -> dict:
    value = dump(canvas.send(request))
    if not isinstance(value, dict):
        raise ValueError("Canvas did not return the requested module progress object")
    return value


def _identity(value: dict, key: str, expected: int) -> None:
    if type(value.get(key)) is not int or value[key] != expected:
        raise ValueError("Canvas returned a different module or module item")


def _item_url(canvas: Canvas, action: ModuleProgressActionTypes) -> str:
    return f"{canvas.config.base_url}/courses/{action.course_id}/modules/items/{action.item_id}"


def _requirement(item: dict, action: ModuleProgressActionTypes, item_url: str) -> dict:
    requirement = item.get("completion_requirement")
    expected = "must_mark_done" if isinstance(action, ModuleMarkDone) else "must_view"
    # Canvas's existing requirement is authoritative. We never manufacture one
    # for an otherwise incompatible type or freeze Canvas's evolving type list.
    if not isinstance(requirement, dict) or requirement.get("type") != expected:
        raise ValueError(f"This action requires an existing {expected} completion requirement; open {item_url} in Canvas")
    if type(requirement.get("completed")) is not bool:
        raise ValueError(f"Canvas did not confirm the current student's completion state; open {item_url} in Canvas")
    return {"type": expected, "completed": requirement["completed"]}


def _available(value: dict, label: str, item_url: str) -> None:
    # Publication is legitimately absent for students without admin access.
    if "published" in value and value["published"] is not True:
        raise ValueError(f"{label} is unpublished or its publication state is unknown; open {item_url} in Canvas")
    if "workflow_state" in value and value["workflow_state"] != "active":
        raise ValueError(f"{label} is not active; open {item_url} in Canvas")
    for key in ("locked_for_user", "locked", "hidden"):
        if key in value and value[key] is not False:
            raise ValueError(f"{label} is locked or its lock state is unknown; open {item_url} in Canvas")


def snapshot(canvas: Canvas, p: dict) -> dict:
    """Read the exact item and access state; called again before dispatch."""
    action = _ADAPTER.validate_python(p)
    item_url = _item_url(canvas, action)
    profile = _object(canvas, _SelfProfile())
    actor = profile.get("id")
    if type(actor) is not int or actor < 1:
        raise ValueError("Canvas did not confirm the current user's identity")
    module = _object(canvas, _ModuleState(action))
    _identity(module, "id", action.module_id)
    _available(module, "Module", item_url)
    if module.get("state") not in {"unlocked", "started", "completed"}:
        raise ValueError(f"Module is locked or the current student's module state is unknown; open {item_url} in Canvas")
    item = _object(canvas, _ItemState(action))
    _identity(item, "id", action.item_id)
    _identity(item, "module_id", action.module_id)
    _available(item, "Module item", item_url)
    requirement = _requirement(item, action, item_url)
    details = item.get("content_details")
    # Official module_item_json always supplies content_details when requested.
    # Its content_details helper calls locked_json for both the underlying
    # content (Page/File/etc.) and the ContentTag fallback (ExternalUrl/etc.).
    # locked_json emits a boolean even when unlocked. Do not infer that an
    # omitted field on a custom/older server means the item is accessible.
    # https://github.com/instructure/canvas-lms/blob/master/lib/api/v1/context_module.rb
    # https://github.com/instructure/canvas-lms/blob/master/lib/api/v1/locked.rb
    if not isinstance(details, dict) or details.get("locked_for_user") is not False:
        raise ValueError(f"Module item is locked or its lock state is unknown; open {item_url} in Canvas")
    _available(details, "Module item content", item_url)
    # Exclude counts, timestamps, signed API links and unrelated content details.
    return {
        "user_id": actor, "course_id": action.course_id,
        "module_id": action.module_id, "item_id": action.item_id,
        "module": {key: module.get(key) for key in (
            "id", "name", "state", "published", "workflow_state", "unlock_at",
            "require_sequential_progress", "requirement_type", "prerequisite_module_ids")},
        "item": {
            **{key: item.get(key) for key in (
                "id", "module_id", "title", "type", "content_id", "page_url", "external_url", "published")},
            "completion_requirement": requirement,
            "content_details": {key: details.get(key) for key in (
                "locked_for_user", "locked", "hidden", "unlock_at", "lock_at")},
        },
    }


def perform(canvas: Canvas, p: dict, target: dict) -> dict:
    """Dispatch once, then confirm the exact item's completion requirement."""
    action = _ADAPTER.validate_python(p)
    item_url = _item_url(canvas, action)
    for key in ("course_id", "module_id", "item_id"):
        _identity(target, key, getattr(action, key))
    if type(target.get("user_id")) is not int or target["user_id"] < 1:
        raise ValueError("Prepared action has no confirmed current user")
    # The caller supplies its freshly revalidated snapshot. Validate the
    # requirement again so direct helper use cannot bypass the action's scope.
    _requirement(target.get("item") or {}, action, item_url)
    canvas.send(_SetProgress(action))
    item = _object(canvas, _ItemState(action))
    _identity(item, "id", action.item_id)
    _identity(item, "module_id", action.module_id)
    requirement = _requirement(item, action, item_url)
    expected = action.done if isinstance(action, ModuleMarkDone) else True
    if requirement["completed"] is not expected:
        raise ValueError("Exact module item completion state was not confirmed")
    return {"verified": True, "user_id": target["user_id"],
            "course_id": action.course_id, "module_id": action.module_id,
            "item_id": action.item_id, "completion_requirement": requirement}
