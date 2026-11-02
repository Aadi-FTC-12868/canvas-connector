"""Real HTTP dispatcher tests; multipart writes must never be replayed."""

import io
import socket
from email import policy
from email.parser import BytesParser

import httpx
import pytest
from canvas_py._config import CanvasConfig
from canvas_py._errors import CanvasError, CanvasRateLimitError, CanvasTransportError
from canvas_py._http import CanvasHTTP
from canvas_py._request import CanvasRequest

BASE = "https://school.example"


class AttachedEntry(CanvasRequest):
    method = "POST"
    allow_retries = False
    allow_redirects = False
    path = "courses/1/discussion_topics/2/entries"

    def form_body(self):
        return {"message": "<p>Reviewed message</p>"}

    def multipart_files(self):
        return {"attachment": ("résumé.txt", b"exact\x00bytes\r\n", "text/plain")}


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("Tests may use only MockTransport")
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fail)


def dispatch(handler, request=None):
    with (httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True,
                       auth=("must", "not-apply")) as client,
          CanvasHTTP(CanvasConfig(base_url=BASE, token="synthetic-token"), client=client,
                     rate_limit_base_delay=0) as canvas):
        return canvas.send(request or AttachedEntry())


def test_real_multipart_contains_exact_immutable_bytes_and_message():
    seen = []
    def handler(request):
        seen.append(request)
        assert str(request.url) == BASE + "/api/v1/courses/1/discussion_topics/2/entries"
        assert request.headers["authorization"] == "Bearer synthetic-token"
        mime = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content)
        parts = {part.get_param("name", header="content-disposition"): part for part in mime.iter_parts()}
        assert set(parts) == {"message", "attachment"}
        assert parts["message"].get_payload(decode=True) == b"<p>Reviewed message</p>"
        assert parts["attachment"].get_payload(decode=True) == b"exact\x00bytes\r\n"
        assert parts["attachment"].get_filename() == "résumé.txt"
        assert parts["attachment"].get_content_type() == "text/plain"
        return httpx.Response(201, json={"id": 4})
    assert dispatch(handler) == {"id": 4}
    assert len(seen) == 1


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("destination", [BASE + "/replay", "https://external.example/steal"])
def test_redirect_never_replays_or_forwards_file_and_auth(status, destination):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(status, headers={"Location": destination})
    with pytest.raises(CanvasError, match="redirects are disabled"):
        dispatch(handler)
    assert len(seen) == 1
    assert seen[0].url.host == "school.example"


@pytest.mark.parametrize("status", [403, 429])
def test_rate_limit_mutation_is_one_attempt(status):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(status, headers={"Retry-After": "0"}, json={"message": "Rate Limit Exceeded"})
    with pytest.raises(CanvasRateLimitError):
        dispatch(handler)
    assert len(seen) == 1


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout, httpx.WriteError])
def test_transport_failure_never_replays(error_type):
    seen = []
    def handler(request):
        seen.append(request)
        raise error_type("synthetic failure", request=request)
    with pytest.raises(CanvasTransportError):
        dispatch(handler)
    assert len(seen) == 1


@pytest.mark.parametrize("files", [None, {"attachment": ("x", b"x", "text/plain")}])
def test_conflicting_json_and_form_or_multipart_rejected_before_io(files):
    request = AttachedEntry()
    request.json_body = lambda: {"message": "bad mixed body"}
    request.multipart_files = lambda: files
    with pytest.raises(ValueError, match="cannot combine"):
        dispatch(lambda _: pytest.fail("No I/O for invalid body"), request)


@pytest.mark.parametrize("files", [
    {}, [], {"attachment": ("x", bytearray(b"x"), "text/plain")},
    {"attachment": ("x", io.BytesIO(b"x"), "text/plain")},
    {"attachment": ("x", "/local/path", "text/plain")},
    {"attachment": ("x\nHeader: bad", b"x", "text/plain")},
    {"attachment": ("x", b"x", "text/plain\r\nHeader: bad")},
    {"bad\nfield": ("x", b"x", "text/plain")},
])
def test_only_immutable_valid_file_parts_allowed(files):
    request = AttachedEntry()
    request.multipart_files = lambda: files
    with pytest.raises(ValueError, match="multipart"):
        dispatch(lambda _: pytest.fail("No I/O for invalid file parts"), request)


def test_untrusted_origin_rejected_before_file_or_auth_sent():
    request = AttachedEntry()
    request.path = "https://external.example/steal"
    with pytest.raises(CanvasError, match="outside configured Canvas origin"):
        dispatch(lambda _: pytest.fail("No I/O outside Canvas"), request)


def test_legacy_get_retains_rate_limit_retry_and_same_origin_redirect():
    type_with_path = type("Read", (CanvasRequest,), {"path": "users/self/profile"})
    request = type_with_path()
    seen = []
    def handler(outgoing):
        seen.append(outgoing)
        if len(seen) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "Rate Limit Exceeded"})
        if len(seen) == 2:
            return httpx.Response(302, headers={"Location": BASE + "/api/v1/redirected"})
        return httpx.Response(200, json={"id": 7})
    assert dispatch(handler, request) == {"id": 7}
    assert len(seen) == 3
    assert all(item.method == "GET" and item.headers["authorization"] == "Bearer synthetic-token" for item in seen)
