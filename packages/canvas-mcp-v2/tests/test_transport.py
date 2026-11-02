"""HTTP boundary tests: authorization, DNS rebinding, and restart-safe requests."""
import os
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from canvas_mcp_v2.transport import create_http_app

KEY = "test-key-not-a-real-secret-0123456789abcdef"


@pytest.fixture
def configured(monkeypatch, tmp_path):
    selected = tmp_path / "selected.env"
    selected.write_text("CANVAS_BASE_URL=https://school.instructure.com\nCANVAS_API_TOKEN=dummy-canvas-token\n")
    monkeypatch.setenv("CANVAS_ENV_PATH", str(selected))
    monkeypatch.setenv("CANVAS_API_TOKEN", "dummy-canvas-token")
    monkeypatch.setenv("CANVAS_MCP_API_KEY", KEY)
    for name in ("CANVAS_MCP_ALLOW_PATH_SECRET", "CANVAS_MCP_ALLOWED_HOSTS",
                 "CANVAS_MCP_ALLOWED_ORIGINS", "CANVAS_MCP_STATELESS_HTTP", "CANVAS_MCP_PATH"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_http_boundary_and_stateless_initialize(configured):
    mcp = FastMCP("test")
    with TestClient(create_http_app(mcp), base_url="http://localhost") as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["canvas_checked"] is False
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Bearer " + KEY[:-1] + "x"}):
            assert client.post("/mcp", headers=headers).status_code == 401
        assert client.post(f"/{KEY}/mcp").status_code == 401
        headers = {"Authorization": f"Bearer {KEY}", "Accept": "application/json, text/event-stream"}
        assert client.get("/other", headers=headers).status_code == 404
        assert client.get("/health", headers={"Host": "evil.example"}).status_code == 421
        assert client.post("/mcp", headers={**headers, "Origin": "https://evil.example"}).status_code == 403
        for call_id in (1, 2):
            response = client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": call_id, "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                           "clientInfo": {"name": "test", "version": "1"}},
            })
            assert response.status_code == 200
            assert "Mcp-Session-Id" not in response.headers
            assert response.json()["id"] == call_id


def test_key_strength_and_separation(configured):
    configured.setenv("CANVAS_MCP_API_KEY", "short")
    with pytest.raises(ValueError, match="32-1024"):
        create_http_app(FastMCP("test"))
    configured.setenv("CANVAS_MCP_API_KEY", KEY)
    Path(os.environ["CANVAS_ENV_PATH"]).write_text(f"CANVAS_BASE_URL=https://school.instructure.com\nCANVAS_API_TOKEN={KEY}\n")
    configured.setenv("CANVAS_API_TOKEN", "stale-ambient-token-must-not-hide-selected-key-reuse")
    with pytest.raises(ValueError, match="different"):
        create_http_app(FastMCP("test"))


def test_dotenv_values_do_not_interpolate_ambient_secrets(configured):
    from canvas_mcp_v2.transport import _value
    configured.setenv("AMBIENT_SECRET", "do-not-expand")
    selected = Path(os.environ["CANVAS_ENV_PATH"])
    selected.write_text(selected.read_text() + "CANVAS_MCP_TEST_VALUE=${AMBIENT_SECRET}\n")
    assert _value("CANVAS_MCP_TEST_VALUE") == "${AMBIENT_SECRET}"


def test_explicit_public_allowlists(configured):
    configured.setenv("CANVAS_MCP_ALLOWED_HOSTS", "canvas.example")
    configured.setenv("CANVAS_MCP_ALLOWED_ORIGINS", "https://canvas.example")
    with TestClient(create_http_app(FastMCP("test")), base_url="https://canvas.example") as client:
        assert client.get("/health", headers={"Origin": "https://canvas.example"}).status_code == 200
        assert client.get("/health", headers={"Origin": "https://canvas.example.evil"}).status_code == 403
        assert client.get("/health", headers={"Host": "localhost"}).status_code == 421


def test_duplicate_auth_headers_are_rejected(configured):
    with TestClient(create_http_app(FastMCP("test")), base_url="http://localhost") as client:
        response = client.post("/mcp", headers=[("Authorization", f"Bearer {KEY}"), ("Authorization", "Bearer wrong")])
        assert response.status_code == 401


def test_legacy_path_secret_needs_explicit_opt_in(configured):
    configured.setenv("CANVAS_MCP_ALLOW_PATH_SECRET", "true")
    with TestClient(create_http_app(FastMCP("test")), base_url="http://localhost") as client:
        # Reaches the inner endpoint, which rejects an empty request body.
        assert client.post(f"/{KEY}/mcp").status_code != 401
        assert client.post("/wrong/mcp").status_code == 401
