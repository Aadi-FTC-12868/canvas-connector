"""Bounded, provenance-bearing text extraction; original document bytes are never edited.

This is a reader, not a layout renderer. PDF extraction uses pypdf, which does not
perform OCR. OOXML is read without executing macros, following external links, or
unpacking archives to disk. Cursor units are pages, slides, or document blocks;
char_offset resumes a unit that did not fit in the previous response.
"""
from __future__ import annotations

import codecs
import csv
import hashlib
import io
import posixpath
import shutil
import subprocess
import tempfile
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bs4 import BeautifulSoup
from defusedxml import ElementTree as SafeXML
from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader

if TYPE_CHECKING:
    from . import Canvas

MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
MAX_XML_BYTES = 8 * 1024 * 1024
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
MAX_PDF_STREAM_BYTES = 8 * 1024 * 1024
MAX_OCR_PAGES = 5
MAX_OCR_PIXELS = 20_000_000
MAX_OCR_OUTPUT_BYTES = 4 * 1024 * 1024
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


class _ReadFailure(Exception):
    def __init__(self, state: str, message: str):
        self.state = state
        self.message = message


class _InvalidCursor(ValueError):
    """Caller-correctable pagination errors, separate from parser failures."""


def document_capabilities() -> dict[str, Any]:
    return {
        "formats": ["pdf", "docx", "pptx", "text", "html", "csv", "tsv"],
        "max_download_bytes": MAX_DOWNLOAD_BYTES,
        "max_units_per_call": 100,
        "max_characters_per_call": 100_000,
        "ocr_available": shutil.which("tesseract") is not None,
        "pdf_ocr_available": shutil.which("tesseract") is not None and shutil.which("pdftoppm") is not None,
        "ocr_language": "eng",
        "max_ocr_pages_per_call": MAX_OCR_PAGES,
        "ocr_processing_budget_seconds": 60,
        "legacy_office_conversion_available": False,
        "originals": "Use download_file locally or fetch_file_content for original bytes; extraction never rewrites a file.",
        "limitations": "Text extraction does not reproduce visual layout. Optional OCR reads images with possible recognition errors; PDF reading order can differ from the page.",
    }


def _validate_window(offset: int, char_offset: int, max_units: int, max_chars: int) -> None:
    if offset < 0 or char_offset < 0:
        raise ValueError("offset and char_offset must be nonnegative")
    if not 1 <= max_units <= 100:
        raise ValueError("max_units must be between 1 and 100")
    if not 1 <= max_chars <= 100_000:
        raise ValueError("max_chars must be between 1 and 100000")


def _format(raw: bytes, filename: str, content_type: str | None) -> str:
    extension = Path(filename).suffix.lower()
    mime = (content_type or "").split(";", 1)[0].lower()
    if raw.startswith(b"%PDF-") or extension == ".pdf" or mime == "application/pdf":
        return "pdf"
    if raw.startswith(OLE_MAGIC):
        if "EncryptedPackage".encode("utf-16-le") in raw:
            return "encrypted_office"
        return "legacy_office"
    if extension in {".doc", ".ppt", ".xls"}:
        return "legacy_office"
    if extension == ".docx" or "wordprocessingml.document" in mime:
        return "docx"
    if extension == ".pptx" or "presentationml.presentation" in mime:
        return "pptx"
    if raw.startswith(b"PK\x03\x04"):
        # An archive is never silently decoded as text, even with a .txt name.
        return "unsupported"
    if mime.startswith("image/") or extension in {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".webp", ".gif", ".heic"}:
        return "image"
    if extension in {".html", ".htm"} or mime in {"text/html", "application/xhtml+xml"}:
        return "html"
    if extension == ".csv" or mime == "text/csv":
        return "csv"
    if extension == ".tsv" or mime == "text/tab-separated-values":
        return "tsv"
    if mime.startswith("text/") or mime in {"application/json", "application/xml"} or extension in {".txt", ".md", ".json", ".xml", ".log", ".rtf"}:
        return "unsupported" if extension == ".rtf" else "text"
    return "unsupported"


def _decode_text(raw: bytes) -> str:
    encoding = "utf-8-sig"
    if raw.startswith((codecs.BOM_UTF32_BE, codecs.BOM_UTF32_LE)):
        encoding = "utf-32"
    elif raw.startswith((codecs.BOM_UTF16_BE, codecs.BOM_UTF16_LE)):
        encoding = "utf-16"
    try:
        value = raw.decode(encoding, errors="strict")
    except UnicodeDecodeError:
        raise _ReadFailure("encoding_required", "Text is not valid UTF-8 or BOM-marked UTF-16/32; convert its encoding explicitly.") from None
    if any(ord(c) < 32 and c not in "\t\n\r\f" for c in value):
        raise _ReadFailure("unsupported", "Binary/control bytes were found in a purported text file; original bytes remain available.")
    return value


def _xml(archive: zipfile.ZipFile, name: str):
    try:
        info = archive.getinfo(name)
    except KeyError:
        raise _ReadFailure("malformed", "The Office archive is missing a required document part.") from None
    if info.flag_bits & 1:
        raise _ReadFailure("encrypted", "Encrypted Office archive; provide an unencrypted copy.")
    if info.file_size > MAX_XML_BYTES:
        raise _ReadFailure("limit_exceeded", "An Office XML part exceeds the 8 MiB extraction safety limit.")
    with archive.open(info) as source:
        data = source.read(MAX_XML_BYTES + 1)
    if len(data) > MAX_XML_BYTES:
        raise _ReadFailure("limit_exceeded", "An Office XML part exceeds the extraction safety limit.")
    return SafeXML.fromstring(data, forbid_dtd=True)


def _paragraph_text(element, namespace: str = W) -> str:
    parts: list[str] = []
    for node in element.iter():
        if node.tag == namespace + "t":
            parts.append(node.text or "")
        elif node.tag == namespace + "tab":
            parts.append("\t")
        elif node.tag in {namespace + "br", namespace + "cr"}:
            parts.append("\n")
    return "".join(parts)


def _word_blocks(element):
    for child in element:
        if child.tag == W + "p":
            yield "paragraph", _paragraph_text(child)
        elif child.tag == W + "tbl":
            rows = []
            for row in child.findall(W + "tr"):
                cells = []
                for cell in row.findall(W + "tc"):
                    cells.append("\n".join(text for _, text in _word_blocks(cell)))
                rows.append("\t".join(cells))
            yield "table", "\n".join(rows)
        elif child.tag in {W + "sdt", W + "sdtContent", W + "ins", W + "customXml"}:
            yield from _word_blocks(child)


def _office_units(raw: bytes, kind: str, warnings: list[str]) -> list[dict[str, Any]]:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) > 10_000 or sum(i.file_size for i in entries) > MAX_PACKAGE_BYTES:
            raise _ReadFailure("limit_exceeded", "Office archive exceeds the entry count or 64 MiB expanded-size safety limit.")
        if len({i.filename for i in entries}) != len(entries):
            raise _ReadFailure("malformed", "Office archive has duplicate part names.")
        if any(i.flag_bits & 1 for i in entries):
            raise _ReadFailure("encrypted", "Encrypted Office archive; provide an unencrypted copy.")
        if kind == "docx":
            root = _xml(archive, "word/document.xml")
            body = root.find(W + "body")
            if body is None:
                raise _ReadFailure("malformed", "DOCX has no document body.")
            units = [{"kind": block_kind, "part": "body", "text": text} for block_kind, text in _word_blocks(body)]
            # These text-bearing parts have no position in the body stream.
            # Include them explicitly, with provenance, after body content.
            extras = sorted(name for name in archive.namelist() if name.startswith("word/") and name.endswith(".xml") and
                            (Path(name).name.startswith(("header", "footer")) or Path(name).name in {"footnotes.xml", "endnotes.xml", "comments.xml"}))
            for name in extras:
                extra = _xml(archive, name)
                paragraphs = [_paragraph_text(p) for p in extra.iter(W + "p")]
                units.append({"kind": "supplement", "part": name, "text": "\n".join(paragraphs)})
            if any(node.tag in {W + "drawing", W + "pict", W + "object", W + "altChunk"} for node in root.iter()):
                warnings.append("DOCX contains images, embedded objects, or imported content; their visual content is not extracted.")
            if any(node.tag in {W + "ins", W + "del"} for node in root.iter()):
                warnings.append("Tracked changes are present; extraction is not a Word revision/layout rendering.")
            if any("officeDocument/2006/math" in node.tag for node in root.iter()):
                warnings.append("DOCX contains equations; equation layout is not extracted.")
            return units

        presentation = _xml(archive, "ppt/presentation.xml")
        relations = _xml(archive, "ppt/_rels/presentation.xml.rels")
        mapping = {r.attrib.get("Id"): r for r in relations}
        ids = presentation.find(P + "sldIdLst")
        if ids is None:
            return []
        units = []
        for index, slide in enumerate(ids):
            relation = mapping.get(slide.attrib.get(R + "id"))
            if relation is None or relation.attrib.get("TargetMode") == "External":
                raise _ReadFailure("malformed", "Presentation slide relationship is absent or external.")
            target = relation.attrib.get("Target", "")
            name = posixpath.normpath(posixpath.join("ppt", target)) if not target.startswith("/") else target.lstrip("/")
            if not name.startswith("ppt/slides/") or not name.endswith(".xml"):
                raise _ReadFailure("malformed", "Presentation slide relationship points outside its slide parts.")
            root = _xml(archive, name)
            paragraphs = [_paragraph_text(p, A) for p in root.iter(A + "p")]
            unit = {"kind": "slide", "slide": index + 1, "part": name, "text": "\n".join(paragraphs)}
            slide_relationships = posixpath.join(posixpath.dirname(name), "_rels", posixpath.basename(name) + ".rels")
            if slide_relationships in archive.namelist():
                for relation in _xml(archive, slide_relationships):
                    if relation.attrib.get("Type", "").endswith("/notesSlide"):
                        if relation.attrib.get("TargetMode") == "External":
                            warnings.append("External speaker notes are not included; external document links are never fetched.")
                            continue
                        target = relation.attrib.get("Target", "")
                        notes_name = posixpath.normpath(posixpath.join(posixpath.dirname(name), target)) if not target.startswith("/") else target.lstrip("/")
                        if not notes_name.startswith("ppt/notesSlides/") or not notes_name.endswith(".xml"):
                            raise _ReadFailure("malformed", "Speaker notes relationship points outside its notes parts.")
                        notes_root = _xml(archive, notes_name)
                        notes = []
                        for shape in notes_root.iter(P + "sp"):
                            placeholders = list(shape.iter(P + "ph"))
                            if any(p.attrib.get("type") in {"sldNum", "hdr", "ftr", "dt"} for p in placeholders):
                                continue
                            notes.extend(_paragraph_text(p, A) for p in shape.iter(A + "p"))
                        unit["notes_part"] = notes_name
                        if any(notes):
                            unit["text"] += "\n\n[Speaker notes]\n" + "\n".join(notes)
            units.append(unit)
            if any(node.tag in {P + "pic", A + "graphicData"} for node in root.iter()):
                warnings.append("PPTX contains images/tables/diagrams; shape text is extracted but visual content/layout may carry additional information.")
        warnings.append("PPTX text follows stored shape order, which may differ from visual reading order.")
        return units


def _pdf_unit(reader: PdfReader, index: int) -> dict[str, Any]:
    page = reader.pages[index]
    contents = page.get_contents()
    if contents is not None and len(contents.get_data()) > MAX_PDF_STREAM_BYTES:
        raise _ReadFailure("limit_exceeded", "A PDF page content stream exceeds the 8 MiB extraction safety limit.")
    text = page.extract_text() or ""
    resources = page.get("/Resources", {})
    if hasattr(resources, "get_object"):
        resources = resources.get_object()
    objects = resources.get("/XObject", {})
    if hasattr(objects, "get_object"):
        objects = objects.get_object()
    has_visuals = bool(objects)
    # Empty text is not proof a page is blank: it may be a scan, image, or outlines.
    state = "ok" if text.strip() else "ocr_required"
    return {"kind": "page", "page": index + 1, "text": text, "state": state, "method": "text_layer",
            "image_or_form_resources": has_visuals}


def _ocr_unit(raw: bytes, page_index: int | None, *, deadline: float | None = None) -> dict[str, Any]:
    """Offline OCR with bounded raster size, subprocess time, and output bytes.

    Only a PDF's selected page is rasterized, without changing the original.
    Confidence is Tesseract's mean word score, not a correctness guarantee.
    """
    tesseract = shutil.which("tesseract")
    pdftoppm = shutil.which("pdftoppm") if page_index is not None else None
    if not tesseract or (page_index is not None and not pdftoppm):
        raise _ReadFailure("ocr_unavailable", "OCR requested but tesseract (and pdftoppm for PDFs) is unavailable in this runtime.")
    def process_timeout() -> float:
        remaining = deadline - time.monotonic() if deadline is not None else 30
        if remaining <= 0:
            raise _ReadFailure("ocr_timeout", "OCR reached this call's processing budget.")
        return min(30, remaining)
    with tempfile.TemporaryDirectory(prefix="canvas-ocr-") as directory:
        work = Path(directory)
        source = work / ("original.pdf" if page_index is not None else "original.image")
        source.write_bytes(raw)
        raster = source
        if page_index is not None:
            prefix = work / "page"
            try:
                subprocess.run([pdftoppm, "-f", str(page_index + 1), "-l", str(page_index + 1),
                                "-singlefile", "-scale-to", "2400", "-gray", "-png", str(source), str(prefix)],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=process_timeout())
            except subprocess.TimeoutExpired:
                raise _ReadFailure("ocr_timeout", "Selected PDF page exceeded the 30 second OCR rendering limit.") from None
            except (subprocess.CalledProcessError, OSError):
                raise _ReadFailure("ocr_failed", "Selected PDF page could not be rendered for OCR.") from None
            raster = prefix.with_suffix(".png")
        try:
            with Image.open(raster) as picture:
                if picture.width * picture.height > MAX_OCR_PIXELS:
                    raise _ReadFailure("limit_exceeded", "Image exceeds the 20 million pixel OCR safety limit.")
                if getattr(picture, "n_frames", 1) > 1:
                    raise _ReadFailure("unsupported", "Multi-frame images require conversion to a paginated PDF before OCR.")
        except (Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise _ReadFailure("limit_exceeded", "Image exceeds the OCR pixel safety limit.") from None
        except (UnidentifiedImageError, OSError):
            raise _ReadFailure("malformed", "Image could not be validated for OCR.") from None
        output = work / "recognized"
        try:
            subprocess.run([tesseract, str(raster), str(output), "-l", "eng", "--psm", "3", "txt", "tsv"],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=process_timeout())
        except subprocess.TimeoutExpired:
            raise _ReadFailure("ocr_timeout", "Selected page exceeded the 30 second OCR recognition limit.") from None
        except (subprocess.CalledProcessError, OSError):
            raise _ReadFailure("ocr_failed", "OCR failed; verify the runtime includes Tesseract English language data.") from None
        text_file, tsv_file = output.with_suffix(".txt"), output.with_suffix(".tsv")
        if not text_file.is_file() or not tsv_file.is_file():
            raise _ReadFailure("ocr_failed", "OCR did not produce its required output files.")
        if text_file.stat().st_size > MAX_OCR_OUTPUT_BYTES or tsv_file.stat().st_size > MAX_OCR_OUTPUT_BYTES:
            raise _ReadFailure("limit_exceeded", "OCR output exceeded the 4 MiB safety limit.")
        text = text_file.read_text(encoding="utf-8")
        confidences = []
        with tsv_file.open(encoding="utf-8", newline="") as source_file:
            for row in csv.DictReader(source_file, delimiter="\t", quoting=csv.QUOTE_NONE):
                try:
                    score = float(row.get("conf", "-1"))
                except ValueError:
                    continue
                if row.get("text", "").strip() and 0 <= score <= 100:
                    confidences.append(score)
        return {"kind": "page" if page_index is not None else "image", "text": text,
                "state": "ok" if text.strip() else "ocr_no_text", "method": "ocr",
                "ocr_mean_word_confidence": round(sum(confidences) / len(confidences), 2) if confidences else None,
                "ocr_language": "eng", **({"page": page_index + 1} if page_index is not None else {})}


def extract_document(raw: bytes, filename: str, content_type: str | None = None, *,
                     offset: int = 0, char_offset: int = 0, max_units: int = 20,
                     max_chars: int = 40_000, expected_sha256: str | None = None, ocr: bool = False) -> dict[str, Any]:
    """Read original bytes without modifying them; return bounded text and an exact cursor.

    offset is a zero-based page, slide, DOCX block, or text-unit index. Resume
    using both next_offset and next_char_offset and the returned sha256. A
    changed hash fails explicitly so callers never stitch different versions.
    Complete text means only the supported text layer, never visual fidelity.
    """
    _validate_window(offset, char_offset, max_units, max_chars)
    ocr_deadline = time.monotonic() + 60
    if ocr:
        max_units = min(max_units, MAX_OCR_PAGES)
    digest = hashlib.sha256(raw).hexdigest()
    kind = _format(raw, filename, content_type)
    result: dict[str, Any] = {
        "state": "ok", "format": kind, "filename": filename, "content_type": content_type,
        "sha256": digest, "byte_count": len(raw), "segments": [], "text": "",
        "offset": offset, "char_offset": char_offset, "next_offset": None, "next_char_offset": None,
        "read_options": {"ocr": ocr, "max_units": max_units, "max_chars": max_chars},
        "warnings": [], "capabilities": document_capabilities(),
        "coverage": {"total_units": None, "returned_units": 0, "returned_characters": 0,
                     "has_more": False, "full_document_in_response": False,
                     "text_complete_for_returned_units": False, "visual_content_complete": False,
                     "all_document_text_extracted": False, "ocr_used": False},
    }
    warnings: list[str] = result["warnings"]
    try:
        if len(raw) > MAX_DOWNLOAD_BYTES:
            raise _ReadFailure("limit_exceeded", "File exceeds the 50 MiB reading safety limit.")
        if expected_sha256 is not None and digest != expected_sha256:
            raise _ReadFailure("source_changed", "Document bytes changed; restart from offset 0 using the new hash.")
        if kind == "encrypted_office":
            raise _ReadFailure("encrypted", "Password-encrypted Office file; provide an unencrypted copy.")
        if kind == "legacy_office":
            raise _ReadFailure("unsupported_legacy", "Legacy .doc/.ppt/.xls requires conversion to DOCX/PPTX/PDF; original bytes are still available.")
        if kind == "image" and not ocr:
            raise _ReadFailure("ocr_required", "This is an image; download the original for visual reading or OCR.")
        if kind == "unsupported":
            raise _ReadFailure("unsupported", "Unsupported document format; download the original and use its native reader/converter.")
        reader = None
        units = None
        if kind == "pdf":
            if not raw.startswith(b"%PDF-") or b"%%EOF" not in raw[-1024:]:
                raise _ReadFailure("malformed", "PDF header/end marker is missing; the document may be malformed or truncated.")
            reader = PdfReader(io.BytesIO(raw), strict=True)
            if reader.is_encrypted:
                raise _ReadFailure("encrypted", "Encrypted PDF; provide an unencrypted copy.")
            total = len(reader.pages)
            warnings.append("PDF text is extracted page by page; tables, equations, images, and reading order require visual review.")
        elif kind == "image":
            units = [_ocr_unit(raw, None, deadline=ocr_deadline)]
            total = 1
        elif kind in {"docx", "pptx"}:
            units = _office_units(raw, kind, warnings)
            total = len(units)
        else:
            text = _decode_text(raw)
            if kind == "html":
                soup = BeautifulSoup(text, "html.parser")
                for node in soup(["script", "style", "noscript", "template"]):
                    node.decompose()
                text = soup.get_text("\n", strip=True)
                if soup.find("img"):
                    warnings.append("HTML includes images; image content is not extracted and external resources are never fetched.")
            units = [{"kind": "text", "text": text}]
            total = 1
        result["coverage"]["total_units"] = total
        if offset > total or (offset == total and char_offset):
            raise _InvalidCursor("offset is outside the document")
        index, within, remaining = offset, char_offset, max_chars
        segments = result["segments"]
        missing_text = False
        for index in range(offset, min(total, offset + max_units)):
            if ocr and time.monotonic() >= ocr_deadline:
                result["next_offset"], result["next_char_offset"] = index, within
                warnings.append("OCR reached this call's processing budget; continue with the returned cursor.")
                break
            unit = _pdf_unit(reader, index) if reader is not None else units[index]
            if ocr and reader is not None and (unit.get("state") == "ocr_required" or unit.get("image_or_form_resources")):
                try:
                    recognized = _ocr_unit(raw, index, deadline=ocr_deadline)
                    if unit["text"].strip():
                        # A scanned page may have a tiny watermark/header text
                        # layer. Retain that layer and explicitly label OCR
                        # rather than let those few words suppress recognition.
                        unit = {**unit, **recognized, "method": "text_layer_and_ocr",
                                "text_layer_characters": len(unit["text"]), "ocr_characters": len(recognized["text"]),
                                "text": unit["text"] + "\n\n[OCR of rendered page]\n" + recognized["text"]}
                    else:
                        unit = {**unit, **recognized}
                except _ReadFailure as error:
                    unit = {**unit, "state": error.state, "method": "ocr_failed"}
                    warnings.append(error.message)
            text = unit["text"]
            if within > len(text):
                raise _InvalidCursor("char_offset is outside the current unit")
            # Reserve separators in the convenience text so its length also
            # respects max_chars; canonical segments retain exact source text.
            separator_size = 2 if segments else 0
            if remaining < separator_size or (remaining == separator_size and text[within:]):
                result["next_offset"], result["next_char_offset"] = index, within
                break
            remaining -= separator_size
            taken = text[within:within + remaining]
            segment = {**unit, "index": index, "char_offset": within, "text": taken,
                       "total_characters": len(text), "complete": within == 0 and len(taken) == len(text)}
            segments.append(segment)
            missing_text = missing_text or unit.get("state", "ok") != "ok"
            remaining -= len(taken)
            if within + len(taken) < len(text):
                result["next_offset"], result["next_char_offset"] = index, within + len(taken)
                break
            within = 0
            if index + 1 < total and (remaining == 0 or len(segments) == max_units):
                result["next_offset"], result["next_char_offset"] = index + 1, 0
                break
        # Segment text is canonical. The convenience join adds only separators.
        result["text"] = "\n\n".join(s["text"] for s in segments)
        full = offset == 0 and char_offset == 0 and result["next_offset"] is None
        result["coverage"].update({
            "returned_units": len(segments), "returned_characters": sum(len(s["text"]) for s in segments),
            "has_more": result["next_offset"] is not None,
            "full_document_in_response": full,
            "text_complete_for_returned_units": not missing_text and all(s["complete"] for s in segments),
        })
        if missing_text:
            warnings.append("At least one returned unit has no readable text; inspect its state and use the original for visual review. Empty extraction is not proof the page is blank.")
            result["state"] = "partial" if any(s["text"].strip() for s in segments) else next(s.get("state", "ocr_required") for s in segments if s.get("state", "ok") != "ok")
        ocr_used = any(s.get("method") in {"ocr", "text_layer_and_ocr"} for s in segments)
        result["coverage"]["ocr_used"] = ocr_used
        if ocr_used:
            warnings.append("OCR used English recognition and can misread or omit words, equations, handwriting, and tables. Confidence is a recognizer score, not verified accuracy; inspect the original for exact wording.")
        if ocr and kind in {"docx", "pptx"}:
            warnings.append("Embedded Office images are not OCRed; convert the document to PDF to OCR visual content.")
        if any(s.get("image_or_form_resources") for s in segments):
            warnings.append("PDF pages contain image/form resources; their visual content may carry additional information beyond the extracted text layer.")
        result["coverage"]["all_document_text_extracted"] = full and not missing_text and not any(
            phrase in warning for warning in warnings for phrase in ("not extracted", "not included", "additional information", "Tracked changes")
        ) and not ocr_used
    except _ReadFailure as error:
        result["state"] = error.state
        warnings.append(error.message)
    except _InvalidCursor:
        raise
    except Exception as error:
        # Parser exception text can contain untrusted document bytes/URLs.
        result["state"] = "malformed"
        warnings.append(f"Document could not be parsed safely ({type(error).__name__}); the file may be malformed or truncated.")
    if result["coverage"]["returned_units"] == 0:
        # Discard incomplete parser results rather than expose segments alongside
        # a misleading zero-unit coverage record after a parser failure.
        result["segments"] = []
    result["warnings"] = list(dict.fromkeys(warnings))
    return result


def read_canvas_file(canvas: Canvas, file_id: int, *, offset: int = 0, char_offset: int = 0,
                     max_units: int = 20, max_chars: int = 40_000, max_bytes: int = MAX_DOWNLOAD_BYTES,
                     expected_sha256: str | None = None, ocr: bool = False) -> dict[str, Any]:
    """Fetch current metadata/bytes using Canvas's authenticated safe downloader.

    No persistent cache or filesystem path is exposed, including over HTTP.
    The originals remain on Canvas; sync/download callers can retain them.
    Authentication/permission/download failures propagate rather than masquerade
    as an empty or unreadable document.
    """
    _validate_window(offset, char_offset, max_units, max_chars)
    if not 1 <= max_bytes <= MAX_DOWNLOAD_BYTES:
        raise ValueError(f"max_bytes must be between 1 and {MAX_DOWNLOAD_BYTES}")
    metadata = canvas.files.get(file_id)
    source = {
        "file_id": file_id, "display_name": metadata.display_name, "filename": metadata.filename,
        "canvas_updated_at": metadata.updated_at.isoformat() if metadata.updated_at else None,
        "canvas_modified_at": metadata.modified_at.isoformat() if metadata.modified_at else None,
        "declared_size": metadata.size, "fetched_at": datetime.now(UTC).isoformat(),
        "hidden_for_user": metadata.hidden_for_user, "locked_for_user": metadata.locked_for_user,
        "cache_hit": False, "freshness": "Metadata and bytes fetched for this call; use sha256 to bind continuation to this version.",
    }
    # Canvas's hidden flag also represents "only available with link". Those
    # files are readable from modules/assignments; locked_for_user is the
    # access decision. The authenticated download still enforces server ACLs.
    if metadata.locked_for_user or (metadata.locked_for_user is None and metadata.locked):
        return {"state": "locked", "source": source, "text": "", "segments": [], "warnings": ["Canvas reports this file locked for this user."]}
    if metadata.size is not None and metadata.size > max_bytes:
        return {"state": "limit_exceeded", "source": source, "text": "", "segments": [], "warnings": ["File exceeds this call's byte limit; no document bytes were downloaded."]}
    with tempfile.TemporaryDirectory(prefix="canvas-read-") as directory:
        destination = Path(directory) / "original"
        canvas.files.download(metadata, destination, max_bytes=max_bytes)
        raw = destination.read_bytes()
    if metadata.size is not None and len(raw) != metadata.size:
        return {"state": "size_mismatch", "source": source, "sha256": hashlib.sha256(raw).hexdigest(), "byte_count": len(raw),
                "text": "", "segments": [], "warnings": ["Downloaded byte count differs from Canvas metadata; refresh/retry before treating this as a complete document."]}
    result = extract_document(raw, metadata.filename or metadata.display_name or f"file-{file_id}", metadata.content_type,
                              offset=offset, char_offset=char_offset, max_units=max_units, max_chars=max_chars,
                              expected_sha256=expected_sha256, ocr=ocr)
    result["source"] = source
    result["file_id"] = file_id
    if result.get("next_offset") is not None:
        result["next_read"] = {"tool": "canvas_read_file", "arguments": {
            "file_id": file_id, "offset": result["next_offset"], "char_offset": result["next_char_offset"],
            "expected_sha256": result["sha256"], "ocr": ocr, "max_units": max_units, "max_chars": max_chars}}
    return result
