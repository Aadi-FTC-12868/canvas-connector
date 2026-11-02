"""Central HTTP dispatcher — the plumbing every endpoint uses.

Mirrors the responsibilities of canvas-ios's API class (research/01-api-layer.md):
    - bearer-token injection
    - retry on rate limit (429 or 403 with rate-limit message, respecting
      Retry-After when present)
    - Link-header pagination
    - typed error exceptions
    - clean abstraction over the underlying HTTP library (httpx)

This module is internal. The public entry point is `Canvas` in _client.py.
"""
from __future__ import annotations

import ipaddress
import logging
import math
import os
import re
import socket
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlencode

import httpx

from ._config import CanvasConfig
from ._errors import (
    CanvasError,
    CanvasRateLimitError,
    CanvasTransportError,
    parse_canvas_error,
)
from ._pagination import parse_link_header
from ._request import CanvasRequest

logger = logging.getLogger("canvas_py.http")

Response = TypeVar("Response")

# Rate-limit retry policy. Canvas uses a leaky-bucket limiter; modest linear
# backoff works in practice (canvas-ios uses 1-1.5s).
_DEFAULT_RATE_LIMIT_RETRIES = 3
_DEFAULT_RATE_LIMIT_BASE_DELAY = 1.0
_MAX_RATE_LIMIT_DELAY = 30.0
_MAX_REDIRECTS = 5


class CanvasHTTP:
    """Thin wrapper around httpx.Client that speaks Canvas."""

    def __init__(
        self,
        config: CanvasConfig,
        *,
        timeout: float = 30.0,
        rate_limit_retries: int = _DEFAULT_RATE_LIMIT_RETRIES,
        rate_limit_base_delay: float = _DEFAULT_RATE_LIMIT_BASE_DELAY,
        max_pages: int = 1000,
        client: httpx.Client | None = None,
    ):
        self._config = config
        if rate_limit_retries < 0 or not math.isfinite(rate_limit_base_delay) or rate_limit_base_delay < 0:
            raise ValueError("retry settings must be finite and nonnegative")
        if max_pages < 1:
            raise ValueError("max_pages must be positive")
        self._rate_limit_retries = rate_limit_retries
        self._rate_limit_base_delay = rate_limit_base_delay
        self._max_pages = max_pages
        self._origin = self._url_origin(httpx.URL(config.base_url))

        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.Client(
                base_url=config.api_base,
                timeout=timeout,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "canvas-py/0.1",
                },
                follow_redirects=False,
                trust_env=False,
            )
            self._owns_client = True

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> CanvasHTTP:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # --- public send/paginate ---

    def send(self, request: CanvasRequest[Response]) -> Response:
        """Execute one request and return the decoded response."""
        raw, _headers = self._raw_request(request, absolute_url=None)
        return request.decode(raw)

    def send_absolute(
        self,
        request: CanvasRequest[Response],
        *,
        absolute_url: str,
    ) -> Response:
        """Execute a request against an absolute URL (e.g. step-3 confirm
        URL from the upload flow, which points at Canvas's confirmation
        endpoint but is not relative to our api/v1 base).
        """
        raw, _headers = self._raw_request(request, absolute_url=absolute_url)
        return request.decode(raw)

    def paginate(self, request: CanvasRequest[Response]) -> Iterator[Any]:
        """Walk a paginated endpoint, yielding each item across all pages.

        Only works when the decoded response is a list. Uses Canvas's
        Link-header rel=next for next-page URLs. The `request.decode` method
        is re-applied on every page so the type stays consistent.
        """
        next_url: str | None = None
        first = True
        seen: set[str] = set()
        page_count = 0
        collection_path: bytes | None = None

        while first or next_url:
            current_url = self._request_url(request, absolute_url=next_url)
            if first:
                collection_path = current_url.raw_path.split(b"?", 1)[0]
            if request.pin_pagination_path and current_url.raw_path.split(b"?", 1)[0] != collection_path:
                raise CanvasError("pagination left the requested Canvas collection path")
            request.validate_pagination_query(current_url.params.multi_items(), page_number=page_count + 1)
            current = str(current_url)
            if current in seen:
                raise CanvasError("pagination loop detected")
            if page_count >= self._max_pages:
                raise CanvasError("pagination exceeded configured page limit")
            seen.add(current)
            page_count += 1
            if first:
                raw, headers = self._raw_request(request, absolute_url=None)
                first = False
            else:
                try:
                    raw, headers = self._raw_request(request, absolute_url=next_url)
                except CanvasError as error:
                    if request.pin_pagination_path and error.status == 404:
                        raise CanvasError("A later Canvas pagination page was unavailable; collection is incomplete") from error
                    raise

            page = request.decode(raw)
            if not isinstance(page, list):
                raise TypeError(
                    f"paginate() expects list responses; got {type(page).__name__}"
                )
            yield from page

            link_header = headers.get("Link") or headers.get("link")
            if request.pin_pagination_path:
                _validate_pinned_links(link_header)
            links = parse_link_header(link_header)
            next_url = links.next

    def exhaust(self, request: CanvasRequest[Response]) -> list[Any]:
        """paginate() into a single flat list. Convenience for non-streaming use."""
        return list(self.paginate(request))

    # --- low-level ---

    def _raw_request(
        self,
        request: CanvasRequest[Any],
        *,
        absolute_url: str | None,
    ) -> tuple[Any, httpx.Headers]:
        """Perform the HTTP call with retry-on-rate-limit.

        If `absolute_url` is given, the client hits it directly (for next-page
        URLs from Link header). Otherwise the client constructs from
        request.path + request.query_pairs().
        """
        attempt = 0

        while True:
            attempt += 1
            try:
                response = self._dispatch(request, absolute_url=absolute_url)
            except httpx.RequestError as e:
                raise CanvasTransportError(
                    f"transport error: {e.__class__.__name__}: {e}",
                    url=str(getattr(e, "request", "")) or absolute_url or request.path,
                ) from e

            if 200 <= response.status_code < 300:
                try:
                    parsed = response.json() if response.content else None
                except ValueError as e:
                    raise CanvasError(
                        f"response was not JSON: {e}",
                        status=response.status_code,
                        url=str(response.url),
                        body=response.text[:500],
                    ) from e
                return parsed, response.headers

            # Error branch. Parse Canvas's error shape.
            err = parse_canvas_error(
                status=response.status_code,
                url=str(response.url),
                body=response.text,
                headers=dict(response.headers),
            )

            # Retryable?
            if (
                isinstance(err, CanvasRateLimitError)
                and request.allow_retries
                and attempt <= self._rate_limit_retries
            ):
                delay = err.retry_after
                if delay is None or not math.isfinite(delay) or delay < 0:
                    delay = self._rate_limit_base_delay * attempt
                delay = min(delay, _MAX_RATE_LIMIT_DELAY)
                logger.warning(
                    "rate limited on %s; sleeping %.1fs (attempt %d/%d)",
                    response.url.path, delay, attempt, self._rate_limit_retries,
                )
                time.sleep(delay)
                continue

            raise err

    def _dispatch(
        self,
        request: CanvasRequest[Any],
        *,
        absolute_url: str | None,
    ) -> httpx.Response:
        """Do one actual HTTP round-trip."""
        url = self._request_url(request, absolute_url=absolute_url)
        headers = httpx.Headers(request.headers())
        headers["Authorization"] = f"Bearer {self._config.token}"
        json_body = request.json_body()
        form_body = request.form_body()
        files = request.multipart_files()
        if json_body is not None and (form_body is not None or files is not None):
            raise ValueError("request cannot combine json_body with form_body or multipart_files")
        if files is not None:
            if not isinstance(files, dict) or not files:
                raise ValueError("multipart_files must contain immutable named file parts")
            for field, part in files.items():
                if (not isinstance(field, str) or not field or
                        not isinstance(part, tuple) or len(part) != 3 or
                        not isinstance(part[0], str) or not part[0] or
                        type(part[1]) is not bytes or not isinstance(part[2], str) or not part[2] or
                        any(ord(c) < 32 or ord(c) == 127 for c in field + part[0] + part[2])):
                    raise ValueError("multipart parts require a field, filename, immutable bytes and content type")

        outgoing = self._client.build_request(
            request.method, url, headers=headers, json=json_body, data=form_body, files=files,
        )
        for redirects in range(_MAX_REDIRECTS + 1):
            # Explicitly disable injected-client auth and automatic redirects.
            # Every subsequent hop must pass our origin check before any I/O.
            self._validate_api_url(outgoing.url)
            response = self._client.send(outgoing, auth=None, follow_redirects=False)
            if not response.has_redirect_location:
                return response
            if not request.allow_redirects:
                response.close()
                raise CanvasError("redirects are disabled for this Canvas request", status=response.status_code,
                                  url=str(outgoing.url))
            if redirects == _MAX_REDIRECTS or response.next_request is None:
                response.close()
                raise CanvasError("API redirect limit exceeded")
            outgoing = response.next_request
            response.close()
        raise AssertionError("unreachable")

    @staticmethod
    def _url_origin(url: httpx.URL) -> tuple[str, str, int | None]:
        return url.scheme, url.host, url.port or {"https": 443, "http": 80}.get(url.scheme)

    def _validate_api_url(self, url: httpx.URL) -> None:
        if url.userinfo or url.fragment or self._url_origin(url) != self._origin:
            raise CanvasError("refusing authenticated request outside configured Canvas origin")

    def _request_url(self, request: CanvasRequest[Any], *, absolute_url: str | None) -> httpx.URL:
        try:
            if absolute_url is not None:
                url = httpx.URL(absolute_url)
            else:
                path = request.path
                # URL joining preserves /api/v1 for endpoint-relative paths.
                url = httpx.URL(f"{self._config.api_base}/").join(path)
                filtered = [(k, _coerce(v)) for k, v in request.query_pairs() if v is not None]
                if filtered:
                    url = url.copy_with(query=urlencode(filtered, doseq=False).encode())
            self._validate_api_url(url)
            return url
        except httpx.InvalidURL as exc:
            raise CanvasError("invalid Canvas request URL") from exc

    def _validate_download_url(self, url: httpx.URL) -> str | None:
        if url.userinfo or url.fragment or not url.host:
            raise CanvasError("invalid file download URL")
        if self._url_origin(url) == self._origin:
            return None
        if url.scheme != "https":
            raise CanvasError("external file downloads require HTTPS")
        try:
            addresses = socket.getaddrinfo(url.host, url.port or 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise CanvasTransportError("could not resolve external download host") from exc
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise CanvasError("external download host must resolve only to public addresses")
        # Pin the checked address for the subsequent connection, closing the
        # validation/connect DNS-rebinding gap. TLS still verifies url.host.
        return addresses[0][4][0]

    def download(self, url: str, destination: str | Path, *, max_bytes: int | None = None) -> Path:
        """Stream to an atomic destination; authenticate only the Canvas origin.

        Signed external URLs and every redirect are validated independently.
        API credentials and session cookies are never forwarded to a CDN.
        """
        if max_bytes is not None and max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        try:
            current = httpx.URL(url)
        except httpx.InvalidURL as exc:
            raise CanvasError("invalid file download URL") from exc
        dest = Path(destination)
        temporary: Path | None = None
        seen: set[str] = set()
        try:
            for redirects in range(_MAX_REDIRECTS + 1):
                public_address = self._validate_download_url(current)
                if str(current) in seen:
                    raise CanvasError("file download redirect loop detected")
                seen.add(str(current))
                connect_url = current.copy_with(host=public_address) if public_address else current
                outgoing = self._client.build_request("GET", connect_url)
                for key in ("Authorization", "Cookie", "Proxy-Authorization"):
                    outgoing.headers.pop(key, None)
                if self._url_origin(current) == self._origin:
                    outgoing.headers["Authorization"] = f"Bearer {self._config.token}"
                else:
                    outgoing.headers["Host"] = current.netloc.decode("ascii")
                    outgoing.extensions["sni_hostname"] = current.host
                    # An IP-address pool key must not reuse TLS authenticated
                    # for another hostname that happens to share that IP.
                    outgoing.headers["Connection"] = "close"
                response = self._client.send(outgoing, auth=None, follow_redirects=False, stream=True)
                try:
                    if response.has_redirect_location:
                        if redirects == _MAX_REDIRECTS or response.next_request is None:
                            raise CanvasError("file download redirect limit exceeded")
                        current = current.join(response.headers["location"])
                        continue
                    if not response.is_success:
                        response.read()
                        raise parse_canvas_error(response.status_code, str(current), response.text, dict(response.headers))
                    length = response.headers.get("content-length")
                    if max_bytes is not None and length and length.isdigit() and int(length) > max_bytes:
                        raise CanvasError("file exceeds download byte limit")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.NamedTemporaryFile(dir=dest.parent, prefix=".canvas-download-", delete=False) as output:
                        temporary = Path(output.name)
                        total = 0
                        for chunk in response.iter_bytes():
                            total += len(chunk)
                            if max_bytes is not None and total > max_bytes:
                                raise CanvasError("file exceeds download byte limit")
                            output.write(chunk)
                    os.replace(temporary, dest)
                    temporary = None
                    return dest
                finally:
                    response.close()
        except httpx.RequestError as exc:
            raise CanvasTransportError(f"file download transport error: {type(exc).__name__}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        raise AssertionError("unreachable")


def _validate_pinned_links(header: str | None) -> None:
    """Do not mistake an unsupported Link representation for exhaustion.

    The legacy parser accepts Canvas's canonical quoted relation format. For
    safety-sensitive collection lookups require the entire header to follow
    that format; an unfamiliar format means incomplete coverage, not no next
    page. Other requests retain their existing parser behavior.
    """
    if not header or not header.strip():
        return
    cursor = 0
    relations: set[str] = set()
    for match in re.finditer(r'<[^<>]+>;\s*rel="([^"]+)"', header):
        separator = header[cursor:match.start()].strip()
        relation = match[1]
        if (separator != ("," if relations else "") or relation in relations or
                relation not in {"current", "next", "prev", "first", "last"}):
            raise CanvasError("Canvas pagination Link header is ambiguous or unsupported; collection is incomplete")
        relations.add(relation)
        cursor = match.end()
    if not relations or header[cursor:].strip():
        raise CanvasError("Canvas pagination Link header is malformed or unsupported; collection is incomplete")


def _coerce(v: Any) -> str:
    """Coerce query values to strings the way Canvas expects."""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)
