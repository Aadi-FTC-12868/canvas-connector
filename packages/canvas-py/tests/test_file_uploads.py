"""Uploads are tested only with mock transport; never create live test files."""
from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path

import httpx
import pytest
import respx

from canvas_py import Canvas, CanvasError, CanvasTransportError, UploadContext
from canvas_py._config import CanvasConfig
from canvas_py._uploads import UploadVerificationError
from canvas_py.endpoints.file_uploads import ConfirmUpload, RequestUploadTarget

BASE = "https://canvas.test"
STORAGE = "https://s3.test/bucket/put"
PINNED_STORAGE = "https://93.184.216.34/bucket/put"
FILE = {"id": 999, "display_name": "hello.txt", "filename": "hello.txt", "content-type": "text/plain", "size": 5}


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "hello.txt"
    path.write_bytes(b"hello")
    return path


@pytest.fixture
def canvas():
    with Canvas(CanvasConfig(base_url=BASE, token="FAKE")) as client:
        yield client


@pytest.fixture(autouse=True)
def public_storage_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])


def target(context="users/self", *, url=STORAGE, params=None):
    return respx.post(f"{BASE}/api/v1/{context}/files").respond(
        200, json={"upload_url": url, "upload_params": params if params is not None else {"key": "opaque", "policy": "SECRET-POLICY", "signature": "SIG"}},
    )


def metadata(**changes):
    return respx.get(f"{BASE}/api/v1/files/999").respond(200, json=FILE | changes)


@pytest.mark.parametrize(("context", "path"), [
    (UploadContext.user(), "users/self/files"),
    (UploadContext.course(2995), "courses/2995/files"),
    (UploadContext.assignment_submission(2995, 100), "courses/2995/assignments/100/submissions/self/files"),
    (UploadContext.submission_comment(2995, 100), "courses/2995/assignments/100/submissions/self/comments/files"),
    (UploadContext.group(42), "groups/42/files"),
])
def test_context_paths(context, path):
    assert context.endpoint_path() == path


@pytest.mark.parametrize("context", [UploadContext(kind="course"), UploadContext.course(0), UploadContext.group(-1), UploadContext.course(True), UploadContext.assignment_submission(1, "2/3")])
def test_context_ids_are_required_positive_integers(context):
    with pytest.raises(ValueError):
        context.endpoint_path()


def test_request_shape_and_confirmation_method():
    req = RequestUploadTarget(UploadContext.user(), name="test.pdf", size=12, content_type="application/pdf")
    assert req.path == "users/self/files"
    assert req.json_body() == {"name": "test.pdf", "size": 12, "content_type": "application/pdf", "on_duplicate": "rename"}
    assert ConfirmUpload(BASE).method == "GET"
    assert ConfirmUpload(BASE, method="POST").headers() == {"Content-Length": "0"}


@pytest.mark.parametrize("kwargs", [
    {"name": ""}, {"name": "../secret.txt"}, {"name": "a\nb"}, {"size": -1}, {"size": True},
    {"on_duplicate": "anything"}, {"parent_folder_id": -2},
    {"parent_folder_id": 1, "parent_folder_path": "x"},
    {"content_type": "text/plain\r\nAuthorization: broken"},
])
def test_request_validation(kwargs):
    with pytest.raises(ValueError):
        RequestUploadTarget(UploadContext.user(), **({"name": "hello.txt"} | kwargs))


def test_submission_context_rejects_custom_folder():
    with pytest.raises(ValueError, match="folder"):
        RequestUploadTarget(UploadContext.assignment_submission(1, 2), name="hello.txt", parent_folder_id=3)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 201])
@respx.mock
def test_redirect_upload_finishes_with_get_and_metadata(source, canvas, status):
    step1 = target()
    location = f"{BASE}/api/v1/files/999/create_success?uuid=PRIVATE"
    transfer = respx.post(PINNED_STORAGE).respond(status, headers={"Location": location})
    confirmation = respx.get(location).respond(200, json=FILE)
    verify = metadata()
    result = canvas.files.upload(source, context=UploadContext.user(), expected_size=5, expected_sha256=hashlib.sha256(b"hello").hexdigest())
    assert result.id == 999 and result.size == 5
    assert [route.call_count for route in (step1, transfer, confirmation, verify)] == [1, 1, 1, 1]
    assert json.loads(step1.calls.last.request.content) == {"name": "hello.txt", "size": 5, "content_type": "text/plain", "on_duplicate": "rename"}
    upload_request = transfer.calls.last.request
    assert "authorization" not in upload_request.headers and "cookie" not in upload_request.headers
    assert upload_request.headers["host"] == "s3.test"
    assert upload_request.extensions["sni_hostname"] == "s3.test"
    body = upload_request.content
    assert body.index(b'name="signature"') < body.index(b'name="file"')
    assert b"SECRET-POLICY" in body and b"hello" in body
    assert confirmation.calls.last.request.headers["authorization"] == "Bearer FAKE"
    assert verify.calls.last.request.headers["authorization"] == "Bearer FAKE"


@pytest.mark.parametrize("status", [200, 201])
@respx.mock
def test_direct_json_success_and_canvas_rename(source, canvas, status):
    target()
    respx.post(PINNED_STORAGE).respond(status, json=FILE | {"display_name": "hello-1.txt"})
    metadata(display_name="hello-1.txt")
    assert canvas.files.upload(source, context=UploadContext.user()).display_name == "hello-1.txt"


@respx.mock
def test_same_origin_storage_has_no_canvas_auth_or_cookies(source, canvas):
    url = f"{BASE}/files_api"
    target(url=url)
    canvas._http._client.cookies.set("private-session", "PRIVATE")
    transfer = respx.post(url).respond(201, json=FILE)
    metadata()
    canvas.files.upload(source, context=UploadContext.user())
    assert "authorization" not in transfer.calls.last.request.headers
    assert "cookie" not in transfer.calls.last.request.headers


@respx.mock
def test_relative_canvas_confirmation(source, canvas):
    url = f"{BASE}/storage/upload"
    target(url=url)
    respx.post(url).respond(302, headers={"Location": "/api/v1/files/999/create_success?uuid=PRIVATE"})
    respx.get(f"{BASE}/api/v1/files/999/create_success?uuid=PRIVATE").respond(200, json=FILE)
    metadata()
    assert canvas.files.upload(source, context=UploadContext.user()).id == 999


@respx.mock
def test_legacy_post_only_after_method_rejection(source, canvas):
    target()
    location = f"{BASE}/api/v1/files/999/create_success?uuid=PRIVATE"
    respx.post(PINNED_STORAGE).respond(302, headers={"Location": location})
    respx.get(location).respond(405, json={"message": "Method not allowed"})
    post = respx.post(location).respond(200, json=FILE)
    metadata()
    canvas.files.upload(source, context=UploadContext.user())
    assert post.calls.last.request.headers["content-length"] == "0"
    assert post.calls.last.request.headers["authorization"] == "Bearer FAKE"
    assert post.calls.last.request.content == b""


@respx.mock
def test_failed_confirmation_does_not_repeat_upload(source, canvas):
    target()
    location = f"{BASE}/api/v1/files/999/create_success?uuid=PRIVATE"
    transfer = respx.post(PINNED_STORAGE).respond(302, headers={"Location": location})
    respx.get(location).respond(500, text="PRIVATE storage details")
    with pytest.raises(UploadVerificationError) as err:
        canvas.files.upload(source, context=UploadContext.user())
    assert err.value.file_id == 999 and "PRIVATE" not in str(err.value)
    assert err.value.url is None and err.value.body is None
    assert transfer.call_count == 1


@pytest.mark.parametrize("location", [
    "https://evil.test/api/v1/files/999/create_success?secret=x",
    "http://canvas.test/api/v1/files/999/create_success",
    "https://canvas.test:444/api/v1/files/999/create_success",
    "https://username:password@canvas.test/api/v1/files/999/create_success",
    f"{BASE}/api/v1/files/999/create_success#fragment",
    f"{BASE}/api/v1/courses/1/assignments/2/submissions",
    "/api/v1/files/999/create_success",  # relative to external storage, not Canvas
])
@respx.mock
def test_unsafe_finalization_never_receives_request(source, canvas, location):
    target()
    respx.post(PINNED_STORAGE).respond(302, headers={"Location": location})
    with pytest.raises(UploadVerificationError, match="confirmation location"):
        canvas.files.upload(source, context=UploadContext.user())
    assert len(respx.calls) == 2


@pytest.mark.parametrize("url", ["http://s3.test/upload", "https://u:p@s3.test/upload", "https://s3.test/upload#frag", "file:///tmp/upload"])
@respx.mock
def test_unsafe_storage_target_rejected(source, canvas, url):
    target(url=url)
    with pytest.raises(CanvasError, match="safety"):
        canvas.files.upload(source, context=UploadContext.user())
    assert len(respx.calls) == 1


@respx.mock
def test_private_storage_dns_rejected(source, canvas, monkeypatch):
    target()
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(CanvasError, match="safety"):
        canvas.files.upload(source, context=UploadContext.user())
    assert len(respx.calls) == 1


@pytest.mark.parametrize("body", [b"", b"not json", b'{"id":999}', b'{"id":0,"size":5}', b'{"id":"bad","size":5}'])
@respx.mock
def test_missing_or_invalid_final_response_is_not_success(source, canvas, body):
    target()
    respx.post(PINNED_STORAGE).respond(201, content=body)
    # An ID-only body is acceptable only if a fresh metadata read verifies it.
    if body == b'{"id":999}':
        respx.get(f"{BASE}/api/v1/files/999").respond(200, json={"id": 999})
    with pytest.raises(UploadVerificationError):
        canvas.files.upload(source, context=UploadContext.user())


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
@respx.mock
def test_storage_error_has_no_sensitive_body_and_no_retry(source, canvas, status):
    target()
    transfer = respx.post(PINNED_STORAGE).respond(status, text="SECRET upload signature")
    with pytest.raises(UploadVerificationError) as err:
        canvas.files.upload(source, context=UploadContext.user())
    assert err.value.status == status and "SECRET" not in str(err.value)
    assert err.value.url is None and err.value.body is None
    assert transfer.call_count == 1


@respx.mock
def test_transport_error_is_redacted_and_not_retried(source, canvas):
    target()
    transfer = respx.post(PINNED_STORAGE).mock(side_effect=httpx.ReadTimeout("SECRET signed url"))
    with pytest.raises(CanvasTransportError) as err:
        canvas.files.upload(source, context=UploadContext.user())
    assert "outcome unknown" in str(err.value) and "SECRET" not in str(err.value)
    assert transfer.call_count == 1


@respx.mock
def test_quota_rejected_before_transferring_bytes(source, canvas):
    respx.post(f"{BASE}/api/v1/users/self/files").respond(400, json={"message": "file size exceeds quota"})
    with pytest.raises(CanvasError, match="quota"):
        canvas.files.upload(source, context=UploadContext.user())
    assert len(respx.calls) == 1


@pytest.mark.parametrize("payload", [{"message": "file size exceeds quota"}, {"upload_url": "SECRET signed URL"}, {"upload_url": STORAGE, "upload_params": {"file": "injected"}}, {"upload_url": STORAGE, "upload_params": {"k": {"nested": "SECRET"}}}])
@respx.mock
def test_invalid_target_is_redacted(source, canvas, payload):
    respx.post(f"{BASE}/api/v1/users/self/files").respond(200, json=payload)
    with pytest.raises(CanvasError) as err:
        canvas.files.upload(source, context=UploadContext.user())
    assert "SECRET" not in str(err.value) and len(respx.calls) == 1


@pytest.mark.parametrize("kwargs", [{"expected_size": 4}, {"expected_size": 6}, {"expected_sha256": "0" * 64}, {"expected_sha256": "invalid"}])
@respx.mock
def test_reviewed_bytes_must_match_before_any_network_write(source, canvas, kwargs):
    with pytest.raises(ValueError):
        canvas.files.upload(source, context=UploadContext.user(), **kwargs)
    assert len(respx.calls) == 0


@respx.mock
def test_source_change_after_validation_cannot_change_uploaded_bytes(source, canvas):
    def on_prepare(request):
        source.write_bytes(b"replacement")
        return httpx.Response(200, json={"upload_url": STORAGE, "upload_params": {"key": "opaque"}})
    respx.post(f"{BASE}/api/v1/users/self/files").mock(side_effect=on_prepare)
    transfer = respx.post(PINNED_STORAGE).respond(200, json=FILE)
    metadata()
    canvas.files.upload(source, context=UploadContext.user(), expected_sha256=hashlib.sha256(b"hello").hexdigest())
    assert b"hello" in transfer.calls.last.request.content
    assert b"replacement" not in transfer.calls.last.request.content


@pytest.mark.parametrize("changes", [{"id": 1000}, {"size": 4}, {"size": None}])
@respx.mock
def test_metadata_identity_and_size_must_match(source, canvas, changes):
    target()
    respx.post(PINNED_STORAGE).respond(201, json=FILE)
    metadata(**changes)
    with pytest.raises(UploadVerificationError) as err:
        canvas.files.upload(source, context=UploadContext.user())
    assert err.value.file_id == 999


@respx.mock
def test_metadata_read_failure_preserves_file_id(source, canvas):
    target()
    respx.post(PINNED_STORAGE).respond(201, json=FILE)
    respx.get(f"{BASE}/api/v1/files/999").respond(403, json={"message": "not accessible"})
    with pytest.raises(UploadVerificationError) as err:
        canvas.files.upload(source, context=UploadContext.user())
    assert err.value.file_id == 999


@respx.mock
def test_folder_id_verified(source, canvas):
    step1 = target()
    respx.post(PINNED_STORAGE).respond(201, json=FILE)
    metadata(folder_id=123)
    canvas.files.upload(source, context=UploadContext.user(), parent_folder_id=123)
    assert json.loads(step1.calls.last.request.content)["parent_folder_id"] == 123


@respx.mock
def test_assignment_attachment_upload_does_not_submit(source, canvas):
    step1 = target("courses/1/assignments/2/submissions/self")
    respx.post(PINNED_STORAGE).respond(201, json=FILE)
    metadata()
    canvas.files.upload(source, context=UploadContext.assignment_submission(1, 2))
    body = json.loads(step1.calls.last.request.content)
    assert "url" not in body and "submit_assignment" not in body
    assert len(respx.calls) == 3  # only prepare, transfer, and metadata read


@respx.mock
def test_caller_can_skip_metadata_but_not_basic_identity_check(source, canvas):
    target()
    respx.post(PINNED_STORAGE).respond(201, json=FILE)
    assert canvas.files.upload(source, context=UploadContext.user(), verify=False).id == 999
    assert len(respx.calls) == 2
