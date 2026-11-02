"""Models for the Canvas three-step file upload flow.

See docs/api-docs/file-uploads.md for protocol details and
docs/research/02-files-and-pdfs.md §1 for how canvas-ios implements it.
"""
from __future__ import annotations

from typing import Any

from .base import CanvasModel


class UploadTarget(CanvasModel):
    """Response from step 1 of the Canvas upload flow.

    Contains the URL to POST the file to (Canvas or external storage) and the
    signed params that must be copied verbatim. The params are
    cryptographically signed by Canvas — any modification causes rejection.
    """
    upload_url: str
    upload_params: dict[str, Any]
