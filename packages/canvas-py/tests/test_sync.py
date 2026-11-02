"""Behavioral coverage for private, resumable course-folder sync."""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from canvas_py._errors import CanvasAuthError, CanvasPermissionError
from canvas_py.models.file import CanvasFile
from canvas_py.sync import SyncLimits, run_sync

NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)


class FakeCanvas:
    def __init__(self):
        self.config = SimpleNamespace(base_url="https://school.example")
        self.calls = []
        self.failures = {}
        self.downloads = []
        self.bodies = {10: b"First handout", 11: b"Page attachment", 12: b"Announcement", 13: b"Syllabus"}
        self.metadata = {key: CanvasFile(id=key, filename=f"{key}.txt", size=len(body),
                                         url=f"https://school.example/files/{key}/download?verifier=SECRET",
                                         updated_at=NOW, **{"content-type": "text/plain"})
                         for key, body in self.bodies.items()}
        self.data = {
            "courses": [{"id": 1, "name": "Course One"}],
            "courses/1/modules": [{"id": 100}],
            "courses/1/modules/100/items": [{"id": 1001, "type": "File", "content_id": 10},
                                             {"id": 1002, "type": "Page", "page_url": "overview"}],
            "courses/1/assignments": [{"id": 200, "name": "Essay", "description": '<a href="/courses/1/files/10/download?verifier=SECRET">Handout</a>',
                                        "due_at": (NOW + timedelta(days=2)).isoformat(),
                                        "html_url": "https://school.example/courses/1/assignments/200",
                                        "submission": {"workflow_state": "unsubmitted"}}],
            "courses/1/pages": [{"url": "overview"}],
            "courses/1/discussion_topics": [{"id": 300, "title": "Notice", "message": "Read this",
                                              "attachments": [{"id": 12}]}],
            "courses/1/files": [],
        }
        self.course_values = {1: {"id": 1, "name": "Course One", "syllabus_body": '<a href="/files/13">Syllabus</a>'}}
        self.page_values = {"overview": {"url": "overview", "title": "Overview", "body": '<a href="/api/v1/files/11">Attachment</a>'}}
        self.courses = SimpleNamespace(get=self.get_course)
        self.pages = SimpleNamespace(get=self.get_page)
        self.assignments = SimpleNamespace(get=self.get_assignment)
        self.files = SimpleNamespace(get=self.get_file, download=self.download)

    def check(self, key):
        self.calls.append(key)
        if key in self.failures:
            raise self.failures[key]

    def paginate(self, request):
        self.check(request.path)
        yield from copy.deepcopy(self.data.get(request.path, []))

    def get_course(self, cid, **kwargs):
        self.check(f"course:{cid}")
        return copy.deepcopy(self.course_values[cid])

    def get_page(self, cid, slug):
        self.check(f"page:{slug}")
        return copy.deepcopy(self.page_values[slug])

    def get_assignment(self, cid, aid, **kwargs):
        self.check(f"assignment:{aid}")
        return {"id": aid, "name": "Linked assignment", "description": ""}

    def get_file(self, fid):
        self.check(f"file:{fid}")
        return self.metadata[fid]

    def download(self, file, destination, *, max_bytes):
        self.check(f"download:{file.id}")
        raw = self.bodies[file.id]
        if len(raw) > max_bytes:
            raise ValueError("too large")
        Path(destination).write_bytes(raw)
        self.downloads.append(file.id)
        return Path(destination)


def manifest_path(root, cid=1):
    return root / f"course-{cid}" / "canvas-managed" / "manifest.json"


def load(root, cid=1):
    return json.loads(manifest_path(root, cid).read_text())


def run(canvas, root, **kwargs):
    return run_sync(canvas, root, course_ids=[1], now=NOW, **kwargs)


def test_preview_discovers_all_sources_without_writing_or_downloading(tmp_path):
    root = tmp_path / "School"
    canvas = FakeCanvas()
    report = run(canvas, root)
    assert not root.exists()
    assert not canvas.downloads
    changes = report["courses"][0]["changes"]
    assert set(changes["downloads_planned"]) == {"file:10", "file:11", "file:12", "file:13"}
    assert report["courses"][0]["attention"][0]["reason"] == "due_within_seven_days"
    assert "SECRET" not in json.dumps(report)


def test_apply_preserves_exact_originals_and_is_idempotent(tmp_path):
    canvas = FakeCanvas()
    run(canvas, tmp_path, apply=True)
    first = manifest_path(tmp_path).read_bytes()
    value = load(tmp_path)
    for fid, raw in canvas.bodies.items():
        entry = value["items"][f"file:{fid}"]
        original = manifest_path(tmp_path).parent / entry["original_path"]
        assert original.read_bytes() == raw
        assert entry["sha256"] == hashlib.sha256(raw).hexdigest()
    assert "SECRET" not in first.decode()
    assert len(canvas.downloads) == 4
    report = run_sync(canvas, tmp_path, course_ids=[1], now=NOW + timedelta(hours=1), apply=True)
    assert manifest_path(tmp_path).read_bytes() == first
    assert len(canvas.downloads) == 4
    assert not report["courses"][0]["changes"]["new"]
    assert not report["courses"][0]["changes"]["changed"]


def test_changed_original_retains_versions_and_old_manifest(tmp_path):
    canvas = FakeCanvas()
    run(canvas, tmp_path, apply=True)
    first = load(tmp_path)["items"]["file:10"]
    canvas.bodies[10] = b"Revised handout"
    canvas.metadata[10].updated_at = NOW + timedelta(days=1)
    canvas.metadata[10].size = len(canvas.bodies[10])
    report = run(canvas, tmp_path, apply=True)
    second = load(tmp_path)["items"]["file:10"]
    assert second["original_path"] != first["original_path"]
    managed = manifest_path(tmp_path).parent
    assert (managed / first["original_path"]).read_bytes() == b"First handout"
    assert (managed / second["original_path"]).read_bytes() == b"Revised handout"
    assert list((managed / "history").glob("manifest-*.json"))
    assert "file:10" in report["courses"][0]["changes"]["changed"]


def test_partial_visibility_keeps_previous_items_and_suppresses_removals(tmp_path):
    canvas = FakeCanvas()
    run(canvas, tmp_path, apply=True)
    old = load(tmp_path)["items"]["file:12"]
    canvas.failures["courses/1/discussion_topics"] = CanvasPermissionError("private", status=403)
    report = run(canvas, tmp_path, apply=True)
    assert load(tmp_path)["items"]["file:12"] == old
    changes = report["courses"][0]["changes"]
    assert "file:12" in changes["not_seen"]
    assert changes["removed"] == []
    assert report["status"] == "partial"


def test_file_catalog_forbidden_still_discovers_linked_files(tmp_path):
    canvas = FakeCanvas()
    canvas.failures["courses/1/files"] = CanvasPermissionError("not allowed", status=403)
    report = run(canvas, tmp_path, apply=True)
    assert report["status"] == "partial"
    assert set(canvas.downloads) == {10, 11, 12, 13}
    assert report["courses"][0]["coverage"]["files_catalog"] == "incomplete"


def test_complete_absence_marks_removed_without_deleting(tmp_path):
    canvas = FakeCanvas()
    run(canvas, tmp_path, apply=True)
    old = load(tmp_path)["items"]["file:12"]
    canvas.data["courses/1/discussion_topics"] = []
    report = run(canvas, tmp_path, apply=True)
    assert "file:12" in report["courses"][0]["changes"]["removed"]
    assert (manifest_path(tmp_path).parent / old["original_path"]).exists()
    assert load(tmp_path)["items"]["file:12"]["remote_state"] == "removed"
    report = run(canvas, tmp_path, apply=True)
    assert not report["courses"][0]["changes"]["removed"]


@pytest.mark.parametrize("failing_key", ["course:1", "file:10", "download:10"])
def test_auth_failure_preserves_all_manifest_bytes(tmp_path, failing_key):
    canvas = FakeCanvas()
    run(canvas, tmp_path, apply=True)
    before = manifest_path(tmp_path).read_bytes()
    canvas.metadata[10].updated_at = NOW + timedelta(days=1)
    canvas.failures[failing_key] = CanvasAuthError("SECRET token error", status=401)
    with pytest.raises(CanvasAuthError, match="no sync manifests were committed"):
        run(canvas, tmp_path, apply=True)
    assert manifest_path(tmp_path).read_bytes() == before


def test_later_course_auth_failure_does_not_commit_first_course(tmp_path):
    canvas = FakeCanvas()
    run(canvas, tmp_path, apply=True)
    before = manifest_path(tmp_path).read_bytes()
    canvas.course_values[1]["name"] = "Renamed course"
    canvas.failures["course:2"] = CanvasAuthError("invalid", status=401)
    with pytest.raises(CanvasAuthError):
        run_sync(canvas, tmp_path, course_ids=[1, 2], apply=True, now=NOW)
    assert manifest_path(tmp_path).read_bytes() == before


def test_failed_changed_download_preserves_good_original_and_retries(tmp_path):
    canvas = FakeCanvas()
    run(canvas, tmp_path, apply=True)
    old = load(tmp_path)["items"]["file:10"]
    canvas.metadata[10].updated_at = NOW + timedelta(days=1)
    canvas.failures["download:10"] = CanvasPermissionError("signed URL failed", status=403)
    report = run(canvas, tmp_path, apply=True)
    failure = load(tmp_path)["items"]["file:10"]
    assert failure["original_path"] == old["original_path"]
    assert failure["fingerprint"] == old["fingerprint"]
    assert "file:10" in report["courses"][0]["changes"]["inaccessible"]
    del canvas.failures["download:10"]
    run(canvas, tmp_path, apply=True)
    assert canvas.downloads.count(10) == 2
    assert load(tmp_path)["items"]["file:10"]["sync_state"] == "current"


def test_modified_local_original_is_preserved_and_recovered(tmp_path):
    canvas = FakeCanvas()
    run(canvas, tmp_path, apply=True)
    old = load(tmp_path)["items"]["file:10"]
    path = manifest_path(tmp_path).parent / old["original_path"]
    path.write_bytes(b"student edit")
    run(canvas, tmp_path, apply=True)
    recovered = load(tmp_path)["items"]["file:10"]
    assert path.read_bytes() == b"student edit"
    assert recovered["original_path"] != old["original_path"]
    assert (manifest_path(tmp_path).parent / recovered["original_path"]).read_bytes() == b"First handout"


def test_download_limits_and_filename_sanitization(tmp_path):
    canvas = FakeCanvas()
    canvas.metadata[10].filename = "../../student/draft.txt"
    report = run(canvas, tmp_path, apply=True, limits=SyncLimits(max_file_bytes=2))
    assert not canvas.downloads
    assert report["status"] == "partial"
    assert load(tmp_path)["items"]["file:10"]["filename"] == "draft.txt"
    assert not (tmp_path / "student").exists()


def test_operation_and_item_caps_are_partial_not_empty_success(tmp_path):
    canvas = FakeCanvas()
    report = run(canvas, tmp_path, limits=SyncLimits(max_operations=2, max_items=1))
    assert report["status"] == "partial"
    assert report["usage"]["operations"] <= 2
    assert report["usage"]["discovered_items"] <= 1


def test_symlinks_and_unmarked_managed_folders_are_rejected(tmp_path):
    canvas = FakeCanvas()
    managed = manifest_path(tmp_path).parent
    managed.mkdir(parents=True)
    draft = managed / "draft.txt"
    draft.write_text("Student draft")
    with pytest.raises(ValueError, match="not marked"):
        run(canvas, tmp_path, apply=True)
    assert draft.read_text() == "Student draft"
    other = tmp_path / "other"
    other.mkdir()
    target = tmp_path / "link"
    target.symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        run(canvas, target, apply=True)


def test_only_same_canvas_links_are_discovered(tmp_path):
    canvas = FakeCanvas()
    canvas.page_values["overview"]["body"] += '<a href="https://evil.example/files/999">Not Canvas</a>'
    run(canvas, tmp_path)
    assert "file:999" not in canvas.calls


def test_active_courses_and_selection_validation(tmp_path):
    canvas = FakeCanvas()
    report = run_sync(canvas, tmp_path, all_active=True, now=NOW)
    assert report["courses"][0]["course_id"] == 1
    with pytest.raises(ValueError, match="choose explicit"):
        run_sync(canvas, tmp_path)
    with pytest.raises(ValueError, match="positive"):
        run_sync(canvas, tmp_path, course_ids=[True])
    with pytest.raises(ValueError, match="choose explicit"):
        run_sync(canvas, tmp_path, course_ids=[1], all_active=True)


def test_interrupted_manifest_commit_recovers_without_overwriting_prior(tmp_path, monkeypatch):
    import canvas_py.sync as sync
    canvas = FakeCanvas()
    run(canvas, tmp_path, apply=True)
    before = manifest_path(tmp_path).read_bytes()
    canvas.metadata[10].updated_at = NOW + timedelta(days=1)
    canvas.bodies[10] = b"Revised original"
    canvas.metadata[10].size = len(canvas.bodies[10])
    original_atomic = sync._atomic

    def interrupted(path, raw):
        if path.name == "manifest.json":
            raise OSError("interrupted before manifest commit")
        return original_atomic(path, raw)

    monkeypatch.setattr(sync, "_atomic", interrupted)
    with pytest.raises(OSError):
        run(canvas, tmp_path, apply=True)
    assert manifest_path(tmp_path).read_bytes() == before
    monkeypatch.setattr(sync, "_atomic", original_atomic)
    run(canvas, tmp_path, apply=True)
    entry = load(tmp_path)["items"]["file:10"]
    assert (manifest_path(tmp_path).parent / entry["original_path"]).read_bytes() == b"Revised original"


def test_metadata_less_files_are_rechecked_without_manifest_churn(tmp_path):
    canvas = FakeCanvas()
    canvas.metadata[10].updated_at = None
    run(canvas, tmp_path, apply=True)
    before = manifest_path(tmp_path).read_bytes()
    run_sync(canvas, tmp_path, course_ids=[1], apply=True, now=NOW + timedelta(hours=1))
    assert canvas.downloads.count(10) == 2
    assert manifest_path(tmp_path).read_bytes() == before
    canvas.bodies[10] = b"OTHER handout"  # Same size and no version timestamp.
    canvas.metadata[10].size = len(canvas.bodies[10])
    report = run(canvas, tmp_path, apply=True)
    entry = load(tmp_path)["items"]["file:10"]
    assert entry["sha256"] == hashlib.sha256(canvas.bodies[10]).hexdigest()
    assert "file:10" in report["courses"][0]["changes"]["changed"]


def test_foreign_root_report_is_preserved(tmp_path):
    path = tmp_path / "canvas-sync-last-run.json"
    path.write_text('{"student_draft": true}')
    with pytest.raises(ValueError, match="run report"):
        run(FakeCanvas(), tmp_path, apply=True)
    assert path.read_text() == '{"student_draft": true}'


def test_locked_content_links_are_not_followed(tmp_path):
    canvas = FakeCanvas()
    canvas.page_values["overview"]["locked_for_user"] = True
    report = run(canvas, tmp_path)
    assert "file:11" not in canvas.calls
    assert report["status"] == "partial"


def test_failed_downloads_consume_total_transfer_budget(tmp_path):
    canvas = FakeCanvas()
    canvas.failures["download:10"] = CanvasPermissionError("expired", status=403)
    report = run(canvas, tmp_path, apply=True, limits=SyncLimits(max_file_bytes=20, max_total_bytes=20))
    assert not canvas.downloads
    assert "download:11" not in canvas.calls
    assert report["usage"]["transfer_budget_used"] == 20


def test_link_only_hidden_file_can_download_when_not_locked(tmp_path):
    canvas = FakeCanvas()
    canvas.metadata[10].hidden_for_user = True
    canvas.metadata[10].locked_for_user = False
    report = run(canvas, tmp_path, apply=True)
    entry = load(tmp_path)["items"]["file:10"]
    assert entry["hidden_for_user"] is True
    assert entry["access"] == "available"
    assert entry["sync_state"] == "current"
    assert 10 in canvas.downloads
    assert "file:10" not in report["courses"][0]["changes"]["inaccessible"]


def test_course_readme_has_escaped_name_original_links_and_partial_warning(tmp_path):
    canvas = FakeCanvas()
    canvas.course_values[1]["name"] = "Course [One] <script>"
    canvas.failures["courses/1/files"] = CanvasPermissionError("hidden catalogue", status=403)
    run(canvas, tmp_path, apply=True)
    readme = (manifest_path(tmp_path).parent / "README.md").read_text()
    original = load(tmp_path)["items"]["file:10"]["original_path"]
    assert "Course \\[One\\] \\<script\\>" in readme
    assert "Partial coverage" in readme
    assert f"]({original})" in readme
    assert "[manifest.json](manifest.json)" in readme
    assert "files\\_catalog: forbidden" in readme
    assert "Last checked:" in readme


def test_hand_edited_readme_and_unrelated_student_draft_are_preserved(tmp_path):
    canvas = FakeCanvas()
    run(canvas, tmp_path, apply=True)
    managed = manifest_path(tmp_path).parent
    draft = managed.parent / "student-draft.txt"
    draft.write_text("My own draft")
    readme = managed / "README.md"
    edited = readme.read_text() + "\nMy personal notes\n"
    readme.write_text(edited)
    report = run(canvas, tmp_path, apply=True)
    assert readme.read_text() == edited
    assert draft.read_text() == "My own draft"
    assert any(issue["reason"] == "local_student_edits_preserved" for issue in report["courses"][0]["issues"])
