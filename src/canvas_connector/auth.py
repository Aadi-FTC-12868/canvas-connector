"""Two-layer OAuth: MCP client -> connector -> institution Canvas OAuth.

The public service never asks students for manually generated Canvas tokens.
Institutions must provision a Canvas developer key for /canvas/callback first.
SDK handlers validate MCP PKCE/redirects; this provider adds one-use durable state,
per-grant credentials, expiry, refresh rotation and revocation.
"""
from __future__ import annotations

import asyncio
import functools
from dataclasses import dataclass, field
from contextlib import suppress
from html import escape
import hmac
import secrets
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

import anyio
import httpx
from canvas_py._config import CanvasConfig
from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams,
    AuthorizeError, RefreshToken, RegistrationError, TokenError)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.auth.middleware.client_auth import ClientAuthenticator, AuthenticationError
from mcp.server.auth.routes import cors_middleware, create_protected_resource_routes
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .security import secure_http_client, validate_canvas_origin
from .storage import open_store, digest

SCOPES = ["canvas:read", "canvas:write"]
ACCESS_SECONDS = 3600
GRANT_SECONDS = 30 * 24 * 3600
COOKIE = "__Host-canvas-oauth"


def _offload_storage(fn):
    """Keep synchronous SQLite/PostgreSQL work off the HTTP event loop."""
    @functools.wraps(fn)
    async def run(*args, **kwargs):
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
    return run


@dataclass(frozen=True)
class Institution:
    origin: str
    client_id: str
    client_secret: str = field(repr=False)
    name: str = "Canvas"

    def __post_init__(self):
        object.__setattr__(self, "origin", validate_canvas_origin(self.origin))
        if not self.client_id or not self.client_secret:
            raise ValueError("Institution client ID and secret are required")


class GrantAccessToken(AccessToken):
    grant_id: str


class GrantRefreshToken(RefreshToken):
    grant_id: str
    access_digest_key: str
    resource: str


class GrantAuthorizationCode(AuthorizationCode):
    grant_id: str


def _redirect(url: str, values: dict[str, str | None]) -> str:
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query += [(k, v) for k, v in values.items() if v is not None]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _headers() -> dict[str, str]:
    return {"Cache-Control": "no-store", "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"}


class CanvasOAuthProvider:
    def __init__(self, *, issuer_url: str, database_path: str | Path | None = None,
                 encryption_key: str | None = None, institutions: list[Institution],
                 allow_local_http: bool = False,
                 http_client_factory: Callable[[], httpx.Client] = secure_http_client,
                 store: Any = None):
        parsed = urlsplit(issuer_url)
        local = allow_local_http and parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (parsed.scheme != "https" and not local) or not parsed.hostname or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("Issuer must be an HTTPS origin (loopback HTTP is development only)")
        self.issuer_url = issuer_url.rstrip("/")
        self.resource_url = self.issuer_url + "/mcp"
        self.callback_url = self.issuer_url + "/canvas/callback"
        self.secure_cookie = not local
        self.cookie_name = COOKIE if self.secure_cookie else "canvas-oauth-dev"
        if not institutions or len({x.origin for x in institutions}) != len(institutions):
            raise ValueError("Configure at least one institution with a unique origin")
        self.institutions = {x.origin: x for x in institutions}
        if store is None and (database_path is None or encryption_key is None):
            raise ValueError("Provide an encrypted storage backend or database path and key")
        self.store = store if store is not None else open_store(database_path, encryption_key)
        self.http_client_factory = http_client_factory

    @property
    def auth_settings(self) -> AuthSettings:
        return AuthSettings(issuer_url=self.issuer_url, resource_server_url=self.resource_url,
            required_scopes=["canvas:read"],
            client_registration_options=ClientRegistrationOptions(enabled=True,
                valid_scopes=SCOPES, default_scopes=SCOPES),
            revocation_options=RevocationOptions(enabled=True))

    @property
    def authorization_issuer(self) -> str:
        # RFC 9207 sections 2.3-2.4 require an exact match to discovery metadata.
        # Use the SDK's serialized URL (including its trailing slash), while
        # keeping issuer_url as the origin used by CSRF and endpoint builders.
        return str(self.auth_settings.issuer_url)

    def routes(self) -> list[Route]:
        return create_protected_resource_routes(
            self.auth_settings.resource_server_url, [self.auth_settings.issuer_url],
            scopes_supported=SCOPES, resource_name="Canvas Connector",
        ) + [Route("/revoke", cors_middleware(self.revoke_request, ["POST", "OPTIONS"]), methods=["POST", "OPTIONS"]),
                Route("/connect", self.connect_get, methods=["GET"]),
                Route("/connect", self.connect_post, methods=["POST"]),
                Route("/canvas/callback", self.canvas_callback, methods=["GET"])]

    async def revoke_request(self, request: Request) -> Response:
        """RFC 7009 with SDK client authentication, including public clients.

        SDK 1.27's RevocationRequest incorrectly requires a client_secret form
        field even for auth_method=none. This narrow adapter preserves SDK auth
        and handles the optional field correctly.
        """
        try:
            client = await ClientAuthenticator(self).authenticate_request(request)
        except AuthenticationError:
            return JSONResponse({"error": "invalid_client"}, status_code=401, headers=_headers())
        form = await request.form()
        raw = form.get("token")
        if not isinstance(raw, str) or not raw:
            return JSONResponse({"error": "invalid_request"}, status_code=400, headers=_headers())
        token = await self.load_access_token(raw)
        if token is None:
            token = await self.load_refresh_token(client, raw)
        if token is not None and token.client_id == client.client_id:
            await self.revoke_token(token)
        return Response(status_code=200, headers=_headers())

    @_offload_storage
    def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        value = self.store.get("client", client_id)
        return OAuthClientInformationFull.model_validate(value) if value else None

    @_offload_storage
    def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id or not client_info.redirect_uris or len(client_info.redirect_uris) > 10:
            raise RegistrationError("invalid_client_metadata", "A client needs 1–10 redirect URIs")
        for uri in client_info.redirect_uris:
            parsed = urlsplit(str(uri))
            loopback = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            if (parsed.scheme != "https" and not loopback) or not parsed.hostname or parsed.fragment or parsed.username or parsed.password:
                raise RegistrationError("invalid_redirect_uri", "Use HTTPS or a loopback HTTP callback")
        if set((client_info.scope or "canvas:read").split()) - set(SCOPES):
            raise RegistrationError("invalid_client_metadata", "Unsupported scope")
        self.store.put("client", client_info.client_id, client_info.model_dump(mode="json"),
                       expires=time.time() + 365 * 86400)

    @_offload_storage
    def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        scopes = params.scopes if params.scopes is not None else (client.scope or "canvas:read").split()
        params = params.model_copy(update={"scopes": scopes})
        if set(scopes) - set(SCOPES) or "canvas:read" not in scopes:
            raise AuthorizeError("invalid_scope", "canvas:read is required; canvas:write is optional")
        if params.resource not in (None, self.resource_url):
            raise AuthorizeError("invalid_request", "Resource must identify this MCP server")
        if not params.code_challenge or len(params.code_challenge) != 43:
            raise AuthorizeError("invalid_request", "S256 PKCE is required")
        # Defense in depth even when called outside the SDK handler.
        if str(params.redirect_uri) not in [str(x) for x in client.redirect_uris or []]:
            raise AuthorizeError("invalid_request", "Redirect URI is not registered")
        key = secrets.token_urlsafe(32)
        self.store.put("pending", key, {"client_id": client.client_id,
            "client_name": (client.client_name or "MCP client")[:150],
            "params": params.model_dump(mode="json"), "expires": time.time() + 600}, expires=time.time() + 600)
        return self.issuer_url + "/connect?" + urlencode({"request": key})

    @_offload_storage
    def connect_get(self, request: Request) -> Response:
        key = request.query_params.get("request", "")
        csrf = secrets.token_urlsafe(32)
        pending = self.store.update("pending", key, lambda value: {**value, "csrf": digest(csrf)})
        if not pending:
            return HTMLResponse("Connection request expired. Start again in your AI app.", status_code=400, headers=_headers())
        params = pending["params"]
        options = "".join(f'<option value="{escape(x.origin, quote=True)}">{escape(x.name)} — {escape(x.origin)}</option>' for x in self.institutions.values())
        write = ""
        if "canvas:write" in (params.get("scopes") or []):
            write = '<label class="check"><input type="checkbox" name="write" value="yes"> Allow assignment submissions, comments, and other enabled Canvas actions when requested in my AI app.</label>'
        body = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Connect Canvas</title>
<style>body{{font:17px system-ui;background:#f5f3ef;color:#241f20;max-width:600px;margin:8vh auto;padding:24px}}h1{{font-size:38px}}label{{display:block;margin:24px 0 8px}}select,button{{font:inherit;padding:12px;width:100%;border:1px solid #948b8e;border-radius:6px}}button{{background:#6e243c;color:white;margin-top:20px}}.check{{line-height:1.5}}small{{color:#655d60}}</style>
<h1>Connect your Canvas</h1><p><strong>{escape(pending['client_name'])}</strong> is requesting access to your Canvas account.</p>
<p>Return address: <strong>{escape(urlsplit(params['redirect_uri']).hostname or '')}</strong></p>
<p>Sign in on your institution’s Canvas website. The connector receives a revocable Canvas authorization; your password stays with Canvas.</p>
<form method="post" action="/connect"><input type="hidden" name="request" value="{escape(key, quote=True)}"><input type="hidden" name="csrf" value="{csrf}">
<label for="institution">Institution</label><select id="institution" name="institution">{options}</select>
<p>Read access includes your courses, assignments, grades, files, discussions, and messages available through Canvas.</p>{write}
<button type="submit">Continue to Canvas</button><button type="submit" name="deny" value="yes">Cancel</button></form>
<p><small>Connections expire after 30 days. Disconnect in your AI app or revoke the app in Canvas to stop access. Your institution controls the available permissions.</small></p></html>'''
        response = HTMLResponse(body, headers=_headers())
        response.set_cookie(self.cookie_name, csrf, max_age=600, httponly=True, secure=self.secure_cookie, samesite="lax", path="/")
        return response

    async def connect_post(self, request: Request) -> Response:
        form = await request.form()
        return await anyio.to_thread.run_sync(self._connect_post, request, form)

    def _connect_post(self, request: Request, form) -> Response:
        if request.headers.get("origin") != self.issuer_url:
            return JSONResponse({"error": "invalid_origin"}, status_code=403, headers=_headers())
        key = str(form.get("request", ""))
        pending = self.store.get("pending", key)
        csrf = str(form.get("csrf", ""))
        cookie = request.cookies.get(self.cookie_name, "")
        if not pending or not csrf or not hmac.compare_digest(csrf, cookie) or not hmac.compare_digest(digest(csrf), pending.get("csrf", "")):
            return JSONResponse({"error": "invalid_csrf_or_expired_request"}, status_code=403, headers=_headers())
        institution = self.institutions.get(str(form.get("institution", "")))
        if not institution:
            return JSONResponse({"error": "institution_not_configured"}, status_code=400, headers=_headers())
        pending = self.store.consume("pending", key)
        if not pending:
            return JSONResponse({"error": "request_already_used"}, status_code=400, headers=_headers())
        params = pending["params"]
        if form.get("deny") == "yes":
            return RedirectResponse(_redirect(params["redirect_uri"], {"error": "access_denied", "state": params.get("state"), "iss": self.authorization_issuer}), status_code=303, headers=_headers())
        scopes = ["canvas:read"]
        if form.get("write") == "yes" and "canvas:write" in (params.get("scopes") or []):
            scopes.append("canvas:write")
        state = secrets.token_urlsafe(32)
        pending.update(institution=institution.origin, scopes=scopes)
        self.store.put("upstream", state, pending, expires=pending["expires"])
        url = institution.origin + "/login/oauth2/auth?" + urlencode({"client_id": institution.client_id,
            "response_type": "code", "redirect_uri": self.callback_url, "state": state})
        return RedirectResponse(url, status_code=303, headers=_headers())

    def _canvas_token(self, institution: Institution, payload: dict[str, str]) -> dict:
        with self.http_client_factory() as client:
            response = client.post(institution.origin + "/login/oauth2/token", data={
                "client_id": institution.client_id, "client_secret": institution.client_secret, **payload})
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("access_token"), str) or not data["access_token"]:
                raise ValueError("Canvas did not return an access token")
            expires_in = float(data.get("expires_in", 3600))
            if not 0 < expires_in <= 365 * 86400:
                raise ValueError("Canvas returned an invalid token lifetime")
            return {"access_token": data["access_token"], "refresh_token": data.get("refresh_token"),
                    "expires_at": time.time() + expires_in}

    def _profile(self, institution: Institution, token: str) -> dict:
        with self.http_client_factory() as client:
            response = client.get(institution.origin + "/api/v1/users/self/profile", headers={"Authorization": "Bearer " + token})
            response.raise_for_status()
            profile = response.json()
            if not isinstance(profile, dict) or not profile.get("id"):
                raise ValueError("Canvas did not identify the authenticated user")
            return {"id": str(profile["id"])}

    def _take_upstream(self, state: str, cookie: str):
        with self.store.transaction():
            pending = self.store.get("upstream", state)
            if not pending or not cookie or not hmac.compare_digest(digest(cookie), pending.get("csrf", "")):
                return None
            return self.store.consume("upstream", state)

    def _create_grant(self, pending, institution, credential, profile):
        grant_id = secrets.token_urlsafe(32)
        expiry = time.time() + GRANT_SECONDS
        params = pending["params"]
        with self.store.transaction():
            self.store.put("grant", grant_id, {"institution": institution.origin, "credential": credential,
                "user_id": profile["id"], "client_id": pending["client_id"], "scopes": pending["scopes"], "expires": expiry},
                expires=expiry, owner=grant_id)
            code = secrets.token_urlsafe(32)
            authorization = GrantAuthorizationCode(code=code, scopes=pending["scopes"], expires_at=time.time() + 120,
                client_id=pending["client_id"], code_challenge=params["code_challenge"],
                redirect_uri=params["redirect_uri"], redirect_uri_provided_explicitly=params["redirect_uri_provided_explicitly"],
                resource=self.resource_url, grant_id=grant_id)
            self.store.put("code", code, authorization.model_dump(mode="json"), expires=authorization.expires_at, owner=grant_id)
            return code

    async def canvas_callback(self, request: Request) -> Response:
        state = request.query_params.get("state", "")
        cookie = request.cookies.get(self.cookie_name, "")
        pending = await anyio.to_thread.run_sync(self._take_upstream, state, cookie)
        if not pending:
            return HTMLResponse("Invalid or expired Canvas sign-in. Start again in your AI app.", status_code=400, headers=_headers())
        params = pending["params"]
        institution = self.institutions[pending["institution"]]
        if request.query_params.get("error") or not request.query_params.get("code"):
            return RedirectResponse(_redirect(params["redirect_uri"], {"error": "access_denied", "state": params.get("state"), "iss": self.authorization_issuer}), status_code=303, headers=_headers())
        try:
            credential = await anyio.to_thread.run_sync(lambda: self._canvas_token(institution, {
                "grant_type": "authorization_code", "code": request.query_params["code"], "redirect_uri": self.callback_url}))
            profile = await anyio.to_thread.run_sync(lambda: self._profile(institution, credential["access_token"]))
        except Exception:
            return HTMLResponse("Canvas sign-in could not be verified. Start again or contact your institution’s administrator.", status_code=502, headers=_headers())
        code = await anyio.to_thread.run_sync(self._create_grant, pending, institution, credential, profile)
        response = RedirectResponse(_redirect(params["redirect_uri"], {"code": code, "state": params.get("state"), "iss": self.authorization_issuer}), status_code=303, headers=_headers())
        response.delete_cookie(self.cookie_name, path="/", secure=self.secure_cookie, httponly=True, samesite="lax")
        return response

    @_offload_storage
    def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        data = self.store.get("code", authorization_code)
        if not data or data["client_id"] != client.client_id or not self.store.get("grant", data["grant_id"]):
            return None
        return GrantAuthorizationCode.model_validate(data)

    def _issue(self, *, grant_id: str, client_id: str, scopes: list[str]) -> OAuthToken:
        # Grant validation and both token rows commit together. A concurrent
        # revocation can happen before or after this transaction, never between.
        with self.store.transaction():
            grant = self.store.get("grant", grant_id)
            if not grant or grant["client_id"] != client_id or set(scopes) - set(grant["scopes"]):
                raise TokenError("invalid_grant", "Grant is no longer valid")
            access_key, refresh_key = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            expires = min(int(time.time()) + ACCESS_SECONDS, int(grant["expires"]))
            access = GrantAccessToken(token=access_key, client_id=client_id, scopes=scopes,
                expires_at=expires, resource=self.resource_url, grant_id=grant_id)
            refresh = GrantRefreshToken(token=refresh_key, client_id=client_id, scopes=scopes,
                expires_at=int(grant["expires"]), resource=self.resource_url, grant_id=grant_id, access_digest_key=access_key)
            self.store.put("access", access_key, access.model_dump(mode="json"), expires=expires, owner=grant_id)
            self.store.put("refresh", refresh_key, refresh.model_dump(mode="json"), expires=grant["expires"], owner=grant_id)
            return OAuthToken(access_token=access_key, refresh_token=refresh_key,
                expires_in=max(0, expires - int(time.time())), scope=" ".join(scopes))

    @_offload_storage
    def exchange_authorization_code(self, client, authorization_code):
        with self.store.transaction():
            data = self.store.consume("code", authorization_code.code)
            if not data or data["client_id"] != client.client_id or data["resource"] != self.resource_url:
                raise TokenError("invalid_grant", "Authorization code is expired or already used")
            return self._issue(grant_id=data["grant_id"], client_id=data["client_id"], scopes=data["scopes"])

    @_offload_storage
    def load_refresh_token(self, client, refresh_token):
        with self.store.transaction():
            spent = self.store.get("spent_refresh", refresh_token)
            if spent and spent["client_id"] == client.client_id:
                # Reuse revokes the entire family even if another worker won
                # the rotation. Returning None commits this revocation.
                self.store.revoke_owner(spent["grant_id"])
                return None
            data = self.store.get("refresh", refresh_token)
            if not data or data["client_id"] != client.client_id or not self.store.get("grant", data["grant_id"]):
                return None
            return GrantRefreshToken.model_validate(data)

    @_offload_storage
    def exchange_refresh_token(self, client, refresh_token, scopes):
        if set(scopes) - set(refresh_token.scopes) or "canvas:read" not in scopes:
            raise TokenError("invalid_scope", "Cannot expand scopes during refresh")
        result = None
        with self.store.transaction():
            data = self.store.get("refresh", refresh_token.token)
            if data and data["client_id"] == client.client_id and data["resource"] == self.resource_url:
                if set(scopes) - set(data["scopes"]):
                    raise TokenError("invalid_scope", "Cannot expand scopes during refresh")
                self.store.consume("refresh", refresh_token.token)
                self.store.put("spent_refresh", refresh_token.token,
                    {"grant_id": data["grant_id"], "client_id": data["client_id"]},
                    expires=data["expires_at"], owner=data["grant_id"])
                self.store.delete("access", data["access_digest_key"])
                result = self._issue(grant_id=data["grant_id"], client_id=data["client_id"], scopes=scopes)
            else:
                spent = self.store.get("spent_refresh", refresh_token.token)
                if spent and spent["client_id"] == client.client_id:
                    self.store.revoke_owner(spent["grant_id"])
        # Raise after committing a replay-triggered family revocation.
        if result is None:
            raise TokenError("invalid_grant", "Refresh token is expired or already used")
        return result

    def validate_access(self, token: str) -> GrantAccessToken | None:
        """Synchronously revalidate identity/scopes immediately before dispatch."""
        with self.store.transaction():
            data = self.store.get("access", token)
            if not data or data["resource"] != self.resource_url:
                return None
            grant = self.store.get("grant", data["grant_id"])
            if (not grant or grant["client_id"] != data["client_id"]
                    or set(data["scopes"]) - set(grant["scopes"])
                    or grant["institution"] not in self.institutions):
                return None
            return GrantAccessToken.model_validate(data)

    async def load_access_token(self, token: str) -> GrantAccessToken | None:
        return await anyio.to_thread.run_sync(self.validate_access, token)

    @_offload_storage
    def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        grant_id = getattr(token, "grant_id", None)
        if grant_id:
            self.store.revoke_owner(grant_id)

    def _refresh_snapshot(self, grant_id: str, lease):
        with self.store.transaction():
            record = self.store.record("grant", grant_id)
            if not record or not self.store.lease_valid(lease):
                raise PermissionError("Connection was revoked or refresh ownership changed")
            credential = record.value["credential"]
            if credential["expires_at"] > time.time() + 60:
                return None
            institution = self.institutions.get(record.value["institution"])
            if not institution:
                raise PermissionError("Institution is no longer configured")
            if not credential.get("refresh_token"):
                raise PermissionError("Canvas authorization expired. Reconnect your account.")
            return record, credential, institution

    def _install_refreshed(self, grant_id, lease, record, refreshed):
        grant = record.value
        grant["credential"] = refreshed
        with self.store.transaction():
            if not self.store.lease_valid(lease):
                raise PermissionError("Connection was revoked or refresh ownership changed")
            if not self.store.compare_and_swap("grant", grant_id, record.version, grant):
                raise PermissionError("Connection was revoked or changed during refresh")

    async def _refresh_canvas_credentials(self, grant_id: str, lease) -> None:
        snapshot = await anyio.to_thread.run_sync(self._refresh_snapshot, grant_id, lease)
        if snapshot is None:
            return
        record, credential, institution = snapshot
        finished, lost = asyncio.Event(), asyncio.Event()

        async def heartbeat():
            while not finished.is_set():
                try:
                    await asyncio.wait_for(finished.wait(), timeout=10)
                except TimeoutError:
                    try:
                        renewed = await anyio.to_thread.run_sync(lambda: self.store.renew_lease(lease, ttl=45))
                    except Exception:
                        renewed = False
                    if not renewed:
                        lost.set()
                        return

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            try:
                refreshed = await anyio.to_thread.run_sync(lambda: self._canvas_token(institution, {
                    "grant_type": "refresh_token", "refresh_token": credential["refresh_token"]}))
            except Exception:
                raise PermissionError("Canvas authorization could not be refreshed. Reconnect your account.") from None
            refreshed["refresh_token"] = refreshed.get("refresh_token") or credential["refresh_token"]
            if lost.is_set():
                raise PermissionError("Refresh ownership changed")
            await anyio.to_thread.run_sync(self._install_refreshed, grant_id, lease, record, refreshed)
        finally:
            finished.set()
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task

    def _credential_state(self, token):
        with self.store.transaction():
            access = self.store.get("access", token)
            if not access or access["resource"] != self.resource_url:
                raise PermissionError("Connection expired or was revoked")
            grant_id = access["grant_id"]
            grant = self.store.get("grant", grant_id)
            if not grant:
                raise PermissionError("Connection expired or was revoked")
            if grant["institution"] not in self.institutions:
                raise PermissionError("Institution is no longer configured")
            credential = grant["credential"]
            if credential["expires_at"] > time.time() + 60:
                return CanvasConfig(base_url=grant["institution"], token=credential["access_token"]), grant_id, None
            lease = self.store.acquire_lease("canvas_refresh", grant_id, ttl=45, owner=grant_id)
            return None, grant_id, lease

    async def get_credentials(self, access_token: str | AccessToken) -> CanvasConfig:
        token = access_token if isinstance(access_token, str) else access_token.token
        deadline = time.monotonic() + 90
        while True:
            config, grant_id, lease = await anyio.to_thread.run_sync(self._credential_state, token)
            if config is not None:
                return config
            if lease is not None:
                try:
                    await self._refresh_canvas_credentials(grant_id, lease)
                finally:
                    await anyio.to_thread.run_sync(self.store.release_lease, lease)
                # Revalidate this MCP token after refresh. Its grant may remain
                # valid while this particular token was rotated/revoked.
                continue
            if time.monotonic() >= deadline:
                raise PermissionError("Another worker is refreshing this connection. Try again shortly.")
            await asyncio.sleep(0.05)
