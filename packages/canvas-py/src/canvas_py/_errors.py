"""Exception hierarchy for Canvas API interactions.

Modeled on canvas-ios's APIError taxonomy (see research/01-api-layer.md).
Canvas returns errors in several shapes; we normalize them to a single
hierarchy so agent code can catch specific failure modes.
"""
from __future__ import annotations

from typing import Any


class CanvasError(Exception):
    """Base class for every Canvas-originated error.

    Attributes:
        status: HTTP status code, or None for transport errors.
        url: the request URL that failed.
        body: the raw response body (str) if any.
        parsed: Canvas's parsed error JSON if we could decode it.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        url: str | None = None,
        body: str | None = None,
        parsed: Any = None,
    ):
        super().__init__(message)
        self.status = status
        self.url = url
        self.body = body
        self.parsed = parsed

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        base = super().__str__()
        if self.status is not None:
            return f"[{self.status}] {base}"
        return base


class CanvasTransportError(CanvasError):
    """Network/transport failure — DNS, connect timeout, read timeout, etc.

    Distinct from HTTP error responses. `status` is None for these.
    """


class CanvasAuthError(CanvasError):
    """401 Unauthorized — token missing, invalid, or expired."""


class CanvasPermissionError(CanvasError):
    """403 Forbidden — authenticated but not allowed.

    Particularly common on files/folder enumeration for students; see
    docs/research/05-real-world-quirks.md §1.
    """


class CanvasNotFoundError(CanvasError):
    """404 Not Found — resource doesn't exist, or feature is disabled
    (e.g. Pages endpoint 404 when the teacher disabled Pages)."""


class CanvasRateLimitError(CanvasError):
    """403 with rate-limit marker, or 429 Too Many Requests.

    Attributes:
        retry_after: suggested delay in seconds, parsed from Retry-After header.
    """

    def __init__(self, *args: Any, retry_after: float | None = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.retry_after = retry_after


class CanvasServerError(CanvasError):
    """5xx — Canvas's problem, not ours. Retryable."""


def parse_canvas_error(
    status: int,
    url: str,
    body: str,
    headers: dict[str, str] | None = None,
) -> CanvasError:
    """Build a typed exception from an HTTP error response.

    Canvas returns errors in varied shapes. We try to extract a useful message
    then wrap in the right class. See canvas-ios's APIError.swift for the
    patterns we cover.
    """
    import json as _json

    parsed: Any = None
    message = ""

    try:
        parsed = _json.loads(body)
    except (ValueError, TypeError):
        parsed = None

    # Canvas shape 1: {"errors": [{"message": "..."}]}
    # Canvas shape 2: {"errors": {"field": [{"message": "..."}]}}
    # Canvas shape 3: {"message": "..."}
    # Canvas shape 4: {"error": "..."}
    # Fallback: raw body.
    if isinstance(parsed, dict):
        if "errors" in parsed:
            errs = parsed["errors"]
            if isinstance(errs, list) and errs:
                first = errs[0]
                if isinstance(first, dict):
                    message = first.get("message") or first.get("error") or ""
                elif isinstance(first, str):
                    message = first
            elif isinstance(errs, dict):
                # field-keyed errors — flatten
                fragments = []
                for field, field_errs in errs.items():
                    if isinstance(field_errs, list):
                        for fe in field_errs:
                            if isinstance(fe, dict):
                                fragments.append(f"{field}: {fe.get('message', '')}")
                if fragments:
                    message = "; ".join(fragments)
        if not message:
            message = parsed.get("message") or parsed.get("error") or ""

    if not message:
        message = (body or "").strip()[:500] or f"HTTP {status}"

    # Rate-limit detection: 429, or 403 with body matching.
    headers = headers or {}
    retry_after_raw = headers.get("Retry-After") or headers.get("retry-after")
    retry_after: float | None = None
    if retry_after_raw:
        try:
            retry_after = float(retry_after_raw)
        except ValueError:
            retry_after = None

    if status == 429:
        return CanvasRateLimitError(
            message, status=status, url=url, body=body, parsed=parsed,
            retry_after=retry_after,
        )
    if status == 403 and "rate limit" in message.lower():
        return CanvasRateLimitError(
            message, status=status, url=url, body=body, parsed=parsed,
            retry_after=retry_after,
        )
    if status == 401:
        return CanvasAuthError(message, status=status, url=url, body=body, parsed=parsed)
    if status == 403:
        return CanvasPermissionError(message, status=status, url=url, body=body, parsed=parsed)
    if status == 404:
        return CanvasNotFoundError(message, status=status, url=url, body=body, parsed=parsed)
    if 500 <= status < 600:
        return CanvasServerError(message, status=status, url=url, body=body, parsed=parsed)
    return CanvasError(message, status=status, url=url, body=body, parsed=parsed)
