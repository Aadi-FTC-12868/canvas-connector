"""Current-user bookmarks, with prepared writes and bounded independent reads.

API: https://canvas.instructure.com/doc/api/bookmarks.html
Implementation contracts:
https://github.com/instructure/canvas-lms/blob/master/app/controllers/bookmarks/bookmarks_controller.rb
https://github.com/instructure/canvas-lms/blob/master/app/models/bookmarks/bookmark.rb
https://github.com/instructure/canvas-lms/blob/master/gems/acts_as_list/lib/active_record/acts/list.rb

Canvas omits user_id from normal bookmark JSON and scopes every endpoint to
the authenticated user. Its positions are stored integers, not fractional
ranks or array indexes: insertion shifts intervening positions; the default
new position is maximum+1 (1 for an empty list). Zero, gaps and negative
positions are preserved. This supports individual moves, not atomic bulk
reordering. Opaque application data is preserved but cannot be edited here.
Bookmark destinations are data only: no target URL is fetched or authorized.
"""
from __future__ import annotations

import json
from itertools import islice
from typing import Annotated, Any, Literal
from urllib.parse import urljoin, urlsplit

from canvas_py import Canvas
from canvas_py._errors import CanvasError
from canvas_py._request import CanvasRequest, QueryPair
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from .responses import dump, envelope, external_link, public_url, window

Id = Annotated[int, Field(gt=0, le=2**63 - 1, strict=True)]
Position = Annotated[int, Field(ge=-(2**31), le=2**31 - 1, strict=True)]
Name = Annotated[str, Field(min_length=1, max_length=255, strict=True)]
URL = Annotated[str, Field(min_length=1, max_length=4096, strict=True)]
_ID, _POSITION = TypeAdapter(Id), TypeAdapter(Position)
_MAX_ORDER = 1000
_PATH = "users/self/bookmarks"


def _safe_url(value: str) -> str:
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c == "\\" for c in value):
        raise ValueError("Bookmark URL must not contain whitespace, control characters, or backslashes")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or "%" in parsed.hostname
                or parsed.port == 0):
            raise ValueError("Bookmark URL must be absolute HTTP(S) without embedded credentials")
    except ValueError as error:
        raise ValueError("Bookmark URL must be absolute HTTP(S) without embedded credentials") from error
    return value


class _Action(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Content(_Action):
    @field_validator("name", check_fields=False)
    @classmethod
    def visible_name(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError("Bookmark name must contain visible text without control characters")
        return value

    @field_validator("url", check_fields=False)
    @classmethod
    def safe_url(cls, value: str | None) -> str | None:
        return _safe_url(value) if value is not None else value


class BookmarkCreate(_Content):
    kind: Literal["bookmark_create"]
    name: Name
    url: URL
    position: Position | None = None


class BookmarkUpdate(_Content):
    kind: Literal["bookmark_update"]
    bookmark_id: Id
    name: Name | None = None
    url: URL | None = None

    @model_validator(mode="after")
    def has_changes(self) -> BookmarkUpdate:
        if self.name is None and self.url is None:
            raise ValueError("Bookmark update requires a name or URL")
        return self


class BookmarkReorder(_Action):
    kind: Literal["bookmark_reorder"]
    bookmark_id: Id
    position: Position


class BookmarkDelete(_Action):
    kind: Literal["bookmark_delete"]
    bookmark_id: Id


BookmarkActionTypes = BookmarkCreate | BookmarkUpdate | BookmarkReorder | BookmarkDelete
KINDS = ["bookmark_create", "bookmark_update", "bookmark_reorder", "bookmark_delete"]
ROUTES = {kind: "canvas_manage_bookmarks" for kind in KINDS}
_ADAPTER = TypeAdapter(Annotated[BookmarkActionTypes, Field(discriminator="kind")])


class _Profile(CanvasRequest[dict[str, Any]]):
    @property
    def path(self) -> str:
        return "users/self/profile"


class _Bookmark(CanvasRequest[dict[str, Any]]):
    def __init__(self, bookmark_id: int):
        self.bookmark_id = bookmark_id

    @property
    def path(self) -> str:
        return f"{_PATH}/{self.bookmark_id}"


class _Bookmarks(CanvasRequest[list[dict[str, Any]]]):
    response_is_paginated = True

    @property
    def path(self) -> str:
        return _PATH

    def query_pairs(self) -> list[QueryPair]:
        return [("per_page", 100)]

    def decode(self, data: Any) -> list[dict[str, Any]]:
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise TypeError("Canvas did not return a bookmark list")
        return data


class _Mutation(CanvasRequest[dict[str, Any]]):
    def __init__(self, action: BookmarkActionTypes):
        self.action = action

    @property
    def path(self) -> str:
        return _PATH if isinstance(self.action, BookmarkCreate) else f"{_PATH}/{self.action.bookmark_id}"

    @property
    def method(self) -> str:
        return "POST" if isinstance(self.action, BookmarkCreate) else "DELETE" if isinstance(self.action, BookmarkDelete) else "PUT"

    def json_body(self) -> dict | None:
        if isinstance(self.action, BookmarkDelete):
            return None
        return {key: value for key, value in self.action.model_dump().items()
                if key in {"name", "url", "position"} and value is not None}


def _object(canvas: Canvas, request: CanvasRequest) -> dict:
    value = dump(canvas.send(request))
    if not isinstance(value, dict):
        raise TypeError("Canvas did not return the requested bookmark object")
    return value


def _identity(value: dict, key: str, expected: int) -> None:
    if type(value.get(key)) is not int or value[key] != expected:
        raise ValueError("Canvas returned a different bookmark or current user")


def _self(canvas: Canvas) -> int:
    return _ID.validate_python(_object(canvas, _Profile()).get("id"))


def _position(value: dict, actor: int) -> dict:
    if "user_id" in value:
        _identity(value, "user_id", actor)
    return {"id": _ID.validate_python(value.get("id")),
            "position": _POSITION.validate_python(value.get("position"))}


def _record(value: dict, actor: int) -> dict:
    position = _position(value, actor)
    for key, bound in (("name", 255), ("url", 4096)):
        if not isinstance(value.get(key), str) or len(value[key]) > bound:
            raise ValueError("Canvas bookmark text is missing or exceeds the supported bound")
    # API data can contain mobile navigation state. Bind it so updates cannot
    # silently overwrite it, but accept no arbitrary data mutation parameter.
    data = value.get("data")
    try:
        encoded = json.dumps(data, sort_keys=True, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as error:
        raise ValueError("Canvas bookmark data is not supported JSON") from error
    if len(encoded) > 8192:
        raise ValueError("Canvas bookmark data exceeds the supported 8192-character bound")
    return {**position, "name": value["name"], "url": value["url"], "data": data}


def _get(canvas: Canvas, bookmark_id: int, actor: int) -> dict:
    value = _record(_object(canvas, _Bookmark(bookmark_id)), actor)
    _identity(value, "id", bookmark_id)
    return value


def _view(canvas: Canvas, value: dict) -> dict:
    raw_url = value["url"]
    # Canvas itself stores relative links (the documented example is /courses/1).
    # Resolve only a single-slash path against the trusted Canvas origin.
    url = urljoin(canvas.config.base_url + "/", raw_url) if raw_url.startswith("/") and not raw_url.startswith("//") else raw_url
    try:
        url = public_url(_safe_url(url))
    except ValueError:
        url = ""
    result = {**value, "url": url or None, "url_available": bool(url)}
    if url:
        result["link"] = external_link(url, canvas.config.base_url)
    return result


def _items(canvas: Canvas, offset: int, limit: int) -> tuple[list[dict], bool]:
    iterator = canvas.paginate(_Bookmarks())
    try:
        items = [dump(item) for item in islice(iterator, offset, offset + limit + 1)]
    finally:
        iterator.close()
    return items[:limit], len(items) > limit


def list_bookmarks(canvas: Canvas, offset: int = 0, limit: int = 30) -> dict:
    window([], offset, limit)
    actor = _self(canvas)
    items, more = _items(canvas, offset, limit)
    bookmarks = [_view(canvas, _record(item, actor)) for item in items]
    return envelope({"user_id": actor, "bookmarks": bookmarks}, origin=canvas.config.base_url,
                    coverage={"scope": "own_bookmarks", "complete": offset == 0 and not more,
                              "offset": offset, "returned": len(bookmarks), "next_offset": offset + limit if more else None,
                              "pagination_note": "Windows may shift if bookmarks change between reads."})


def get_bookmark(canvas: Canvas, bookmark_id: int) -> dict:
    bookmark_id = _ID.validate_python(bookmark_id)
    actor = _self(canvas)
    return envelope({"user_id": actor, "bookmark": _view(canvas, _get(canvas, bookmark_id, actor))},
                    origin=canvas.config.base_url)


def _order(canvas: Canvas, actor: int) -> list[dict]:
    items, more = _items(canvas, 0, _MAX_ORDER)
    if more:
        raise ValueError("Bookmark ordering exceeds the 1000-bookmark verification bound; open Canvas")
    order = [_position(item, actor) for item in items]
    if len({item["id"] for item in order}) != len(order):
        raise ValueError("Canvas returned duplicate bookmarks during ordering verification")
    if order != sorted(order, key=lambda item: (item["position"], item["id"])):
        raise ValueError("Canvas did not return bookmarks in their documented order")
    return order


def snapshot(canvas: Canvas, p: dict) -> dict:
    action = _ADAPTER.validate_python(p)
    actor = _self(canvas)
    target: dict = {"user_id": actor}
    if not isinstance(action, BookmarkCreate):
        target["bookmark"] = _get(canvas, action.bookmark_id, actor)
    if not isinstance(action, BookmarkUpdate):
        target["order"] = _order(canvas, actor)
        if isinstance(action, BookmarkCreate):
            if len(target["order"]) >= _MAX_ORDER:
                raise ValueError("Creating another bookmark would exceed the 1000-bookmark verification bound; open Canvas")
            _POSITION.validate_python(max((item["position"] for item in target["order"]), default=0) + 1)
        elif {"id": action.bookmark_id, "position": target["bookmark"]["position"]} not in target["order"]:
            raise ValueError("Exact bookmark position disagrees with the current ordered list")
    return target


def _move(order: list[dict], bookmark_id: int, old: int, new: int) -> list[dict]:
    result = []
    for item in order:
        position = item["position"]
        if item["id"] == bookmark_id:
            position = new
        elif new <= position < old:
            position += 1
        elif old < position <= new:
            position -= 1
        result.append({"id": item["id"], "position": position})
    return sorted(result, key=lambda item: (item["position"], item["id"]))


def perform(canvas: Canvas, p: dict, target: dict) -> dict:
    action = _ADAPTER.validate_python(p)
    if snapshot(canvas, p) != target:
        raise ValueError("Bookmark or ordering changed; prepare the action again")
    response = _object(canvas, _Mutation(action))
    actor = target["user_id"]
    bookmark_id = _ID.validate_python(response.get("id"))
    if not isinstance(action, BookmarkCreate):
        _identity(response, "id", action.bookmark_id)
    elif bookmark_id in {item["id"] for item in target["order"]}:
        raise ValueError("Canvas did not return a new bookmark identity")
    if _self(canvas) != actor:
        raise ValueError("Current user changed during bookmark operation")
    result = {"verified": True, "user_id": actor, "bookmark_id": bookmark_id}
    if isinstance(action, BookmarkDelete):
        try:
            canvas.send(_Bookmark(bookmark_id))
        except CanvasError as error:
            if error.status != 404:
                raise
        else:
            raise ValueError("Exact bookmark deletion was not confirmed")
        old = target["bookmark"]["position"]
        expected_order = sorted(
            ({"id": item["id"], "position": item["position"] - (item["position"] > old)}
             for item in target["order"] if item["id"] != bookmark_id),
            key=lambda item: (item["position"], item["id"]),
        )
        result["deleted"] = True
    else:
        current = _get(canvas, bookmark_id, actor)
        if isinstance(action, BookmarkCreate):
            bottom = max((item["position"] for item in target["order"]), default=0) + 1
            position = action.position if action.position is not None else bottom
            expected = {"id": bookmark_id, "name": action.name, "url": action.url, "position": position, "data": None}
            expected_order = _move([*target["order"], {"id": bookmark_id, "position": bottom}], bookmark_id, bottom, position)
        else:
            expected = dict(target["bookmark"])
            if isinstance(action, BookmarkUpdate):
                expected.update({key: value for key, value in (("name", action.name), ("url", action.url)) if value is not None})
            else:
                expected["position"] = action.position
                expected_order = _move(target["order"], bookmark_id, target["bookmark"]["position"], action.position)
        if current != expected:
            raise ValueError("Exact bookmark name, URL, position, and preserved data were not confirmed")
        result["bookmark"] = _view(canvas, current)
    if not isinstance(action, BookmarkUpdate):
        if _order(canvas, actor) != expected_order:
            raise ValueError("Affected bookmark ordering was not confirmed")
        result["order"] = expected_order
    return result
