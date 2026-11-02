"""Local stdio and authenticated, single-user Streamable HTTP transports."""
from __future__ import annotations

import hmac
import logging
import os
import re
from pathlib import Path
from typing import Any

from canvas_py._config import load_config
from dotenv import dotenv_values
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)
LOOPBACK_HOSTS = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "[::1]", "[::1]:*"]
LOOPBACK_ORIGINS = ["http://localhost:*", "http://127.0.0.1:*", "http://[::1]:*"]


def _value(name: str, default: str = "") -> str:
    """Read the explicitly selected environment file without changing process env."""
    if name in os.environ:
        return os.environ[name]
    selected = os.environ.get("CANVAS_ENV_PATH")
    if selected:
        return dotenv_values(Path(selected).expanduser(), interpolate=False).get(name) or default
    return default


def _flag(name: str, default: bool = False) -> bool:
    value = _value(name, str(default)).lower()
    if value not in {"true", "false", "1", "0"}:
        raise ValueError(f"{name} must be true or false")
    return value in {"true", "1"}


def _api_key() -> str:
    key = _value("CANVAS_MCP_API_KEY")
    if not re.fullmatch(r"[!-~]{32,1024}", key):
        raise ValueError("CANVAS_MCP_API_KEY must contain 32-1024 printable ASCII characters without spaces")
    canvas_token = load_config().token
    if hmac.compare_digest(key.encode(), canvas_token.encode()):
        raise ValueError("CANVAS_MCP_API_KEY must be different from CANVAS_API_TOKEN")
    return key


def _security() -> TransportSecuritySettings:
    def allowlist(name: str, defaults: list[str]) -> list[str]:
        value = _value(name)
        values = [x.strip() for x in value.split(",") if x.strip()] if value else defaults
        if any("*" in x and not x.endswith(":*") for x in values):
            raise ValueError(f"{name} permits exact values or a wildcard port only")
        return values

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowlist("CANVAS_MCP_ALLOWED_HOSTS", LOOPBACK_HOSTS),
        allowed_origins=allowlist("CANVAS_MCP_ALLOWED_ORIGINS", LOOPBACK_ORIGINS),
    )


def wrap_with_bearer_auth(app: Any, expected_token: str, *, allow_path_secret: bool = False) -> Any:
    """Authenticate every HTTP request. Legacy URL credentials require explicit opt-in."""
    async def middleware(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await app(scope, receive, send)
            return
        authorizations = [v for k, v in scope.get("headers", []) if k.lower() == b"authorization"]
        expected = expected_token.encode("ascii")
        ok = len(authorizations) == 1 and hmac.compare_digest(authorizations[0], b"Bearer " + expected)
        path = scope.get("path", "")
        if allow_path_secret and not ok:
            parts = path.split("/", 2)
            if len(parts) == 3 and hmac.compare_digest(parts[1].encode(), expected):
                scope = dict(scope)
                scope["path"] = "/" + parts[2]
                scope["raw_path"] = scope["path"].encode()
                ok = True
        if not ok:
            response = JSONResponse(
                {"error": "unauthorized"}, status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="canvas-connector"', "Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        await app(scope, receive, send)
    return middleware


def create_http_app(mcp: Any) -> Any:
    """Build an authenticated ASGI app; /health is liveness, never Canvas readiness.

    A remote instance represents exactly one Canvas account. Stateless requests
    survive container sleeps/restarts without trusting MCP session IDs as auth.
    """
    key = _api_key()
    path = _value("CANVAS_MCP_PATH", "/mcp")
    if not re.fullmatch(r"/[A-Za-z0-9_/-]+", path) or path.endswith("/") or path == "/health":
        raise ValueError("CANVAS_MCP_PATH must be an absolute endpoint without a trailing slash")
    mcp.settings.streamable_http_path = path
    # Actions no longer issue server-to-client confirmation requests. Both
    # reads and actions can use restart-tolerant stateless JSON responses.
    mcp.settings.stateless_http = _flag("CANVAS_MCP_STATELESS_HTTP", True)
    mcp.settings.json_response = True
    security = _security()
    mcp.settings.transport_security = security
    guard = TransportSecurityMiddleware(security)
    inner = mcp.streamable_http_app()

    async def routes(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and scope.get("path") != path:
            await JSONResponse({"error": "not_found"}, status_code=404)(scope, receive, send)
            return
        await inner(scope, receive, send)

    authenticated = wrap_with_bearer_auth(routes, key, allow_path_secret=_flag("CANVAS_MCP_ALLOW_PATH_SECRET"))

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            # Guard health as well as MCP, and reject ambiguous security headers.
            for name in (b"host", b"origin"):
                if sum(k.lower() == name for k, _ in scope.get("headers", [])) > 1:
                    await JSONResponse({"error": "ambiguous_headers"}, status_code=400)(scope, receive, send)
                    return
            rejected = await guard.validate_request(Request(scope), is_post=False)
            if rejected is not None:
                await rejected(scope, receive, send)
                return
            if scope.get("path") == "/health":
                if scope.get("method") not in {"GET", "HEAD"}:
                    await JSONResponse({"error": "method_not_allowed"}, status_code=405)(scope, receive, send)
                    return
                await JSONResponse({"status": "ok", "service": "canvas-connector", "canvas_checked": False},
                                   headers={"Cache-Control": "no-store"})(scope, receive, send)
                return
        await authenticated(scope, receive, send)
    return app


def run_server(mcp: Any) -> None:
    logging.basicConfig(level=_value("CANVAS_MCP_LOG_LEVEL", "WARNING"),
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # Upstream signed download URLs may contain temporary credentials.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    transport = _value("CANVAS_MCP_TRANSPORT", "stdio").lower()
    if transport == "stdio":
        mcp.run()
        return
    if transport not in {"streamable-http", "http"}:
        raise ValueError("CANVAS_MCP_TRANSPORT must be stdio or streamable-http; legacy SSE is retired")
    app = create_http_app(mcp)
    import uvicorn
    # Never log URLs: old clients may still send a credential in the path.
    uvicorn.run(app, host=_value("CANVAS_MCP_HOST", "127.0.0.1"),
                port=int(_value("CANVAS_MCP_PORT", "8080")),
                log_level="info", access_log=False, proxy_headers=False)
