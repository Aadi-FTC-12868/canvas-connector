"""Upload-to-send integration using actual receipts, MCP and native HTTP fixtures."""
import asyncio
import copy
import json
import socket

import httpx
import pytest
from cryptography.fernet import Fernet
from mcp.server.fastmcp import FastMCP

from canvas_connector.actions import ActionStore
from canvas_connector.storage import Store
from canvas_connector.tools import register_tools
from test_inbox_attachments import State, client, P
from test_inbox_messages import API


class Combined(State):
    def __init__(self):
        super().__init__()
        self.messages = API()
        self.messages.set_files = self.attach_native_files

    def api(self, request):
        path = request.url.path
        if path.startswith("/api/v1/conversations") or path == "/api/v1/search/recipients":
            return self.messages.handle(request)
        return super().api(request)

    def attach_native_files(self, outgoing, body):
        assert body["attachment_ids"] == [90]
        # Native Canvas accepts only the sender's conversation-folder files.
        candidates = [f for f in self.folders.values() if f["name"] == "conversation attachments"]
        if self.file["folder_id"] == candidates[0]["id"]:
            outgoing["attachments"] = [copy.deepcopy(self.file)]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Only synthetic transports are allowed")
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", denied)


def upload(store, canvas):
    prepared = store.prepare(canvas, "alice", P)
    result = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
    assert result["state"] == "succeeded", result
    return result


def message_payload(receipt, reply=False):
    common = {"body": "Please review this file.", "upload_action_ids": [receipt["action_id"]]}
    return ({"kind": "inbox_reply_audience", "conversation_id": 10, "recipients": [8], **common} if reply else
            {"kind": "inbox_send_attachments", "subject": "Reviewed file", "recipients": [9], **common})


@pytest.mark.parametrize("reply", [False, True])
@pytest.mark.parametrize("persistent", [False, True])
def test_verified_upload_receipt_sends_exact_file_once_across_store_restart(tmp_path, reply, persistent):
    state = Combined()
    database = Store(tmp_path / "inbox-files.db", Fernet.generate_key()) if persistent else None
    try:
        store = ActionStore(persistence=database)
        with client(state) as canvas:
            receipt = upload(store, canvas)
            assert not state.messages.writes
            prepared = store.prepare(canvas, "alice", message_payload(receipt, reply))
            assert "synthetic-secret" not in json.dumps(prepared)
            assert prepared["review"]["target"]["files"][0]["id"] == 90
            if persistent:
                store = ActionStore(persistence=database)
            sent = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
            assert sent["state"] == "succeeded", sent
            assert sent["result"]["verified"]
            assert store.execute(canvas, "alice", prepared["action_id"], lambda: None) == sent
        assert len(state.transfers) == len(state.messages.writes) == 1
        assert state.downloaded >= 3
    finally:
        if database:
            database.close()


@pytest.mark.parametrize("case", ["other_owner", "pending", "wrong_kind", "wrong_hash", "wrong_actor", "wrong_folder", "duplicate_receipt"])
def test_nonmatching_receipts_cannot_authorize_attachment_send(case):
    state, store = Combined(), ActionStore()
    with client(state) as canvas:
        receipt = upload(store, canvas)
        record = store.records[receipt["action_id"]]
        owner = "bob" if case == "other_owner" else "alice"
        if case == "pending":
            record["state"] = "prepared"
        elif case == "wrong_kind":
            record["kind"] = "upload"
        elif case == "wrong_hash":
            record["result"]["sha256"] = "b" * 64
        elif case == "wrong_actor":
            record["result"]["actor_id"] = 99
        elif case == "wrong_folder":
            record["result"]["folder_id"] = 44
        p = message_payload(receipt)
        if case == "duplicate_receipt":
            p["upload_action_ids"] *= 2
        with pytest.raises(ValueError):
            store.prepare(canvas, owner, p)
    assert not state.messages.writes


@pytest.mark.parametrize("change", ["folder", "bytes", "name"])
def test_changed_saved_file_rejects_prepared_message_before_dispatch(change):
    state, store = Combined(), ActionStore()
    with client(state) as canvas:
        receipt = upload(store, canvas)
        prepared = store.prepare(canvas, "alice", message_payload(receipt))
        if change == "folder":
            state.file["folder_id"] = 1
        elif change == "bytes":
            state.raw = b"different bytes"
        else:
            state.file["display_name"] = "changed.txt"
        result = store.execute(canvas, "alice", prepared["action_id"], lambda: None)
        assert result["state"] == "rejected" and result["result"]["upstream_write_attempted"] is False
    assert not state.messages.writes and len(state.transfers) == 1


def test_actual_mcp_separates_upload_and_send_permissions():
    state, store = Combined(), ActionStore()
    with client(state) as canvas:
        mcp = FastMCP("Synthetic attachment receipts")
        register_tools(mcp, lambda: canvas, lambda: None, owner=lambda: "alice", store=store)

        async def run():
            _, response = await mcp.call_tool("canvas_prepare_action", {"action": P})
            prepared = response["data"]
            _, wrong = await mcp.call_tool("canvas_send_message", {"action_id": prepared["action_id"]})
            assert wrong["status"] == "invalid_request"
            assert not state.transfers
            _, uploaded = await mcp.call_tool("canvas_upload_inbox_attachment", {"action_id": prepared["action_id"]})
            assert uploaded["data"]["state"] == "succeeded"
            assert not state.messages.writes
            _, message = await mcp.call_tool("canvas_prepare_action", {"action": message_payload(uploaded["data"])})
            _, wrong = await mcp.call_tool("canvas_upload_inbox_attachment", {"action_id": message["data"]["action_id"]})
            assert wrong["status"] == "invalid_request" and not state.messages.writes
            _, sent = await mcp.call_tool("canvas_send_message", {"action_id": message["data"]["action_id"]})
            assert sent["data"]["state"] == "succeeded", sent

        asyncio.run(run())
    assert len(state.messages.writes) == len(state.transfers) == 1
