"""Modules endpoints.

Distilled from:
    - research: docs/research/04-content-and-communication.md §3
    - api-docs:  docs/api-docs/modules.md
    - iOS ref:   canvas-ios/Core/Core/Features/Modules/

Modules is the most reliable discovery surface for "what is in this course"
(see research/05-real-world-quirks.md §5). Prefer this over assignments
enumeration for discovery when possible.
"""
from __future__ import annotations

from typing import Any, ClassVar

from .._request import CanvasRequest, QueryPair
from ..models.module import Module, ModuleItem

# Always include items + content_details — returns nearly-complete course map
# in a single call, matching what the Canvas app itself fetches.
_DEFAULT_INCLUDES = ["items", "content_details"]


class ListModules(CanvasRequest[list[Module]]):
    """`GET /api/v1/courses/:course_id/modules?include[]=items&include[]=content_details`.

    Returns modules with their items inlined and full lock/availability info.
    When a module has too many items to inline, Canvas returns `items_url`
    instead — the caller is expected to follow that URL via GetModuleItems.
    """

    method: ClassVar[str] = "GET"
    response_is_paginated: ClassVar[bool] = True

    def __init__(
        self,
        course_id: int,
        *,
        include: list[str] | None = None,
        search_term: str | None = None,
        per_page: int = 50,
    ):
        self.course_id = course_id
        self.include = include if include is not None else list(_DEFAULT_INCLUDES)
        self.search_term = search_term
        self.per_page = per_page

    @property
    def path(self) -> str:
        return f"courses/{self.course_id}/modules"

    def query_pairs(self) -> list[QueryPair]:
        pairs: list[QueryPair] = [("include[]", inc) for inc in self.include]
        if self.search_term:
            pairs.append(("search_term", self.search_term))
        pairs.append(("per_page", self.per_page))
        return pairs

    def decode(self, data: Any) -> list[Module]:
        if not isinstance(data, list):
            raise TypeError(f"expected list from /modules, got {type(data).__name__}")
        return [Module.model_validate(item) for item in data]


class GetModuleItems(CanvasRequest[list[ModuleItem]]):
    """Paginated item collection, including when Canvas omits inline items."""

    response_is_paginated: ClassVar[bool] = True

    def __init__(self, course_id: int, module_id: int):
        self.course_id, self.module_id = course_id, module_id

    @property
    def path(self) -> str:
        return f"courses/{self.course_id}/modules/{self.module_id}/items"

    def query_pairs(self) -> list[QueryPair]:
        return [("include[]", "content_details"), ("per_page", 100)]

    def decode(self, data: Any) -> list[ModuleItem]:
        if not isinstance(data, list):
            raise TypeError("expected a module item list")
        return [ModuleItem.model_validate(item) for item in data]


class GetModuleItem(CanvasRequest[ModuleItem]):
    def __init__(self, course_id: int, module_id: int, item_id: int):
        self.course_id, self.module_id, self.item_id = course_id, module_id, item_id

    @property
    def path(self) -> str:
        return f"courses/{self.course_id}/modules/{self.module_id}/items/{self.item_id}"

    def query_pairs(self) -> list[QueryPair]:
        return [("include[]", "content_details")]

    def decode(self, data: Any) -> ModuleItem:
        return ModuleItem.model_validate(data)


class GetModuleItemSequence(CanvasRequest[dict[str, Any]]):
    """Canvas's actual previous/current/next relationships, not inferred order."""

    def __init__(self, course_id: int, asset_type: str, asset_id: str):
        if asset_type not in {"ModuleItem", "File", "Page", "Discussion", "Assignment", "Quiz", "ExternalTool"}:
            raise ValueError("unsupported module sequence asset type")
        self.course_id, self.asset_type, self.asset_id = course_id, asset_type, asset_id

    @property
    def path(self) -> str:
        return f"courses/{self.course_id}/module_item_sequence"

    def query_pairs(self) -> list[QueryPair]:
        return [("asset_type", self.asset_type), ("asset_id", self.asset_id)]

    def decode(self, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise TypeError("expected a module item sequence object")
        return data
