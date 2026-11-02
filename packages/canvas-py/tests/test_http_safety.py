"""Security and bounded-failure tests; all HTTP uses an in-memory transport."""
from __future__ import annotations

import socket

import httpx
import pytest

from canvas_py._config import CanvasConfig
from canvas_py._errors import CanvasError
from canvas_py._http import CanvasHTTP
from canvas_py.endpoints.courses import ListCourses


def dispatcher(handler, **kwargs):
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return CanvasHTTP(CanvasConfig(base_url="https://canvas.test", token="secret"), client=client, **kwargs)


@pytest.mark.parametrize("url", [
    "https://evil.test/api/v1/courses", "http://canvas.test/api/v1/courses",
    "https://canvas.test:444/api/v1/courses", "https://secret@canvas.test/api/v1/courses",
    "//evil.test/api/v1/courses",
])
def test_absolute_requests_cannot_escape_canvas_origin(url):
    requests = []
    http = dispatcher(lambda req: requests.append(req) or httpx.Response(200, json=[]))
    with pytest.raises(CanvasError, match="origin"):
        http.send_absolute(ListCourses(), absolute_url=url)
    assert requests == []


def test_cross_origin_pagination_is_blocked_before_second_request():
    requests = []
    http = dispatcher(lambda req: requests.append(req) or httpx.Response(200, json=[{"id": 1}], headers={"Link": '<https://evil.test/courses?page=2>; rel="next"'}))
    with pytest.raises(CanvasError, match="origin"):
        http.exhaust(ListCourses())
    assert len(requests) == 1
    assert requests[0].headers["Authorization"] == "Bearer secret"


def test_api_redirect_cannot_forward_bearer_to_other_origin():
    requests = []
    http = dispatcher(lambda req: requests.append(req) or httpx.Response(302, headers={"Location": "https://evil.test/data"}))
    with pytest.raises(CanvasError, match="origin"):
        http.send(ListCourses())
    assert len(requests) == 1


def test_same_origin_redirect_preserves_authentication():
    requests = []
    def handler(req):
        requests.append(req)
        if len(requests) == 1:
            return httpx.Response(302, headers={"Location": "/api/v1/redirected"})
        return httpx.Response(200, json=[])
    assert dispatcher(handler).send(ListCourses()) == []
    assert len(requests) == 2
    assert all(req.headers["Authorization"] == "Bearer secret" for req in requests)


def test_repeated_pagination_link_fails_without_repeating_same_page():
    requests = []
    url = "https://canvas.test/api/v1/courses?page=2"
    http = dispatcher(lambda req: requests.append(req) or httpx.Response(200, json=[{"id": len(requests)}], headers={"Link": f'<{url}>; rel="next"'}))
    with pytest.raises(CanvasError, match="loop"):
        http.exhaust(ListCourses())
    assert len(requests) == 2


def test_unique_unending_pagination_has_a_page_cap():
    requests = []
    def handler(req):
        requests.append(req)
        return httpx.Response(200, json=[], headers={"Link": f'<https://canvas.test/api/v1/courses?page={len(requests)+1}>; rel="next"'})
    with pytest.raises(CanvasError, match="page limit"):
        dispatcher(handler, max_pages=3).exhaust(ListCourses())
    assert len(requests) == 3


@pytest.mark.parametrize("retry_after,expected", [("999999", 30.0), ("-10", 1.0), ("nan", 1.0), ("inf", 1.0), ("0", 0.0)])
def test_retry_wait_is_finite_and_bounded(retry_after, expected, monkeypatch):
    sleeps = []
    monkeypatch.setattr("canvas_py._http.time.sleep", sleeps.append)
    def handler(req):
        if not sleeps:
            return httpx.Response(429, json={"message": "rate limited"}, headers={"Retry-After": retry_after})
        return httpx.Response(200, json=[])
    assert dispatcher(handler).send(ListCourses()) == []
    assert sleeps == [expected]


def resolve_public(monkeypatch):
    monkeypatch.setattr("canvas_py._http.socket.getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])


def test_signed_download_redirect_strips_bearer_and_cookie(tmp_path, monkeypatch):
    resolve_public(monkeypatch)
    requests = []
    def handler(req):
        requests.append(req)
        if len(requests) == 1:
            return httpx.Response(302, headers={"Location": "https://cdn.test/file?signature=example", "Set-Cookie": "canvas_session=secret; Path=/"})
        return httpx.Response(200, content=b"file contents")
    http = dispatcher(handler)
    http._client.headers["Authorization"] = "Bearer stale-client-token"
    http._client.headers["Cookie"] = "stale-cookie=secret"
    dest = http.download("https://canvas.test/files/1/download", tmp_path / "file")
    assert dest.read_bytes() == b"file contents"
    assert requests[0].headers["Authorization"] == "Bearer secret"
    assert "authorization" not in requests[1].headers
    assert "cookie" not in requests[1].headers
    assert requests[1].url.host == "93.184.216.34"
    assert requests[1].headers["Host"] == "cdn.test"
    assert requests[1].extensions["sni_hostname"] == "cdn.test"
    assert requests[1].headers["Connection"] == "close"


def test_external_relative_redirect_retains_logical_hostname(tmp_path, monkeypatch):
    resolve_public(monkeypatch)
    requests = []
    def handler(req):
        requests.append(req)
        if len(requests) == 1:
            return httpx.Response(302, headers={"Location": "/second?signature=example"})
        return httpx.Response(200, content=b"ok")
    dispatcher(handler).download("https://cdn.test/first", tmp_path / "file")
    assert len(requests) == 2
    assert requests[1].url.path == "/second"
    assert all(req.url.host == "93.184.216.34" for req in requests)
    assert all(req.headers["Host"] == "cdn.test" for req in requests)


def test_external_download_never_has_default_bearer(tmp_path, monkeypatch):
    resolve_public(monkeypatch)
    requests = []
    http = dispatcher(lambda req: requests.append(req) or httpx.Response(200, content=b"ok"))
    http._client.headers["Authorization"] = "Bearer inherited"
    http.download("https://cdn.test/file", tmp_path / "file")
    assert "authorization" not in requests[0].headers


@pytest.mark.parametrize("address", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "fe80::1"])
def test_private_external_download_destination_is_rejected_before_io(tmp_path, monkeypatch, address):
    monkeypatch.setattr("canvas_py._http.socket.getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))])
    requests = []
    http = dispatcher(lambda req: requests.append(req) or httpx.Response(200, content=b"bad"))
    with pytest.raises(CanvasError, match="public addresses"):
        http.download("https://external.test/file", tmp_path / "file")
    assert requests == []
    assert not (tmp_path / "file").exists()


def test_streamed_byte_limit_preserves_existing_destination(tmp_path):
    dest = tmp_path / "file"
    dest.write_bytes(b"original")
    http = dispatcher(lambda req: httpx.Response(200, stream=httpx.ByteStream(b"too large")))
    with pytest.raises(CanvasError, match="byte limit"):
        http.download("https://canvas.test/files/1", dest, max_bytes=3)
    assert dest.read_bytes() == b"original"
    assert list(tmp_path.glob(".canvas-download-*")) == []
