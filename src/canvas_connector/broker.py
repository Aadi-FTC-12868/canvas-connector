"""Built by Aadi browser accounts and per-account Canvas credential brokering.

Assistant OAuth tokens are distinct from browser sessions and Canvas tokens.
The operator must separately resolve Canvas API-policy and deployment approval;
enabling this implementation does not establish permission to collect tokens.
"""
from __future__ import annotations

from html import escape
import hmac
import json
import re
import secrets
import time
from urllib.parse import urlencode, urlsplit

from canvas_py._config import CanvasConfig
from mcp.server.auth.provider import TokenError
from mcp.server.auth.routes import cors_middleware, create_protected_resource_routes
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route
import anyio

from .accounts import AccountError, AccountService, MailUnavailable, RateLimited, SESSION_SECONDS, ACCOUNT_SECONDS
from .auth import CanvasOAuthProvider, GrantAuthorizationCode, GRANT_SECONDS, SCOPES, _headers, _redirect, _offload_storage
from .security import validate_canvas_origin
from .storage import digest

_OPAQUE = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
_LOCAL_SETUP = "https://builtbyaadi.com/canvas-connector/local.html"
_STYLE = """body{margin:0;background:#101010;color:#f5f3f0;font:17px/1.6 Inter,system-ui,sans-serif}
main{max-width:660px;margin:5vh auto;padding:24px}a{color:#f4a8c1;text-underline-offset:.2em}
h1{font:700 clamp(2rem,6vw,3rem)/1.15 Syne,system-ui,sans-serif;letter-spacing:-.04em}h2{font-size:1.2rem}
label{display:block;margin:18px 0 6px}input{box-sizing:border-box;width:100%;padding:12px;background:#202020;color:#fff;border:1px solid #777;border-radius:6px;font:inherit}
input[type=checkbox]{width:auto;margin-right:10px}button{padding:12px 18px;border:1px solid #bb5878;border-radius:6px;background:#812943;color:#fff;font:inherit;cursor:pointer;margin:20px 10px 0 0}
button.secondary{background:transparent}button:focus-visible,input:focus-visible,a:focus-visible{outline:3px solid #f4a8c1;outline-offset:3px}
.notice{border-left:4px solid #b75b79;padding:4px 20px;margin:28px 0;background:#1c1619}.small{font-size:.85rem;color:#c5bec1}.error{color:#ffd0d0}nav{display:flex;gap:18px;flex-wrap:wrap}hr{border:0;border-top:1px solid #444;margin:32px 0}code{overflow-wrap:anywhere}form{margin:20px 0}fieldset{border:0;padding:0}summary{cursor:pointer}
"""


def _field(name: str, label: str, *, kind: str = "text", autocomplete: str = "", required=True) -> str:
    extra = ' required' if required else ''
    if kind == "password":
        extra += ' minlength="12" maxlength="128"'
    elif kind == "email":
        extra += ' maxlength="254"'
    return f'<label for="{name}">{label}</label><input id="{name}" name="{name}" type="{kind}" autocomplete="{autocomplete}"{extra}>'


def _hidden(name: str, value: str) -> str:
    return f'<input type="hidden" name="{name}" value="{escape(value, quote=True)}">'


class BuiltByAadiProvider(CanvasOAuthProvider):
    public_auth_mode = "builtbyaadi"

    def __init__(self, *, mailer=None, local_setup_url: str = _LOCAL_SETUP, **kwargs):
        if "institutions" in kwargs or "credential_mode" in kwargs:
            raise ValueError("Account broker does not accept institution configuration")
        super().__init__(institutions=[], credential_mode="personal", **kwargs)
        parsed = urlsplit(local_setup_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Local setup link must be an HTTPS URL")
        self.local_setup_url = local_setup_url
        self.account_setup_available = mailer is not None
        self.session_cookie = "__Host-bba-session" if self.secure_cookie else "bba-session-dev"
        self.form_cookie = "__Host-bba-form" if self.secure_cookie else "bba-form-dev"
        self.accounts = AccountService(self.store, mailer=mailer, on_security_change=self._revoke_account_grants)

    def routes(self):
        return create_protected_resource_routes(self.auth_settings.resource_server_url,
            [self.auth_settings.issuer_url], scopes_supported=SCOPES, resource_name="Built by Aadi Canvas Connector") + [
            Route("/revoke", cors_middleware(self.revoke_request, ["POST", "OPTIONS"]), methods=["POST", "OPTIONS"]),
            Route("/connect", self.connect_get, methods=["GET"]),
            Route("/connect", self.connect_post, methods=["POST"]),
            Route("/setup", self.setup_get, methods=["GET"]),
            Route("/setup", self.setup_post, methods=["POST"]),
            Route("/account/{operation}", self.account_get, methods=["GET"]),
            Route("/account/{operation}", self.account_post, methods=["POST"]),
        ]

    def _page(self, title, body, *, status=200, form_redirects=()):
        return HTMLResponse(f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)} · Built by Aadi</title><style>{_STYLE}</style></head><body><main><nav><a href="https://builtbyaadi.com">Built by Aadi</a><a href="{escape(self.local_setup_url, quote=True)}">Set up locally</a></nav><h1>{escape(title)}</h1>{body}</main></body></html>', status_code=status, headers=_headers(form_redirects=form_redirects))

    def _redirect(self, path):
        return RedirectResponse(path, status_code=303, headers=_headers())

    def _pending_key(self, value):
        return value if isinstance(value, str) and _OPAQUE.fullmatch(value) and self.store.get("pending", value) else ""

    @staticmethod
    def _path(path, pending=""):
        return path + ("?" + urlencode({"request": pending}) if pending else "")

    def _session(self, request):
        return self.accounts.session(request.cookies.get(self.session_cookie, ""))

    def _browser_fields(self, request, purpose, pending):
        csrf = secrets.token_urlsafe(32)
        self.store.put("browser_form", csrf, {"purpose": purpose}, expires=time.time() + 600)
        return csrf, _hidden("csrf", csrf) + _hidden("request", pending)

    def _set_form_cookie(self, response, csrf):
        response.set_cookie(self.form_cookie, csrf, max_age=600, secure=self.secure_cookie,
                            httponly=True, samesite="lax", path="/")
        return response

    def _check_form(self, request, form, purpose, session=None):
        if request.headers.get("origin") != self.issuer_url:
            raise AccountError("This form came from an invalid origin. Reload the page.")
        token = form.get("csrf", "")
        if not isinstance(token, str) or len(token) > 128:
            raise AccountError("This form expired. Reload the page.")
        if session:
            if not token or not hmac.compare_digest(token, session["csrf"]):
                raise AccountError("This form expired. Reload the page.")
            return
        cookie = request.cookies.get(self.form_cookie, "")
        if not token or not cookie or not hmac.compare_digest(token, cookie):
            raise AccountError("This form expired. Reload the page.")
        with self.store.transaction():
            saved = self.store.get("browser_form", token)
            if not saved or saved["purpose"] != purpose:
                raise AccountError("This form expired. Reload the page.")
            self.store.delete("browser_form", token)

    @_offload_storage
    def account_get(self, request):
        operation = request.path_params["operation"]
        if operation not in {"signup", "login", "verify", "reset"}:
            return self._page("Page not found", "<p>Use the account sign-in page.</p>", status=404)
        pending = self._pending_key(request.query_params.get("request"))
        csrf, hidden = self._browser_fields(request, operation, pending)
        titles = {"signup": "Create your account", "login": "Sign in", "verify": "Verify your email", "reset": "Reset your password"}
        body = ""
        if operation == "signup":
            if not self.account_setup_available:
                return self._page("Account setup is not available yet", '<p>Email verification is not configured. You can use the local connector now.</p>', status=503)
            body = '<p>Use an email address you control. We will send a verification code.</p>'
        if operation == "verify":
            body = '<p>Enter the code from your email and the password you chose when creating this account. Codes expire after 20 minutes.</p>'
        if operation == "reset":
            body = '<p>Request a reset code, then use it with a new password. Resetting signs out existing sessions and disconnects AI assistants.</p>'
        body += f'<form method="post" action="/account/{operation}">{hidden}'
        body += _field("email", "Email address", kind="email", autocomplete="email")
        if operation in {"signup", "login", "verify"}:
            body += _field("password", "Password", kind="password", autocomplete="new-password" if operation == "signup" else "current-password")
        if operation == "verify":
            body += _field("code", "Verification code", autocomplete="one-time-code")
        if operation == "reset":
            body += '<button type="submit" name="stage" value="request">Email a reset code</button><hr>'
            body += _field("code", "Reset code", autocomplete="one-time-code", required=False)
            body += _field("password", "New password", kind="password", autocomplete="new-password", required=False)
            body += '<button type="submit" name="stage" value="finish">Set new password</button>'
        else:
            body += '<button type="submit">' + {"signup": "Create account", "login": "Sign in", "verify": "Verify email"}[operation] + '</button>'
        body += '</form><nav>'
        for route, label in (("login", "Sign in"), ("signup", "Create account"), ("verify", "Verify email"), ("reset", "Forgot password")):
            if route != operation:
                body += f'<a href="{escape(self._path("/account/" + route, pending), quote=True)}">{label}</a>'
        return self._set_form_cookie(self._page(titles[operation], body + '</nav>'), csrf)

    async def account_post(self, request):
        form = await request.form()
        return await anyio.to_thread.run_sync(self._account_post, request, form)

    def _account_post(self, request, form):
        operation = request.path_params["operation"]
        if operation not in {"signup", "login", "verify", "reset", "logout", "delete"}:
            return self._page("Page not found", "<p>Use the account sign-in page.</p>", status=404)
        pending = self._pending_key(form.get("request"))
        session = self._session(request) if operation in {"logout", "delete"} else None
        try:
            if operation in {"logout", "delete"} and not session:
                return self._redirect(self._path("/account/login", pending))
            self._check_form(request, form, operation, session=session)
            peer = request.client.host if request.client else "unknown"
            if operation == "signup":
                self.accounts.signup(form.get("email", ""), form.get("password", ""), peer)
                return self._page("Check your email", '<p>If this address can be registered and email delivery is available, a verification code will arrive shortly. If it does not arrive, try again later.</p><p><a href="' + escape(self._path('/account/verify', pending), quote=True) + '">Enter verification code</a></p>')
            if operation == "verify":
                self.accounts.verify(form.get("email", ""), form.get("password", ""), form.get("code", ""), peer)
                return self._redirect(self._path("/account/login", pending))
            if operation == "login":
                token = self.accounts.login(form.get("email", ""), form.get("password", ""), peer)
                self.accounts.logout(request.cookies.get(self.session_cookie, ""))
                response = self._redirect(self._path("/connect" if pending else "/setup", pending))
                response.set_cookie(self.session_cookie, token, max_age=SESSION_SECONDS,
                    secure=self.secure_cookie, httponly=True, samesite="lax", path="/")
                return response
            if operation == "reset":
                if form.get("stage") == "request":
                    self.accounts.request_reset(form.get("email", ""), peer)
                    return self._page("Check your email", '<p>If a verified account exists and email delivery is available, a reset code will arrive shortly. If it does not arrive, try again later.</p><p><a href="' + escape(self._path('/account/reset', pending), quote=True) + '">Enter reset code</a></p>')
                if form.get("stage") != "finish":
                    raise AccountError("Choose a reset action.")
                self.accounts.reset(form.get("email", ""), form.get("password", ""), form.get("code", ""), peer)
                response = self._redirect(self._path("/account/login", pending))
            elif operation == "delete":
                self.accounts.delete(request.cookies.get(self.session_cookie, ""), form.get("password", ""), peer)
                response = self._page("Account deleted", '<p>Your stored Canvas credential and assistant connections have been removed. You can also revoke the token in Canvas.</p>')
            else:
                self.accounts.logout(request.cookies.get(self.session_cookie, ""))
                response = self._redirect("/account/login")
            response.delete_cookie(self.session_cookie, path="/", secure=self.secure_cookie, httponly=True, samesite="lax")
            return response
        except AccountError as exc:
            status = 503 if isinstance(exc, MailUnavailable) else 429 if isinstance(exc, RateLimited) else 400
            return self._page("Account request could not be completed", '<p class="error">' + escape(str(exc)) + '</p><p><a href="' + escape(self._path('/account/' + (operation if operation not in {'logout', 'delete'} else 'login'), pending), quote=True) + '">Return to the form</a></p>', status=status)

    def _revoke_account_grants(self, account_id):
        # The caller holds a transaction. Each grant's owner tombstone also
        # fences old action/continuation requests after connection replacement.
        for row in self.store.scan("account_grant", owner="account:" + account_id):
            grant_id = row.value["grant_id"]
            self.store.revoke_owner(grant_id)
            self.store.delete("account_grant", grant_id)

    def _connection(self, account_id):
        value = self.store.get("canvas_connection", account_id)
        return value if value and value.get("account_id") == account_id and value.get("status") == "active" else None

    def _connection_version(self, account_id):
        # Keep a generation even after removing a connection. A delete during
        # first-use profile verification must fence that in-flight setup too.
        epoch = self.store.get("canvas_connection_epoch", account_id)
        return epoch["version"] if epoch else ""

    def _grant_active(self, grant):
        if not grant or grant.get("mode") != "personal" or not self.accounts.active(grant.get("account_id", "")):
            return False
        connection = self._connection(grant["account_id"])
        return bool(connection and connection["id"] == grant.get("connection_id")
                    and connection["version"] == grant.get("credential_version")
                    and connection["user_id"] == grant.get("user_id"))

    def _credential_state(self, token):
        with self.store.transaction():
            access = self.validate_access(token)
            if not access:
                raise PermissionError("Connection expired or was revoked. Reconnect your account.")
            grant = self.store.get("grant", access.grant_id)
            connection = self._connection(grant["account_id"])
            return CanvasConfig(base_url=connection["origin"], token=connection["token"]), access.grant_id, None

    def _verify_canvas(self, origin, token):
        if not isinstance(origin, str) or len(origin) > 2048:
            raise AccountError("Enter your Canvas HTTPS origin without a path.")
        try:
            origin = validate_canvas_origin(origin.strip())
        except ValueError:
            raise AccountError("Enter your Canvas HTTPS origin without a path.") from None
        if (not isinstance(token, str) or not 20 <= len(token) <= 8192
                or any(not 33 <= ord(char) <= 126 for char in token)):
            raise AccountError("Enter a valid Canvas access token.")
        try:
            with self.http_client_factory() as client:
                with client.stream("GET", origin + "/api/v1/users/self/profile",
                        headers={"Authorization": "Bearer " + token}, follow_redirects=False) as response:
                    if response.status_code != 200:
                        raise ValueError("Profile not available")
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 256 * 1024:
                            raise ValueError("Profile too large")
                        chunks.append(chunk)
                    profile = json.loads(b"".join(chunks))
            user_id = profile.get("id") if isinstance(profile, dict) else None
            if type(user_id) is not int or user_id <= 0:
                raise ValueError("Profile did not identify an account")
            name = profile.get("name")
            name = name.replace(token, "[redacted]")[:200] if isinstance(name, str) else "Canvas user"
            return origin, str(user_id), name
        except Exception:
            raise AccountError("Canvas could not verify this URL and token. Check them in Canvas and try again.") from None

    def save_connection(self, session_token, origin, token, expected_version):
        session = self.accounts.session(session_token)
        if not session:
            raise AccountError("Sign in again before connecting Canvas.")
        if self._connection_version(session["account_id"]) != expected_version:
            raise AccountError("Your connection changed. Reload setup before trying again.")
        self.accounts._rate("canvas_setup", session["email"], session["account_id"])
        origin, user_id, name = self._verify_canvas(origin, token)
        with self.store.transaction():
            current_session = self.accounts.session(session_token)
            if (not current_session or current_session["auth_version"] != session["auth_version"]
                    or self._connection_version(session["account_id"]) != expected_version):
                raise AccountError("Your connection changed. Reload setup before trying again.")
            self._revoke_account_grants(session["account_id"])
            connection = {"id": secrets.token_urlsafe(24), "version": secrets.token_urlsafe(24),
                "account_id": session["account_id"], "origin": origin, "token": token,
                "user_id": user_id, "name": name, "status": "active", "verified_at": time.time()}
            self.store.put("canvas_connection", session["account_id"], connection,
                expires=time.time() + ACCOUNT_SECONDS, owner="account:" + session["account_id"])
            self.store.put("canvas_connection_epoch", session["account_id"], {"version": connection["version"]},
                expires=time.time() + ACCOUNT_SECONDS, owner="account:" + session["account_id"])

    def disconnect(self, session_token):
        with self.store.transaction():
            session = self.accounts.session(session_token)
            if not session:
                raise AccountError("Sign in again before disconnecting.")
            self._revoke_account_grants(session["account_id"])
            self.store.delete("canvas_connection", session["account_id"])
            self.store.put("canvas_connection_epoch", session["account_id"], {"version": secrets.token_urlsafe(24)},
                expires=time.time() + ACCOUNT_SECONDS, owner="account:" + session["account_id"])

    @_offload_storage
    def setup_get(self, request):
        pending = self._pending_key(request.query_params.get("request"))
        session = self._session(request)
        if not session:
            return self._redirect(self._path("/account/login", pending))
        connection = self._connection(session["account_id"])
        fields = _hidden("csrf", session["csrf"]) + _hidden("request", pending)
        body = '<p>Signed in as ' + escape(session["email"]) + '.</p>'
        if connection:
            body += '<h2>Canvas connected</h2><p>' + escape(connection["name"]) + ' · ' + escape(connection["origin"]) + '</p><p class="small">Replacing this credential disconnects existing AI assistants. Reconnect them after saving.</p>'
        body += '<aside class="notice" aria-labelledby="beta-title"><h2 id="beta-title">Hosted beta</h2><p>This hosted connector is in beta. Bugs, outages, or unauthorized access could expose your Canvas data. Use it at your own risk; security and availability are not guaranteed.</p><p><strong>We strongly recommend local setup</strong> if you prefer to keep your Canvas token on your own computer. Local setup gives you more control, but still depends on your device and configuration.</p><p><a href="' + escape(self.local_setup_url, quote=True) + '">Set up locally</a></p><p class="small">To the extent permitted by applicable law, Built by Aadi disclaims warranties and liability for loss, outages, or unauthorized disclosure arising from use of this beta. This notice does not exclude rights or liabilities that cannot legally be excluded.</p></aside>'
        body += '<form method="post" action="/setup">' + fields + _hidden("connection_version", self._connection_version(session["account_id"]))
        body += '<h2>' + ('Replace Canvas connection' if connection else 'Connect your Canvas') + '</h2><p>In Canvas, open Account → Settings → Approved Integrations → New Access Token. This option is available only if your school enables it. If it is absent, use the options your school permits; this service cannot enable it.</p>'
        body += '<p>Use a dedicated token with an expiration date. Copy your school’s Canvas origin, such as https://school.instructure.com, without a page path.</p>'
        body += '<p class="small">Canvas’s published API policy requires multi-user applications to obtain tokens through Canvas OAuth. This beta uses a manually supplied token and is not institution-approved. <a href="https://developerdocs.instructure.com/services/canvas/oauth2/file.oauth">Read the Canvas policy</a>.</p>'
        body += _field("origin", "Canvas URL", kind="url", autocomplete="off")
        body += '<label for="token">Canvas access token</label><input id="token" name="token" type="password" autocomplete="off" maxlength="8192" required>'
        body += '<p class="small">Your token is stored encrypted in our Cloudflare infrastructure and sent to your Canvas server to authenticate requests. It is never provided to ChatGPT or Claude. Your assistant receives the Canvas data you authorize it to request.</p>'
        body += '<label><input type="checkbox" name="acknowledge" value="yes" required>I understand the beta risks and have permission to connect this Canvas account.</label><button type="submit" name="operation" value="save">Verify and save Canvas connection</button></form>'
        if connection:
            body += '<p>Use <code>' + escape(self.resource_url) + '</code> in your assistant’s connector settings. It will open Built by Aadi to request your consent.</p>'
            if pending:
                body += '<p><a href="' + escape(self._path('/connect', pending), quote=True) + '">Continue connecting your assistant</a></p>'
            body += '<form method="post" action="/setup">' + fields + '<button class="secondary" name="operation" value="revoke">Disconnect all AI assistants</button><button class="secondary" name="operation" value="disconnect">Remove stored Canvas credential</button></form>'
        body += '<hr><form method="post" action="/account/logout">' + fields + '<button class="secondary">Sign out</button></form>'
        body += '<details><summary>Delete Built by Aadi account</summary><p>This removes your stored Canvas credential, account, and assistant connections. It does not revoke the original token in Canvas.</p><form method="post" action="/account/delete">' + fields + _field('password', 'Confirm password', kind='password', autocomplete='current-password') + '<button>Delete account</button></form></details>'
        return self._page("Set up your connector", body)

    async def setup_post(self, request):
        form = await request.form()
        return await anyio.to_thread.run_sync(self._setup_post, request, form)

    def _setup_post(self, request, form):
        pending = self._pending_key(form.get("request"))
        session = self._session(request)
        if not session:
            return self._redirect(self._path("/account/login", pending))
        try:
            self._check_form(request, form, "setup", session=session)
            operation = form.get("operation")
            if operation == "save":
                if form.get("acknowledge") != "yes":
                    raise AccountError("Read and acknowledge the beta notice before continuing.")
                self.save_connection(request.cookies[self.session_cookie], form.get("origin", ""),
                                     form.get("token", ""), form.get("connection_version", ""))
            elif operation == "disconnect":
                self.disconnect(request.cookies[self.session_cookie])
            elif operation == "revoke":
                with self.store.transaction():
                    if not self._session(request):
                        raise AccountError("Sign in again before disconnecting assistants.")
                    self._revoke_account_grants(session["account_id"])
            else:
                raise AccountError("Choose a setup action.")
            return self._redirect(self._path("/connect" if pending and operation == "save" else "/setup", pending))
        except AccountError as exc:
            return self._page("Canvas setup could not be completed", '<p class="error">' + escape(str(exc)) + '</p><p><a href="' + escape(self._path('/setup', pending), quote=True) + '">Return to setup</a></p>', status=429 if isinstance(exc, RateLimited) else 400)

    @_offload_storage
    def connect_get(self, request):
        pending_key = self._pending_key(request.query_params.get("request"))
        if not pending_key:
            return self._page("Connection request expired", '<p>Start the connection again in your AI assistant.</p>', status=400)
        session = self._session(request)
        if not session:
            return self._redirect(self._path("/account/login", pending_key))
        with self.store.transaction():
            connection = self._connection(session["account_id"])
            if not connection:
                return self._redirect(self._path("/setup", pending_key))
            pending = self.store.update("pending", pending_key, lambda value: {**value,
                "account_id": session["account_id"], "auth_version": session["auth_version"],
                "credential_version": connection["version"], "csrf": digest(session["csrf"])})
        if not pending:
            return self._page("Connection request expired", '<p>Start the connection again in your AI assistant.</p>', status=400)
        body = '<p><strong>' + escape(pending['client_name']) + '</strong> requests access to ' + escape(connection['name']) + ' at ' + escape(connection['origin']) + '.</p><p>Return address: <strong>' + escape(urlsplit(pending['params']['redirect_uri']).hostname or '') + '</strong></p><p>Read access includes available courses, assignments, grades, files, discussions, and messages. Your Canvas token is not shared with the assistant.</p>'
        body += '<form method="post" action="/connect">' + _hidden('csrf', session['csrf']) + _hidden('request', pending_key)
        if 'canvas:write' in pending['params'].get('scopes', []):
            body += '<label><input type="checkbox" name="write" value="yes">Allow supported Canvas changes when I authorize them in my assistant.</label>'
        body += '<button name="decision" value="allow">Connect assistant</button><button class="secondary" name="decision" value="deny">Cancel</button></form><p class="small">This assistant connection expires after 30 days. You can disconnect it from setup at any time. Write access is off unless you select it.</p>'
        return self._page("Connect your assistant", body, form_redirects=(pending['params']['redirect_uri'],))

    async def connect_post(self, request):
        form = await request.form()
        return await anyio.to_thread.run_sync(self._broker_connect_post, request, form)

    def _broker_connect_post(self, request, form):
        session = self._session(request)
        if not session:
            return self._page("Sign in again", '<p>Your account session expired.</p>', status=403)
        try:
            self._check_form(request, form, "connect", session=session)
            key = self._pending_key(form.get('request'))
            with self.store.transaction():
                pending = self.store.get('pending', key)
                current_session = self._session(request)
                connection = self._connection(session['account_id'])
                if (not current_session or not pending or not connection
                        or pending.get('account_id') != session['account_id']
                        or pending.get('auth_version') != current_session['auth_version']
                        or pending.get('credential_version') != connection['version']
                        or pending.get('csrf') != digest(current_session['csrf'])):
                    raise AccountError("Connection request expired or changed. Start again in your assistant.")
                if form.get('decision') not in {'allow', 'deny'}:
                    raise AccountError("Choose whether to connect this assistant.")
                self.store.delete('pending', key)
                params = pending['params']
                values = {'state': params.get('state'), 'iss': self.authorization_issuer}
                if form.get('decision') == 'deny':
                    values['error'] = 'access_denied'
                else:
                    scopes = ['canvas:read']
                    if form.get('write') == 'yes' and 'canvas:write' in params.get('scopes', []):
                        scopes.append('canvas:write')
                    values['code'] = self._create_account_grant(pending, connection, scopes)
                return RedirectResponse(_redirect(params['redirect_uri'], values), status_code=303, headers=_headers())
        except AccountError as exc:
            return self._page("Connection could not be completed", '<p>' + escape(str(exc)) + '</p>', status=403)

    def _create_account_grant(self, pending, connection, scopes):
        active_grants = 0
        for row in self.store.scan('account_grant', owner='account:' + connection['account_id']):
            existing_id = row.value['grant_id']
            if self.store.get('grant', existing_id):
                active_grants += 1
            else:
                self.store.delete('account_grant', existing_id)
        if active_grants >= 32:
            raise AccountError('This account has too many assistant connections. Disconnect unused assistants in setup first.')
        grant_id, code = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        expiry = time.time() + GRANT_SECONDS
        grant = {'mode': 'personal', 'account_id': connection['account_id'], 'connection_id': connection['id'],
                 'credential_version': connection['version'], 'user_id': connection['user_id'],
                 'client_id': pending['client_id'], 'scopes': scopes, 'expires': expiry}
        if not self._grant_active(grant):
            raise TokenError('invalid_grant', 'Canvas connection changed')
        self.store.put('grant', grant_id, grant, expires=expiry, owner=grant_id)
        self.store.put('account_grant', grant_id, {'grant_id': grant_id}, expires=expiry,
                       owner='account:' + connection['account_id'])
        params = pending['params']
        authorization = GrantAuthorizationCode(code=code, scopes=scopes, expires_at=time.time() + 120,
            client_id=pending['client_id'], code_challenge=params['code_challenge'], redirect_uri=params['redirect_uri'],
            redirect_uri_provided_explicitly=params['redirect_uri_provided_explicitly'], resource=self.resource_url, grant_id=grant_id)
        self.store.put('code', code, authorization.model_dump(mode='json'), expires=authorization.expires_at, owner=grant_id)
        return code
