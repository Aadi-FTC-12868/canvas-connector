from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import threading
import time
from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import Fernet
import httpx
import pytest
from mcp.server.auth.routes import create_auth_routes
from starlette.applications import Starlette
from starlette.testclient import TestClient

from canvas_connector import accounts
from canvas_connector.accounts import AccountError, AccountService
from canvas_connector.personal import PersonalProvider
from canvas_connector.personal_admin import initialize_owner, reset_owner_password
from canvas_connector.server import create_http_app
from canvas_connector.storage import OwnerRevokedError, Store
from test_auth import BASE, REDIRECT, CHALLENGE, register
from test_broker_auth import fields, post, authorize, connect_canvas, PAT

EMAIL = "owner@example.test"
PASSWORD = "synthetic owner password for tests"


@pytest.fixture
def personal(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "_derive", lambda value, salt: hashlib.sha256(salt + value).digest())
    key, path = Fernet.generate_key().decode(), tmp_path / "personal.db"
    store = Store(path, key)
    initialize_owner(store, EMAIL, PASSWORD)
    calls = []
    def upstream(request):
        calls.append(request)
        assert request.url.path == "/api/v1/users/self/profile"
        return httpx.Response(200, json={"id": 22 if request.headers["authorization"].endswith("foreign") else 11,
                                       "name": "Synthetic Owner"})
    provider = PersonalProvider(issuer_url=BASE, store=store,
        http_client_factory=lambda: httpx.Client(transport=httpx.MockTransport(upstream)))
    settings = provider.auth_settings
    app = Starlette(routes=provider.routes() + create_auth_routes(provider, settings.issuer_url,
        client_registration_options=settings.client_registration_options,
        revocation_options=settings.revocation_options))
    with TestClient(app, base_url=BASE) as client:
        yield client, provider, calls, path, key
    store.close()


def login(client, provider):
    result = post(client, "/account/login", {**fields(client.get("/account/login")), "email": EMAIL, "password": PASSWORD})
    assert result.status_code == 303, result.text
    return client.cookies.get(provider.session_cookie)


def second_account(store):
    delivered = []
    service = AccountService(store, mailer=lambda *args: delivered.append(args))
    service.signup("other@example.test", PASSWORD, "peer")
    service.verify("other@example.test", PASSWORD, delivered[-1][2], "peer")
    token = service.login("other@example.test", PASSWORD, "peer")
    return service.session(token)["account_id"], token


@pytest.mark.parametrize("operation", ["signup", "verify", "reset", "delete", "unknown"])
def test_forbidden_account_routes_return_404_even_with_valid_owner_session(personal, operation):
    client, provider, calls, *_ = personal
    for authenticated in [False, True]:
        if authenticated:
            login(client, provider)
        assert client.get("/account/" + operation).status_code == 404
        result = post(client, "/account/" + operation, {"email": EMAIL, "password": PASSWORD})
        assert result.status_code == 404 and not calls


def test_personal_login_setup_and_consent_without_public_signup(personal):
    client, provider, calls, path, _ = personal
    assert client.get("/", follow_redirects=False).headers["location"] == "/setup"
    page = client.get("/account/login")
    assert "server owner" in page.text and "operator" in page.text
    for text in ["/account/signup", "/account/verify", "/account/reset", "Cloudflare", "Hosted beta"]:
        assert text not in page.text
    session = login(client, provider)
    page = client.get("/setup")
    assert "personal Canvas connector" in page.text and "first verified" in page.text
    assert "Hosted beta" not in page.text and "Cloudflare" not in page.text and "Delete" not in page.text
    result = post(client, "/setup", {**fields(page), "origin": "https://school.example", "token": PAT, "operation": "save"})
    assert result.status_code == 303
    tokens, consent = authorize(client, register(client, "Duet <script>"))
    assert tokens["scope"] == "canvas:read" and "checked" not in consent.text
    assert "&lt;script&gt;" in consent.text and "<script>" not in consent.text
    assert "https://client.example" in consent.headers["content-security-policy"]
    credentials = asyncio.run(provider.get_credentials(tokens["access_token"]))
    assert credentials.token == PAT and credentials.base_url == "https://school.example"
    listed = provider.connections(session)
    assert len(listed) == 1 and listed[0]["name"] == "Duet <script>" and listed[0]["callback_host"] == "client.example"
    html = client.get("/setup").text
    assert "Disconnect this assistant" in html and "&lt;script&gt;" in html
    assert PAT not in html and len(calls) == 1
    for secret in [EMAIL, PASSWORD, PAT, session, tokens["access_token"], tokens["refresh_token"]]:
        assert secret.encode() not in path.read_bytes()


@pytest.mark.parametrize("method", ["signup", "verify", "request_reset", "reset", "delete"])
def test_account_service_cannot_bypass_owner_provisioning(personal, method):
    _, provider, *_ = personal
    with pytest.raises(AccountError, match="operator"):
        getattr(provider.accounts, method)()


def test_second_valid_account_cannot_login_resolve_session_or_save_credentials(personal):
    client, provider, calls, *_ = personal
    other_id, token = second_account(provider.store)
    assert provider.accounts.active(other_id) is None and provider.accounts.session(token) is None
    with pytest.raises(AccountError):
        provider.accounts.login("other@example.test", PASSWORD, "peer")
    with pytest.raises(AccountError):
        provider.save_connection(token, "https://school.example", PAT, "")
    client.cookies.set(provider.session_cookie, token)
    assert client.get("/setup", follow_redirects=False).headers["location"] == "/account/login"
    assert not calls and provider._connection(other_id) is None


def test_second_account_cannot_borrow_owner_connection_or_grant(personal):
    client, provider, *_ = personal
    login(client, provider)
    connect_canvas(client)
    tokens, _ = authorize(client, register(client))
    access = asyncio.run(provider.load_access_token(tokens["access_token"]))
    other_id, _ = second_account(provider.store)
    grant = provider.store.record("grant", access.grant_id)
    grant.value["account_id"] = other_id
    provider.store.put("grant", access.grant_id, grant.value, expires=grant.expires, owner=grant.owner)
    assert asyncio.run(provider.load_access_token(tokens["access_token"])) is None
    with pytest.raises(PermissionError):
        asyncio.run(provider.get_credentials(tokens["access_token"]))


def test_individual_revocation_preserves_other_app_and_tombstones_work(personal):
    client, provider, *_ = personal
    session = login(client, provider)
    connect_canvas(client)
    first, _ = authorize(client, register(client, "Duet"))
    second, _ = authorize(client, register(client, "Other app"))
    access = asyncio.run(provider.load_access_token(first["access_token"]))
    provider.store.put("action", "old", {"state": "prepared"}, expires=time.time() + 300, owner=access.grant_id)
    provider.store.put("continuation", "old", {"text": "private"}, expires=time.time() + 300, owner=access.grant_id)
    result = post(client, "/setup", {**fields(client.get("/setup")), "operation": "revoke_one", "grant_id": access.grant_id})
    assert result.status_code == 303
    assert asyncio.run(provider.load_access_token(first["access_token"])) is None
    assert asyncio.run(provider.load_access_token(second["access_token"]))
    assert provider.store.get("action", "old") is None and provider.store.get("continuation", "old") is None
    with pytest.raises(OwnerRevokedError):
        provider.store.put("action", "new", {}, expires=time.time() + 300, owner=access.grant_id)
    assert len(provider.connections(session)) == 1


def test_individual_revocation_rejects_foreign_grant_and_bad_csrf(personal):
    client, provider, *_ = personal
    session = login(client, provider)
    other_id, _ = second_account(provider.store)
    expiry = time.time() + 1000
    provider.store.put("grant", "foreign", {"account_id": other_id, "mode": "personal"}, expires=expiry, owner="foreign")
    provider.store.put("account_grant", "foreign", {"grant_id": "foreign"}, expires=expiry, owner="account:" + provider.owner_id)
    with pytest.raises(AccountError):
        provider.revoke_connection(session, "foreign")
    assert provider.store.get("grant", "foreign") and not provider.store.is_owner_revoked("foreign")
    result = post(client, "/setup", {"operation": "revoke_one", "grant_id": "foreign", "csrf": "wrong"})
    assert result.status_code == 400


@pytest.mark.parametrize("origin,token", [("https://school.example", PAT + "foreign"), ("https://other.example", PAT)])
def test_canvas_identity_is_pinned_across_rotation_and_disconnect(personal, origin, token):
    client, provider, *_ = personal
    session = login(client, provider)
    connect_canvas(client)
    first, _ = authorize(client, register(client))
    version = provider._connection_version(provider.owner_id)
    with pytest.raises(AccountError, match="different Canvas account"):
        provider.save_connection(session, origin, token, version)
    assert asyncio.run(provider.load_access_token(first["access_token"]))
    assert provider._connection_version(provider.owner_id) == version
    provider.disconnect(session)
    with pytest.raises(AccountError, match="different Canvas account"):
        provider.save_connection(session, origin, token, provider._connection_version(provider.owner_id))
    assert provider._connection(provider.owner_id) is None


def test_token_rotation_revokes_all_apps_without_changing_canvas_identity(personal):
    client, provider, *_ = personal
    session = login(client, provider)
    connect_canvas(client)
    old, _ = authorize(client, register(client))
    connect_canvas(client, PAT + "rotated")
    assert asyncio.run(provider.load_access_token(old["access_token"])) is None
    assert provider.accounts.session(session)
    assert provider._connection(provider.owner_id)["token"] == PAT + "rotated"
    assert provider.store.get("personal_canvas_identity", provider.owner_id) == {"origin": "https://school.example", "user_id": "11"}


@pytest.mark.parametrize("action", ["reset", "disconnect", "logout"])
def test_security_change_during_profile_verification_cannot_commit(personal, action):
    client, provider, *_ = personal
    session = login(client, provider)
    original = provider._verify_canvas
    def paused(origin, token):
        profile = original(origin, token)
        if action == "reset":
            reset_owner_password(provider.store, PASSWORD + "new")
        elif action == "disconnect":
            provider.disconnect(session)
        else:
            provider.accounts.logout(session)
        return profile
    provider._verify_canvas = paused
    with pytest.raises(AccountError, match="changed"):
        provider.save_connection(session, "https://school.example", PAT, "")
    assert provider._connection(provider.owner_id) is None
    assert provider.store.get("personal_canvas_identity", provider.owner_id) is None


def test_concurrent_first_setup_pins_only_one_identity(personal):
    client, provider, *_ = personal
    session = login(client, provider)
    barrier, original = threading.Barrier(2), provider._verify_canvas
    def paused(origin, token):
        result = original(origin, token)
        barrier.wait(timeout=5)
        return result
    provider._verify_canvas = paused
    def save(token):
        try:
            provider.save_connection(session, "https://school.example", token, "")
            return True
        except AccountError:
            return False
    with ThreadPoolExecutor(2) as executor:
        outcomes = list(executor.map(save, [PAT, PAT + "foreign"]))
    assert sorted(outcomes) == [False, True]
    connection = provider._connection(provider.owner_id)
    assert provider.store.get("personal_canvas_identity", provider.owner_id)["user_id"] == connection["user_id"]


def test_password_recovery_revokes_pending_consent_and_refresh(personal):
    client, provider, *_ = personal
    old_session = login(client, provider)
    connect_canvas(client)
    client_id = register(client)
    tokens, _ = authorize(client, client_id)
    start = client.get("/authorize", params={"client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT,
        "scope": "canvas:read", "code_challenge": CHALLENGE, "code_challenge_method": "S256"}, follow_redirects=False)
    pending = fields(client.get(start.headers["location"]))
    reset_owner_password(provider.store, PASSWORD + "new")
    assert provider.accounts.session(old_session) is None
    assert post(client, "/connect", {**pending, "decision": "allow"}).status_code == 403
    assert client.post("/token", data={"client_id": client_id, "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}).status_code == 400
    assert provider._connection(provider.owner_id)


def test_owner_connection_and_app_access_survive_restart(personal):
    client, provider, _, path, key = personal
    session = login(client, provider)
    connect_canvas(client)
    tokens, _ = authorize(client, register(client))
    reopened = PersonalProvider(issuer_url=BASE, database_path=path, encryption_key=key)
    try:
        assert reopened.owner_id == provider.owner_id and reopened.accounts.session(session)
        assert asyncio.run(reopened.get_credentials(tokens["access_token"])).token == PAT
        assert len(reopened.connections(session)) == 1
    finally:
        reopened.store.close()


def test_real_mcp_read_default_write_denial_and_revocation(personal, monkeypatch):
    from canvas_py import Canvas
    import canvas_connector.security as security
    client, provider, *_ = personal
    session = login(client, provider)
    connect_canvas(client)
    tokens, _ = authorize(client, register(client))
    calls = []
    def factory(config, **kwargs):
        def upstream(request):
            calls.append(request)
            assert request.method == "GET" and request.url.path == "/api/v1/users/self/profile"
            return httpx.Response(200, json={"id": 11, "name": "Synthetic Owner"})
        canvas = Canvas(config, http_client=httpx.Client(transport=httpx.MockTransport(upstream)))
        canvas._http._owns_client = True
        return canvas
    monkeypatch.setattr(security, "guarded_canvas", factory)
    with TestClient(create_http_app(provider), base_url=BASE) as mcp:
        headers = {"Accept": "application/json, text/event-stream", "Authorization": "Bearer " + tokens["access_token"]}
        def rpc(method, params):
            return mcp.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        assert rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}).status_code == 200
        result = rpc("tools/call", {"name": "canvas_connection_status", "arguments": {}})
        assert result.status_code == 200 and result.json()["result"]["structuredContent"]["data"]["user_id"] == 11
        assert PAT not in result.text and len(calls) == 1
        result = rpc("tools/call", {"name": "canvas_submit_assignment", "arguments": {"action_id": "synthetic"}})
        assert result.status_code == 200 and "read-only" in result.text
        assert len(calls) == 1
        provider.revoke_connection(session, provider.connections(session)[0]["id"])
        assert rpc("tools/list", {}).status_code == 401


def test_consent_cancel_redirect_works_without_grant(personal):
    client, provider, *_ = personal
    login(client, provider)
    connect_canvas(client)
    client_id = register(client)
    start = client.get("/authorize", params={"client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT,
        "scope": "canvas:read", "code_challenge": CHALLENGE, "code_challenge_method": "S256"}, follow_redirects=False)
    page = client.get(start.headers["location"])
    response = post(client, "/connect", {**fields(page), "decision": "deny"})
    assert response.status_code == 303
    assert parse_qs(urlsplit(response.headers["location"]).query)["error"] == ["access_denied"]
    assert not provider.store.scan("grant")
