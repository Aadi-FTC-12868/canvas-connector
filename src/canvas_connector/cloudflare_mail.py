"""Fixed private Cloudflare Container-to-Worker account mail bridge.

Only the Worker outbound handler can resolve this internal destination. There
is no operator-supplied URL, Cloudflare API key, or public email relay here.
"""
from __future__ import annotations

import re
import json
from collections.abc import Callable

import httpx

MAIL_URL = "http://canvas-mail.internal/send"


class AccountMailUnavailable(RuntimeError):
    """Delivery was unavailable or ambiguous; no sensitive details are exposed."""


class CloudflareMailer:
    def __init__(self, *, client_factory: Callable[[], httpx.Client] | None = None):
        self.client_factory = client_factory or (
            lambda: httpx.Client(trust_env=False, follow_redirects=False, timeout=10)
        )

    def __call__(self, email: str, purpose: str, code: str) -> None:
        if (not isinstance(email, str) or len(email) > 254
                or not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", email)
                or purpose not in {"verify", "reset"}
                or not isinstance(code, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{6,128}", code)):
            raise AccountMailUnavailable("Account email is unavailable")
        try:
            with self.client_factory() as client:
                # A lost response might follow actual delivery. Never retry it
                # automatically or expose the service response/recipient/code.
                with client.stream("POST", MAIL_URL, json={"email": email, "purpose": purpose, "code": code},
                                   follow_redirects=False) as response:
                    if response.status_code != 200:
                        raise AccountMailUnavailable("Account email is unavailable")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 4096:
                            raise AccountMailUnavailable("Account email is unavailable")
                    result = json.loads(body)
                    if not isinstance(result, dict) or result.get("sent") is not True:
                        raise AccountMailUnavailable("Account email is unavailable")
        except (httpx.HTTPError, OSError, ValueError):
            raise AccountMailUnavailable("Account email is unavailable") from None
