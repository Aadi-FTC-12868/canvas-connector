from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from mcp.server.auth.routes import create_auth_routes
from starlette.applications import Starlette
from starlette.testclient import TestClient

from canvas_connector.auth import CanvasOAuthProvider, Institution

BASE = "https://connector.example"
REDIRECT = "https://client.example/callback"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")


@pytest.fixture
def setup(tmp_path, request):
    calls = []
    def upstream(req):
        calls.append(req)
        if req.url.path == "/login/oauth2/token":
            form = parse_qs(req.content.decode())
            assert form["client_secret"] == ["institution-private-secret"]
            if form["grant_type"] == ["refresh_token"]:
                return httpx.Response(200, json={"access_token": "canvas-refreshed", "expires_in": 3600})
            return httpx.Response(200, json={"access_token": "canvas-private-token", "refresh_token": "canvas-private-refresh", "expires_in": 3600})
        if req.url.path == "/api/v1/users/self/profile":
            assert req.headers["authorization"] == "Bearer canvas-private-token"
            return httpx.Response(200, json={"id": 11})
        raise AssertionError(req.url)
    key = Fernet.generate_key().decode()
    provider = CanvasOAuthProvider(issuer_url=getattr(request, "param", BASE), database_path=tmp_path / "auth.db", encryption_key=key,
        institutions=[Institution("https://school.example", "canvas-client", "institution-private-secret", "School")],
        http_client_factory=lambda: httpx.Client(transport=httpx.MockTransport(upstream)))
    settings = provider.auth_settings
    app = Starlette(routes=provider.routes() + create_auth_routes(provider, settings.issuer_url,
        client_registration_options=settings.client_registration_options,
        revocation_options=settings.revocation_options))
    with TestClient(app, base_url=BASE) as client:
        yield client, provider, calls, tmp_path / "auth.db", key
    provider.store.close()


def register(client, name="Example AI"):
    response = client.post("/register", json={"client_name": name, "redirect_uris": [REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "none", "scope": "canvas:read canvas:write"})
    assert response.status_code == 201, response.text
    return response.json()["client_id"]


def begin(client, client_id, *, scope="canvas:read canvas:write"):
    auth = client.get("/authorize", params={"client_id": client_id, "response_type": "code",
        "redirect_uri": REDIRECT, "scope": scope, "code_challenge": CHALLENGE,
        "code_challenge_method": "S256", "resource": BASE + "/mcp", "state": "client-state"}, follow_redirects=False)
    assert auth.status_code == 302, auth.text
    page = client.get(auth.headers["location"])
    assert page.status_code == 200
    fields = dict(re.findall(r'name="(request|csrf)" value="([^"]+)"', page.text))
    fields["institution"] = "https://school.example"
    return fields, page


def login(client, client_id, *, write=True):
    fields, page = begin(client, client_id)
    assert "password" in page.text and 'name="token"' not in page.text
    if write:
        fields["write"] = "yes"
    response = client.post("/connect", data=fields, headers={"Origin": BASE}, follow_redirects=False)
    assert response.status_code == 303, response.text
    location = urlsplit(response.headers["location"])
    assert location.netloc == "school.example"
    upstream_state = parse_qs(location.query)["state"][0]
    callback = client.get("/canvas/callback", params={"code": "canvas-code", "state": upstream_state}, follow_redirects=False)
    assert callback.status_code == 303, callback.text
    params = parse_qs(urlsplit(callback.headers["location"]).query)
    assert params["state"] == ["client-state"]
    assert params["iss"] == [client.get("/.well-known/oauth-authorization-server").json()["issuer"]]
    return params["code"][0], upstream_state


def exchange(client, client_id, code, verifier=VERIFIER):
    return client.post("/token", data={"grant_type": "authorization_code", "client_id": client_id,
        "code": code, "redirect_uri": REDIRECT, "code_verifier": verifier})


def test_oauth_end_to_end_pkce_rotation_revocation_and_encryption(setup):
    client, provider, calls, db, key = setup
    client_id = register(client)
    code, state = login(client, client_id)
    assert exchange(client, client_id, code, "bad-verifier").status_code == 400
    result = exchange(client, client_id, code)
    assert result.status_code == 200, result.text
    tokens = result.json()
    assert tokens["scope"] == "canvas:read canvas:write"
    assert exchange(client, client_id, code).status_code == 400
    assert client.get("/canvas/callback", params={"state": state, "code": "canvas-code"}).status_code == 400
    access = asyncio.run(provider.load_access_token(tokens["access_token"]))
    assert access.grant_id and access.resource == BASE + "/mcp"
    credential = asyncio.run(provider.get_credentials(access))
    assert credential.token == "canvas-private-token"
    raw = db.read_bytes()
    for secret in ["canvas-private-token", "canvas-private-refresh", tokens["access_token"], tokens["refresh_token"], code]:
        assert secret.encode() not in raw
    refresh = client.post("/token", data={"client_id": client_id, "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
    assert refresh.status_code == 200, refresh.text
    rotated = refresh.json()
    assert asyncio.run(provider.load_access_token(tokens["access_token"])) is None
    assert asyncio.run(provider.load_access_token(rotated["access_token"])) is not None
    assert client.post("/token", data={"client_id": client_id, "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}).status_code == 400
    assert asyncio.run(provider.load_access_token(rotated["access_token"])) is None
    revoke = client.post("/revoke", data={"client_id": client_id, "token": rotated["access_token"]})
    assert revoke.status_code == 200, revoke.text
    assert asyncio.run(provider.load_access_token(rotated["access_token"])) is None
    assert client.post("/token", data={"client_id": client_id, "grant_type": "refresh_token", "refresh_token": rotated["refresh_token"]}).status_code == 400


@pytest.mark.parametrize("setup", [BASE, BASE + "/"], indirect=True)
@pytest.mark.parametrize("outcome", ["success", "consent_denied", "canvas_denied", "missing_code"])
def test_authorization_response_issuer_exactly_matches_discovery(setup, outcome):
    client, provider, calls, *_ = setup
    metadata_response = client.get("/.well-known/oauth-authorization-server")
    assert metadata_response.status_code == 200
    metadata = metadata_response.json()
    # The SDK serializes an origin-only AnyHttpUrl with a trailing slash.
    # RFC 9207 requires exact string equality, not equivalent URL origins.
    assert metadata["issuer"] == BASE + "/"
    assert metadata["authorization_endpoint"] == BASE + "/authorize"
    assert provider.issuer_url == BASE
    assert provider.resource_url == BASE + "/mcp"
    assert provider.callback_url == BASE + "/canvas/callback"
    client_id = register(client)
    fields, _ = begin(client, client_id)
    if outcome == "consent_denied":
        fields["deny"] = "yes"
    consent = client.post("/connect", data=fields, headers={"Origin": BASE}, follow_redirects=False)
    assert consent.status_code == 303
    if outcome == "consent_denied":
        response = consent
    else:
        upstream = parse_qs(urlsplit(consent.headers["location"]).query)
        assert upstream["redirect_uri"] == [BASE + "/canvas/callback"]
        callback = {"state": upstream["state"][0]}
        if outcome == "success":
            callback["code"] = "canvas-code"
        elif outcome == "canvas_denied":
            callback["error"] = "access_denied"
        response = client.get("/canvas/callback", params=callback, follow_redirects=False)
    assert response.status_code == 303
    redirect = urlsplit(response.headers["location"])
    assert redirect._replace(query="").geturl() == REDIRECT
    result = parse_qs(redirect.query)
    assert result["iss"] == [metadata["issuer"]]
    assert result["state"] == ["client-state"]
    if outcome == "success":
        assert "error" not in result
        assert exchange(client, client_id, result["code"][0]).status_code == 200
    else:
        assert result["error"] == ["access_denied"]
        assert "code" not in result
        assert not calls


def test_scopes_cannot_be_escalated_and_tenants_are_isolated(setup):
    client, provider, *_ = setup
    first = register(client, "First")
    second = register(client, "Second")
    code, _ = login(client, first, write=False)
    assert exchange(client, second, code).status_code == 400
    a = exchange(client, first, code).json()
    assert a["scope"] == "canvas:read"
    assert client.post("/token", data={"client_id": first, "grant_type": "refresh_token", "refresh_token": a["refresh_token"], "scope": "canvas:read canvas:write"}).status_code == 400
    code, _ = login(client, second)
    b = exchange(client, second, code).json()
    a_access = asyncio.run(provider.load_access_token(a["access_token"]))
    b_access = asyncio.run(provider.load_access_token(b["access_token"]))
    assert a_access.grant_id != b_access.grant_id
    asyncio.run(provider.revoke_token(a_access))
    assert asyncio.run(provider.load_access_token(a["access_token"])) is None
    assert asyncio.run(provider.load_access_token(b["access_token"])) is not None


def test_consent_csrf_and_request_replay(setup):
    client, provider, calls, *_ = setup
    client_id = register(client, '<script>alert("x")</script>')
    fields, page = begin(client, client_id)
    assert '<script>' not in page.text
    assert '&lt;script&gt;' in page.text
    assert client.post("/connect", data=fields, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/connect", data={**fields, "csrf": "wrong"}, headers={"Origin": BASE}).status_code == 403
    valid = client.post("/connect", data=fields, headers={"Origin": BASE}, follow_redirects=False)
    assert valid.status_code == 303
    assert client.post("/connect", data=fields, headers={"Origin": BASE}).status_code == 403
    state = parse_qs(urlsplit(valid.headers["location"]).query)["state"][0]
    client.cookies.clear()
    assert client.get("/canvas/callback", params={"state": state, "code": "x"}).status_code == 400
    assert not calls


def test_resource_redirect_and_pkce_validation(setup):
    client, provider, *_ = setup
    client_id = register(client)
    params = {"client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT,
        "scope": "canvas:read", "code_challenge": CHALLENGE, "code_challenge_method": "S256", "resource": "https://wrong.example/mcp"}
    result = client.get("/authorize", params=params, follow_redirects=False)
    assert result.status_code in {302, 400}
    assert "invalid_request" in result.headers.get("location", result.text)
    params["resource"] = BASE + "/mcp"
    params["redirect_uri"] = "https://evil.example/callback"
    result = client.get("/authorize", params=params, follow_redirects=False)
    assert result.status_code == 400
    assert "evil.example" not in result.headers.get("location", "")
    assert client.post("/register", json={"redirect_uris": ["javascript:alert(1)"]}).status_code == 400


def test_canvas_refresh_and_persistence(setup):
    client, provider, calls, db, key = setup
    client_id = register(client)
    code, _ = login(client, client_id)
    token = exchange(client, client_id, code).json()["access_token"]
    access = asyncio.run(provider.load_access_token(token))
    grant = provider.store.get("grant", access.grant_id)
    grant["credential"]["expires_at"] = time.time() - 1
    provider.store.put("grant", access.grant_id, grant, expires=grant["expires"], owner=access.grant_id)
    config = asyncio.run(provider.get_credentials(token))
    assert config.token == "canvas-refreshed"
    assert provider.store.get("grant", access.grant_id)["credential"]["refresh_token"] == "canvas-private-refresh"
    other = CanvasOAuthProvider(issuer_url=BASE, database_path=db, encryption_key=key,
        institutions=list(provider.institutions.values()))
    try:
        assert asyncio.run(other.load_access_token(token)).grant_id == access.grant_id
        assert asyncio.run(other.get_credentials(token)).token == "canvas-refreshed"
    finally:
        other.store.close()


def test_revocation_endpoint_revokes_live_grant(setup):
    client, provider, *_ = setup
    client_id = register(client)
    code, _ = login(client, client_id)
    token = exchange(client, client_id, code).json()
    assert asyncio.run(provider.load_access_token(token["access_token"])) is not None
    result = client.post("/revoke", data={"client_id": client_id, "token": token["refresh_token"]})
    assert result.status_code == 200
    assert asyncio.run(provider.load_access_token(token["access_token"])) is None


def test_full_http_app_discovery_mcp_protocol_and_account_context(setup, monkeypatch):
    from canvas_py import Canvas
    from canvas_connector.server import create_http_app
    import canvas_connector.security as security

    _, provider, _, _, _ = setup
    seen = []
    def canvas_factory(config):
        def handle(request):
            seen.append(request.headers.get("authorization"))
            if request.url.path == "/api/v1/users/self/profile":
                return httpx.Response(200, json={"id": 101 if config.token == "account-one" else 202, "name": "Fixture Student"})
            raise AssertionError(request.url)
        canvas = Canvas(config, http_client=httpx.Client(transport=httpx.MockTransport(handle)))
        canvas._http._owns_client = True
        return canvas
    monkeypatch.setattr(security, "guarded_canvas", canvas_factory)
    app = create_http_app(provider)
    with TestClient(app, base_url=BASE) as client:
        metadata = client.get("/.well-known/oauth-protected-resource/mcp")
        assert metadata.status_code == 200
        assert metadata.json()["scopes_supported"] == ["canvas:read", "canvas:write"]
        assert metadata.json()["resource"] == BASE + "/mcp"
        oauth = client.get("/.well-known/oauth-authorization-server").json()
        assert oauth["code_challenge_methods_supported"] == ["S256"]
        headers = {"Accept": "application/json, text/event-stream"}
        unauth = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
        assert unauth.status_code == 401
        assert "resource_metadata" in unauth.headers["www-authenticate"]
        client_id = register(client)
        tokens = []
        for canvas_token in ("account-one", "account-two"):
            code, _ = login(client, client_id, write=False)
            data = exchange(client, client_id, code).json()
            access = asyncio.run(provider.load_access_token(data["access_token"]))
            grant = provider.store.get("grant", access.grant_id)
            grant["credential"]["access_token"] = canvas_token
            provider.store.put("grant", access.grant_id, grant, expires=grant["expires"], owner=access.grant_id)
            tokens.append(data["access_token"])
        for index, token in enumerate(tokens):
            auth_headers = {**headers, "Authorization": "Bearer " + token}
            result = client.post("/mcp", headers=auth_headers, json={"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
            assert result.status_code == 200, result.text
            assert result.json()["result"]["serverInfo"]["name"] == "Canvas Connector"
            listed = client.post("/mcp", headers=auth_headers, json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
            assert listed.status_code == 200
            names = {tool["name"] for tool in listed.json()["result"]["tools"]}
            assert {"canvas_dashboard", "canvas_read_file", "canvas_submit_assignment", "canvas_send_message", "canvas_upload_file"} <= names
            assert "canvas_execute_action" not in names
            call = client.post("/mcp", headers=auth_headers, json={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "canvas_connection_status", "arguments": {}}})
            assert call.status_code == 200
            assert not call.json()["result"].get("isError")
            assert str(101 if index == 0 else 202) in call.text
            prepared = client.post("/mcp", headers=auth_headers, json={"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "canvas_prepare_action", "arguments": {"action": {"kind": "inbox_send", "recipients": [99], "subject": "Synthetic test", "body": "This must never be sent."}}}})
            assert not prepared.json()["result"].get("isError"), prepared.text
            action_id = prepared.json()["result"]["structuredContent"]["data"]["action_id"]
            wrong_route = client.post("/mcp", headers=auth_headers, json={"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "canvas_submit_assignment", "arguments": {"action_id": action_id}}})
            assert wrong_route.json()["result"].get("isError"), wrong_route.text
            assert "read-only" in wrong_route.text
            denied = client.post("/mcp", headers=auth_headers, json={"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "canvas_send_message", "arguments": {"action_id": action_id}}})
            assert denied.status_code == 200
            assert denied.json()["result"].get("isError")
            assert "read-only" in denied.text
        assert seen == ["Bearer account-one", "Bearer account-two"]
        assert client.post("/token", data={"resource": "https://wrong.example/mcp"}).status_code == 400
        assert client.get("/health", headers={"Host": "attacker.example"}).status_code == 400
        result = client.post("/revoke", data={"client_id": client_id, "token": tokens[0]})
        assert result.status_code == 200
        assert asyncio.run(provider.load_access_token(tokens[0])) is None
        assert asyncio.run(provider.load_access_token(tokens[1])) is not None


def test_default_client_requests_both_scopes_but_write_consent_is_unchecked(setup):
    client, provider, *_ = setup
    registered = client.post("/register", json={"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none"})
    assert registered.status_code == 201
    client_id = registered.json()["client_id"]
    assert set(registered.json()["scope"].split()) == {"canvas:read", "canvas:write"}
    response = client.get("/authorize", params={"client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT,
        "code_challenge": CHALLENGE, "code_challenge_method": "S256"}, follow_redirects=False)
    assert response.status_code == 302
    page = client.get(response.headers["location"])
    assert 'name="write"' in page.text
    assert 'checked' not in page.text


def test_expired_code_and_removed_institution_fail_closed(setup):
    client, provider, *_ = setup
    client_id = register(client)
    code, _ = login(client, client_id)
    original = provider.store.get("code", code)
    provider.store.put("code", code, original, expires=time.time() - 1)
    assert exchange(client, client_id, code).status_code == 400
    code, _ = login(client, client_id)
    token = exchange(client, client_id, code).json()["access_token"]
    provider.institutions.clear()
    with pytest.raises(PermissionError, match="no longer configured"):
        asyncio.run(provider.get_credentials(token))


def test_concurrent_mcp_calls_keep_tenant_context_and_do_not_block_event_loop(setup, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from canvas_py import Canvas
    from canvas_connector.server import create_http_app
    import canvas_connector.security as security

    _, provider, *_ = setup
    barrier = threading.Barrier(2, timeout=3)
    observed = []
    def canvas_factory(config):
        def handle(request):
            barrier.wait()  # Both calls must reach separate workers concurrently.
            observed.append(request.headers.get("authorization"))
            return httpx.Response(200, json={"id": 301 if config.token == "concurrent-one" else 302})
        canvas = Canvas(config, http_client=httpx.Client(transport=httpx.MockTransport(handle)))
        canvas._http._owns_client = True
        return canvas
    monkeypatch.setattr(security, "guarded_canvas", canvas_factory)
    with TestClient(create_http_app(provider), base_url=BASE) as client:
        client_id = register(client)
        tokens = []
        for canvas_token in ("concurrent-one", "concurrent-two"):
            code, _ = login(client, client_id, write=False)
            token = exchange(client, client_id, code).json()["access_token"]
            access = asyncio.run(provider.load_access_token(token))
            grant = provider.store.get("grant", access.grant_id)
            grant["credential"]["access_token"] = canvas_token
            provider.store.put("grant", access.grant_id, grant, expires=grant["expires"], owner=access.grant_id)
            tokens.append(token)
        def call(token):
            return client.post("/mcp", headers={"Authorization": "Bearer " + token,
                "Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": 1,
                "method": "tools/call", "params": {"name": "canvas_connection_status", "arguments": {}}})
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(call, tokens))
        for response, identity in zip(responses, (301, 302)):
            assert response.status_code == 200
            assert not response.json()["result"].get("isError"), response.text
            assert str(identity) in response.text
        assert set(observed) == {"Bearer concurrent-one", "Bearer concurrent-two"}


def test_duplicate_authorization_and_untrusted_origin_are_rejected(setup):
    from canvas_connector.server import create_http_app
    _, provider, *_ = setup
    with TestClient(create_http_app(provider), base_url=BASE) as client:
        response = client.post("/mcp", headers=[("Authorization", "Bearer first"), ("Authorization", "Bearer second"),
            ("Accept", "application/json, text/event-stream")], json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert response.status_code == 400
        client_id = register(client)
        code, _ = login(client, client_id, write=False)
        token = exchange(client, client_id, code).json()["access_token"]
        response = client.post("/mcp", headers={"Authorization": "Bearer " + token,
            "Origin": "https://attacker.example", "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert response.status_code == 403


def test_authorization_code_consumption_is_atomic_across_database_connections(setup):
    from concurrent.futures import ThreadPoolExecutor
    from mcp.server.auth.provider import TokenError
    client, provider, _, db, key = setup
    client_id = register(client)
    code, _ = login(client, client_id)
    registered = asyncio.run(provider.get_client(client_id))
    loaded = asyncio.run(provider.load_authorization_code(registered, code))
    other = CanvasOAuthProvider(issuer_url=BASE, database_path=db, encryption_key=key,
        institutions=list(provider.institutions.values()))
    def exchange_once(instance):
        try:
            return asyncio.run(instance.exchange_authorization_code(registered, loaded))
        except TokenError:
            return None
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(exchange_once, [provider, other]))
        assert sum(item is not None for item in result) == 1
    finally:
        other.store.close()


def test_canvas_refresh_is_serialized_and_revocation_during_refresh_is_final(setup, monkeypatch):
    import threading
    client, provider, calls, *_ = setup
    client_id = register(client)
    code, _ = login(client, client_id)
    token = exchange(client, client_id, code).json()["access_token"]
    access = asyncio.run(provider.load_access_token(token))
    def expire():
        grant = provider.store.get("grant", access.grant_id)
        grant["credential"]["expires_at"] = time.time() - 1
        provider.store.put("grant", access.grant_id, grant, expires=grant["expires"], owner=access.grant_id)
    expire()
    before = len(calls)
    async def concurrent_refresh():
        return await asyncio.gather(provider.get_credentials(token), provider.get_credentials(token))
    credentials = asyncio.run(concurrent_refresh())
    assert [c.token for c in credentials] == ["canvas-refreshed", "canvas-refreshed"]
    assert len(calls) == before + 1
    expire()
    started, release = threading.Event(), threading.Event()
    def delayed_token(*args):
        started.set()
        assert release.wait(3)
        return {"access_token": "late-result", "refresh_token": "late-refresh", "expires_at": time.time() + 3600}
    monkeypatch.setattr(provider, "_canvas_token", delayed_token)
    # A new loop is only used by the test harness; the server has one event loop.
    provider.refresh_lock = asyncio.Lock()
    async def revoke_inflight():
        pending = asyncio.create_task(provider.get_credentials(token))
        assert await asyncio.to_thread(started.wait, 3)
        await provider.revoke_token(access)
        release.set()
        with pytest.raises(PermissionError, match="revoked"):
            await pending
    asyncio.run(revoke_inflight())
    assert provider.store.get("grant", access.grant_id) is None
    assert asyncio.run(provider.load_access_token(token)) is None


def test_hosted_auth_rate_and_body_limits(setup, monkeypatch):
    from types import SimpleNamespace

    from canvas_connector import storage
    from canvas_connector.server import create_http_app

    # Control only the durable limiter's clock. Replacing time.time globally
    # would also affect unrelated libraries; monotonic/event-loop time stays real.
    limiter_time = [time.time()]
    monkeypatch.setattr(storage, "time", SimpleNamespace(time=lambda: limiter_time[0]))
    _, provider, *_ = setup
    with TestClient(create_http_app(provider), base_url=BASE) as client:
        oversized = client.post("/register", content=b"x" * 65537, headers={"Content-Type": "application/json"})
        assert oversized.status_code == 413
        for _ in range(119):
            response = client.get("/health")
            assert response.status_code == 200
        limited = client.get("/health")
        assert limited.status_code == 429
        assert limited.headers["Retry-After"] == "60"
        limiter_time[0] += 1.25
        limited = client.get("/health")
        assert limited.status_code == 429
        assert limited.headers["Retry-After"] == "59"
        limiter_time[0] += 58.5
        limited = client.get("/health")
        assert limited.status_code == 429
        assert limited.headers["Retry-After"] == "1"
        limiter_time[0] += 0.25
        assert client.get("/health").status_code == 200
