"""Initial, current-student selection of an exact offered Mastery Path.

Public contract: https://developerdocs.instructure.com/services/canvas/resources/modules
Use module_item_sequence for the current student's singular mastery_path state.
The official module-items controller additionally supports include[]=mastery_paths
(still undocumented in its parameter table), which serializes offered assignment
models through assignment_json. Sequence embeds raw models; never assume those
models have the same schema. Only identities are compared between the two reads.

Canvas's OverrideHandler and its spec support switching eligible sets. Its web
path-option.jsx disables alternatives once selected. This connector deliberately
implements initial selection only, not a claim that the API cannot switch.

Assignment release and submission-cache refresh can lag the POST. Fresh independent
GET must confirm selected_set_id. still_processing is only supplied for automatic
singleton paths; absence on a multi-option path is normal. Neither selection nor
assignment access establishes coursework completion. No GET marks content read.
"""
from __future__ import annotations

from itertools import islice
from typing import Annotated, Any, Literal

from canvas_py import Canvas
from canvas_py._errors import CanvasError
from canvas_py._request import CanvasRequest, QueryPair
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .responses import dump, envelope, window

Id = Annotated[int, Field(gt=0, le=2**63 - 1, strict=True)]
_ID = TypeAdapter(Id)
_MAX_DISCOVERY = 1000
_MAX_ACCESS = 30
_ALREADY_SELECTED = ("A Mastery Path is already selected. This connector currently supports "
    "initial selection only. Canvas's API also supports changing eligible selections; "
    "this workflow does not expose that change.")


class MasteryPathSelect(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["mastery_path_select"]
    course_id: Id
    module_id: Id
    item_id: Id
    assignment_set_id: Id


MasteryPathActionTypes = MasteryPathSelect
KINDS = ["mastery_path_select"]
ROUTES = {"mastery_path_select": "canvas_select_mastery_path"}


class _Request(CanvasRequest[Any]):
    def __init__(self, path: str, *, method: str = "GET", body: dict | None = None,
                 query: list[QueryPair] | None = None):
        self._path, self.method, self.body, self.query = path, method, body, query or []
        self.allow_retries = method == "GET"
        self.allow_redirects = method == "GET"

    @property
    def path(self) -> str:
        return self._path

    def json_body(self) -> dict | None:
        return self.body

    def query_pairs(self) -> list[QueryPair]:
        return self.query


def _object(canvas: Canvas, request: CanvasRequest) -> dict:
    value = dump(canvas.send(request))
    if not isinstance(value, dict):
        raise ValueError("Canvas did not return the requested Mastery Path object")
    return value


def _identity(value: dict, key: str, expected: int) -> None:
    if type(value.get(key)) is not int or value[key] != expected:
        raise ValueError(f"Canvas returned a different or unknown {key}")


def _all(canvas: Canvas, request: CanvasRequest) -> list[dict]:
    iterator = canvas.paginate(request)
    try:
        values = [dump(v) for v in islice(iterator, _MAX_DISCOVERY + 1)]
    except TypeError as error:
        raise ValueError("Canvas did not return a Mastery Path discovery list") from error
    finally:
        iterator.close()
    if len(values) > _MAX_DISCOVERY:
        raise ValueError("Mastery Path discovery exceeded its safe limit; open Canvas")
    if any(not isinstance(v, dict) for v in values):
        raise ValueError("Canvas did not return a Mastery Path discovery list")
    return values


def _context(canvas: Canvas, course_id: int) -> dict:
    actor = _ID.validate_python(_object(canvas, _Request("users/self/profile")).get("id"))
    course = _object(canvas, _Request(f"courses/{course_id}"))
    _identity(course, "id", course_id)
    enrollments = _all(canvas, _Request(f"courses/{course_id}/enrollments", query=[
        ("user_id", actor), ("type[]", "StudentEnrollment"),
        ("state[]", "active"), ("state[]", "completed")]))
    if not enrollments:
        raise ValueError("Canvas did not confirm the current student's enrollment")
    for enrollment in enrollments:
        _identity(enrollment, "user_id", actor)
        _identity(enrollment, "course_id", course_id)
        _ID.validate_python(enrollment.get("id"))
        if enrollment.get("type") != "StudentEnrollment" or enrollment.get("enrollment_state") not in {"active", "completed"}:
            raise ValueError("Canvas did not confirm the current student's enrollment")
    permissions = _object(canvas, _Request(f"courses/{course_id}/permissions", query=[
        ("permissions[]", "read"), ("permissions[]", "participate_as_student")]))
    if permissions.get("read") is not True:
        raise ValueError("Canvas did not grant course read access to the current student")
    return {"user_id": actor, "course_id": course_id,
        "course": {k: course[k] for k in ("id", "name", "workflow_state", "access_restricted_by_date") if k in course},
        "enrollments": sorted([{k: e[k] for k in ("id", "user_id", "course_id", "type", "enrollment_state")}
                               for e in enrollments], key=lambda e: e["id"]),
        "permissions": {k: permissions.get(k) for k in ("read", "participate_as_student")}}


def _set_signatures(sets: Any) -> list[dict]:
    if not isinstance(sets, list) or len(sets) > _MAX_DISCOVERY:
        raise ValueError("Canvas did not provide a bounded offered assignment-set list")
    seen_sets, seen_associations, total, result = set(), set(), 0, []
    for option in sets:
        if not isinstance(option, dict):
            raise ValueError("Canvas returned an unknown assignment-set shape")
        set_id = _ID.validate_python(option.get("id"))
        if set_id in seen_sets:
            raise ValueError("Canvas returned duplicate assignment sets")
        seen_sets.add(set_id)
        associations = option.get("assignment_set_associations")
        if not isinstance(associations, list):
            raise ValueError("Canvas did not provide the exact offered assignment associations")
        total += len(associations)
        if total > _MAX_DISCOVERY:
            raise ValueError("Offered assignment associations exceeded the safe discovery limit")
        rows, seen_assignments = [], set()
        for association in associations:
            if not isinstance(association, dict):
                raise ValueError("Canvas returned an unknown assignment association")
            association_id = _ID.validate_python(association.get("id"))
            assignment_id = _ID.validate_python(association.get("assignment_id"))
            _identity(association, "assignment_set_id", set_id)
            if association_id in seen_associations or assignment_id in seen_assignments:
                raise ValueError("Canvas returned duplicate assignment associations")
            seen_associations.add(association_id)
            seen_assignments.add(assignment_id)
            if type(association.get("position")) is not int:
                raise ValueError("Canvas did not confirm assignment association order")
            rows.append({k: association[k] for k in ("id", "assignment_id", "assignment_set_id", "position")})
        if type(option.get("position")) is not int:
            raise ValueError("Canvas did not confirm assignment set order")
        result.append({"id": set_id, "position": option["position"],
                       "assignment_set_associations": sorted(rows, key=lambda a: (a["position"], a["id"]))})
    return sorted(result, key=lambda s: (s["position"], s["id"]))


def _offered_sets(path: dict, course_id: int) -> list[dict]:
    signatures = _set_signatures(path.get("assignment_sets"))
    original = {option["id"]: option for option in path["assignment_sets"]}
    result = []
    for option in signatures:
        associations = {a["id"]: a for a in original[option["id"]]["assignment_set_associations"]}
        assignments = []
        for identity in option["assignment_set_associations"]:
            model = associations[identity["id"]].get("model")
            if not isinstance(model, dict):
                raise ValueError("Canvas did not provide assignment metadata for the offered option")
            _identity(model, "id", identity["assignment_id"])
            _identity(model, "course_id", course_id)
            # Use only Canvas's offered summary. The assignment itself may not
            # be accessible until selection; do not fetch hidden content here.
            summary = {k: model[k] for k in ("id", "course_id", "name", "points_possible", "published",
                "grading_type", "submission_types", "due_at", "unlock_at", "lock_at", "locked_for_user") if k in model}
            assignments.append({**identity, "assignment": summary})
        result.append({"id": option["id"], "position": option["position"],
                       "assignment_set_associations": assignments})
    return result


def _state(canvas: Canvas, course_id: int, module_id: int, item_id: int) -> dict:
    context = _context(canvas, course_id)
    module = _object(canvas, _Request(f"courses/{course_id}/modules/{module_id}"))
    _identity(module, "id", module_id)
    sequence = dump(canvas.modules.sequence(course_id, "ModuleItem", str(item_id)))
    nodes = sequence.get("items") if isinstance(sequence, dict) else None
    if not isinstance(nodes, list) or len(nodes) != 1 or not isinstance(nodes[0], dict):
        raise ValueError("Canvas did not return the exact module-item sequence")
    current = nodes[0].get("current")
    if not isinstance(current, dict):
        raise ValueError("Canvas did not identify the current module item")
    _identity(current, "id", item_id)
    _identity(current, "module_id", module_id)
    # List, not show: Horizon's show endpoint can mark a module item viewed.
    values = _all(canvas, _Request(f"courses/{course_id}/modules/{module_id}/items", query=[
        ("include[]", "content_details"), ("include[]", "mastery_paths"), ("per_page", 100)]))
    matches = [v for v in values if v.get("id") == item_id]
    if len(matches) != 1:
        raise ValueError("The exact visible module item was not confirmed")
    item = matches[0]
    _identity(item, "id", item_id)
    _identity(item, "module_id", module_id)
    for key in ("type", "content_id", "page_url"):
        if current.get(key) != item.get(key):
            raise ValueError("The module item's content changed between independent reads")
    result = {**context, "module_id": module_id, "item_id": item_id,
        "module": {k: module[k] for k in ("id", "name", "state", "workflow_state", "published",
            "unlock_at", "require_sequential_progress", "prerequisite_module_ids") if k in module},
        "item": {k: item[k] for k in ("id", "module_id", "title", "type", "content_id", "page_url", "published", "workflow_state") if k in item}}
    path, item_path = nodes[0].get("mastery_path"), item.get("mastery_paths")
    if path is None and item_path is None:
        return {**result, "mastery_path": None, "assignment_sets": []}
    if not isinstance(path, dict) or not isinstance(item_path, dict):
        raise ValueError("Canvas did not confirm the same Mastery Path in both reads")
    for key in ("locked", "selected_set_id"):
        if key not in path or key not in item_path or path[key] != item_path[key]:
            raise ValueError("Mastery Path state changed or was omitted between reads")
    if type(path["locked"]) is not bool or type(path.get("awaiting_choice")) is not bool:
        raise ValueError("Canvas did not confirm the Mastery Path choice state")
    if type(item_path["locked"]) is not bool:
        raise ValueError("Canvas did not confirm the offered-set lock state")
    selected = path["selected_set_id"]
    if selected is not None:
        _ID.validate_python(selected)
        _ID.validate_python(item_path["selected_set_id"])
    if "still_processing" in path and type(path["still_processing"]) is not bool:
        raise ValueError("Canvas returned an unknown Mastery Path processing state")
    if "is_student" in path and path["is_student"] is not True:
        raise ValueError("Canvas did not return the current student's Mastery Path")
    signature = _set_signatures(path.get("assignment_sets"))
    if signature != _set_signatures(item_path.get("assignment_sets")):
        raise ValueError("Offered assignment sets changed between independent reads")
    if selected is not None and selected not in {s["id"] for s in signature}:
        raise ValueError("The selected assignment set was not among the returned eligible sets")
    if path["awaiting_choice"] is not (selected is None and bool(signature)):
        raise ValueError("Canvas returned inconsistent Mastery Path choice state")
    result["mastery_path"] = {k: path[k] for k in ("locked", "selected_set_id", "awaiting_choice", "still_processing") if k in path}
    result["assignment_sets"] = _offered_sets(item_path, course_id)
    return result


def _availability(state: dict) -> dict:
    reasons = []
    if state["course"].get("workflow_state") != "available" or state["permissions"].get("participate_as_student") is not True:
        reasons.append("Canvas did not confirm current student participation in this course.")
    if not any(e["enrollment_state"] == "active" for e in state["enrollments"]):
        reasons.append("No active student enrollment was confirmed.")
    if "access_restricted_by_date" in state["course"] and state["course"]["access_restricted_by_date"] is not False:
        reasons.append("Course access is restricted or unknown.")
    if state["module"].get("state") not in {"unlocked", "started", "completed"}:
        reasons.append("The module is locked or its current-student state is unknown.")
    for value in (state["module"], state["item"]):
        if "published" in value and value["published"] is not True:
            reasons.append("The module or source item is unpublished.")
        if "workflow_state" in value and value["workflow_state"] != "active":
            reasons.append("The module or source item is not active.")
    path = state.get("mastery_path")
    if path is None:
        reasons.append("Canvas did not return a Mastery Path for this item; it may be disabled or not a source item.")
    elif path.get("selected_set_id") is not None:
        reasons.append(_ALREADY_SELECTED)
    elif path.get("locked") is not False or path.get("awaiting_choice") is not True:
        reasons.append("Canvas has not offered a choice for this source item.")
    elif path.get("still_processing") is True:
        reasons.append("Canvas is still processing the Mastery Path.")
    elif len(state["assignment_sets"]) < 2:
        reasons.append("Canvas did not offer multiple assignment sets to choose between.")
    return {"can_select": not reasons, "initial_selection_only": True, "reasons": reasons}


def _chosen(state: dict, set_id: int) -> dict:
    matches = [option for option in state["assignment_sets"] if option["id"] == set_id]
    if len(matches) != 1:
        raise ValueError("The requested assignment set is not an exact offered choice")
    option = matches[0]
    if not option["assignment_set_associations"]:
        raise ValueError("The offered assignment set has no confirmed assignments")
    if any(a["assignment"].get("published") is not True for a in option["assignment_set_associations"]):
        raise ValueError("The chosen set contains unpublished or unconfirmed assignments; inspect Canvas")
    return option


def _access(canvas: Canvas, course_id: int, option: dict) -> dict:
    """Bounded independent access evidence; never completion or extra writes."""
    associations = option["assignment_set_associations"]
    rows = []
    for association in associations[:_MAX_ACCESS]:
        assignment_id = association["assignment_id"]
        try:
            assignment = _object(canvas, _Request(f"courses/{course_id}/assignments/{assignment_id}"))
        except CanvasError as error:
            rows.append({"assignment_id": assignment_id, "accessible": None,
                         "status": "not_confirmed", "http_status": error.status})
            continue
        _identity(assignment, "id", assignment_id)
        _identity(assignment, "course_id", course_id)
        accessible = assignment.get("published") is True and assignment.get("locked_for_user") is False
        rows.append({"assignment_id": assignment_id, "accessible": accessible,
            **{k: assignment[k] for k in ("published", "locked_for_user", "unlock_at", "lock_at", "due_at") if k in assignment}})
    complete = len(rows) == len(associations) and all(r["accessible"] is True for r in rows)
    return {"complete": complete, "assignments": rows, "checked": len(rows), "total": len(associations),
            "unchecked": len(associations) - len(rows),
            "note": "Selection does not override publication, assignment dates or module prerequisites. Content release can lag selection."}


def read_mastery_paths(canvas: Canvas, course_id: int, module_id: int, item_id: int,
                       offset: int = 0, limit: int = 30) -> dict:
    """Read offered choices and selection for the exact item, without choosing."""
    course_id, module_id, item_id = (_ID.validate_python(v) for v in (course_id, module_id, item_id))
    window([], offset, limit)
    state = _state(canvas, course_id, module_id, item_id)
    selected, coverage = window(state["assignment_sets"], offset, limit)
    coverage.update(scope="offered_mastery_path_sets")
    data = {**state, "assignment_sets": selected, "availability": _availability(state),
            "coursework_complete": None, "completion_note": "Choosing a path does not complete its assignments or module requirements."}
    if state["mastery_path"] is not None and state["mastery_path"]["selected_set_id"] is not None:
        chosen = next(s for s in state["assignment_sets"] if s["id"] == state["mastery_path"]["selected_set_id"])
        data["assignment_access"] = _access(canvas, course_id, chosen)
        if not data["assignment_access"]["complete"]:
            coverage["complete"] = False
    status = "ok" if state["mastery_path"] is not None else "unavailable"
    return envelope(data, origin=canvas.config.base_url, status=status, coverage=coverage)


def snapshot(canvas: Canvas, p: dict) -> dict:
    action = MasteryPathSelect.model_validate(p)
    state = _state(canvas, action.course_id, action.module_id, action.item_id)
    availability = _availability(state)
    if not availability["can_select"]:
        raise ValueError(" ".join(availability["reasons"]))
    chosen = _chosen(state, action.assignment_set_id)
    return {**state, "assignment_set_id": action.assignment_set_id, "chosen_set": chosen,
            "effect": "Select this offered assignment set for the current student. This assigns its coursework; it does not complete or submit it."}


def perform(canvas: Canvas, p: dict, target: dict) -> dict:
    """One initial selection; stale server readback remains unconfirmed."""
    action = MasteryPathSelect.model_validate(p)
    for key in ("course_id", "module_id", "item_id", "assignment_set_id"):
        _identity(target, key, getattr(action, key))
    actor = _ID.validate_python(target.get("user_id"))
    availability = _availability(target)
    if not availability["can_select"]:
        raise ValueError(" ".join(availability["reasons"]))
    chosen = _chosen(target, action.assignment_set_id)
    if target.get("chosen_set") != chosen:
        raise ValueError("Prepared chosen set does not match the exact offered assignments")
    canvas.send(_Request(f"courses/{action.course_id}/modules/{action.module_id}/items/{action.item_id}/select_mastery_path",
                         method="POST", body={"assignment_set_id": action.assignment_set_id}))
    after = _state(canvas, action.course_id, action.module_id, action.item_id)
    _identity(after, "user_id", actor)
    path = after.get("mastery_path")
    if (not isinstance(path, dict) or path.get("selected_set_id") != action.assignment_set_id
            or path.get("awaiting_choice") is not False or path.get("locked") is not False):
        raise ValueError("Exact Mastery Path selection was not confirmed by fresh readback; do not repeat the POST")
    after_chosen = next((s for s in after["assignment_sets"] if s["id"] == action.assignment_set_id), None)
    if after_chosen is None or _set_signatures([chosen]) != _set_signatures([after_chosen]):
        raise ValueError("The selected assignment-set contents changed during selection")
    access = _access(canvas, action.course_id, after_chosen)
    return {"verified": True, "selection_verified": True, "user_id": actor,
        "course_id": action.course_id, "module_id": action.module_id, "item_id": action.item_id,
        "selected_set_id": action.assignment_set_id, "assignment_access": access, "coursework_complete": None,
        "completion_note": "Selection was verified. Coursework and module completion were not inferred.",
        "coverage": {"scope": "mastery_path_selection_and_assignment_access", "complete": access["complete"],
                     "selection_verified": True, "assignment_access_complete": access["complete"]}}
