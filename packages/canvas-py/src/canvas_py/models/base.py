"""Base model with permissive configuration.

Canvas adds fields over time; strict validation would make upgrades painful.
We allow extra fields to pass through and are lenient on types where Canvas
sometimes returns strings for what should be numbers.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class CanvasModel(BaseModel):
    """Base for all Canvas API object models."""

    model_config = ConfigDict(
        extra="allow",               # unknown fields pass through as attributes
        populate_by_name=True,       # accept both field names and aliases
        str_strip_whitespace=False,  # preserve server whitespace
        arbitrary_types_allowed=False,
    )
