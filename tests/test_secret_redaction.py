"""Known credential reflection is removed at the public MCP result boundary."""
import asyncio
from contextvars import ContextVar
import hashlib
import html
import json
import threading
from types import SimpleNamespace
from urllib.parse import quote

from cryptography.fernet import Fernet
from mcp.server.fastmcp import FastMCP
import pytest
from starlette.testclient import TestClient

from canvas_py._config import CanvasConfig
from canvas_connector.responses import ResponseStore, SecretRedactor, envelope
from canvas_connector.storage import Store
from canvas_connector.tools import register_tools

PAT = "synthetic-private-credential/with&symbols+value"


def make_server(*, name=PAT, responses=None, secret_values=None, send=None):
    canvas = SimpleNamespace(config=CanvasConfig("https://school.example", PAT),
        send=send or (lambda request: {"id": 11, "name": name}))
    server = FastMCP("redaction boundary", stateless_http=True, json_response=True)
    register_tools(server, lambda: canvas, lambda: None, owner=lambda: "alice",
                   responses=responses, secret_values=secret_values or (lambda: (PAT,)))
    functions = {tool.name: tool.fn for tool in server._tool_manager.list_tools()}
    return server, functions


def test_structured_keys_values_and_key_collisions_preserve_safe_values():
    other = "second-synthetic-private-credential"
    redactor = SecretRedactor((PAT, other))
    raw = {PAT: [PAT, {other: "prefix " + PAT}], "[redacted]": False, "zero": 0, "unknown": None}
    result = redactor.redact(raw)
    assert PAT not in json.dumps(result) and other not in json.dumps(result)
    assert len(result) == len(raw) and result["[redacted] [2]"] is False
    assert result["zero"] == 0 and result["unknown"] is None
    assert raw[PAT][0] == PAT  # No mutation of internal/source state.


@pytest.mark.parametrize("transform", [lambda value: value, lambda value: quote(value, safe=""),
    lambda value: quote(value, safe="").replace("%2F", "%2f"), lambda value: html.escape(value, quote=True),
    lambda value: json.dumps(value)[1:-1]])
def test_common_secret_representations_are_redacted(transform):
    form = transform(PAT)
    assert SecretRedactor((PAT,)).redact({"body": "before " + form + " after"}) == {
        "body": "before [redacted] after"}


def test_short_fixture_tokens_do_not_rewrite_ordinary_words():
    redactor = SecretRedactor(("secret", "id", "[redacted]"))
    result = redactor.redact({"identity": "secretary", "id": "secret", "header": "Bearer secret"})
    assert result["identity"] == "secretary"
    assert result["[removed:1]"] == "[removed:1]"
    assert result["header"] == "Bearer [removed:1]"


def test_capabilities_capture_once_without_calling_canvas_provider():
    calls = []
    server = FastMCP("no implicit provider")
    def unavailable():
        raise AssertionError("Capabilities must not acquire a Canvas client")
    register_tools(server, unavailable, lambda: None, secret_values=lambda: calls.append(True) or (PAT,))
    fn = next(tool.fn for tool in server._tool_manager.list_tools() if tool.name == "canvas_capabilities")
    assert asyncio.run(fn())["status"] == "ok" and calls == [True]


def test_credential_callback_failure_does_not_expose_its_exception():
    def failed():
        raise RuntimeError(PAT)
    _, functions = make_server(secret_values=failed)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(functions["canvas_capabilities"]())
    assert PAT not in str(caught.value) and caught.value.__suppress_context__


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError, PermissionError])
def test_error_reflection_is_removed(error_type):
    def failed(request):
        raise error_type("upstream reflected " + PAT)
    _, functions = make_server(send=failed)
    if error_type is ValueError:
        result = asyncio.run(functions["canvas_connection_status"]())
        assert result["status"] == "invalid_request" and PAT not in json.dumps(result)
    else:
        with pytest.raises(RuntimeError) as caught:
            asyncio.run(functions["canvas_connection_status"]())
        assert PAT not in str(caught.value) and caught.value.__suppress_context__


def test_oversized_results_are_redacted_before_storage_and_hashing(tmp_path):
    persistence = Store(tmp_path / "responses.db", Fernet.generate_key())
    responses = ResponseStore(inline_chars=200, persistence=persistence)
    _, functions = make_server(name=(PAT + " ") * 3000, responses=responses)
    first = asyncio.run(functions["canvas_connection_status"]())
    key = first["data"]["result_id"]
    stored = persistence.get("response", key)
    assert PAT not in stored["text"] and "[redacted]" in stored["text"]
    pieces, cursor = [first["data"]["text"]], first["data"]["next_read"]
    while cursor:
        piece = asyncio.run(functions["canvas_read_result"](cursor["result_id"], cursor["offset"], cursor["max_chars"]))
        assert PAT not in json.dumps(piece)
        pieces.append(piece["data"]["text"])
        cursor = piece["data"]["next_read"]
    reconstructed = "".join(pieces)
    assert hashlib.sha256(reconstructed.encode()).hexdigest() == first["data"]["sha256"]
    assert json.loads(reconstructed)["data"]["name"] == ("[redacted] ") * 3000
    persistence.close()


@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("encoded", [False, True])
def test_legacy_continuation_is_refused_before_any_secret_fragment_window(tmp_path, durable, encoded):
    persistence = Store(tmp_path / "legacy.db", Fernet.generate_key()) if durable else None
    responses = ResponseStore(inline_chars=100, persistence=persistence)
    reflected = quote(PAT, safe="").replace("%2F", "%2f") if encoded else PAT
    first = responses.compact(envelope({"text": "x" * 39990 + reflected + "y" * 1000}), "alice")
    key = first["data"]["result_id"]
    before = persistence.get("response", key) if durable else dict(responses.records[key])
    _, functions = make_server(responses=responses)
    # The requested window is only a fragment of the secret. Whole-record
    # checking must still refuse it before slicing.
    for offset in (0, before["text"].index(reflected) + 5):
        result = asyncio.run(functions["canvas_read_result"](key, offset, 7))
        assert result["status"] == "invalid_request" and "repeat the source read" in result["errors"][0]["message"]
        assert result["data"] is None
    after = persistence.get("response", key) if durable else responses.records[key]
    assert before == after  # No cursor/hash mutation of an existing record.
    if persistence:
        persistence.close()


def test_legacy_json_escaped_credential_is_detected():
    token = 'synthetic-token-"quoted"-and\\backslash'
    encoded = json.dumps({"text": token})
    assert token not in encoded and SecretRedactor((token,)).contains_serialized(encoded)


def test_two_concurrent_accounts_keep_request_scoped_redaction():
    current = ContextVar("synthetic account")
    barrier = threading.Barrier(2)
    accounts = {name: "synthetic-private-credential-" + name for name in ("alice", "bob")}
    def provider():
        name = current.get()
        def send(request):
            barrier.wait(timeout=5)
            return {"id": name, "name": name + " " + accounts[name]}
        return SimpleNamespace(config=CanvasConfig("https://school.example", accounts[name]), send=send)
    server = FastMCP("concurrent redaction")
    register_tools(server, provider, lambda: None, owner=current.get,
                   secret_values=lambda: (accounts[current.get()],))
    fn = next(tool.fn for tool in server._tool_manager.list_tools() if tool.name == "canvas_connection_status")
    async def call(name):
        context = current.set(name)
        try:
            return await fn()
        finally:
            current.reset(context)
    async def both():
        return await asyncio.gather(call("alice"), call("bob"))
    results = asyncio.run(both())
    assert [result["data"]["name"] for result in results] == ["alice [redacted]", "bob [redacted]"]


def test_real_mcp_http_text_and_structured_result_are_both_redacted():
    server, _ = make_server(name="profile " + PAT)
    with TestClient(server.streamable_http_app(), base_url="http://localhost:8000") as client:
        result = client.post("/mcp", headers={"Accept": "application/json, text/event-stream"}, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "canvas_connection_status", "arguments": {}}})
    assert result.status_code == 200 and PAT not in result.text
    payload = result.json()["result"]
    assert payload["structuredContent"]["data"]["name"] == "profile [redacted]"
    assert "[redacted]" in payload["content"][0]["text"]
