"""Private hosted MCP for one operator-provisioned Canvas owner.

Local and remote assistants each receive separate OAuth grants. The browser
surface cannot create another owner or reset the owner's password.
"""
from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from urllib.parse import urlsplit

from starlette.responses import HTMLResponse
from starlette.routing import Route

from .accounts import AccountError, AccountService, RateLimited
from .auth import _headers, _offload_storage
from .broker import BuiltByAadiProvider, _field, _hidden, _STYLE
from .personal_admin import (OWNER_EXPIRY, PersonalConfigurationError, load_owner,
                             revoke_owner_grants)
from .storage import digest


class OwnerAccountService(AccountService):
    """The persisted owner binding applies to every account service entrypoint."""

    def __init__(self, store, *, owner_id, owner_email, on_security_change):
        super().__init__(store, on_security_change=on_security_change)
        self.owner_id, self.owner_email = owner_id, owner_email

    def _owner(self):
        try:
            return load_owner(self.store, expected_id=self.owner_id, expected_email=self.owner_email)
        except PersonalConfigurationError:
            return None

    def _account_by_email(self, email):
        return self._owner() if email == self.owner_email else None

    def active(self, account_id):
        if account_id != self.owner_id:
            return None
        owner = self._owner()
        return owner.value if owner else None

    def login(self, email, password, peer):
        try:
            return super().login(email, password, peer)
        except RateLimited:
            raise
        except AccountError:
            raise AccountError("Sign-in failed. Check your email and password.") from None

    def _operator_only(self, *args, **kwargs):
        raise AccountError("Only the server operator can provision or recover this account.")

    signup = verify = request_reset = reset = delete = _operator_only


class PersonalProvider(BuiltByAadiProvider):
    public_auth_mode = "personal"

    def __init__(self, **kwargs):
        if "mailer" in kwargs:
            raise ValueError("Personal hosting does not use account email delivery")
        supplied_store = kwargs.get("store") is not None
        super().__init__(mailer=None, **kwargs)
        try:
            with self.store.transaction():
                owner = load_owner(self.store)
        except Exception:
            if not supplied_store:
                self.store.close()
            raise
        self.owner_id, self.owner_email = owner.value["id"], owner.value["email"]
        self.accounts = OwnerAccountService(self.store, owner_id=self.owner_id,
            owner_email=self.owner_email, on_security_change=self._revoke_account_grants)
        self.session_cookie = "__Host-personal-session" if self.secure_cookie else "personal-session-dev"
        self.form_cookie = "__Host-personal-form" if self.secure_cookie else "personal-form-dev"

    def routes(self):
        # The private instance opens its owner dashboard, not the public
        # distribution's multi-user landing page.
        return [Route("/", lambda request: self._redirect("/setup"), methods=["GET"]), *super().routes()]

    def _page(self, title, body, *, status=200, form_redirects=()):
        return HTMLResponse(f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)} · Personal Canvas</title><style>{_STYLE}</style></head><body><main><nav><a href="/setup">Personal Canvas</a></nav><h1>{escape(title)}</h1>{body}</main></body></html>',
            status_code=status, headers=_headers(form_redirects=form_redirects))

    def _not_found(self):
        return self._page("Page not found", '<p><a href="/account/login">Sign in to your connector</a>.</p>', status=404)

    @_offload_storage
    def account_get(self, request):
        if request.path_params.get("operation") != "login":
            return self._not_found()
        pending = self._pending_key(request.query_params.get("request"))
        csrf, hidden = self._browser_fields(request, "login", pending)
        body = '<p>This private connector is available only to its server owner.</p>'
        body += '<form method="post" action="/account/login">' + hidden
        body += _field("email", "Email address", kind="email", autocomplete="email")
        body += _field("password", "Password", kind="password", autocomplete="current-password")
        body += '<button type="submit">Sign in</button></form><p class="small">Your account was provisioned by the server operator. Password recovery requires operator access to the server.</p>'
        return self._set_form_cookie(self._page("Sign in to Personal Canvas", body), csrf)

    def _account_post(self, request, form):
        if request.path_params.get("operation") not in {"login", "logout"}:
            return self._not_found()
        return super()._account_post(request, form)

    def _session(self, request):
        session = super()._session(request)
        return session if session and session["account_id"] == self.owner_id else None

    def _connection(self, account_id):
        if account_id != self.owner_id or not self.accounts.active(account_id):
            return None
        connection = super()._connection(account_id)
        identity = self.store.get("personal_canvas_identity", account_id)
        if (not connection or not identity or identity != {
                "origin": connection["origin"], "user_id": connection["user_id"]}):
            return None
        return connection

    def _grant_active(self, grant):
        return bool(grant and grant.get("account_id") == self.owner_id and super()._grant_active(grant))

    def _revoke_account_grants(self, account_id):
        if account_id != self.owner_id:
            raise AccountError("This account cannot use the personal connector.")
        revoke_owner_grants(self.store, account_id)

    def _check_canvas_identity(self, account_id, origin, user_id):
        if account_id != self.owner_id or not self.accounts.active(account_id):
            raise AccountError("Sign in again before connecting Canvas.")
        identity = {"origin": origin, "user_id": user_id}
        current = self.store.get("personal_canvas_identity", account_id)
        if current is not None and current != identity:
            raise AccountError("This connector is bound to a different Canvas account. Use a token for the original account; changing identity requires a new operator-provisioned deployment.")
        if current is None:
            self.store.put("personal_canvas_identity", account_id, identity,
                expires=OWNER_EXPIRY, owner="account:" + account_id)

    def _check_setup_acknowledgement(self, form):
        # The authenticated owner is configuring their own private instance.
        # Public beta signup/terms acknowledgement belongs to the other mode.
        return None

    def _create_account_grant(self, pending, connection, scopes):
        with self.store.transaction():
            code = super()._create_account_grant(pending, connection, scopes)
            authorization = self.store.get("code", code)
            grant_id = authorization["grant_id"]
            index = self.store.record("account_grant", grant_id)
            self.store.put("account_grant", grant_id, {**index.value,
                "client_name": str(pending.get("client_name") or "AI assistant")[:150],
                "callback_host": urlsplit(pending["params"]["redirect_uri"]).hostname or ""},
                expires=index.expires, owner=index.owner)
            return code

    def connections(self, session_token):
        """List grants for the logged-in owner without exposing bearer secrets."""
        with self.store.transaction():
            session = self.accounts.session(session_token)
            if not session:
                raise AccountError("Sign in again to manage assistants.")
            result = []
            for row in self.store.scan("account_grant", owner="account:" + self.owner_id):
                grant_id = row.value.get("grant_id")
                if not isinstance(grant_id, str) or row.key_hash != digest(grant_id):
                    continue
                grant = self.store.get("grant", grant_id)
                if not self._grant_active(grant):
                    continue
                result.append({"id": grant_id, "name": row.value.get("client_name", "AI assistant"),
                    "callback_host": row.value.get("callback_host", ""),
                    "scopes": grant["scopes"], "expires": grant["expires"]})
            return result

    def revoke_connection(self, session_token, grant_id):
        with self.store.transaction():
            if not self.accounts.session(session_token):
                raise AccountError("Sign in again to manage assistants.")
            if not isinstance(grant_id, str) or not 1 <= len(grant_id) <= 128:
                raise AccountError("Assistant connection was not found.")
            index = self.store.record("account_grant", grant_id)
            grant = self.store.get("grant", grant_id)
            if (not index or index.owner != "account:" + self.owner_id
                    or index.value.get("grant_id") != grant_id or not grant
                    or grant.get("mode") != "personal" or grant.get("account_id") != self.owner_id):
                raise AccountError("Assistant connection was not found.")
            self.store.revoke_owner(grant_id)
            self.store.delete("account_grant", grant_id)

    @_offload_storage
    def setup_get(self, request):
        pending = self._pending_key(request.query_params.get("request"))
        session = self._session(request)
        if not session:
            return self._redirect(self._path("/account/login", pending))
        connection = self._connection(self.owner_id)
        fields = _hidden("csrf", session["csrf"]) + _hidden("request", pending)
        body = '<p>Signed in as ' + escape(session["email"]) + '.</p>'
        if connection:
            body += '<h2>Canvas connected</h2><p>' + escape(connection["name"]) + ' · ' + escape(connection["origin"]) + '</p>'
            body += '<p>Use <code>' + escape(self.resource_url) + '</code> in each assistant’s connector settings. Sign in here to give each app its own revocable connection.</p>'
            if pending:
                body += '<p><a href="' + escape(self._path('/connect', pending), quote=True) + '">Continue connecting your assistant</a></p>'
        body += '<h2>' + ('Rotate your Canvas token' if connection else 'Connect your Canvas') + '</h2>'
        body += '<p>Use your school’s Canvas URL and a personal access token. In Canvas, open Account → Settings → Approved Integrations → New Access Token, if your school enables that option. Choose an expiration date.</p>'
        body += '<p class="small">The first verified Canvas account is bound to this private instance. Replacing its token disconnects all assistants. The token is encrypted in server storage and sent to Canvas to authenticate requests; assistants receive separate connector tokens.</p>'
        body += '<form method="post" action="/setup">' + fields + _hidden("connection_version", self._connection_version(self.owner_id))
        body += _field("origin", "Canvas URL", kind="url", autocomplete="off")
        body += '<label for="token">Canvas access token</label><input id="token" name="token" type="password" autocomplete="off" maxlength="8192" required>'
        body += '<button type="submit" name="operation" value="save">Verify and save Canvas connection</button></form>'
        grants = self.connections(request.cookies.get(self.session_cookie, ""))
        body += '<h2>Connected assistants</h2>'
        if not grants:
            body += '<p>No assistants are connected.</p>'
        for grant in grants:
            expiry = datetime.fromtimestamp(grant["expires"], UTC).strftime("%Y-%m-%d %H:%M UTC")
            body += '<section><h3>' + escape(str(grant["name"])) + '</h3><p>'
            body += escape(str(grant["callback_host"])) + ' · ' + escape(', '.join(grant["scopes"]))
            body += ' · Expires ' + escape(expiry) + '</p><form method="post" action="/setup">'
            body += fields + _hidden("grant_id", grant["id"]) + '<button class="secondary" name="operation" value="revoke_one">Disconnect this assistant</button></form></section>'
        if connection:
            body += '<form method="post" action="/setup">' + fields + '<button class="secondary" name="operation" value="revoke">Disconnect all assistants</button><button class="secondary" name="operation" value="disconnect">Remove Canvas token</button></form>'
        body += '<hr><form method="post" action="/account/logout">' + fields + '<button class="secondary">Sign out</button></form>'
        return self._page("Your personal Canvas connector", body)

    def _setup_post(self, request, form):
        if form.get("operation") != "revoke_one":
            return super()._setup_post(request, form)
        session = self._session(request)
        if not session:
            return self._redirect("/account/login")
        try:
            self._check_form(request, form, "setup", session=session)
            self.revoke_connection(request.cookies.get(self.session_cookie, ""), form.get("grant_id"))
            return self._redirect("/setup")
        except AccountError as exc:
            return self._page("Assistant could not be disconnected", '<p class="error">' + escape(str(exc)) + '</p>', status=400)
