"""Deterministic, process-local Canvas configuration.

Explicit constructor values win. A selected ``env_path`` (or
``CANVAS_ENV_PATH``) is authoritative for other values; otherwise only the
ambient environment is used. No current-directory or archival file search.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values


@dataclass(frozen=True)
class CanvasConfig:
    """Resolved configuration. Credentials are deliberately omitted from repr."""

    base_url: str
    token: str = field(repr=False)
    token_expires: str | None = None
    allow_insecure_localhost: bool = False

    def __post_init__(self) -> None:
        try:
            parsed = urlsplit(self.base_url)
            # Accessing port also rejects malformed/out-of-range ports.
            parsed.port
        except (ValueError, TypeError) as exc:
            raise ValueError("CANVAS_BASE_URL must be a valid HTTPS origin") from exc
        local_http = (
            self.allow_insecure_localhost
            and parsed.scheme == "http"
            and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        )
        if (
            (parsed.scheme != "https" and not local_http)
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or any(c.isspace() for c in self.base_url)
            or "\\" in self.base_url
        ):
            raise ValueError("CANVAS_BASE_URL must be an HTTPS origin without credentials, path, query, or fragment")
        if not self.token or not self.token.strip() or any(c in self.token for c in "\r\n"):
            raise ValueError("CANVAS_API_TOKEN must be nonempty and contain no line breaks")
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))

    @property
    def api_base(self) -> str:
        return f"{self.base_url}/api/v1"


def load_config(
    *,
    base_url: str | None = None,
    token: str | None = None,
    env_path: str | Path | None = None,
    allow_insecure_localhost: bool = False,
) -> CanvasConfig:
    """Resolve configuration without loading credentials into ``os.environ``.

    A selected file must exist and contain required non-overridden values.
    Falling back to stale ambient credentials after selecting a file would
    silently connect to the wrong Canvas account, so it is prohibited.
    """
    selected = env_path if env_path is not None else os.environ.get("CANVAS_ENV_PATH")
    if selected is not None:
        if not str(selected).strip():
            raise ValueError("CANVAS_ENV_PATH must name a configuration file")
        path = Path(selected).expanduser()
        if not path.is_file():
            raise ValueError("Selected Canvas configuration file does not exist or is not a file")
        # Disable interpolation: credentials must not depend on ambient values.
        source = dotenv_values(path, interpolate=False)
    else:
        source = os.environ

    resolved_base = base_url if base_url is not None else source.get("CANVAS_BASE_URL")
    resolved_token = token if token is not None else source.get("CANVAS_API_TOKEN")
    if not resolved_base:
        raise ValueError("CANVAS_BASE_URL not found in selected configuration; pass base_url= or configure it")
    if not resolved_token:
        raise ValueError("CANVAS_API_TOKEN not found in selected configuration; pass token= or configure it")

    return CanvasConfig(
        base_url=resolved_base,
        token=resolved_token,
        token_expires=source.get("CANVAS_TOKEN_EXPIRES") or None,
        allow_insecure_localhost=allow_insecure_localhost,
    )
