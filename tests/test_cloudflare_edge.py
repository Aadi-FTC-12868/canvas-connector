import hashlib
import hmac

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from canvas_connector.edge import CloudflareEdgeMiddleware

SECRET = "synthetic-gateway-secret-for-test-123456"


def headers(*, method="GET", target="/health?x=1", address="203.0.113.9", timestamp="1000"):
    message = f"v1\n{timestamp}\n{address}\n{method}\n{target}"
    return {"x-canvas-edge-ip": address, "x-canvas-edge-time": timestamp,
            "x-canvas-edge-signature": hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()}


@pytest.fixture
def client():
    async def observed(request):
        return JSONResponse({"client": request.client.host, "headers": dict(request.headers), "scheme": request.url.scheme})
    app = Starlette(routes=[Route("/health", observed, methods=["GET", "POST"]), Route("/é", observed)])
    app.add_middleware(CloudflareEdgeMiddleware, secret=SECRET, clock=lambda: 1000)
    with TestClient(app, base_url="https://canvas.example") as client:
        yield client


def test_valid_gateway_ip_is_used_and_untrusted_metadata_is_removed(client):
    h = headers()
    h.update({"x-forwarded-for": "attacker", "Forwarded": "for=attacker"})
    response = client.get("/health?x=1", headers=h)
    assert response.status_code == 200
    assert response.json()["client"] == "203.0.113.9"
    assert not any(x.startswith(("x-canvas-edge-", "x-forwarded-")) or x == "forwarded"
                   for x in response.json()["headers"])


@pytest.mark.parametrize("change", [
    {"x-canvas-edge-time": "940"}, {"x-canvas-edge-ip": "198.51.100.9"},
    {"x-canvas-edge-signature": "0" * 64}, {"x-canvas-edge-time": "1000.0"},
])
def test_unsigned_metadata_changes_are_rejected(client, change):
    assert client.get("/health?x=1", headers={**headers(), **change}).status_code == 403


@pytest.mark.parametrize("stamp", ["939", "1061"])
def test_expired_or_future_signature_is_rejected(client, stamp):
    assert client.get("/health?x=1", headers=headers(timestamp=stamp)).status_code == 403


def test_missing_duplicate_and_changed_targets_never_reach_application(client):
    assert client.get("/health").status_code == 403
    assert client.get("/health?x=2", headers=headers()).status_code == 403
    assert client.post("/health?x=1", headers=headers()).status_code == 403
    duplicate = list(headers().items()) + [("x-canvas-edge-ip", "203.0.113.9")]
    assert client.get("/health?x=1", headers=duplicate).status_code == 403


def test_ipv6_and_encoded_query_are_bound_verbatim(client):
    response = client.get("/health?x=a%2Fb", headers=headers(address="2001:db8::9", target="/health?x=a%2Fb"))
    assert response.status_code == 200
    assert response.json()["client"] == "2001:db8::9"


def test_signed_encoded_unicode_path_uses_raw_asgi_path(client):
    response = client.get("/%C3%A9?x=1", headers=headers(target="/%C3%A9?x=1"))
    assert response.status_code == 200


def test_authenticated_gateway_preserves_external_https_over_private_http_hop(client):
    response = client.get("http://canvas.example/health?x=1", headers=headers())
    assert response.status_code == 200
    assert response.json()["scheme"] == "https"
