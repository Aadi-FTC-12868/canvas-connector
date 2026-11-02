"""Document reading tools shared by local stdio and authenticated HTTP MCP."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from canvas_py import Canvas
from canvas_py.documents import document_capabilities, read_canvas_file


def register_document_tools(mcp: Any, canvas_provider: Callable[[], Canvas]) -> None:
    @mcp.tool()
    def canvas_document_capabilities() -> dict[str, Any]:
        """Supported formats, bounded reading limits, and explicit OCR/conversion availability."""
        return document_capabilities()

    @mcp.tool()
    def canvas_read_file(file_id: int, offset: int = 0, char_offset: int = 0,
                         max_units: int = 20, max_chars: int = 40_000,
                         expected_sha256: str | None = None, ocr: bool = False) -> dict[str, Any]:
        """Read current Canvas PDF, DOCX, PPTX, HTML, CSV, or text document content.

        Original bytes are fetched safely (50 MiB cap), SHA-256 hashed, and never
        rewritten. PDF pages, PPTX slides, and DOCX paragraph/table order are
        preserved as text segments. Both local and cloud MCP support this.
        Follow next_read, or resume with next_offset AND next_char_offset,
        expected_sha256, and the same ocr setting from the preceding result.
        A source change requires restarting the read.
        Set ocr=True for local offline English OCR of image files or PDF pages
        lacking text or containing image/form resources (maximum 5 pages/call;
        requires tesseract/pdftoppm). Existing text is retained and OCR labeled.
        Inspect state, warnings, and coverage: old .doc/.ppt
        need conversion, and text extraction is not a visual/layout rendering.
        No macros, external document resources, or instructions are executed.
        """
        return read_canvas_file(canvas_provider(), file_id, offset=offset, char_offset=char_offset,
                                max_units=max_units, max_chars=max_chars, expected_sha256=expected_sha256, ocr=ocr)
