"""Prepared student appointment reservations; never edit teacher-owned slots.

Contracts: https://developerdocs.instructure.com/services/canvas/resources/appointment_groups
https://developerdocs.instructure.com/services/canvas/resources/calendar_events
Source: app/{controllers/calendar_events_api_controller,models/calendar_event,
models/appointment_group}.rb and lib/api/v1/calendar_event.rb in instructure/canvas-lms.

The source-supported appointment_group_ID calendar context plus explicit date
bounds includes past slots, unlike the group's nested appointments. Do not use
all_events=true: that skips the controller's appointment-context branch. Group
reserved_times can omit group reservations. Own nested children identify those;
direct child reads exclude child_events to preserve the actual owner context.
Rescheduling uses one transactional cancel_existing POST, which replaces ALL own
reservations in the group; this adapter permits it only with exactly one booking.
List reads may use a replica. Disagreement stays unverified; never replay writes.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from itertools import islice
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from canvas_py import Canvas
from canvas_py._request import CanvasRequest
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .responses import dump, envelope, window

Id = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
_ID = TypeAdapter(Id)
_MAX = 1000
_GROUP_FIELDS = ("id", "title", "description", "location_name", "location_address", "start_at", "end_at",
    "workflow_state", "context_codes", "sub_context_codes", "participant_type", "participant_visibility",
    "appointments_count", "max_appointments_per_participant", "min_appointments_per_participant", "participants_per_appointment")


class _Target(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    course_id: Id
    appointment_group_id: Id
    participant_group_id: Id | None = None


class AppointmentReserve(_Target):
    kind: Literal["appointment_reserve"]
    slot_id: Id
    comments: Annotated[str, Field(strict=True, max_length=255)] | None = None


class AppointmentCancel(_Target):
    kind: Literal["appointment_cancel"]
    reservation_id: Id
    cancel_reason: Annotated[str, Field(strict=True, max_length=1000)] | None = None


class AppointmentReschedule(_Target):
    kind: Literal["appointment_reschedule"]
    reservation_id: Id
    new_slot_id: Id
    comments: Annotated[str, Field(strict=True, max_length=255,
        description="Replacement reservation comments. Omit to preserve the existing comments; use an empty string to clear them.")] | None = None


AppointmentActionTypes = AppointmentReserve | AppointmentCancel | AppointmentReschedule
KINDS = ["appointment_reserve", "appointment_cancel", "appointment_reschedule"]
ROUTES = {kind: "canvas_manage_appointments" for kind in KINDS}
_ADAPTER = TypeAdapter(Annotated[AppointmentActionTypes, Field(discriminator="kind")])


class _Request(CanvasRequest[Any]):
    pin_pagination_path = True

    def __init__(self, path: str, *, method: str = "GET", query: list | None = None, body: dict | None = None):
        self._path, self.method, self._query, self._body = path, method, query or [], body
        self.allow_retries = method == "GET"
        self.allow_redirects = False

    @property
    def path(self) -> str:
        return self._path

    def query_pairs(self) -> list:
        return self._query

    def json_body(self) -> dict | None:
        return self._body

    def validate_pagination_query(self, pairs: list[tuple[str, str]], *, page_number: int) -> None:
        # A Link can move through pages, never broaden the participant, course,
        # date window, or read includes before the next request is dispatched.
        expected = sorted((key, str(value).lower() if isinstance(value, bool) else str(value))
                          for key, value in self._query if key not in {"page", "per_page"} and value is not None)
        actual = sorted((key, value) for key, value in pairs if key not in {"page", "per_page"})
        pages = [value for key, value in pairs if key == "page"]
        sizes = [value for key, value in pairs if key == "per_page"]
        expected_sizes = [str(value) for key, value in self._query if key == "per_page"]
        if (actual != expected or len(pages) > 1 or any(not v or len(v) > 4096 for v in pages)
                or sizes != expected_sizes):
            raise ValueError("Canvas pagination changed the exact appointment discovery scope")
        if page_number > 1:
            if len(pages) != 1:
                raise ValueError("Canvas pagination omitted the next appointment page")
            page = pages[0]
            # One-shard collections can use ordinary page ordinals. Other
            # collections, including enrollments, use opaque bookmark tokens.
            if page.isascii() and page.isdigit():
                valid = page == str(page_number)
            else:
                valid = page.startswith("bookmark:") and len(page) > len("bookmark:")
            if not valid:
                raise ValueError("Canvas pagination skipped or changed the appointment page sequence")


def _object(canvas: Canvas, path: str, **kwargs: Any) -> dict:
    value = dump(canvas.send(_Request(path, **kwargs)))
    if not isinstance(value, dict):
        raise ValueError("Canvas did not return the requested appointment object")  # noqa: TRY004 -- upstream contract failure, safely exposed by MCP
    return value


def _all(canvas: Canvas, path: str, query: list | None = None) -> list[dict]:
    iterator = canvas.paginate(_Request(path, query=[*(query or []), ("per_page", 100)]))
    try:
        rows = [dump(row) for row in islice(iterator, _MAX + 1)]
    except TypeError as error:
        raise ValueError("Canvas did not return an appointment discovery list") from error
    finally:
        iterator.close()
    if len(rows) > _MAX or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Appointment discovery is incomplete or exceeds 1000 records; inspect Canvas")
    ids = [_ID.validate_python(row.get("id")) for row in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("Canvas returned duplicate appointment discovery identities")
    return rows


def _identity(row: dict, field: str, expected: int) -> None:
    if type(row.get(field)) is not int or row[field] != expected:
        raise ValueError(f"Canvas did not confirm the exact appointment {field}")


def _time(value: Any) -> datetime:
    if (not isinstance(value, str) or len(value) > 64
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value)):
        raise ValueError("Canvas did not provide a bounded appointment timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Canvas did not provide a valid appointment timestamp") from error
    if parsed.utcoffset() is None:
        raise ValueError("Appointment timestamps require an explicit timezone")
    return parsed


def _times(row: dict) -> None:
    if _time(row.get("end_at")) < _time(row.get("start_at")):
        raise ValueError("Appointment end precedes its start")


def _count(value: Any, *, nullable: bool = False) -> int | None:
    if nullable and value is None:
        return None
    if type(value) is not int or not 0 <= value <= 2**31 - 1:
        raise ValueError("Canvas returned an unknown appointment count or limit")
    return value


def _context(canvas: Canvas, course_id: int) -> dict:
    actor = _ID.validate_python(_object(canvas, "users/self/profile").get("id"))
    course = _object(canvas, f"courses/{course_id}")
    _identity(course, "id", course_id)
    if course.get("workflow_state") != "available" or course.get("access_restricted_by_date", False) is not False:
        raise ValueError("Appointments require an available current student course")
    rows = _all(canvas, f"courses/{course_id}/enrollments", [
        ("user_id", actor), ("type[]", "StudentEnrollment"), ("state[]", "active")])
    if not rows:
        raise ValueError("Canvas did not confirm an active student enrollment")
    for row in rows:
        _identity(row, "user_id", actor)
        _identity(row, "course_id", course_id)
        if row.get("type") != "StudentEnrollment" or row.get("enrollment_state") != "active":
            raise ValueError("Canvas did not confirm the current active student enrollment")
    return {"user_id": actor, "course_id": course_id,
        "enrollments": sorted([{k: r.get(k) for k in ("id", "user_id", "course_id", "course_section_id", "type", "enrollment_state")} for r in rows], key=lambda r: r["id"])}


def _group(row: dict, course_id: int) -> dict:
    _ID.validate_python(row.get("id"))
    if row.get("workflow_state") != "active" or row.get("participant_type") not in {"User", "Group"}:
        raise ValueError("Canvas did not confirm a published student appointment group")
    contexts, subs = row.get("context_codes"), row.get("sub_context_codes")
    if (not isinstance(contexts, list) or len(contexts) > 100 or f"course_{course_id}" not in contexts
            or any(not isinstance(c, str) or not re.fullmatch(r"course_[1-9][0-9]*", c) for c in contexts)
            or not isinstance(subs, list) or len(subs) > _MAX):
        raise ValueError("Canvas did not confirm the appointment course contexts")
    if row["participant_type"] == "Group":
        if len(subs) != 1 or not isinstance(subs[0], str) or not re.fullmatch(r"group_category_[1-9][0-9]*", subs[0]):
            raise ValueError("Canvas did not identify one appointment group category")
    elif any(not isinstance(c, str) or not re.fullmatch(r"course_section_[1-9][0-9]*", c) for c in subs):
        raise ValueError("Canvas returned unknown appointment section restrictions")
    for field in ("max_appointments_per_participant", "min_appointments_per_participant", "participants_per_appointment"):
        if field not in row:
            raise ValueError("Canvas omitted an appointment limit")
        _count(row[field], nullable=True)
    _count(row.get("appointments_count"))
    if not isinstance(row.get("title"), str) or not row["title"]:
        raise ValueError("Canvas did not identify the appointment group title")
    for field in ("title", "description", "location_name", "location_address"):
        if row.get(field) is not None and (not isinstance(row[field], str) or len(row[field]) > 20000):
            raise ValueError("Appointment text exceeds the supported bound")
    if row["appointments_count"]:
        _times(row)
    return {k: row[k] for k in _GROUP_FIELDS if k in row}


def _catalog(canvas: Canvas, course_id: int) -> list[dict]:
    return [_group(r, course_id) for r in _all(canvas, "appointment_groups", [
        ("scope", "reservable"), ("context_codes[]", f"course_{course_id}"), ("include_past_appointments", True)])]


def _candidate(canvas: Canvas, row: dict) -> int:
    _ID.validate_python(row.get("id"))
    value = row.get("reserve_url")
    if not isinstance(value, str) or len(value) > 4096 or "\\" in value:
        raise ValueError("Canvas did not identify the current reserving participant")
    parsed, origin = urlsplit(value), urlsplit(canvas.config.base_url)
    if (parsed.scheme != origin.scheme or parsed.netloc != origin.netloc or parsed.query or parsed.fragment
            or parsed.username or parsed.password):
        raise ValueError("Canvas returned an unexpected reservation URL")
    match = re.fullmatch(rf"/api/v1/calendar_events/{row['id']}/reservations/([1-9][0-9]*)(?:\.json)?", parsed.path)
    if not match:
        raise ValueError("Canvas did not identify the current reserving participant")
    return _ID.validate_python(int(match[1]))


def _participant(canvas: Canvas, context: dict, group: dict, candidates: set[int]) -> dict:
    actor, course_id = context["user_id"], context["course_id"]
    if group["participant_type"] == "User":
        if candidates - {actor}:
            raise ValueError("Canvas offered a reservation for a different participant")
        sections = {f"course_section_{e['course_section_id']}" for e in context["enrollments"] if type(e["course_section_id"]) is int}
        if group["sub_context_codes"] and not sections.intersection(group["sub_context_codes"]):
            raise ValueError("Canvas did not confirm the appointment's student section eligibility")
        return {"type": "User", "id": actor, "context_code": f"user_{actor}", "can_cancel": True}
    category_id = int(group["sub_context_codes"][0].rsplit("_", 1)[1])
    own = [r for r in _all(canvas, "users/self/groups") if r.get("group_category_id") == category_id]
    if not candidates and len(own) == 1:
        # Empty groups offer no slots to mutate. A unique own category group
        # still makes their read-only participant information useful.
        candidates = {own[0]["id"]}
    if len(candidates) != 1:
        raise ValueError("Canvas did not identify a unique current student-group participant")
    participant_id = next(iter(candidates))
    matches = [r for r in own if r["id"] == participant_id]
    if len(matches) != 1:
        raise ValueError("The appointment participant is not one of your groups in this category")
    actual = _object(canvas, f"groups/{participant_id}")
    _identity(actual, "id", participant_id)
    _identity(actual, "course_id", course_id)
    _identity(actual, "group_category_id", category_id)
    if actual.get("context_type") != "Course" or actual.get("concluded", False) is not False or actual.get("non_collaborative", False) is not False:
        raise ValueError("Canvas did not confirm an active collaborative course group")
    member = _object(canvas, f"groups/{participant_id}/users/self")
    _identity(member, "group_id", participant_id)
    _identity(member, "user_id", actor)
    _ID.validate_python(member.get("id"))
    if member.get("workflow_state") != "accepted":
        raise ValueError("Canvas did not confirm your accepted student group membership")
    permissions = _object(canvas, f"groups/{participant_id}/permissions", query=[
        ("permissions[]", "read"), ("permissions[]", "manage_calendar")])
    if permissions.get("read") is not True:
        raise ValueError("Canvas did not grant current student group access")
    return {"type": "Group", "id": participant_id, "context_code": f"group_{participant_id}",
        "name": actual.get("name"), "group_category_id": category_id,
        "membership_id": member["id"], "can_cancel": permissions.get("manage_calendar") is True}


def _reservation(row: dict, group_id: int, participant: dict, *, slot_id: int | None = None, deleted: bool = False) -> dict:
    _ID.validate_python(row.get("id"))
    _identity(row, "appointment_group_id", group_id)
    parent = _ID.validate_python(row.get("parent_event_id"))
    if slot_id is not None and parent != slot_id:
        raise ValueError("Canvas returned a reservation for a different slot")
    if row.get("context_code") != participant["context_code"] or row.get("participant_type") != participant["type"]:
        raise ValueError("Canvas did not confirm reservation ownership")
    if row.get("workflow_state") not in ({"deleted"} if deleted else {"active", "locked"}):
        raise ValueError("Canvas did not confirm reservation state")
    if row.get("series_uuid") or row.get("rrule") or _count(row.get("child_events_count")) != 0:
        raise ValueError("Recurring or parent events cannot be treated as appointment reservations")
    _times(row)
    comments = row.get("comments")
    if comments is not None and (not isinstance(comments, str) or len(comments) > 255):
        raise ValueError("Canvas returned unknown reservation comments")
    return {k: row.get(k) for k in ("id", "appointment_group_id", "parent_event_id", "context_code", "participant_type", "workflow_state", "start_at", "end_at", "comments")}


def _slot(row: dict, group: dict, participant: dict) -> dict:
    _ID.validate_python(row.get("id"))
    _identity(row, "appointment_group_id", group["id"])
    if ("parent_event_id" not in row or row["parent_event_id"] is not None or row.get("participant_type") != group["participant_type"]
            or row.get("workflow_state") not in {"active", "locked"} or row.get("series_uuid") or row.get("rrule")):
        raise ValueError("Canvas did not return a reservable appointment slot")
    _times(row)
    if type(row.get("reserved")) is not bool:
        raise ValueError("Canvas did not confirm current participant reservation eligibility")
    count = _count(row.get("child_events_count"))
    cap = _count(row.get("participants_per_appointment"), nullable=True)
    available = _count(row.get("available_slots"), nullable=True)
    if (cap is None and available is not None) or (cap is not None and available != max(cap - count, 0)):
        raise ValueError("Canvas returned inconsistent slot capacity")
    children = row.get("child_events")
    if not isinstance(children, list) or len(children) > _MAX or any(not isinstance(c, dict) for c in children):
        raise ValueError("Canvas did not return bounded slot reservations")
    own = []
    for child in children:
        if child.get("own_reservation") is True:
            identity = child.get("user" if participant["type"] == "User" else "group")
            if not isinstance(identity, dict):
                raise ValueError("Canvas did not identify the own reservation participant")
            _identity(identity, "id", participant["id"])
            reservation = _reservation(child, group["id"], participant, slot_id=row["id"])
            if any(reservation[k] != row[k] for k in ("start_at", "end_at")):
                raise ValueError("Reservation times disagree with the parent slot")
            own.append(reservation)
    if len(own) != int(row["reserved"]) or count < len(children):
        raise ValueError("Canvas did not confirm exactly the current participant's slot reservation")
    return {"id": row["id"], "appointment_group_id": group["id"], "start_at": row["start_at"], "end_at": row["end_at"],
        "title": row.get("title"), "location_name": row.get("location_name"), "location_address": row.get("location_address"),
        "workflow_state": row["workflow_state"], "reserved": row["reserved"], "child_events_count": count,
        "participants_per_appointment": cap, "available_slots": available, "own_reservations": own}


def _state(canvas: Canvas, course_id: int, group_id: int) -> dict:
    context = _context(canvas, course_id)
    matches = [r for r in _catalog(canvas, course_id) if r["id"] == group_id]
    if len(matches) != 1:
        raise ValueError("The appointment group is not currently reservable by this student")
    group = matches[0]
    if group["appointments_count"] > _MAX:
        raise ValueError("Complete appointment inventory exceeds 1000 slots; inspect Canvas")
    shown = _group(_object(canvas, f"appointment_groups/{group_id}"), course_id)
    if shown != group:
        raise ValueError("Appointment group policy changed between reads")
    rows = []
    if group["appointments_count"]:
        rows = _all(canvas, "calendar_events", [("type", "event"), ("context_codes[]", f"appointment_group_{group_id}"),
            ("start_date", group["start_at"]), ("end_date", group["end_at"])])
    if len(rows) != group["appointments_count"]:
        raise ValueError("Canvas did not confirm a complete appointment slot inventory, including past slots")
    candidates = {_candidate(canvas, r) for r in rows}
    participant = _participant(canvas, context, group, candidates)
    slots = sorted([_slot(r, group, participant) for r in rows], key=lambda r: r["id"])
    if slots and (min(_time(s["start_at"]) for s in slots) != _time(group["start_at"])
                  or max(_time(s["end_at"]) for s in slots) != _time(group["end_at"])):
        raise ValueError("Canvas did not confirm the complete appointment group's time bounds")
    reservations = sorted([r for slot in slots for r in slot["own_reservations"]], key=lambda r: r["id"])
    if len({r["id"] for r in reservations}) != len(reservations):
        raise ValueError("Canvas returned duplicate reservation identities")
    return {**context, "appointment_group_id": group_id, "appointment_group": group, "participant": participant,
        "slots": slots, "own_reservations": reservations, "inventory_complete": True}


def _reserve_reasons(state: dict, slot: dict, *, replacing: bool = False) -> list[str]:
    reasons = []
    if _time(slot["end_at"]) < datetime.now(UTC):
        reasons.append("This appointment has already ended.")
    if slot["reserved"]:
        reasons.append("The current participant already reserved this slot.")
    if slot["available_slots"] == 0:
        reasons.append("This slot has no remaining capacity.")
    maximum = state["appointment_group"]["max_appointments_per_participant"]
    if maximum is not None and (1 if replacing else len(state["own_reservations"]) + 1) > maximum:
        reasons.append("The participant's appointment limit would be exceeded; past bookings also count.")
    return reasons


def read_appointment_groups(canvas: Canvas, course_id: int, offset: int = 0, limit: int = 30) -> dict:
    course_id = _ID.validate_python(course_id)
    window([], offset, limit)
    context = _context(canvas, course_id)
    selected, coverage = window(_catalog(canvas, course_id), offset, limit)
    coverage["scope"] = "current_student_reservable_appointment_groups_including_past"
    return envelope({**context, "appointment_groups": selected}, origin=canvas.config.base_url, coverage=coverage)


def read_appointments(canvas: Canvas, course_id: int, appointment_group_id: int, offset: int = 0, limit: int = 30) -> dict:
    course_id, appointment_group_id = (_ID.validate_python(v) for v in (course_id, appointment_group_id))
    window([], offset, limit)
    state = _state(canvas, course_id, appointment_group_id)
    slots = [{**s, "availability": {"can_reserve": not _reserve_reasons(state, s), "reasons": _reserve_reasons(state, s)}} for s in state["slots"]]
    selected, coverage = window(slots, offset, limit)
    coverage.update(scope="appointment_slots_and_current_participant_reservations", inventory_complete=True)
    return envelope({**{k: v for k, v in state.items() if k != "slots"}, "slots": selected,
        "note": "Group reservations affect the entire verified student group. Slot times are Canvas times with explicit timezone offsets; other calendar conflicts are not ruled out."},
        origin=canvas.config.base_url, coverage=coverage)


def _direct_reservation(canvas: Canvas, reservation_id: int, state: dict, *, deleted: bool = False) -> dict:
    row = _object(canvas, f"calendar_events/{reservation_id}", query=[("excludes[]", "child_events")])
    _identity(row, "id", reservation_id)
    return _reservation(row, state["appointment_group_id"], state["participant"], deleted=deleted)


def _direct_slot(canvas: Canvas, slot_id: int, state: dict) -> dict:
    row = _object(canvas, f"calendar_events/{slot_id}")
    if _candidate(canvas, row) != state["participant"]["id"]:
        raise ValueError("The current appointment participant changed")
    _identity(row, "id", slot_id)
    return _slot(row, state["appointment_group"], state["participant"])


def snapshot(canvas: Canvas, p: dict) -> dict:
    a = _ADAPTER.validate_python(p)
    state = _state(canvas, a.course_id, a.appointment_group_id)
    participant = state["participant"]
    if a.participant_group_id != (participant["id"] if participant["type"] == "Group" else None):
        raise ValueError("Action must name the exact discovered student group, or omit it for personal appointments")
    old = None
    if isinstance(a, (AppointmentCancel, AppointmentReschedule)):
        matches = [r for r in state["own_reservations"] if r["id"] == a.reservation_id]
        if len(matches) != 1 or not participant["can_cancel"]:
            raise ValueError("Canvas did not confirm an owned cancellable appointment reservation")
        old = matches[0]
        if _time(old["end_at"]) < datetime.now(UTC):
            raise ValueError("Past-ended appointment reservations cannot be cancelled or rescheduled")
        if _direct_reservation(canvas, a.reservation_id, state) != old:
            raise ValueError("The exact reservation changed between independent reads")
        if isinstance(a, AppointmentReschedule) and len(state["own_reservations"]) != 1:
            raise ValueError("Rescheduling replaces every booking in this appointment group and requires exactly one existing reservation")
    chosen = None
    if not isinstance(a, AppointmentCancel):
        slot_id = a.slot_id if isinstance(a, AppointmentReserve) else a.new_slot_id
        matches = [s for s in state["slots"] if s["id"] == slot_id]
        if len(matches) != 1:
            raise ValueError("The exact offered appointment slot was not found")
        chosen = matches[0]
        if chosen != _direct_slot(canvas, slot_id, state):
            raise ValueError("The offered slot changed between independent reads")
        reasons = _reserve_reasons(state, chosen, replacing=isinstance(a, AppointmentReschedule))
        if reasons:
            raise ValueError(" ".join(reasons))
    remaining = len(state["own_reservations"]) - int(old is not None) + int(chosen is not None)
    minimum = state["appointment_group"]["min_appointments_per_participant"]
    replacement = None
    comments = None
    effect = "Cancel the exact reviewed reservation."
    if isinstance(a, AppointmentReserve):
        comments = a.comments
        effect = "Reserve the exact offered slot for the current participant, preserving other bookings."
    if isinstance(a, AppointmentReschedule):
        comments = a.comments if a.comments is not None else old["comments"]
        replacement = {"native_scope": "all_current_participant_reservations_in_appointment_group",
            "observed_preflight_reservation_ids": [old["id"]], "atomic_count_condition_supported": False,
            "concurrent_reservations_may_also_be_cancelled": True}
        effect = ("Replace ALL current participant bookings in this appointment group with the selected slot. "
                  "Exactly one was observed at preflight, but Canvas offers no atomic count condition: "
                  "concurrently created bookings may also be cancelled.")
    if participant["type"] == "Group":
        effect += " This changes the reservation for the entire verified student group."
    return {**{k: v for k, v in state.items() if k != "slots"}, "kind": a.kind, "slot": chosen, "reservation": old,
        "cancel_existing": isinstance(a, AppointmentReschedule), "reservations_after": remaining,
        "replacement_effect": replacement, "reservation_comments": comments,
        "below_requested_minimum_after": minimum is not None and remaining < minimum,
        "effect": effect}


def _slot_identity(slot: dict) -> dict:
    return {k: slot[k] for k in ("id", "appointment_group_id", "start_at", "end_at", "title", "location_name", "location_address", "participants_per_appointment")}


def perform(canvas: Canvas, p: dict, target: dict) -> dict:
    a = _ADAPTER.validate_python(p)
    _identity(target, "course_id", a.course_id)
    _identity(target, "appointment_group_id", a.appointment_group_id)
    _ID.validate_python(target.get("user_id"))
    if target.get("kind") != a.kind or target.get("inventory_complete") is not True:
        raise ValueError("Prepared appointment operation is incomplete or different")
    participant = target["participant"]
    if a.participant_group_id != (participant["id"] if participant["type"] == "Group" else None):
        raise ValueError("Prepared appointment participant differs")
    old, chosen = target["reservation"], target["slot"]
    if isinstance(a, AppointmentCancel):
        _identity(old, "id", a.reservation_id)
        result = _object(canvas, f"calendar_events/{a.reservation_id}", method="DELETE",
            body={"cancel_reason": a.cancel_reason} if a.cancel_reason is not None else None)
        _identity(result, "id", a.reservation_id)
        new_id = None
    else:
        slot_id = a.slot_id if isinstance(a, AppointmentReserve) else a.new_slot_id
        _identity(chosen, "id", slot_id)
        replacing = isinstance(a, AppointmentReschedule)
        if target.get("cancel_existing") is not replacing:
            raise ValueError("Prepared replacement effect differs")
        if replacing:
            _identity(old, "id", a.reservation_id)
            if target["own_reservations"] != [old]:
                raise ValueError("Rescheduling requires exactly one reviewed reservation")
        body = {"participant_id": participant["id"], "cancel_existing": replacing}
        comments = a.comments if a.comments is not None else old["comments"] if replacing else None
        if target["reservation_comments"] != comments:
            raise ValueError("Prepared reservation comments differ")
        if comments is not None:
            body["comments"] = comments
        result = _object(canvas, f"calendar_events/{slot_id}/reservations", method="POST", body=body)
        new_id = _ID.validate_python(result.get("id"))
        _identity(result, "parent_event_id", slot_id)
        _identity(result, "appointment_group_id", a.appointment_group_id)
        if new_id in {r["id"] for r in target["own_reservations"]}:
            raise ValueError("Canvas did not create a distinct reservation")
    after = _state(canvas, a.course_id, a.appointment_group_id)
    if after["user_id"] != target["user_id"] or after["participant"] != participant or after["appointment_group"] != target["appointment_group"]:
        raise ValueError("Appointment identity, membership or policy changed during verification")
    expected = [r for r in target["own_reservations"] if old is None or r["id"] != old["id"]]
    if new_id is not None:
        new = _direct_reservation(canvas, new_id, after)
        if new["parent_event_id"] != chosen["id"] or any(new[k] != chosen[k] for k in ("start_at", "end_at")):
            raise ValueError("The new reservation does not match the selected slot")
        if new["comments"] != target["reservation_comments"]:
            raise ValueError("Canvas did not confirm the reservation comments")
        slot = _direct_slot(canvas, chosen["id"], after)
        if _slot_identity(slot) != _slot_identity(chosen) or slot["own_reservations"] != [new]:
            raise ValueError("The new reservation was not confirmed independently in its slot")
        expected.append(new)
    if old is not None:
        removed = _direct_reservation(canvas, old["id"], after, deleted=True)
        if {**removed, "workflow_state": old["workflow_state"]} != old:
            raise ValueError("The cancelled reservation identity changed")
        old_slot = _direct_slot(canvas, old["parent_event_id"], after)
        if any(r["id"] == old["id"] for r in old_slot["own_reservations"]):
            raise ValueError("The cancelled reservation remains in its parent slot")
    if sorted(expected, key=lambda r: r["id"]) != after["own_reservations"]:
        raise ValueError("The complete participant reservation inventory did not confirm the exact change")
    replacing = isinstance(a, AppointmentReschedule)
    return {"verified": True, "course_id": a.course_id, "appointment_group_id": a.appointment_group_id,
        "participant": participant, "reservation_id": new_id, "cancelled_reservation_id": old["id"] if old else None,
        "confirmed_cancelled_reservation_ids": [old["id"]] if old else [],
        "replacement_effect": target["replacement_effect"],
        "own_reservations": after["own_reservations"], "attendance_confirmed": False,
        "coverage": {"complete": not replacing, "scope": "requested_appointment_change_and_final_participant_reservations",
            "final_inventory_complete": True, "known_cancellations_verified": True,
            "cancelled_set_complete": not replacing,
            "note": "Canvas may also cancel a booking created concurrently after preflight; the full cancelled set is not independently observable."
                    if replacing else "The exact requested change and current participant reservation inventory were independently verified."}}
