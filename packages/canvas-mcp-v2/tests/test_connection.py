"""Configuration rotation and server-side file boundaries, without live secrets."""
from types import SimpleNamespace

import httpx
import pytest

from canvas_mcp_v2 import server


def test_selected_file_reloads_without_restart(tmp_path, monkeypatch):
    env = tmp_path/"canvas.env"
    env.write_text("CANVAS_BASE_URL=https://school.instructure.com\nCANVAS_API_TOKEN=first\n")
    monkeypatch.setenv("CANVAS_ENV_PATH", str(env))
    monkeypatch.setenv("CANVAS_API_TOKEN", "stale-inherited-token")
    monkeypatch.setattr(server, "_current_client", None)
    first = server._canvas()
    assert first.config.token == "first"
    assert server._canvas() is first
    env.write_text("CANVAS_BASE_URL=https://school.instructure.com\nCANVAS_API_TOKEN=second\n")
    second = server._canvas()
    assert second is not first
    assert second.config.token == "second"
    assert "second" not in repr(second.config)
    first.close()
    second.close()


def test_missing_selected_file_fails_instead_of_other_account(tmp_path, monkeypatch):
    monkeypatch.setenv("CANVAS_ENV_PATH", str(tmp_path/"missing.env"))
    monkeypatch.setenv("CANVAS_BASE_URL", "https://other.instructure.com")
    monkeypatch.setenv("CANVAS_API_TOKEN", "other-account")
    with pytest.raises((ValueError, FileNotFoundError)):
        server._canvas()


def test_http_download_cannot_write_server_files(tmp_path, monkeypatch):
    monkeypatch.setenv("CANVAS_MCP_TRANSPORT", "streamable-http")
    target = tmp_path/"config.env"
    target.write_text("keep this")
    with pytest.raises(ValueError, match="stdio"):
        server.download_file(1, str(target))
    assert target.read_text() == "keep this"


def test_inline_oversized_file_rejected_before_download(monkeypatch):
    def fail_download(*args, **kwargs):
        pytest.fail("An oversized file must not be downloaded")
    monkeypatch.setattr(server, "_canvas", lambda: SimpleNamespace(files=SimpleNamespace(
        get=lambda _: SimpleNamespace(size=9*1024*1024), download=fail_download)))
    with pytest.raises(ValueError, match="8 MiB"):
        server.fetch_file_content(1)


def test_http_profile_from_selected_file_hides_write_tools(tmp_path, monkeypatch):
    from mcp.server.fastmcp import FastMCP
    env = tmp_path/"canvas.env"
    env.write_text("CANVAS_MCP_TRANSPORT=streamable-http\n")
    monkeypatch.setenv("CANVAS_ENV_PATH", str(env))
    monkeypatch.delenv("CANVAS_MCP_TRANSPORT", raising=False)
    monkeypatch.delenv("CANVAS_MCP_ENABLE_PLANNER_WRITES", raising=False)
    mcp = FastMCP("profile test")
    for name in ("download_file", "create_planner_note", "get_inbox_thread"):
        mcp.add_tool(lambda: None, name=name)
    monkeypatch.setattr(server, "mcp", mcp)
    server.configure_tools()
    tools = mcp._tool_manager.list_tools()
    assert [tool.name for tool in tools] == ["get_inbox_thread"]
    assert tools[0].annotations.readOnlyHint


def test_http_inbox_reads_cannot_mark_as_read(monkeypatch):
    monkeypatch.setenv("CANVAS_MCP_TRANSPORT", "streamable-http")
    with pytest.raises(ValueError, match="preserve unread"):
        server.get_inbox_thread(1, auto_mark_as_read=True)


def test_inline_file_pins_client_across_credential_rotation(monkeypatch):
    calls = []
    def provider():
        calls.append(True)
        assert len(calls) == 1, "tool must resolve credentials only once"
        def download(file, path, **kwargs):
            path.write_bytes(b"hello")
        return SimpleNamespace(files=SimpleNamespace(
            get=lambda _: SimpleNamespace(size=5,display_name="note.txt",content_type="text/plain"),
            download=download))
    monkeypatch.setattr(server, "_canvas", provider)
    result = server.fetch_file_content(1, as_text=True)
    assert result["content"] == "hello"


def test_legacy_tools_present_rotated_token_like_canvas_tools(tmp_path, monkeypatch):
    # 2026-09-30: legacy tools returned "[401] Expired access token" while canvas_* tools
    # worked. The cause was a stale same-named server outside this package, but this pins
    # the connector side: no legacy path may keep a client or token across calls.
    origin = "https://school.instructure.com"
    env = tmp_path/"canvas.env"
    env.write_text(f"CANVAS_BASE_URL={origin}\nCANVAS_API_TOKEN=first\n")
    monkeypatch.setenv("CANVAS_ENV_PATH", str(env))
    monkeypatch.setenv("CANVAS_API_TOKEN", "stale-inherited-token")
    monkeypatch.delenv("CANVAS_MCP_TRANSPORT", raising=False)
    monkeypatch.setattr(server, "_current_client", None)
    valid = {"token": "first"}
    seen = []

    def handler(request):
        seen.append(request.headers.get("Authorization"))
        if request.headers.get("Authorization") != f"Bearer {valid['token']}":
            return httpx.Response(401, json={"errors": [{"message": "Expired access token"}]})
        routes = {
            "/api/v1/files/7": {"id": 7, "display_name": "note.txt", "content-type": "text/plain",
                                "size": 5, "url": f"{origin}/files/7/download?download_frd=1"},
            "/api/v1/courses/3/assignments/5/submissions/self": {"id": 11, "assignment_id": 5, "workflow_state": "unsubmitted"},
            "/api/v1/users/self/profile": {"id": 42},
            "/api/v1/courses": [],
        }
        if request.url.path == "/files/7/download":
            return httpx.Response(200, content=b"hello")
        return httpx.Response(200, json=routes[request.url.path])

    clients = []
    real_canvas = server.Canvas

    def canvas_with_mock_transport(config):
        clients.append(httpx.Client(transport=httpx.MockTransport(handler)))
        return real_canvas(config, http_client=clients[-1])

    monkeypatch.setattr(server, "Canvas", canvas_with_mock_transport)

    def call_every_tool(round_name):
        assert server.get_file(7)["id"] == 7
        target = tmp_path/round_name/"note.txt"
        assert server.download_file(7, str(target))["size"] == 5
        assert target.read_bytes() == b"hello"
        assert server.fetch_file_content(7, as_text=True)["content"] == "hello"
        assert server.get_my_submission(3, 5)["workflow_state"] == "unsubmitted"
        assert server.canvas_connection_status()["user_id"] == 42

    call_every_tool("first")
    first_round = len(seen)
    env.write_text(f"CANVAS_BASE_URL={origin}\nCANVAS_API_TOKEN=second\n")
    valid["token"] = "second"
    call_every_tool("second")

    assert set(seen[:first_round]) == {"Bearer first"}
    assert set(seen[first_round:]) == {"Bearer second"}
    assert len(clients) == 2
    for client in clients:
        client.close()
