import socket
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from cryptography.fernet import Fernet
from httpcore._backends.sync import SyncBackend

from canvas_connector.security import PublicNetworkBackend, secure_http_client, validate_canvas_origin, validate_public_url
from canvas_connector.storage import Store


@pytest.mark.parametrize("url", ["http://school.example", "https://127.0.0.1", "https://[::1]", "https://169.254.169.254", "https://localhost", "https://example.com:8443", "https://a:b@example.com", "https://example.com/#x"])
def test_rejects_unsafe_urls(url):
    with pytest.raises(ValueError):
        validate_public_url(url)


def test_origin_allowlist():
    assert validate_canvas_origin("https://School.Example/") == "https://school.example"
    with pytest.raises(ValueError):
        validate_canvas_origin("https://school.example", ["https://other.example"])
    with pytest.raises(ValueError):
        validate_canvas_origin("https://school.example/path")


def test_socket_pins_validated_ip_and_blocks_mixed_answers(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
    seen = []
    monkeypatch.setattr(SyncBackend, "connect_tcp", lambda self, host, *a, **kw: seen.append(host) or "stream")
    assert PublicNetworkBackend().connect_tcp("school.example", 443) == "stream"
    assert seen == ["8.8.8.8"]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)), (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
    with pytest.raises(httpx.ConnectError):
        PublicNetworkBackend().connect_tcp("rebind.example", 443)
    assert seen == ["8.8.8.8"]


def test_secure_http_adapter_constructs():
    with secure_http_client() as client:
        assert isinstance(client._transport._pool._network_backend, PublicNetworkBackend)
        assert not client.follow_redirects


def test_store_consume_atomic_and_ciphertext(tmp_path):
    path = tmp_path / "auth.db"
    store = Store(path, Fernet.generate_key())
    store.put("code", "one-use-secret", {"credential": "canvas-secret"}, expires=9999999999)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.consume("code", "one-use-secret"), range(8)))
    assert sum(result is not None for result in results) == 1
    assert b"canvas-secret" not in path.read_bytes()
    assert b"one-use-secret" not in path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    store.close()


def test_guarded_canvas_guards_anonymous_uploads_and_download_client(monkeypatch):
    from canvas_py._config import CanvasConfig
    from canvas_py._errors import CanvasTransportError
    from canvas_py._uploads import _storage_upload
    from canvas_py.models.upload import UploadTarget
    from canvas_connector.security import guarded_canvas
    from io import BytesIO

    canvas = guarded_canvas(CanvasConfig("https://school.example", "synthetic-token"))
    try:
        assert isinstance(canvas._http._client._transport._pool._network_backend, PublicNetworkBackend)
        # Same-origin upload validation in the library intentionally trusts local
        # installs. Hosted anonymous transfer still resolves/pins public IPs.
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
        with pytest.raises(CanvasTransportError):
            _storage_upload(canvas._http,
                UploadTarget(upload_url="https://school.example/upload", upload_params={}),
                BytesIO(b"synthetic coursework"), filename="sample.txt", content_type="text/plain", timeout=1)
    finally:
        canvas._http.close()
