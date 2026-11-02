"""Native HTTP failures must never silently replay a Canvas write."""
from __future__ import annotations

import httpx
import pytest

from canvas_py._config import CanvasConfig
from canvas_py._errors import CanvasError, CanvasRateLimitError, CanvasTransportError
from canvas_py._http import CanvasHTTP
from canvas_py._request import CanvasRequest
from canvas_py.endpoints.file_uploads import ConfirmUpload


class Operation(CanvasRequest):
    # Deliberately inherit True flags: callers must not need to remember to
    # override every old or newly added mutation endpoint to prevent replay.
    def __init__(self, method="POST", encoding="json"):
        self.method, self.encoding = method, encoding

    @property
    def path(self):
        return "synthetic/operation"

    def json_body(self):
        return {"message": "one reviewed action"} if self.encoding == "json" else None

    def form_body(self):
        return {"message": "one reviewed action"} if self.encoding in {"form", "multipart"} else None

    def multipart_files(self):
        return {"attachment": ("example.txt", b"one file", "text/plain")} if self.encoding == "multipart" else None


def dispatcher(handler):
    # Even an injected client configured to follow redirects must not do so
    # for a mutation; CanvasHTTP owns dispatch policy.
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return CanvasHTTP(CanvasConfig("https://canvas.example", "synthetic-token"), client=client,
                      rate_limit_retries=3, rate_limit_base_delay=0)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "post"])
@pytest.mark.parametrize("status", [429, 403])
def test_rate_limited_writes_are_dispatched_once_despite_inherited_flags(method, status, monkeypatch):
    requests, sleeps = [], []
    monkeypatch.setattr("canvas_py._http.time.sleep", sleeps.append)

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(status, json={"errors": [{"message": "Rate Limit Exceeded"}]},
                                  headers={"Retry-After": "0"})
        return httpx.Response(200, json={"id": 2})

    operation = Operation(method)
    assert operation.allow_retries is True
    with pytest.raises(CanvasRateLimitError):
        dispatcher(handler).send(operation)
    assert len(requests) == 1 and sleeps == []


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_write_redirects_neither_replay_nor_change_method(method, status):
    requests = []

    def handler(request):
        requests.append(request)
        return (httpx.Response(status, headers={"Location": "/api/v1/synthetic/redirected"})
                if len(requests) == 1 else httpx.Response(200, json={"id": 2}))

    with pytest.raises(CanvasError, match="redirects are disabled") as error:
        dispatcher(handler).send(Operation(method))
    assert error.value.status == status
    assert [(r.method, r.url.path) for r in requests] == [(method, "/api/v1/synthetic/operation")]


@pytest.mark.parametrize("encoding", ["json", "form", "multipart"])
@pytest.mark.parametrize("failure", ["rate_limit", "redirect", "timeout", "disconnect", "server_error"])
def test_transport_policy_covers_all_mutation_encodings(encoding, failure):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) > 1:
            return httpx.Response(200, json={"id": 2})
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic timeout after accepting bytes", request=request)
        if failure == "disconnect":
            raise httpx.RemoteProtocolError("synthetic disconnect after accepting bytes", request=request)
        if failure == "redirect":
            return httpx.Response(307, headers={"Location": "/api/v1/synthetic/redirected"})
        return httpx.Response(429 if failure == "rate_limit" else 503, json={"message": "uncertain response"},
                              headers={"Retry-After": "0"})

    error = CanvasTransportError if failure in {"timeout", "disconnect"} else CanvasError
    with pytest.raises(error):
        dispatcher(handler).send(Operation(encoding=encoding))
    assert len(requests) == 1 and b"one reviewed action" in requests[0].content.replace(b"+", b" ")
    if encoding == "multipart":
        assert b"one file" in requests[0].content


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_read_retry_and_same_origin_redirect_remain_supported(method, monkeypatch):
    requests, sleeps = [], []
    monkeypatch.setattr("canvas_py._http.time.sleep", sleeps.append)

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, json={"message": "rate limited"}, headers={"Retry-After": "0"})
        if len(requests) == 2:
            return httpx.Response(307, headers={"Location": "/api/v1/synthetic/redirected"})
        return httpx.Response(200)

    assert dispatcher(handler).send(Operation(method, encoding="none")) is None
    assert [r.method for r in requests] == [method] * 3 and sleeps == [0]
    assert requests[-1].url.path == "/api/v1/synthetic/redirected"


@pytest.mark.parametrize("status", [429, 307])
def test_side_effecting_get_upload_confirmation_keeps_explicit_opt_out(status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"message": "rate limited"},
                              headers={"Retry-After": "0", "Location": "/api/v1/files/1/create_success"})

    url = "https://canvas.example/api/v1/files/1/create_success"
    with pytest.raises(CanvasError):
        dispatcher(handler).send_absolute(ConfirmUpload(url), absolute_url=url)
    assert len(requests) == 1 and requests[0].method == "GET"
