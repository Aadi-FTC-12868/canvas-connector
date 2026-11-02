"""File model — see api-docs/files-read.md for field semantics.

Note: Canvas returns `content-type` with a hyphen (not underscore) in File
objects. Pydantic handles this via Field alias.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, computed_field

from .base import CanvasModel


class CanvasFile(CanvasModel):
    """Canvas File object.

    Reference: ~/Projects/canvas-py/docs/api-docs/files-read.md
    The `url` field is a pre-signed download URL that works with the bearer
    token. See research/05-real-world-quirks.md §3.
    """
    id: int
    folder_id: int | None = None
    display_name: str | None = None
    filename: str | None = None
    # Canvas emits this key with a hyphen; pydantic aliases let us access it normally.
    content_type: str | None = Field(default=None, alias="content-type")
    url: str | None = None
    size: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    modified_at: datetime | None = None
    unlock_at: datetime | None = None
    lock_at: datetime | None = None
    locked: bool | None = None
    hidden: bool | None = None
    hidden_for_user: bool | None = None
    locked_for_user: bool | None = None
    lock_info: dict[str, Any] | None = None
    lock_explanation: str | None = None
    visibility_level: str | None = None
    thumbnail_url: str | None = None
    preview_url: str | None = None
    mime_class: str | None = None
    media_entry_id: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def human_size(self) -> str:
        """Bytes rendered in kilobytes/megabytes."""
        if self.size is None:
            return "?"
        if self.size < 1024:
            return f"{self.size} B"
        if self.size < 1024 * 1024:
            return f"{self.size / 1024:.1f} KB"
        return f"{self.size / (1024 * 1024):.1f} MB"
