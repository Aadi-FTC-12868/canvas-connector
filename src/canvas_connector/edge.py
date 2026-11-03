"""Authenticate Cloudflare gateway metadata before using it for rate limits.

This signature conveys the observed client IP, not a Canvas or user identity.
MCP bearer grants and browser sessions are still verified independently.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import time

from starlette.responses import JSONResponse

EDGE_HEADERS = {b"x-canvas-edge-ip", b"x-canvas-edge-time", b"x-canvas-edge-signature"}


def validate_edge_secret(secret: str) -> str:
    if not isinstance(secret, str) or not 32 <= len(secret) <= 256 or not re.fullmatch(r"[!-~]+", secret):
        raise ValueError("A separate private gateway secret is required")
    return secret


class CloudflareEdgeMiddleware:
    def __init__(self, app, *, secret: str, clock=time.time):
        self.app = app
        self._key = validate_edge_secret(secret).encode()
        self.clock = clock

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        try:
            headers = scope.get("headers", [])
            selected = [(key.lower(), value) for key, value in headers if key.lower() in EDGE_HEADERS]
            if len(selected) != 3 or len(dict(selected)) != 3:
                raise ValueError
            fields = dict(selected)
            address = fields[b"x-canvas-edge-ip"].decode("ascii")
            stamp = fields[b"x-canvas-edge-time"].decode("ascii")
            signature = fields[b"x-canvas-edge-signature"].decode("ascii")
            if not re.fullmatch(r"[0-9]{1,12}", stamp) or abs(self.clock() - int(stamp)) > 60:
                raise ValueError
            if "%" in address or not re.fullmatch(r"[0-9a-f]{64}", signature):
                raise ValueError
            parsed = ipaddress.ip_address(address)
            raw_path = scope.get("raw_path")
            if raw_path is None:
                raw_path = scope["path"].encode("ascii")
            path = raw_path.decode("ascii")
            query = scope.get("query_string", b"").decode("ascii")
            target = path + ("?" + query if query else "")
            message = f"v1\n{stamp}\n{address}\n{scope['method'].upper()}\n{target}"
            expected = hmac.new(self._key, message.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
        except (ValueError, KeyError, UnicodeError, TypeError):
            return await JSONResponse({"error": "invalid_gateway"}, 403,
                                      headers={"Cache-Control": "no-store"})(scope, receive, send)
        clean = [(k, v) for k, v in headers
                 if k.lower() not in EDGE_HEADERS and not k.lower().startswith(b"x-forwarded-")
                 and k.lower() != b"forwarded"]
        # The public gateway accepts HTTPS only; its private Container hop may
        # use HTTP. Preserve the external scheme for generated redirect URLs.
        scope = {**scope, "client": (str(parsed), 0), "scheme": "https", "headers": clean}
        return await self.app(scope, receive, send)
