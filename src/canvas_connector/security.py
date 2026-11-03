"""Hosted egress validation, including DNS pinning at socket creation."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

import httpx
from httpcore._backends.sync import SyncBackend


def validate_public_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (ValueError, TypeError) as exc:
        raise ValueError("Expected a public HTTPS URL") from exc
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or port not in (None, 443)
            or parsed.fragment or "\\" in url or any(c.isspace() for c in url)):
        raise ValueError("Expected a public HTTPS URL without credentials")
    host = parsed.hostname
    if host.lower() in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        raise ValueError("Private destinations are unavailable in hosted mode")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return url
    if not address.is_global:
        raise ValueError("Private destinations are unavailable in hosted mode")
    return url


def validate_canvas_origin(origin: str, allowed_origins: list[str] | tuple[str, ...] | None = None) -> str:
    validate_public_url(origin)
    parsed = urlsplit(origin)
    if parsed.path not in ("", "/") or parsed.query:
        raise ValueError("Canvas origin must not include a path or query")
    normalized = f"https://{parsed.netloc.lower()}".removesuffix(":443")
    if allowed_origins is not None and normalized not in allowed_origins:
        raise ValueError("This institution has not been configured by the server operator")
    return normalized


class PublicNetworkBackend(SyncBackend):
    """Resolve once, reject mixed/private answers, connect to the validated IP.

    httpcore retains the original hostname for TLS certificate checks and SNI.
    This backend intentionally fails closed for Unix sockets and private IPs.
    """
    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if port != 443:
            raise httpx.ConnectError("Hosted egress requires HTTPS on port 443")
        try:
            answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
        except OSError as exc:
            raise httpx.ConnectError("Could not resolve destination") from exc
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise httpx.ConnectError("Destination must resolve only to public addresses")
        last = None
        for address in addresses:
            try:
                return super().connect_tcp(address, port, timeout, local_address, socket_options)
            except Exception as exc:
                last = exc
        raise httpx.ConnectError("Could not connect to destination") from last

    def connect_unix_socket(self, *args, **kwargs):
        raise httpx.ConnectError("Unix sockets are unavailable in hosted mode")


def secure_http_client(*, base_url: str = "", timeout: float = 30, request_gate=None) -> httpx.Client:
    # httpx 0.28/httpcore 1.x adapter; covered by a construction test.
    transport = httpx.HTTPTransport(retries=0)
    transport._pool._network_backend = PublicNetworkBackend()
    if request_gate is not None:
        transport = request_gate.wrap(transport)
    return httpx.Client(base_url=base_url, transport=transport, trust_env=False,
                        follow_redirects=False, timeout=timeout,
                        event_hooks={"request": [lambda req: validate_public_url(str(req.url))]})


def guarded_canvas(config, *, request_gate=None):
    from canvas_py import Canvas
    client = (secure_http_client(base_url=config.api_base) if request_gate is None else
              secure_http_client(base_url=config.api_base, request_gate=request_gate))
    canvas = Canvas(config, http_client=client)
    # The injected client is created solely for this Canvas instance.
    canvas._http._owns_client = True
    # Multipart storage transfers deliberately use an anonymous client. Guard
    # that separate transport too, including same-origin Canvas upload targets.
    canvas._http._anonymous_client_factory = (
        secure_http_client if request_gate is None else
        lambda **kwargs: secure_http_client(request_gate=request_gate, **kwargs))
    return canvas
