"""canvas-py — personal Canvas capability library.

Distilled from Instructure's open-source Canvas iOS app. Architecture mirrors
the patterns documented in docs/research/01-api-layer.md.

Public surface kept small. Most users want:

    from canvas_py import Canvas

    canvas = Canvas.from_env()
    courses = canvas.courses.list_active()

Everything prefixed with `_` is internal; prefer the high-level namespaces
on `Canvas` over poking at module internals.
"""
from __future__ import annotations

from ._client import Canvas
from ._errors import (
    CanvasAuthError,
    CanvasError,
    CanvasNotFoundError,
    CanvasPermissionError,
    CanvasRateLimitError,
    CanvasServerError,
    CanvasTransportError,
)
from .endpoints.file_uploads import UploadContext

__all__ = [
    "Canvas",
    "UploadContext",
    "CanvasError",
    "CanvasAuthError",
    "CanvasPermissionError",
    "CanvasNotFoundError",
    "CanvasRateLimitError",
    "CanvasServerError",
    "CanvasTransportError",
]

__version__ = "0.2.1"
