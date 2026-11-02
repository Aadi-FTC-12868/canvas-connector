"""Module and ModuleItem models — see api-docs/modules.md for field semantics."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .base import CanvasModel


class ModuleItem(CanvasModel):
    """Item inside a module (Assignment, Page, Discussion, Quiz, File, etc.).

    The `type` field determines what `content_id` references.
    """
    id: int
    module_id: int | None = None
    position: int | None = None
    title: str | None = None
    indent: int | None = None
    type: str | None = None           # Assignment|Page|Discussion|Quiz|File|
                                      # ExternalUrl|ExternalTool|SubHeader
    content_id: int | None = None
    html_url: str | None = None
    url: str | None = None
    page_url: str | None = None
    external_url: str | None = None
    new_tab: bool | None = None
    completion_requirement: dict[str, Any] | None = None
    content_details: dict[str, Any] | None = None
    published: bool | None = None


class Module(CanvasModel):
    """Canvas Module (group of items inside a course)."""
    id: int
    name: str | None = None
    position: int | None = None
    workflow_state: str | None = None
    unlock_at: datetime | None = None
    require_sequential_progress: bool | None = None
    requirement_type: str | None = None
    prerequisite_module_ids: list[int] | None = None
    items_count: int | None = None
    items_url: str | None = None
    state: str | None = None          # per-user: locked|unlocked|started|completed
    completed_at: datetime | None = None
    published: bool | None = None
    items: list[ModuleItem] | None = None   # included via include[]=items
