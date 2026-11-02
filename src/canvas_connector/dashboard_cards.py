"""Current-user native dashboard cards and reviewed merge-only ordering.

The cards route is native Canvas source, not a documented Users list API:
https://github.com/instructure/canvas-lms/blob/master/config/routes.rb
https://github.com/instructure/canvas-lms/blob/master/app/helpers/dashboard_helper.rb
https://github.com/instructure/canvas-lms/blob/master/app/presenters/course_for_menu_presenter.rb
https://github.com/instructure/canvas-lms/blob/master/app/controllers/users_controller.rb
https://github.com/instructure/canvas-lms/blob/master/lib/api/v1/user_profile.rb
Positions GET/PUT: https://canvas.instructure.com/doc/api/users.html

PUT merges preferences; never send unrelated positions back. Numeric strings
are legitimate legacy values and must survive untouched for unrelated assets.
Native card order resolves position ties and missing positions; do not invent
an alphabetical order or approximate the card set with favorites/enrollments.
"""
from __future__ import annotations

import re
from itertools import islice
from typing import Annotated, Any, Literal

from canvas_py import Canvas
from canvas_py._request import CanvasRequest
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from .responses import dump, envelope

Id = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
_ID = TypeAdapter(Id)
MAX_RECORDS = 1000
_POSITIONS = "users/self/dashboard_positions"
_CARDS = "dashboard/dashboard_cards"


class DashboardReorder(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["dashboard_reorder"]
    course_ids: Annotated[list[Id], Field(min_length=1, max_length=MAX_RECORDS)]

    @field_validator("course_ids")
    @classmethod
    def unique_courses(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("Dashboard course IDs must be unique")
        return value


DashboardCardActionTypes = DashboardReorder
KINDS = ["dashboard_reorder"]
ROUTES = {"dashboard_reorder": "canvas_reorder_dashboard"}
_ADAPTER = TypeAdapter(DashboardCardActionTypes)


class _Request(CanvasRequest[Any]):
    allow_redirects = False

    def __init__(self, path: str, *, body: dict | None = None):
        self._path, self.body = path, body
        self.method = "GET" if body is None else "PUT"
        self.allow_retries = body is None

    @property
    def path(self) -> str:
        return self._path

    def json_body(self) -> dict | None:
        return self.body


class _Cards(_Request):
    response_is_paginated = True
    pin_pagination_path = True

    def __init__(self):
        super().__init__(_CARDS)

    def validate_pagination_query(self, pairs: list[tuple[str, str]], *, page_number: int) -> None:
        # The native controller emits its whole displayed card set. A future
        # pagination contract must be reviewed, never silently truncated.
        if pairs or page_number != 1:
            raise ValueError("Canvas returned unsupported native dashboard pagination")

    def decode(self, data: Any) -> list[dict]:
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise ValueError("Canvas did not provide the native dashboard card array")
        return data


def _object(canvas: Canvas, request: CanvasRequest) -> dict:
    result = dump(canvas.send(request))
    if not isinstance(result, dict):
        raise ValueError("Canvas did not provide the current dashboard resource")  # noqa: TRY004 - adapter reports invalid upstream shape
    return result


def _profile(canvas: Canvas) -> dict:
    value = _object(canvas, _Request("users/self/profile"))
    flag = value.get("k5_user")
    return {"user_id": _ID.validate_python(value.get("id")), "k5_user": flag if type(flag) is bool else None}


def _numeric_position(value: Any) -> int:
    if type(value) is int:
        result = value
    elif isinstance(value, str) and len(value) <= 32 and re.fullmatch(r"[+-]?[0-9]+", value.strip()):
        result = int(value.strip())
    else:
        raise ValueError("Canvas returned a nonnumeric dashboard position")
    if not -1000 <= result <= 1000:
        raise ValueError("Canvas dashboard positions must be within -1000..1000")
    return result


def _position_map(row: dict) -> dict:
    positions = row.get("dashboard_positions")
    if not isinstance(positions, dict) or len(positions) > MAX_RECORDS:
        raise ValueError("Canvas did not provide a complete bounded dashboard positions map")
    for key, value in positions.items():
        if not isinstance(key, str) or len(key) > 100 or re.fullmatch(r"[a-z][a-z0-9_]*_[1-9][0-9]*", key) is None:
            raise ValueError("Canvas returned an unknown dashboard position asset")
        _numeric_position(value)
    return dict(positions)


def _positions(canvas: Canvas) -> dict:
    return _position_map(_object(canvas, _Request(_POSITIONS)))


def _cards(canvas: Canvas, positions: dict) -> list[dict]:
    iterator = canvas.paginate(_Cards())
    try:
        rows = [dump(row) for row in islice(iterator, MAX_RECORDS + 1)]
    finally:
        iterator.close()
    if len(rows) > MAX_RECORDS:
        raise ValueError("Native dashboard exceeds 1000 cards; its complete order is unavailable")
    result, seen = [], set()
    previous = -1001
    for row in rows:
        course_id = _ID.validate_python(row.get("id"))
        asset = f"course_{course_id}"
        if course_id in seen or row.get("assetString") != asset:
            raise ValueError("Canvas returned duplicate or mismatched dashboard course identities")
        seen.add(course_id)
        saved = _numeric_position(positions[asset]) if asset in positions else None
        position = row.get("position")
        if position is not None and (type(position) is not int or not -1000 <= position <= 1000):
            raise ValueError("Canvas returned an unknown native dashboard card position")
        if position != saved:
            raise ValueError("Native dashboard cards and saved positions changed or disagree; refresh first")
        rank = 1001 if position is None else position
        if rank < previous:
            raise ValueError("Canvas did not return its native dashboard position order")
        previous = rank
        card = {key: row.get(key) for key in (
            "id", "assetString", "shortName", "longName", "originalName", "courseCode",
            "enrollmentState", "enrollmentType", "isFavorited", "isK5Subject", "isHomeroom", "published")}
        for flag in ("isFavorited", "isK5Subject", "isHomeroom", "published"):
            if type(card[flag]) is not bool:
                card[flag] = None
        card.update(position=position, html_url=f"{canvas.config.base_url}/courses/{course_id}")
        result.append(card)
    return result


def _state(canvas: Canvas) -> dict:
    profile = _profile(canvas)
    positions = _positions(canvas)
    cards = _cards(canvas, positions)
    # Bind the whole map, not just visible card positions: unrelated assets
    # must not disappear or race with the proposed merge.
    if _positions(canvas) != positions or _profile(canvas) != profile:
        raise ValueError("Current user, dashboard layout, or saved positions changed during the read")
    if profile["k5_user"] is True:
        layout = "unsupported_k5"
    elif profile["k5_user"] is False:
        # Course K5 flags describe courses, not this user's dashboard mode.
        # A teacher can disable K5 while retaining elementary course cards.
        layout = "native_cards"
    else:
        layout = "unknown"
    return {**profile, "cards": cards, "course_ids": [card["id"] for card in cards],
            "dashboard_positions": positions, "layout": layout}


def read_dashboard_cards(canvas: Canvas) -> dict:
    state = _state(canvas)
    supported = state["layout"] == "native_cards"
    data = {**state, "reorder_supported": supported and bool(state["cards"]),
            "active_dashboard_view_verified": False, "html_url": canvas.config.base_url + "/"}
    if not supported:
        data["handoff"] = "Use Canvas for K–5 or an unconfirmed dashboard mode; saved native card reordering is unavailable."
    return envelope(data, origin=canvas.config.base_url, coverage={
        "scope": "saved_native_dashboard_card_order", "complete": supported,
        "active_dashboard_view_verified": False,
        "all_enrollments": False, "returned": len(state["cards"]),
        "selection_note": "Canvas selects the native card set. Its default-course limit is 50; favorites can exceed that limit. The active widget, educator, or other dashboard view is not established."})


def snapshot(canvas: Canvas, p: dict) -> dict:
    action = _ADAPTER.validate_python(p)
    state = _state(canvas)
    if state["layout"] != "native_cards":
        raise ValueError("Dashboard reordering requires a confirmed non-K5 user dashboard mode; use Canvas for K–5 or unknown modes")
    if set(action.course_ids) != set(state["course_ids"]) or len(action.course_ids) != len(state["cards"]):
        raise ValueError("course_ids must be a complete permutation of the fresh native dashboard cards")
    patch = {f"course_{course_id}": index for index, course_id in enumerate(action.course_ids, 1)}
    if len(state["dashboard_positions"] | patch) > MAX_RECORDS:
        raise ValueError("Resulting dashboard preferences would exceed the 1000-position verification bound")
    return {**state, "requested_course_ids": list(action.course_ids), "requested_positions": patch,
            "active_dashboard_view_verified": False,
            "effect": "Reorder the current user's saved native card order. Preserve every unrelated saved position. This does not establish which dashboard view is currently active."}


def perform(canvas: Canvas, p: dict, target: dict) -> dict:
    action = _ADAPTER.validate_python(p)
    fresh = snapshot(canvas, p)
    if fresh != target:
        raise ValueError("Dashboard actor, cards, layout, or positions changed; prepare a fresh reorder")
    patch = {f"course_{course_id}": index for index, course_id in enumerate(action.course_ids, 1)}
    expected = target["dashboard_positions"] | patch
    acknowledged = _position_map(_object(canvas, _Request(_POSITIONS, body={"dashboard_positions": patch})))
    if acknowledged != expected:
        raise ValueError("Canvas did not acknowledge the exact merged dashboard positions; verify before retrying")
    after = _state(canvas)
    old_cards = {card["id"]: {key: value for key, value in card.items() if key != "position"} for card in target["cards"]}
    new_cards = {card["id"]: {key: value for key, value in card.items() if key != "position"} for card in after["cards"]}
    if (after["user_id"] != target["user_id"] or after["layout"] != "native_cards"
            or after["dashboard_positions"] != expected or after["course_ids"] != action.course_ids
            or old_cards != new_cards):
        raise ValueError("Canvas did not independently confirm the exact dashboard order and preserved preferences")
    return {"verified": True, "user_id": after["user_id"], "course_ids": after["course_ids"],
            "cards": after["cards"], "dashboard_positions": after["dashboard_positions"],
            "unrelated_positions_preserved": True, "active_dashboard_view_verified": False,
            "html_url": canvas.config.base_url + "/"}
