from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from canvas_py._config import CanvasConfig
from canvas_py.models.file import CanvasFile
from mcp.server.fastmcp import FastMCP

from canvas_mcp_v2.actions import (
    _assignment_state,
    register_action_tools,
)


class FakeCanvas:
    def __init__(self):
        self.config = CanvasConfig("https://school.instructure.com", "dummy-token")
        self.writes = []
        self.uploaded = {}
        self.upload_names = {}
        self.assignment_includes = []
        self.assignment = {
            "id": 20,
            "name": "Assignment",
            "submission_types": ["online_upload"],
            "allowed_extensions": ["txt"],
            "allowed_attempts": 3,
            "locked_for_user": False,
        }
        self.submission = {
            "id": 30,
            "assignment_id": 20,
            "user_id": 7,
            "attempt": 0,
            "workflow_state": "unsubmitted",
            "attachments": [],
        }
        self.assignments = SimpleNamespace(get=self.get_assignment)
        self.submissions = SimpleNamespace(get=lambda *args: copy.deepcopy(self.submission))
        self.navigation = SimpleNamespace(clear_cache=lambda: None)
        self.files = SimpleNamespace(upload=self.upload, download=self.download, get=self.get_file)

    def get_assignment(self, *args, include=None):
        self.assignment_includes.append(include)
        return dict(self.assignment)

    def get_file(self, file_id):
        return CanvasFile(
            id=file_id,
            display_name=self.upload_names[file_id],
            filename=self.upload_names[file_id],
            size=len(self.uploaded[file_id]),
        )

    def upload(self, path, **kwargs):
        raw = Path(path).read_bytes()
        assert len(raw) == kwargs["expected_size"]
        assert hashlib.sha256(raw).hexdigest() == kwargs["expected_sha256"]
        assert kwargs["on_duplicate"] == "rename" and kwargs["verify"]
        self.writes.append("upload")
        file_id = 41 + len(self.uploaded)
        self.uploaded[file_id] = raw
        self.upload_names[file_id] = kwargs["name"]
        return self.get_file(file_id)

    def download(self, file, path, **kwargs):
        Path(path).write_bytes(self.uploaded[file.id])

    def send(self, request):
        self.writes.append("submit")
        self.submission = {
            "id": 30,
            "assignment_id": request.assignment_id,
            "user_id": 7,
            "submission_type": "online_upload",
            "attempt": self.submission["attempt"] + 1,
            "workflow_state": "submitted",
            "submitted_at": "2026-09-05T12:00:00Z",
            "attachments": [{"id": x} for x in request.file_ids],
        }
        return copy.deepcopy(self.submission)


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.delenv("CANVAS_ENV_PATH", raising=False)
    monkeypatch.setenv("CANVAS_MCP_TRANSPORT", "stdio")
    monkeypatch.delenv("CANVAS_MCP_ENABLE_ACTIONS", raising=False)
    canvas = FakeCanvas()
    mcp = FastMCP("action test")
    store = register_action_tools(mcp, lambda: canvas)
    yield canvas, mcp, store
    store.close()


def fn(mcp, name):
    return mcp._tool_manager.get_tool(name).fn


def prepare(store, canvas, **kwargs):
    return store.prepare_upload(
        canvas, content_base64=base64.b64encode(b"hello").decode(), filename="note.txt", **kwargs
    )





def test_prepare_stages_exact_bytes_without_canvas_write(setup, tmp_path):
    canvas, _, store = setup
    path = tmp_path / "draft.txt"
    path.write_bytes(b"original")
    review = store.prepare_upload(canvas, source_path=str(path))
    path.write_bytes(b"changed")
    assert not canvas.writes
    assert review["review"]["sha256"] == hashlib.sha256(b"original").hexdigest()
    assert Path(store.get(canvas, review["action_id"])["staged_path"]).read_bytes() == b"original"


@pytest.mark.asyncio
async def test_declined_preparation_is_cancelled_without_canvas_write(setup):
    canvas, mcp, store = setup
    review = prepare(store, canvas)
    result = fn(mcp, "canvas_cancel_action")(review["action_id"])
    assert result["state"] == "cancelled"
    assert (await fn(mcp, "canvas_execute_action")(review["action_id"]))["state"] == "cancelled"
    assert canvas.writes == []



@pytest.mark.asyncio
async def test_upload_roundtrip_verified_and_replay_does_not_upload_again(setup):
    canvas, mcp, store = setup
    review = prepare(store, canvas)
    execute = fn(mcp, "canvas_execute_action")
    result = await execute(review["action_id"])
    assert result["state"] == "succeeded"
    assert result["result"]["submitted"] is False and result["result"]["verified"]
    assert (await execute(review["action_id"]))["state"] == "succeeded"
    assert canvas.writes == ["upload"]


@pytest.mark.asyncio
async def test_submission_is_separate_review_and_verifies_attachments(setup):
    canvas, mcp, store = setup
    upload = prepare(store, canvas, destination="assignment", course_id=10, assignment_id=20)
    execute = fn(mcp, "canvas_execute_action")
    await execute(upload["action_id"])
    assert canvas.writes == ["upload"]
    submission = store.prepare_submission(canvas, 10, 20, [upload["action_id"]])
    assert canvas.writes == ["upload"]
    result = await execute(submission["action_id"])
    assert result["result"]["submitted"] and result["result"]["file_ids"] == [41]
    assert canvas.writes == ["upload", "submit"]
    with pytest.raises(ValueError, match="already submitted"):
        store.prepare_submission(canvas, 10, 20, [upload["action_id"]])


@pytest.mark.asyncio
async def test_changed_assignment_after_review_cannot_submit(setup):
    canvas, mcp, store = setup
    upload = prepare(store, canvas, destination="assignment", course_id=10, assignment_id=20)
    execute = fn(mcp, "canvas_execute_action")
    await execute(upload["action_id"])
    review = store.prepare_submission(canvas, 10, 20, [upload["action_id"]])
    canvas.submission["attempt"] = 1
    with pytest.raises(ValueError, match="changed"):
        await execute(review["action_id"])
    assert canvas.writes == ["upload"]



@pytest.mark.asyncio
async def test_uncertain_write_is_not_replayed(setup):
    canvas, mcp, store = setup

    def upload(*args, **kwargs):
        canvas.writes.append("upload")
        raise TimeoutError("outcome unknown")

    canvas.files.upload = upload
    review = prepare(store, canvas)
    execute = fn(mcp, "canvas_execute_action")
    assert (await execute(review["action_id"]))["state"] == "uncertain"
    assert (await execute(review["action_id"]))["state"] == "uncertain"
    assert canvas.writes == ["upload"]


@pytest.mark.asyncio
async def test_roundtrip_hash_mismatch_not_reported_verified(setup):
    canvas, mcp, store = setup
    canvas.files.download = lambda file, path, **kw: Path(path).write_bytes(b"wrong")
    review = prepare(store, canvas)
    result = await fn(mcp, "canvas_execute_action")(review["action_id"])
    assert result["state"] == "uncertain" and result["result"]["file_id"] == 41


def test_other_account_expired_and_remote_path_are_refused(setup, monkeypatch):
    canvas, _, store = setup
    review = prepare(store, canvas)
    other = FakeCanvas()
    other.config = CanvasConfig("https://other.instructure.com", "other-token")
    with pytest.raises(ValueError, match="not found"):
        store.get(other, review["action_id"])
    record = store.get(canvas, review["action_id"])
    record["expires"] = -1
    assert store.get(canvas, review["action_id"])["state"] == "expired"
    monkeypatch.setenv("CANVAS_MCP_TRANSPORT", "http")
    with pytest.raises(ValueError, match="server filesystem"):
        store.prepare_upload(canvas, source_path="/private/file")


@pytest.mark.asyncio
async def test_parallel_execute_requests_write_only_once_and_cannot_be_cancelled_midflight(setup):
    canvas, mcp, store = setup
    review = prepare(store, canvas)
    started, release = threading.Event(), threading.Event()
    original = canvas.files.upload
    def blocked(*args, **kwargs):
        started.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)
    canvas.files.upload = blocked
    execute = fn(mcp, "canvas_execute_action")
    first = asyncio.create_task(execute(review["action_id"]))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        assert (await execute(review["action_id"]))["state"] == "executing"
        with pytest.raises(ValueError, match="already executing"):
            fn(mcp, "canvas_cancel_action")(review["action_id"])
    finally:
        release.set()
    assert (await first)["state"] == "succeeded"
    assert canvas.writes == ["upload"]



@pytest.mark.asyncio
@pytest.mark.parametrize("advertise_forms", [False, True])
async def test_real_stdio_upload_and_submission_never_request_a_form(setup, tmp_path, advertise_forms):
    import json
    import os
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    script = tmp_path / "mock_server.py"
    script.write_text("from test_actions import FakeCanvas\nfrom canvas_mcp_v2.actions import register_action_tools\n"
                      "from mcp.server.fastmcp import FastMCP\nmcp=FastMCP('mock')\nc=FakeCanvas()\n"
                      "register_action_tools(mcp,lambda:c)\nmcp.run()\n")
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).parent))
    prompts = []
    async def unexpected(ctx, params):
        prompts.append(params.message)
        raise RuntimeError("The connector must not request a form")
    def payload(result):
        assert not result.isError
        return result.structuredContent or json.loads(result.content[0].text)
    async with stdio_client(StdioServerParameters(command=sys.executable,args=[str(script)],env=env)) as streams:  # noqa: SIM117
        async with ClientSession(*streams, elicitation_callback=unexpected if advertise_forms else None) as session:
            await session.initialize()
            upload = payload(await session.call_tool("canvas_prepare_upload", {"filename":"note.txt", "content_base64":"aGVsbG8=", "destination":"assignment", "course_id":10, "assignment_id":20}))
            assert not upload["requires_client_user_confirmation"]
            result = payload(await session.call_tool("canvas_execute_action", {"action_id":upload["action_id"]}))
            assert result["state"] == "succeeded" and result["result"]["submitted"] is False
            review = payload(await session.call_tool("canvas_prepare_submission", {"course_id":10, "assignment_id":20, "upload_action_ids":[upload["action_id"]]}))
            result = payload(await session.call_tool("canvas_execute_action", {"action_id":review["action_id"]}))
            assert result["state"] == "succeeded" and result["result"]["submitted"] is True
    assert prompts == []



def completed_assignment_upload(store, canvas):
    review = prepare(store, canvas, destination="assignment", course_id=10, assignment_id=20)
    result = store.perform(canvas, store.get(canvas, review["action_id"]))
    assert result["state"] == "succeeded"
    return review


def test_assignment_requests_canvas_submission_permission(setup):
    canvas, _, store = setup
    prepare(store, canvas, destination="assignment", course_id=10, assignment_id=20)
    assert "can_submit" in canvas.assignment_includes[-1]


@pytest.mark.parametrize(
    "can_submit,allowed,attempt,extra,accepted",
    [
        (False, 3, 0, 0, False),
        (True, 1, 1, 0, True),
        (None, 1, 1, 1, True),
        (None, 1, 2, 1, False),
        (None, 0, 3, 0, True),
        (None, -1, 3, 0, True),
    ],
)
def test_canvas_permission_and_extra_attempts(setup, can_submit, allowed, attempt, extra, accepted):
    canvas, _, _ = setup
    canvas.assignment.update(allowed_attempts=allowed, can_submit=can_submit)
    canvas.submission.update(attempt=attempt, extra_attempts=extra)
    if accepted:
        state = _assignment_state(canvas, 10, 20, ["note.txt"])
        assert state["extra_attempts"] == extra
    else:
        with pytest.raises(ValueError):
            _assignment_state(canvas, 10, 20, ["note.txt"])
    assert canvas.writes == []


@pytest.mark.parametrize(
    "changes",
    [
        {"unlock_at": "2999-01-01T00:00:00Z"},
        {"lock_at": "2000-01-01T00:00:00Z"},
        {"group_category_id": 30},
        {"submission_types": ["online_text_entry"]},
        {"allowed_extensions": ["pdf"]},
        {"published": False},
    ],
)
def test_assignment_constraints_refuse_without_canvas_write(setup, changes):
    canvas, _, store = setup
    canvas.assignment.update(changes)
    with pytest.raises(ValueError):
        prepare(store, canvas, destination="assignment", course_id=10, assignment_id=20)
    assert canvas.writes == []


@pytest.mark.asyncio
async def test_distinct_actions_for_same_assignment_cannot_submit_concurrently(setup):
    canvas, _, store = setup
    upload = completed_assignment_upload(store, canvas)
    first = store.prepare_submission(canvas, 10, 20, [upload["action_id"]])
    second = store.prepare_submission(canvas, 10, 20, [upload["action_id"]])
    started, release = threading.Event(), threading.Event()
    original_send = canvas.send

    def blocked_send(request):
        started.set()
        assert release.wait(timeout=5)
        return original_send(request)

    canvas.send = blocked_send
    first_task = asyncio.create_task(
        asyncio.to_thread(store.perform, canvas, store.get(canvas, first["action_id"]))
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        with pytest.raises(ValueError, match="Another action"):
            await asyncio.to_thread(store.perform, canvas, store.get(canvas, second["action_id"]))
        # The reservation must not hold the global store lock or stop other targets.
        assert store.get(canvas, first["action_id"])["state"] == "executing"
        other = prepare(store, canvas)
        result = await asyncio.to_thread(
            store.perform, canvas, store.get(canvas, other["action_id"])
        )
        assert result["state"] == "succeeded"
    finally:
        release.set()
    assert (await first_task)["state"] == "succeeded"
    assert canvas.writes.count("submit") == 1
    assert not store.active_targets
    with pytest.raises(ValueError, match="changed"):
        store.perform(canvas, store.get(canvas, second["action_id"]))
    assert canvas.writes.count("submit") == 1


@pytest.mark.asyncio
async def test_changed_remote_bytes_after_review_cannot_be_submitted(setup):
    canvas, mcp, store = setup
    upload = completed_assignment_upload(store, canvas)
    review = store.prepare_submission(canvas, 10, 20, [upload["action_id"]])
    canvas.uploaded[41] = b"other"
    with pytest.raises(ValueError, match="bytes changed"):
        await fn(mcp, "canvas_execute_action")(review["action_id"])
    assert canvas.writes == ["upload"] and not store.active_targets



@pytest.mark.parametrize("changed", ["size", "name", "id", "unavailable"])
def test_remote_file_metadata_rechecked_before_submission(setup, changed):
    canvas, _, store = setup
    upload = completed_assignment_upload(store, canvas)
    review = store.prepare_submission(canvas, 10, 20, [upload["action_id"]])
    if changed == "size":
        canvas.uploaded[41] = b"longer"
    elif changed == "name":
        canvas.upload_names[41] = "changed.pdf"
    elif changed == "id":
        canvas.files.get = lambda file_id: CanvasFile(id=99, size=5, display_name="note.txt")
    else:

        def unavailable(file_id):
            raise RuntimeError("SECRET signed URL")

        canvas.files.get = unavailable
    with pytest.raises(ValueError, match="Uploaded file") as error:
        store.perform(canvas, store.get(canvas, review["action_id"]))
    assert "SECRET" not in str(error.value)
    assert canvas.writes == ["upload"]
    assert not store.active_targets


@pytest.mark.parametrize(
    "changes",
    [
        {"attachments": [{"id": 41}, {"id": 999}]},
        {"attachments": [{"id": 41}, {"id": 41}]},
        {"attachments": []},
        {"attempt": 0},
        {"attempt": 2},
        {"submitted_at": None},
        {"submitted_at": "2026-09-05T12:00:01Z"},
        {"assignment_id": 99},
        {"user_id": 99},
        {"submission_type": "online_text_entry"},
    ],
)
@pytest.mark.asyncio
async def test_submission_readback_must_be_exact_and_new(setup, changes):
    canvas, mcp, store = setup
    upload = completed_assignment_upload(store, canvas)
    review = store.prepare_submission(canvas, 10, 20, [upload["action_id"]])
    original_send = canvas.send

    def changed_readback(request):
        posted = original_send(request)
        canvas.submission.update(copy.deepcopy(changes))
        return posted

    canvas.send = changed_readback
    execute = fn(mcp, "canvas_execute_action")
    result = await execute(review["action_id"])
    assert result["state"] == "uncertain" and result["result"]["verified"] is False
    assert canvas.writes == ["upload", "submit"]
    assert (await execute(review["action_id"]))["state"] == "uncertain"
    assert canvas.writes == ["upload", "submit"]


def test_expiry_during_final_preflight_releases_stage_and_prevents_write(setup):
    canvas, _, store = setup
    review = prepare(store, canvas, destination="assignment", course_id=10, assignment_id=20)
    record = store.get(canvas, review["action_id"])
    stage = Path(record["staged_path"])
    original_get = canvas.assignments.get

    def expire_during_metadata(*args, **kwargs):
        record["expires"] = -1
        return original_get(*args, **kwargs)

    canvas.assignments.get = expire_during_metadata
    with pytest.raises(ValueError, match="expired"):
        store.perform(canvas, record)
    assert record["state"] == "expired"
    assert not stage.exists() and not store.active_targets
    assert canvas.writes == []


@pytest.mark.asyncio
async def test_disabled_deployment_prevents_execution(setup, monkeypatch):
    canvas, mcp, store = setup
    review = prepare(store, canvas)
    monkeypatch.setenv("CANVAS_MCP_ENABLE_ACTIONS", "false")
    with pytest.raises(ValueError, match="disabled"):
        await fn(mcp, "canvas_execute_action")(review["action_id"])
    assert canvas.writes == []



def test_disable_during_final_preflight_prevents_write(setup, monkeypatch):
    canvas, _, store = setup
    review = prepare(store, canvas, destination="assignment", course_id=10, assignment_id=20)
    record = store.get(canvas, review["action_id"])
    original_get = canvas.assignments.get

    def disable_during_metadata(*args, **kwargs):
        monkeypatch.setenv("CANVAS_MCP_ENABLE_ACTIONS", "false")
        return original_get(*args, **kwargs)

    canvas.assignments.get = disable_during_metadata
    with pytest.raises(ValueError, match="disabled"):
        store.perform(canvas, record)
    assert not store.active_targets and canvas.writes == []


@pytest.mark.asyncio
async def test_account_rotation_after_preparation_prevents_write(setup):
    canvas, mcp, store = setup
    review = prepare(store, canvas)
    canvas.config = CanvasConfig("https://other.instructure.com", "new-token")
    with pytest.raises(ValueError, match="not found"):
        await fn(mcp, "canvas_execute_action")(review["action_id"])
    assert canvas.writes == []



@pytest.mark.parametrize("failure", ["open", "write", "record"])
def test_staging_failure_does_not_leave_untracked_tempfile(setup, monkeypatch, failure):
    from canvas_mcp_v2 import actions

    canvas, _, store = setup
    if failure == "record":

        def fail_add(*args, **kwargs):
            raise OSError("record write failure")

        monkeypatch.setattr(store, "_add", fail_add)
    elif failure == "open":

        def fail_open(*args, **kwargs):
            raise OSError("open failure")

        monkeypatch.setattr(actions.os, "fdopen", fail_open)
    else:
        original = actions.os.fdopen

        class FailingWrite:
            def __init__(self, *args, **kwargs):
                self.stream = original(*args, **kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()

            def write(self, raw):
                raise OSError("disk full")

        monkeypatch.setattr(actions.os, "fdopen", FailingWrite)
    with pytest.raises(OSError):
        prepare(store, canvas)
    assert list(Path(store.directory.name).iterdir()) == []
    assert store.records == {} and canvas.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("actual_name,allowed", [("note (1).txt", True), ("note.pdf", False)])
async def test_submission_review_uses_verified_canvas_filename_after_rename(
    setup, actual_name, allowed
):
    canvas, mcp, store = setup
    original_upload = canvas.files.upload

    def renamed_upload(*args, **kwargs):
        file = original_upload(*args, **kwargs)
        canvas.upload_names[file.id] = actual_name
        return canvas.get_file(file.id)

    canvas.files.upload = renamed_upload
    upload = prepare(store, canvas, destination="assignment", course_id=10, assignment_id=20)
    execute = fn(mcp, "canvas_execute_action")
    uploaded = await execute(upload["action_id"])
    assert uploaded["state"] == "succeeded"
    assert uploaded["review"]["filename"] == "note.txt"
    assert uploaded["result"]["filename"] == actual_name
    if not allowed:
        with pytest.raises(ValueError, match="extension"):
            store.prepare_submission(canvas, 10, 20, [upload["action_id"]])
        assert canvas.writes == ["upload"]
        return
    review = store.prepare_submission(canvas, 10, 20, [upload["action_id"]])
    reviewed_file = review["review"]["files"][0]
    assert reviewed_file["filename"] == actual_name
    assert reviewed_file["sha256"] == upload["review"]["sha256"]
    assert actual_name in review["review_text"]
    result = await execute(review["action_id"])
    assert result["state"] == "succeeded" and result["result"]["submitted"] is True
    assert canvas.writes == ["upload", "submit"]
