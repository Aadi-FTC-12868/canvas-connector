"""Real in-memory PDF/OOXML bytes exercise parsing, cursors, and safe failure states."""
from __future__ import annotations

import hashlib
import io
import shutil
import zipfile
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image, ImageDraw
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from canvas_py.documents import (
    MAX_DOWNLOAD_BYTES,
    document_capabilities,
    extract_document,
    read_canvas_file,
)
from canvas_py.models.file import CanvasFile


def pdf_bytes(texts, *, password=None):
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=612, height=792)
        if text:
            font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                     NameObject("/Subtype"): NameObject("/Type1"),
                                     NameObject("/BaseFont"): NameObject("/Helvetica")})
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET".encode())
            page[NameObject("/Contents")] = writer._add_object(stream)
    if password:
        writer.encrypt(password)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def package(parts):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/></Types>')
        for name, data in parts.items():
            archive.writestr(name, data)
    return output.getvalue()


def docx_bytes():
    return package({"word/document.xml": '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
      <w:p><w:r><w:t>Before</w:t></w:r></w:p>
      <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Left</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Right</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
      <w:p><w:r><w:t>After</w:t><w:tab/><w:t>tab</w:t><w:br/><w:t>line</w:t></w:r></w:p>
      </w:body></w:document>''',
      "word/header1.xml": '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>Header</w:t></w:r></w:p></w:hdr>'})


def pptx_bytes():
    def slide(text):
        return f'<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>'
    return package({
        "ppt/presentation.xml": '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId id="256" r:id="rId2"/><p:sldId id="257" r:id="rId1"/></p:sldIdLst></p:presentation>',
        "ppt/_rels/presentation.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="slides/slide1.xml"/><Relationship Id="rId2" Target="slides/slide2.xml"/></Relationships>',
        "ppt/slides/slide1.xml": slide("Second"), "ppt/slides/slide2.xml": slide("First"),
    })


def test_pdf_page_boundaries_and_hash():
    raw = pdf_bytes(["Page one", "Page two"])
    result = extract_document(raw, "packet.pdf")
    assert result["state"] == "ok"
    assert [s["page"] for s in result["segments"]] == [1, 2]
    assert [s["text"] for s in result["segments"]] == ["Page one", "Page two"]
    assert result["sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["coverage"]["full_document_in_response"]
    assert not result["coverage"]["visual_content_complete"]


def test_cursor_resumes_long_page_without_loss_and_then_next_page():
    raw = pdf_bytes(["abcdefghijklm", "second page"])
    options = {"max_chars": 5, "max_units": 1}
    chunks = []
    for _ in range(10):
        result = extract_document(raw, "packet.pdf", **options)
        chunks.extend(s["text"] for s in result["segments"])
        assert result["coverage"]["returned_characters"] <= 5
        assert result["coverage"]["returned_units"] <= 1
        if result["next_offset"] is None:
            break
        options.update(offset=result["next_offset"], char_offset=result["next_char_offset"], expected_sha256=result["sha256"])
    assert "".join(chunks) == "abcdefghijklmsecond page"
    assert not result["coverage"]["full_document_in_response"]


def test_page_window_does_not_extract_unrequested_pdf_pages(monkeypatch):
    import canvas_py.documents as documents
    original = documents._pdf_unit
    indices = []
    monkeypatch.setattr(documents, "_pdf_unit", lambda reader, index: indices.append(index) or original(reader, index))
    result = extract_document(pdf_bytes(["one", "two", "three"]), "x.pdf", offset=1, max_units=1)
    assert indices == [1]
    assert result["next_offset"] == 2


def test_empty_pdf_page_is_explicitly_ocr_or_visual_review_required(monkeypatch):
    monkeypatch.setattr("canvas_py.documents.shutil.which", lambda _: None)
    result = extract_document(pdf_bytes([None]), "scan.pdf")
    assert result["state"] == "ocr_required"
    assert result["segments"][0]["state"] == "ocr_required"
    assert not result["coverage"]["all_document_text_extracted"]
    assert result["capabilities"]["ocr_available"] is False


def test_mixed_pdf_never_claims_all_text_read():
    result = extract_document(pdf_bytes(["Readable", None]), "mixed.pdf")
    assert result["state"] == "partial"
    assert result["segments"][0]["text"] == "Readable"
    assert not result["coverage"]["text_complete_for_returned_units"]


def test_encrypted_pdf_and_truncated_pdf_are_not_text():
    assert extract_document(pdf_bytes(["secret"], password="private"), "locked.pdf")["state"] == "encrypted"
    assert extract_document(pdf_bytes(["good"])[:-30], "partial.pdf")["state"] == "malformed"
    assert extract_document(b"not a PDF", "bad.pdf")["state"] == "malformed"


def test_docx_interleaves_paragraphs_tables_and_supplement_parts():
    raw = docx_bytes()
    result = extract_document(raw, "notes.docx")
    assert result["state"] == "ok"
    assert [s["kind"] for s in result["segments"]] == ["paragraph", "table", "paragraph", "supplement"]
    assert [s["text"] for s in result["segments"]] == ["Before", "Left\tRight", "After\ttab\nline", "Header"]
    assert result["sha256"] == hashlib.sha256(raw).hexdigest()


def test_pptx_uses_presentation_relationship_order_not_filename_sort():
    result = extract_document(pptx_bytes(), "slides.pptx")
    assert result["state"] == "ok"
    assert [(s["slide"], s["text"]) for s in result["segments"]] == [(1, "First"), (2, "Second")]


def test_pptx_speaker_notes_follow_their_slide_relationship():
    with zipfile.ZipFile(io.BytesIO(pptx_bytes())) as archive:
        parts = {name: archive.read(name) for name in archive.namelist() if name != "[Content_Types].xml"}
    parts["ppt/slides/_rels/slide2.xml.rels"] = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rNotes" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide" Target="../notesSlides/notesSlide2.xml"/></Relationships>'
    parts["ppt/notesSlides/notesSlide2.xml"] = '<p:notes xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Teacher explanation</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:notes>'
    result = extract_document(package(parts), "slides.pptx")
    assert result["segments"][0]["text"] == "First\n\n[Speaker notes]\nTeacher explanation"
    assert result["segments"][0]["notes_part"] == "ppt/notesSlides/notesSlide2.xml"
    assert result["segments"][1]["text"] == "Second"


def test_response_character_cap_includes_separators_and_blank_pages():
    result = extract_document(pdf_bytes([None, None, None]), "empty.pdf", max_chars=1)
    assert len(result["text"]) <= 1
    assert result["next_offset"] == 1
    result = extract_document(pdf_bytes(["one", "two"]), "packet.pdf", max_chars=7)
    assert len(result["text"]) == 7
    assert result["next_offset"] == 1 and result["next_char_offset"] == 2


def test_unexpected_parser_failures_return_an_explicit_state(monkeypatch):
    monkeypatch.setattr("canvas_py.documents.PdfReader", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("raw sensitive document data")))
    result = extract_document(pdf_bytes(["content"]), "malformed.pdf")
    assert result["state"] == "malformed"
    assert "sensitive" not in str(result)
    assert result["segments"] == []
    assert result["coverage"]["all_document_text_extracted"] is False


@pytest.mark.parametrize("filename,raw,state", [
    ("old.ppt", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1more", "unsupported_legacy"),
    ("old.doc", b"legacy", "unsupported_legacy"),
    ("locked.docx", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + "EncryptedPackage".encode("utf-16-le"), "encrypted"),
    ("notes.docx", b"PK\x03\x04broken", "malformed"),
    ("photo.png", b"PNG", "ocr_required"),
    ("binary.txt", b"hello\x00world", "unsupported"),
    ("fake.txt", b"PK\x03\x04binaryzip", "unsupported"),
    ("unknown.bin", b"readable ascii", "unsupported"),
    ("invalid.txt", b"invalid\xff", "encoding_required"),
])
def test_explicit_nonreadable_states(filename, raw, state):
    result = extract_document(raw, filename)
    assert result["state"] == state
    assert result["text"] == ""


def test_html_and_csv_do_not_execute_or_rewrite_source():
    html = b'<h1>Title</h1><script>evil()</script><p>Body</p><img src="https://external.invalid/secret">'
    result = extract_document(html, "index.html")
    assert result["text"] == "Title\nBody"
    assert result["sha256"] == hashlib.sha256(html).hexdigest()
    csv = b'Name,Value\r\n"Long, name",42\r\n'
    assert extract_document(csv, "data.csv")["text"] == csv.decode()
    assert extract_document("text \u03b1".encode("utf-16"), "bom.txt")["text"] == "text \u03b1"


def test_changed_hash_prevents_mixing_document_versions():
    result = extract_document(b"new", "file.txt", expected_sha256=hashlib.sha256(b"old").hexdigest())
    assert result["state"] == "source_changed"
    assert result["text"] == ""


def test_office_xml_entity_and_expansion_limits_fail_closed(monkeypatch):
    raw = package({"word/document.xml": '<!DOCTYPE x [<!ENTITY example "unsafe">]><x>&example;</x>'})
    assert extract_document(raw, "attack.docx")["state"] == "malformed"
    monkeypatch.setattr("canvas_py.documents.MAX_PACKAGE_BYTES", 64)
    assert extract_document(docx_bytes(), "big.docx")["state"] == "limit_exceeded"


@pytest.mark.parametrize("options", [{"offset": -1}, {"char_offset": -1}, {"max_units": 0}, {"max_units": 101}, {"max_chars": 100001}, {"offset": 2}, {"char_offset": 9}])
def test_invalid_offsets_and_limits_raise(options):
    with pytest.raises(ValueError):
        extract_document(b"hello", "file.txt", **options)


def fake_canvas(raw=b"Read me", *, size=None, locked=False):
    calls = []
    metadata = CanvasFile(id=12, filename="source.txt", display_name="Friendly", size=len(raw) if size is None else size,
                          updated_at="2026-09-05T15:00:00Z", locked_for_user=locked,
                          url="https://canvas.test/files/12/download?verifier=do-not-expose")
    def download(file, destination, *, max_bytes):
        calls.append((file.id, max_bytes, destination))
        destination.write_bytes(raw)
        return destination
    return SimpleNamespace(files=SimpleNamespace(get=lambda _: metadata, download=download)), calls


def test_read_canvas_file_has_current_provenance_bound_and_temp_cleanup():
    canvas, calls = fake_canvas()
    result = read_canvas_file(canvas, 12)
    assert result["text"] == "Read me"
    assert result["source"]["canvas_updated_at"] == "2026-09-05T15:00:00+00:00"
    assert result["source"]["cache_hit"] is False
    assert calls[0][1] == MAX_DOWNLOAD_BYTES
    assert not calls[0][2].exists()
    assert "verifier" not in str(result)


def test_reader_accepts_50_mib_cap_but_rejects_unbounded_custom_limit():
    assert document_capabilities()["max_download_bytes"] == 50 * 1024 * 1024
    canvas, calls = fake_canvas()
    with pytest.raises(ValueError, match="52428800"):
        read_canvas_file(canvas, 12, max_bytes=50 * 1024 * 1024 + 1)
    assert calls == []


def test_size_mismatch_locked_and_oversized_do_not_pretend_success():
    canvas, _ = fake_canvas(size=999)
    assert read_canvas_file(canvas, 12)["state"] == "size_mismatch"
    canvas, calls = fake_canvas(locked=True)
    assert read_canvas_file(canvas, 12)["state"] == "locked"
    assert calls == []
    canvas, calls = fake_canvas(size=MAX_DOWNLOAD_BYTES + 1)
    assert read_canvas_file(canvas, 12)["state"] == "limit_exceeded"
    assert calls == []


def test_link_only_hidden_file_is_readable_when_canvas_reports_not_locked():
    canvas, calls = fake_canvas()
    metadata = canvas.files.get(12)
    metadata.hidden_for_user = True
    metadata.hidden = True
    metadata.locked_for_user = False
    result = read_canvas_file(canvas, 12)
    assert result["state"] == "ok"
    assert result["text"] == "Read me"
    assert result["source"]["hidden_for_user"] is True
    assert len(calls) == 1


def test_download_failure_propagates_instead_of_becoming_empty_text():
    canvas, _ = fake_canvas()
    def fail(*args, **kwargs):
        raise httpx.ConnectError("download failed")
    canvas.files.download = fail
    with pytest.raises(httpx.ConnectError):
        read_canvas_file(canvas, 12)


def image_bytes(format="PNG"):
    picture = Image.new("RGB", (1200, 250), "white")
    ImageDraw.Draw(picture).text((45, 55), "Canvas document reading 12345", fill="black", font_size=55)
    output = io.BytesIO()
    picture.save(output, format=format)
    return output.getvalue()


def test_ocr_unavailable_is_explicit_for_images_and_pdf(monkeypatch):
    monkeypatch.setattr("canvas_py.documents.shutil.which", lambda _: None)
    assert extract_document(image_bytes(), "photo.png", ocr=True)["state"] == "ocr_unavailable"
    result = extract_document(pdf_bytes([None]), "scan.pdf", ocr=True)
    assert result["state"] == "ocr_unavailable"
    assert result["segments"][0]["state"] == "ocr_unavailable"


def test_ocr_is_selected_page_only_and_capped_at_five(monkeypatch):
    import canvas_py.documents as documents
    pages = []
    def recognized(raw, page_index, **kwargs):
        pages.append(page_index)
        return {"kind": "page", "page": page_index + 1, "text": f"recognized {page_index}", "state": "ok", "method": "ocr"}
    monkeypatch.setattr(documents, "_ocr_unit", recognized)
    result = extract_document(pdf_bytes([None] * 8), "scan.pdf", ocr=True, offset=1, max_units=100)
    assert pages == [1, 2, 3, 4, 5]
    assert result["next_offset"] == 6
    assert result["coverage"]["ocr_used"]
    assert not result["coverage"]["all_document_text_extracted"]


def test_ocr_does_not_replace_existing_pdf_text_layer(monkeypatch):
    monkeypatch.setattr("canvas_py.documents._ocr_unit", lambda *_: pytest.fail("No OCR needed for text layer"))
    assert extract_document(pdf_bytes(["original text"]), "text.pdf", ocr=True)["text"] == "original text"


def test_ocr_reads_image_pages_with_a_small_text_layer_without_losing_original(monkeypatch):
    monkeypatch.setattr("canvas_py.documents._pdf_unit", lambda reader, index: {
        "kind": "page", "page": index + 1, "text": "watermark", "state": "ok", "method": "text_layer", "image_or_form_resources": True})
    monkeypatch.setattr("canvas_py.documents._ocr_unit", lambda *args, **kwargs: {
        "kind": "page", "page": 1, "text": "actual scanned content", "state": "ok", "method": "ocr", "ocr_mean_word_confidence": 93.2})
    result = extract_document(pdf_bytes(["watermark"]), "scan.pdf", ocr=True)
    assert result["state"] == "ok"
    assert result["segments"][0]["method"] == "text_layer_and_ocr"
    assert result["text"] == "watermark\n\n[OCR of rendered page]\nactual scanned content"
    assert result["coverage"]["ocr_used"]
    assert result["read_options"]["ocr"]


def test_next_read_preserves_ocr_mode_and_original_hash():
    canvas, _ = fake_canvas()
    result = read_canvas_file(canvas, 12, max_chars=3, ocr=True)
    assert result["next_read"]["tool"] == "canvas_read_file"
    assert result["next_read"]["arguments"]["ocr"] is True
    assert result["next_read"]["arguments"]["expected_sha256"] == result["sha256"]
    assert result["next_read"]["arguments"]["char_offset"] == 3


def test_ocr_failure_does_not_erase_other_readable_pages(monkeypatch):
    import canvas_py.documents as documents
    def fail(*_, **kwargs):
        raise documents._ReadFailure("ocr_timeout", "Recognition timeout")
    monkeypatch.setattr(documents, "_ocr_unit", fail)
    result = extract_document(pdf_bytes(["readable", None]), "mixed.pdf", ocr=True)
    assert result["state"] == "partial"
    assert result["segments"][0]["text"] == "readable"
    assert result["segments"][1]["state"] == "ocr_timeout"


@pytest.mark.skipif(not shutil.which("tesseract"), reason="Optional native Tesseract runtime is unavailable")
def test_real_tesseract_image_ocr_preserves_hash_and_has_confidence():
    raw = image_bytes()
    result = extract_document(raw, "sample.png", ocr=True)
    assert result["state"] == "ok"
    assert "Canvas document reading 12345" in result["text"]
    assert result["sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["coverage"]["ocr_used"]
    assert result["segments"][0]["ocr_mean_word_confidence"] > 0


@pytest.mark.skipif(not shutil.which("tesseract") or not shutil.which("pdftoppm"), reason="Optional native PDF OCR runtime unavailable")
def test_real_image_pdf_ocr_renders_selected_page_and_recognizes_text():
    picture = Image.open(io.BytesIO(image_bytes()))
    output = io.BytesIO()
    picture.save(output, format="PDF", resolution=150)
    raw = output.getvalue()
    assert extract_document(raw, "scan.pdf")["state"] == "ocr_required"
    result = extract_document(raw, "scan.pdf", ocr=True)
    assert result["state"] == "ok"
    assert "Canvas document reading 12345" in result["text"]
    assert result["segments"][0]["page"] == 1


def test_ocr_pixel_limits_are_checked_before_tesseract(monkeypatch):
    monkeypatch.setattr("canvas_py.documents.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("canvas_py.documents.MAX_OCR_PIXELS", 100)
    monkeypatch.setattr("canvas_py.documents.subprocess.run", lambda *args, **kwargs: pytest.fail("Must reject before process"))
    assert extract_document(image_bytes(), "large.png", ocr=True)["state"] == "limit_exceeded"
