from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import re
import threading
from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import Fernet
import httpx
import pytest
from mcp.server.auth.routes import create_auth_routes
from starlette.applications import Starlette
from starlette.testclient import TestClient

from canvas_connector import accounts
from canvas_connector.accounts import AccountError
from canvas_connector.auth import CanvasOAuthProvider, Institution, _headers, _form_origin
from canvas_connector.broker import BuiltByAadiProvider
from canvas_connector.server import create_http_app
from canvas_connector.storage import OwnerRevokedError
from test_auth import BASE, REDIRECT, CHALLENGE, register, exchange, begin

EMAIL = "student@example.test"
PASSWORD = "correct horse battery staple"
PAT = "synthetic-canvas-private-token-one"


@pytest.fixture
def broker(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "_derive", lambda value, salt: hashlib.sha256(salt + value).digest())
    deliveries, calls = [], []
    def upstream(request):
        calls.append(request)
        assert request.url.path == "/api/v1/users/self/profile"
        return httpx.Response(200, json={"id": 11 if request.headers['authorization'].endswith('one') else 22, "name": "Fixture Student"})
    provider = BuiltByAadiProvider(issuer_url=BASE, database_path=tmp_path / "broker.db",
        encryption_key=Fernet.generate_key().decode(), mailer=lambda *args: deliveries.append(args),
        http_client_factory=lambda: httpx.Client(transport=httpx.MockTransport(upstream), follow_redirects=True))
    settings = provider.auth_settings
    app = Starlette(routes=provider.routes() + create_auth_routes(provider, settings.issuer_url,
        client_registration_options=settings.client_registration_options,
        revocation_options=settings.revocation_options))
    with TestClient(app, base_url=BASE) as client:
        yield client, provider, deliveries, calls, tmp_path / "broker.db"
    provider.store.close()


def fields(response):
    assert response.status_code == 200, response.text
    return dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', response.text))


def post(client, path, data, **kwargs):
    return client.post(path, data=data, headers={"Origin": BASE}, follow_redirects=False, **kwargs)


def create_login(client, provider, deliveries, email=EMAIL):
    response = post(client, '/account/signup', {**fields(client.get('/account/signup')), 'email': email, 'password': PASSWORD})
    assert response.status_code == 200, response.text
    code = deliveries[-1][2]
    response = post(client, '/account/verify', {**fields(client.get('/account/verify')), 'email': email, 'password': PASSWORD, 'code': code})
    assert response.status_code == 303
    response = post(client, '/account/login', {**fields(client.get('/account/login')), 'email': email, 'password': PASSWORD})
    assert response.status_code == 303, response.text
    return client.cookies.get(provider.session_cookie)


def connect_canvas(client, token=PAT, *, origin='https://school.example'):
    page = client.get('/setup')
    response = post(client, '/setup', {**fields(page), 'origin': origin, 'token': token, 'acknowledge': 'yes', 'operation': 'save'})
    assert response.status_code == 303, response.text
    assert token not in response.text
    return response


def authorize(client, client_id, write=False):
    start = client.get('/authorize', params={'client_id': client_id, 'response_type': 'code',
        'redirect_uri': REDIRECT, 'scope': 'canvas:read canvas:write', 'code_challenge': CHALLENGE,
        'code_challenge_method': 'S256', 'resource': BASE + '/mcp', 'state': 'state'}, follow_redirects=False)
    assert start.status_code == 302, start.text
    page = client.get(start.headers['location'], follow_redirects=False)
    form = fields(page)
    data = {**form, 'decision': 'allow'}
    if write:
        data['write'] = 'yes'
    response = post(client, '/connect', data)
    assert response.status_code == 303, response.text
    query = parse_qs(urlsplit(response.headers['location']).query)
    assert query['iss'] == [BASE + '/'] and query['state'] == ['state']
    token_response = exchange(client, client_id, query['code'][0])
    assert token_response.status_code == 200, token_response.text
    return token_response.json(), page


def test_complete_browser_flow_assistant_oauth_and_no_secret_leaks(broker):
    client, provider, deliveries, calls, db = broker
    session_token = create_login(client, provider, deliveries)
    assert len(deliveries) == 1
    assert 'httponly' in str(client.cookies).lower() or client.cookies.get(provider.session_cookie)
    page = client.get('/setup')
    assert page.text.index('Hosted beta') < page.text.index('name="token"')
    assert 'local.html' in page.text and 'school enables' in page.text and 'Cloudflare' in page.text
    connect_canvas(client)
    config = provider._connection(provider.accounts.session(session_token)['account_id'])
    assert config['user_id'] == '11' and len(calls) == 1
    client_id = register(client)
    tokens, consent = authorize(client, client_id)
    assert tokens['scope'] == 'canvas:read'
    assert 'checked' not in consent.text
    access = asyncio.run(provider.load_access_token(tokens['access_token']))
    resolved = asyncio.run(provider.get_credentials(access))
    assert resolved.token == PAT and resolved.base_url == 'https://school.example'
    grant = provider.store.get('grant', access.grant_id)
    assert 'credential' not in grant and 'token' not in grant and grant['connection_id'] == config['id']
    assert PAT not in consent.text and PAT not in client.get('/setup').text
    for secret in (PAT, PASSWORD, EMAIL, session_token, tokens['access_token'], tokens['refresh_token']):
        assert secret.encode() not in db.read_bytes()


def test_pending_oauth_survives_signup_and_setup_without_open_return_url(broker):
    client, provider, deliveries, *_ = broker
    client_id = register(client)
    start = client.get('/authorize', params={'client_id': client_id, 'response_type': 'code', 'redirect_uri': REDIRECT,
        'scope': 'canvas:read', 'code_challenge': CHALLENGE, 'code_challenge_method': 'S256', 'resource': BASE + '/mcp'}, follow_redirects=False)
    key = parse_qs(urlsplit(start.headers['location']).query)['request'][0]
    login = client.get(start.headers['location'], follow_redirects=False)
    assert login.headers['location'] == '/account/login?request=' + key
    page = client.get('/account/login', params={'request': 'https://evil.example'})
    assert fields(page)['request'] == '' and 'evil.example' not in page.text
    create_login(client, provider, deliveries)
    setup = client.get('/connect', params={'request': key}, follow_redirects=False)
    assert setup.headers['location'] == '/setup?request=' + key
    form = fields(client.get(setup.headers['location']))
    saved = post(client, '/setup', {**form, 'origin': 'https://school.example', 'token': PAT, 'acknowledge': 'yes', 'operation': 'save'})
    assert saved.headers['location'] == '/connect?request=' + key
    consent = client.get(saved.headers['location'])
    assert consent.status_code == 200 and 'Connect assistant' in consent.text


@pytest.mark.parametrize('operation', ['signup', 'login', 'verify', 'reset'])
def test_public_forms_require_origin_cookie_csrf_and_one_use(broker, operation):
    client, _, _, calls, _ = broker
    data = {**fields(client.get('/account/' + operation)), 'email': EMAIL, 'password': PASSWORD, 'code': 'x', 'stage': 'request'}
    wrong_origin = client.post('/account/' + operation, data=data, headers={'Origin': 'https://evil.example'})
    assert wrong_origin.status_code == 400
    wrong_csrf = post(client, '/account/' + operation, {**data, 'csrf': 'wrong'})
    assert wrong_csrf.status_code == 400
    first = post(client, '/account/' + operation, data)
    assert first.status_code in {200, 400, 303}
    replay = post(client, '/account/' + operation, data)
    assert replay.status_code == 400 and 'expired' in replay.text
    assert not calls


def test_login_rotates_session_and_cookie_flags(broker):
    client, provider, deliveries, *_ = broker
    old = create_login(client, provider, deliveries)
    response = post(client, '/account/login', {**fields(client.get('/account/login')), 'email': EMAIL, 'password': PASSWORD})
    new = client.cookies.get(provider.session_cookie)
    assert old != new and provider.accounts.session(old) is None and provider.accounts.session(new)
    cookie = response.headers['set-cookie'].lower()
    assert 'secure' in cookie and 'httponly' in cookie and 'samesite=lax' in cookie and 'domain=' not in cookie


def test_failed_mail_transport_does_not_enumerate_existing_accounts(broker):
    client, provider, deliveries, _, _ = broker
    create_login(client, provider, deliveries)
    def broken(*args):
        raise RuntimeError('private-mail-error')
    provider.accounts.mailer = broken
    for operation, extra in [('signup', {'password': PASSWORD}), ('reset', {'stage': 'request'})]:
        responses = []
        for email in [EMAIL, 'unknown@example.test']:
            response = post(client, '/account/' + operation, {**fields(client.get('/account/' + operation)), 'email': email, **extra})
            responses.append((response.status_code, response.text))
        assert responses[0] == responses[1]
        assert responses[0][0] == 200 and 'private-mail-error' not in responses[0][1]
    assert not provider.store.scan('account_reset') and not provider.store.scan('account_verify')


@pytest.mark.parametrize('url', ['http://school.example', 'https://127.0.0.1', 'https://user:password@school.example',
                                 'https://school.example/api/v1', 'https://school.example?token=secret'])
def test_unsafe_origins_refused_without_http(broker, url):
    client, provider, deliveries, calls, _ = broker
    create_login(client, provider, deliveries)
    response = post(client, '/setup', {**fields(client.get('/setup')), 'origin': url, 'token': PAT, 'acknowledge': 'yes', 'operation': 'save'})
    assert response.status_code == 400 and not calls


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308, 401, 403, 500])
def test_profile_denial_or_redirect_does_not_save_or_forward_token(broker, status):
    client, provider, deliveries, _, _ = broker
    session = create_login(client, provider, deliveries)
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status, headers={'location': 'https://evil.example/collect'})
    provider.http_client_factory = lambda: httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True)
    response = post(client, '/setup', {**fields(client.get('/setup')), 'origin': 'https://school.example', 'token': PAT, 'acknowledge': 'yes', 'operation': 'save'})
    assert response.status_code == 400 and len(calls) == 1
    assert calls[0].url.host == 'school.example' and PAT not in response.text
    assert provider._connection(provider.accounts.session(session)['account_id']) is None


@pytest.mark.parametrize('profile', [{'id': True}, {'id': -1}, {'id': '11'}, [], {'id': 11, 'name': 'x' * 270000}])
def test_invalid_or_oversized_profile_is_not_saved(broker, profile):
    client, provider, deliveries, _, _ = broker
    create_login(client, provider, deliveries)
    provider.http_client_factory = lambda: httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=profile)))
    response = post(client, '/setup', {**fields(client.get('/setup')), 'origin': 'https://school.example', 'token': PAT, 'acknowledge': 'yes', 'operation': 'save'})
    assert response.status_code == 400


def test_replace_revokes_grants_actions_and_does_not_refresh_canvas_pat(broker):
    client, provider, deliveries, calls, _ = broker
    session = create_login(client, provider, deliveries)
    connect_canvas(client)
    client_id = register(client)
    tokens, _ = authorize(client, client_id, write=True)
    access = asyncio.run(provider.load_access_token(tokens['access_token']))
    provider.store.put('action', 'old-action', {'state': 'prepared'}, expires=9999999999, owner=access.grant_id)
    provider.store.put('continuation', 'old-result', {'text': 'private'}, expires=9999999999, owner=access.grant_id)
    connect_canvas(client, 'synthetic-canvas-private-token-two')
    assert asyncio.run(provider.load_access_token(tokens['access_token'])) is None
    assert provider.store.get('action', 'old-action') is None and provider.store.get('continuation', 'old-result') is None
    with pytest.raises(OwnerRevokedError):
        provider.store.put('action', 'old-action', {}, expires=9999999999, owner=access.grant_id)
    refresh = client.post('/token', data={'client_id': client_id, 'grant_type': 'refresh_token', 'refresh_token': tokens['refresh_token']})
    assert refresh.status_code == 400
    assert provider._connection(provider.accounts.session(session)['account_id'])['user_id'] == '22'
    assert [request.url.path for request in calls] == ['/api/v1/users/self/profile'] * 2


def test_refresh_rotates_only_assistant_tokens_and_replay_revokes(broker):
    client, provider, deliveries, calls, _ = broker
    create_login(client, provider, deliveries)
    connect_canvas(client)
    client_id = register(client)
    tokens, _ = authorize(client, client_id)
    response = client.post('/token', data={'client_id': client_id, 'grant_type': 'refresh_token', 'refresh_token': tokens['refresh_token']})
    assert response.status_code == 200
    rotated = response.json()
    assert asyncio.run(provider.load_access_token(tokens['access_token'])) is None
    assert asyncio.run(provider.load_access_token(rotated['access_token']))
    assert client.post('/token', data={'client_id': client_id, 'grant_type': 'refresh_token', 'refresh_token': tokens['refresh_token']}).status_code == 400
    assert asyncio.run(provider.load_access_token(rotated['access_token'])) is None and len(calls) == 1


def test_cross_account_sessions_connections_and_grants_are_isolated(broker):
    client, provider, deliveries, _, _ = broker
    first_session = create_login(client, provider, deliveries)
    connect_canvas(client)
    client_id = register(client)
    first, _ = authorize(client, client_id)
    client.cookies.clear()
    second_session = create_login(client, provider, deliveries, 'other@example.test')
    connect_canvas(client, 'synthetic-canvas-private-token-two', origin='https://second-school.example')
    second, _ = authorize(client, client_id)
    assert provider.accounts.session(first_session)['account_id'] != provider.accounts.session(second_session)['account_id']
    a = asyncio.run(provider.get_credentials(first['access_token']))
    b = asyncio.run(provider.get_credentials(second['access_token']))
    assert a.token == PAT and b.token.endswith('two') and a.base_url != b.base_url
    provider.disconnect(second_session)
    assert asyncio.run(provider.load_access_token(first['access_token']))
    assert asyncio.run(provider.load_access_token(second['access_token'])) is None


def test_stale_consent_and_forged_account_connection_reference_are_denied(broker):
    client, provider, deliveries, _, _ = broker
    session = create_login(client, provider, deliveries)
    connect_canvas(client)
    client_id = register(client)
    tokens, _ = authorize(client, client_id)
    access = asyncio.run(provider.load_access_token(tokens['access_token']))
    record = provider.store.record('grant', access.grant_id)
    record.value['account_id'] = 'someone-else'
    provider.store.put('grant', access.grant_id, record.value, expires=record.expires, owner=record.owner)
    assert asyncio.run(provider.load_access_token(tokens['access_token'])) is None
    assert client.post('/token', data={'client_id': client_id, 'grant_type': 'refresh_token', 'refresh_token': tokens['refresh_token']}).status_code == 400
    assert provider.accounts.session(session)


def test_competing_replacements_only_one_credential_commits(broker):
    client, provider, deliveries, _, _ = broker
    session = create_login(client, provider, deliveries)
    connect_canvas(client)
    account_id = provider.accounts.session(session)['account_id']
    version = provider._connection(account_id)['version']
    barrier = threading.Barrier(2)
    original = provider._verify_canvas
    def paused(origin, token):
        result = original(origin, token)
        barrier.wait(timeout=5)
        return result
    provider._verify_canvas = paused
    def save(token):
        try:
            provider.save_connection(session, 'https://school.example', token, version)
            return True
        except AccountError:
            return False
    with ThreadPoolExecutor(2) as executor:
        outcomes = list(executor.map(save, ['synthetic-canvas-private-token-two', 'synthetic-canvas-private-token-three']))
    assert sorted(outcomes) == [False, True]


def test_disconnect_fences_first_use_profile_verification(broker):
    client, provider, deliveries, _, _ = broker
    session = create_login(client, provider, deliveries)
    account_id = provider.accounts.session(session)['account_id']
    original = provider._verify_canvas
    def concurrent_disconnect(origin, token):
        result = original(origin, token)
        provider.disconnect(session)
        return result
    provider._verify_canvas = concurrent_disconnect
    with pytest.raises(AccountError, match='changed'):
        provider.save_connection(session, 'https://school.example', PAT, '')
    assert provider._connection(account_id) is None
    assert provider._connection_version(account_id)


def test_consent_cannot_complete_after_credential_replacement(broker):
    client, provider, deliveries, _, _ = broker
    create_login(client, provider, deliveries)
    connect_canvas(client)
    client_id = register(client)
    start = client.get('/authorize', params={'client_id': client_id, 'response_type': 'code', 'redirect_uri': REDIRECT,
        'scope': 'canvas:read', 'code_challenge': CHALLENGE, 'code_challenge_method': 'S256'}, follow_redirects=False)
    consent_fields = fields(client.get(start.headers['location']))
    connect_canvas(client, 'synthetic-canvas-private-token-two')
    result = post(client, '/connect', {**consent_fields, 'decision': 'allow'})
    assert result.status_code == 403 and not provider.store.scan('grant')


def test_password_reset_and_account_deletion_revoke_assistant_access(broker):
    client, provider, deliveries, _, _ = broker
    session = create_login(client, provider, deliveries)
    connect_canvas(client)
    tokens, _ = authorize(client, register(client))
    provider.accounts.request_reset(EMAIL, 'peer')
    provider.accounts.reset(EMAIL, PASSWORD + 'new', deliveries[-1][2], 'peer')
    assert provider.accounts.session(session) is None
    assert asyncio.run(provider.load_access_token(tokens['access_token'])) is None
    current = provider.accounts.login(EMAIL, PASSWORD + 'new', 'peer')
    account_id = provider.accounts.session(current)['account_id']
    provider.accounts.delete(current, PASSWORD + 'new', 'peer')
    assert provider._connection(account_id) is None and provider.accounts.active(account_id) is None


def test_real_http_mcp_uses_broker_identity_and_revocation(broker, monkeypatch):
    from canvas_py import Canvas
    import canvas_connector.security as security
    client, provider, deliveries, _, _ = broker
    session = create_login(client, provider, deliveries)
    connect_canvas(client)
    tokens, _ = authorize(client, register(client))
    seen = []
    def factory(config):
        def handle(request):
            seen.append(request.headers['authorization'])
            assert request.url.path == '/api/v1/users/self/profile'
            return httpx.Response(200, json={'id': 11, 'name': 'Fixture Student'})
        canvas = Canvas(config, http_client=httpx.Client(transport=httpx.MockTransport(handle)))
        canvas._http._owns_client = True
        return canvas
    monkeypatch.setattr(security, 'guarded_canvas', factory)
    with TestClient(create_http_app(provider), base_url=BASE) as mcp:
        account_page = mcp.get('/account/login')
        assert set(value.strip() for value in account_page.headers['referrer-policy'].split(',')) == {'same-origin'}
        headers = {'Accept': 'application/json, text/event-stream', 'Authorization': 'Bearer ' + tokens['access_token']}
        result = mcp.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}})
        assert result.status_code == 200
        result = mcp.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'canvas_connection_status', 'arguments': {}}})
        assert result.status_code == 200 and '11' in result.text and not result.json()['result'].get('isError')
        assert PAT not in result.text and seen == ['Bearer ' + PAT]
        provider.disconnect(session)
        assert mcp.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'}).status_code == 401


def test_default_provider_cannot_silently_enable_personal_mode(tmp_path):
    with pytest.raises(ValueError, match='broker'):
        CanvasOAuthProvider(issuer_url=BASE, institutions=[], credential_mode='personal',
            database_path=tmp_path / 'bad.db', encryption_key=Fernet.generate_key().decode())


def test_browser_auth_keeps_strict_origin_despite_referrer_policy_change(broker):
    client, _, _, calls, _ = broker
    page = client.get('/account/signup')
    assert page.headers['referrer-policy'] == 'same-origin'
    response = client.post('/account/signup', data={**fields(page), 'email': EMAIL, 'password': PASSWORD},
                           headers={'Origin': 'null'})
    assert response.status_code == 400 and not calls


@pytest.mark.parametrize('target', ['https://example.test;img-src*/callback', "https://example.test'foo/callback",
    'https://example.test\n/callback', 'https://user:pass@example.test/callback', 'javascript:alert(1)',
    'http://example.test/callback', 'https://example.test:bad/callback', 'https://example.test/callback#fragment'])
def test_csp_sources_reject_header_and_policy_injection(target):
    with pytest.raises(ValueError):
        _headers(form_redirects=(target,))


@pytest.mark.parametrize(('target', 'origin'), [
    ('https://client.example/callback?request=abc', 'https://client.example'),
    ('https://CLIENT.example:443/a/b', 'https://client.example'),
    ('https://client.example:8443/callback', 'https://client.example:8443'),
    ('http://127.0.0.1:9999/callback', 'http://127.0.0.1:9999'),
    ('http://[::1]:9999/callback', 'http://[::1]:9999'),
    ('https://bücher.example/callback', 'https://xn--bcher-kva.example'),
])
def test_csp_sources_are_origins_without_callback_path_or_query(target, origin):
    assert _form_origin(target) == origin
    csp = _headers(form_redirects=(target, target))['Content-Security-Policy']
    assert f"form-action 'self' {origin};" in csp and '/callback' not in csp and 'request=' not in csp


def test_broker_consent_only_allows_registered_callback_origin(broker):
    client, provider, deliveries, *_ = broker
    create_login(client, provider, deliveries)
    connect_canvas(client)
    _, page = authorize(client, register(client))
    csp = page.headers['content-security-policy']
    assert "form-action 'self' https://client.example;" in csp
    assert 'school.example' not in csp
    assert "form-action 'self';" in client.get('/setup').headers['content-security-policy']


def test_institution_consent_allows_only_configured_canvas_and_registered_cancel(broker):
    _, broker_provider, *_ = broker
    provider = CanvasOAuthProvider(issuer_url=BASE, store=broker_provider.store,
        institutions=[Institution('https://school.example', 'synthetic', 'synthetic-secret')])
    settings = provider.auth_settings
    app = Starlette(routes=provider.routes() + create_auth_routes(provider, settings.issuer_url,
        client_registration_options=settings.client_registration_options,
        revocation_options=settings.revocation_options))
    with TestClient(app, base_url=BASE) as client:
        _, page = begin(client, register(client))
        assert "form-action 'self' https://client.example https://school.example;" in page.headers['content-security-policy']
