"""Exact module-item reads without the Horizon SHOW progress side effect.

ContextModuleItemsApiController#show invokes context_module_action(:read) in
Canvas Horizon courses. Collection index does not. Keep raw collection rows
intact: absent student-only/admin-only fields must not acquire model defaults
before progress authorization or comparison with persisted action snapshots.
https://github.com/instructure/canvas-lms/blob/master/app/controllers/context_module_items_api_controller.rb
"""
from __future__ import annotations

from collections.abc import Iterator
from itertools import islice
from typing import Any, Protocol

from ._errors import CanvasError, CanvasNotFoundError
from ._request import CanvasRequest, QueryPair

MAX_MODULE_ITEMS = 1000


class _Reader(Protocol):
    def paginate(self, request: CanvasRequest[Any]) -> Iterator[Any]: ...


class _Items(CanvasRequest[list[dict[str, Any]]]):
    response_is_paginated = True
    pin_pagination_path = True
    # A collection redirect can also point at SHOW before pagination sees it.
    allow_redirects = False

    def __init__(self, course_id: int, module_id: int):
        self.course_id, self.module_id = course_id, module_id

    @property
    def path(self) -> str:
        return f"courses/{self.course_id}/modules/{self.module_id}/items"

    def query_pairs(self) -> list[QueryPair]:
        return [("include[]", "content_details"), ("per_page", 100)]

    def decode(self, data: Any) -> list[dict[str, Any]]:
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise CanvasError("Canvas did not return a complete module item collection")
        return data


def read_module_item_raw(reader: _Reader, course_id: int, module_id: int, item_id: int) -> dict[str, Any]:
    """Find one exact item in a fully inspected, bounded, non-mutating list.

    A match on an early page is not sufficient: later pages can expose a
    duplicate ID or fail, so neither uniqueness nor absence is then confirmed.
    """
    if any(type(value) is not int or not 0 < value <= 2**63 - 1 for value in (course_id, module_id, item_id)):
        raise ValueError("Course, module and item IDs must be positive integers")
    request = _Items(course_id, module_id)
    iterator = reader.paginate(request)
    try:
        rows = list(islice(iterator, MAX_MODULE_ITEMS + 1))
    finally:
        iterator.close()
    if len(rows) > MAX_MODULE_ITEMS:
        raise CanvasError("Module item lookup exceeds 1000 records; the collection is incomplete")
    seen: set[int] = set()
    found = None
    for row in rows:
        if (not isinstance(row, dict) or type(row.get("id")) is not int or
                not 0 < row["id"] <= 2**63 - 1 or type(row.get("module_id")) is not int or row["module_id"] != module_id):
            raise CanvasError("Canvas returned an invalid or different module item identity")
        if row["id"] in seen:
            raise CanvasError("Canvas returned duplicate module item identities")
        seen.add(row["id"])
        if row["id"] == item_id:
            found = row
    if found is None:
        raise CanvasNotFoundError("The requested module item is absent from the complete visible collection",
                                  status=404, url=request.path)
    return found
