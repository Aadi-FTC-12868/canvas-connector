"""Email/password accounts; private records use the encrypted Store contract.

Passwords use OWASP's scrypt baseline (N=2**17, r=8, p=1). Hashing and
delivery happen outside storage transactions. A mailer is deliberately required:
verification codes are never returned to browsers or written to application logs.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from typing import Callable

ACCOUNT_SECONDS = 10 * 365 * 86400
PENDING_ACCOUNT_SECONDS = 24 * 3600
SESSION_SECONDS = 7 * 86400
MAX_SESSIONS = 16
CODE_SECONDS = 20 * 60
_HASH_SLOTS = threading.BoundedSemaphore(2)
_EMAIL = re.compile(r"[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?\.[a-zA-Z]{2,63}\Z")


class AccountError(ValueError):
    """Safe account error; never contains a submitted value."""


class MailUnavailable(AccountError):
    pass


class RateLimited(AccountError):
    pass


def normalize_email(value: str) -> str:
    if not isinstance(value, str) or not 3 <= len(value) <= 254:
        raise AccountError("Enter a valid email address.")
    value = value.strip().lower()
    if not _EMAIL.fullmatch(value) or ".." in value or len(value.split("@")[0]) > 64:
        raise AccountError("Enter a valid email address.")
    return value


def validate_password(value: str) -> bytes:
    if not isinstance(value, str) or not 12 <= len(value) <= 128:
        raise AccountError("Use a password between 12 and 128 characters.")
    encoded = value.encode("utf-8")
    if len(encoded) > 512 or "\x00" in value:
        raise AccountError("Use a password between 12 and 128 characters.")
    return encoded


def _derive(value: bytes, salt: bytes) -> bytes:
    # The semaphore prevents parallel unauthenticated requests from exhausting
    # container memory. Admission/rate limits run before entering this function.
    if not _HASH_SLOTS.acquire(timeout=3):
        raise RateLimited("Too many account requests. Try again shortly.")
    try:
        return hashlib.scrypt(value, salt=salt, n=2**17, r=8, p=1,
                              maxmem=192 * 1024 * 1024, dklen=32)
    finally:
        _HASH_SLOTS.release()


def hash_password(value: str) -> dict:
    salt = secrets.token_bytes(16)
    return {"algorithm": "scrypt-v1", "n": 2**17, "r": 8, "p": 1,
            "salt": salt.hex(), "hash": _derive(validate_password(value), salt).hex()}


def verify_password(value: str, record: dict | None) -> bool:
    try:
        encoded = validate_password(value)
    except AccountError:
        return False
    valid = bool(isinstance(record, dict) and record.get("algorithm") == "scrypt-v1"
                 and (record.get("n"), record.get("r"), record.get("p")) == (2**17, 8, 1))
    try:
        salt = bytes.fromhex(record["salt"]) if valid else bytes(16)
        expected = bytes.fromhex(record["hash"]) if valid else bytes(32)
        if len(salt) != 16 or len(expected) != 32:
            valid, salt, expected = False, bytes(16), bytes(32)
    except (TypeError, ValueError, KeyError):
        valid, salt, expected = False, bytes(16), bytes(32)
    result = _derive(encoded, salt)
    return hmac.compare_digest(result, expected) and valid


class AccountService:
    def __init__(self, store, *, mailer: Callable[[str, str, str], None] | None = None,
                 on_security_change: Callable[[str], None] | None = None):
        self.store, self.mailer = store, mailer
        self.on_security_change = on_security_change or (lambda account_id: None)

    def _rate(self, operation: str, email: str, peer: str) -> None:
        for key, limit, period in ((f"account:{operation}:peer:{peer}", 30, 900),
                                   (f"account:{operation}:email:{email}", 8, 900)):
            if not self.store.increment_window(key, limit, period)[0]:
                raise RateLimited("Too many account requests. Try again later.")

    def _account_by_email(self, email: str):
        index = self.store.get("account_email", email)
        return self.store.record("account", index["id"]) if index else None

    def active(self, account_id: str) -> dict | None:
        value = self.store.get("account", account_id)
        if not value or not value.get("verified") or self.store.is_owner_revoked("account:" + account_id):
            return None
        return value

    def _send(self, email: str, purpose: str, code: str) -> None:
        if self.mailer is None:
            raise MailUnavailable("Account email delivery is not configured yet.")
        try:
            self.mailer(email, purpose, code)
        except Exception:
            # Do not expose provider errors that may contain addresses or codes.
            raise MailUnavailable("Account email could not be sent. Try again later.") from None

    def signup(self, email: str, password: str, peer: str) -> None:
        if self.mailer is None:
            raise MailUnavailable("Account email delivery is not configured yet.")
        email = normalize_email(email)
        self._rate("signup", email, peer)
        password_hash = hash_password(password)
        code, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        send = False
        with self.store.transaction():
            existing = self._account_by_email(email)
            if not existing or not existing.value["verified"]:
                account_id = existing.value["id"] if existing else secrets.token_urlsafe(24)
                owner = "account:" + account_id
                account = {"id": account_id, "email": email, "password": password_hash,
                           "verified": False, "auth_version": nonce, "created_at": time.time()}
                self.store.put("account", account_id, account, expires=time.time() + PENDING_ACCOUNT_SECONDS, owner=owner)
                self.store.put("account_email", email, {"id": account_id}, expires=time.time() + PENDING_ACCOUNT_SECONDS, owner=owner)
                self.store.put("account_verify", code, {"account_id": account_id, "auth_version": nonce},
                               expires=time.time() + CODE_SECONDS, owner=owner)
                send = True
        if send:
            try:
                self._send(email, "verify", code)
            except MailUnavailable:
                self.store.delete("account_verify", code)
                # Delivery failure must not distinguish a new address from an
                # existing verified account. No code remains usable and the
                # account stays unverified; the browser gets the same response.

    def verify(self, email: str, password: str, code: str, peer: str) -> None:
        email = normalize_email(email)
        self._rate("verify", email, peer)
        existing = self._account_by_email(email)
        matches = verify_password(password, existing.value["password"] if existing else None)
        if not existing or not matches or not isinstance(code, str) or len(code) > 128:
            raise AccountError("Verification could not be completed. Check the email, code, and password.")
        with self.store.transaction():
            current = self.store.record("account", existing.value["id"])
            verification = self.store.get("account_verify", code)
            if (not current or current.version != existing.version or current.value["verified"]
                    or not verification or verification["account_id"] != current.value["id"]
                    or verification["auth_version"] != current.value["auth_version"]):
                raise AccountError("Verification could not be completed. Check the email, code, and password.")
            account = {**current.value, "verified": True}
            expiry = time.time() + ACCOUNT_SECONDS
            self.store.put("account", account["id"], account, expires=expiry, owner=current.owner)
            self.store.put("account_email", email, {"id": account["id"]}, expires=expiry, owner=current.owner)
            self.store.delete("account_verify", code)

    def login(self, email: str, password: str, peer: str) -> str:
        email = normalize_email(email)
        self._rate("login", email, peer)
        existing = self._account_by_email(email)
        matches = verify_password(password, existing.value["password"] if existing else None)
        if not existing or not matches or not existing.value.get("verified"):
            raise AccountError("Sign-in failed. Check your email and password, and verify your email first.")
        token = secrets.token_urlsafe(32)
        with self.store.transaction():
            current = self.store.record("account", existing.value["id"])
            if not current or current.version != existing.version or not self.active(current.value["id"]):
                raise AccountError("Sign-in failed. Try again.")
            self.store.put("account_session", token, {"account_id": current.value["id"],
                "auth_version": current.value["auth_version"], "csrf": secrets.token_urlsafe(32),
                "created_at": time.time()}, expires=time.time() + SESSION_SECONDS, owner="account:" + current.value["id"])
            index = self.store.get("account_sessions", current.value["id"]) or {"tokens": []}
            live = []
            for old_token in index["tokens"]:
                previous = self.store.get("account_session", old_token)
                if previous and previous["auth_version"] == current.value["auth_version"]:
                    live.append(old_token)
                else:
                    self.store.delete("account_session", old_token)
            live.append(token)
            for old_token in live[:-MAX_SESSIONS]:
                self.store.delete("account_session", old_token)
            self.store.put("account_sessions", current.value["id"], {"tokens": live[-MAX_SESSIONS:]},
                           expires=time.time() + SESSION_SECONDS, owner="account:" + current.value["id"])
        return token

    def session(self, token: str) -> dict | None:
        if not token or len(token) > 128:
            return None
        with self.store.transaction():
            session = self.store.get("account_session", token)
            account = self.active(session["account_id"]) if session else None
            if not account or session["auth_version"] != account["auth_version"]:
                return None
            return {**session, "email": account["email"]}

    def logout(self, token: str) -> None:
        with self.store.transaction():
            session = self.store.get("account_session", token)
            self.store.delete("account_session", token)
            if session:
                index = self.store.get("account_sessions", session["account_id"])
                if index:
                    remaining = [value for value in index["tokens"] if value != token]
                    self.store.put("account_sessions", session["account_id"], {"tokens": remaining},
                        expires=time.time() + SESSION_SECONDS, owner="account:" + session["account_id"])

    def _drop_sessions(self, account_id: str) -> None:
        index = self.store.get("account_sessions", account_id)
        for token in index["tokens"] if index else []:
            self.store.delete("account_session", token)
        self.store.delete("account_sessions", account_id)

    def request_reset(self, email: str, peer: str) -> None:
        if self.mailer is None:
            raise MailUnavailable("Account email delivery is not configured yet.")
        email = normalize_email(email)
        self._rate("reset", email, peer)
        code = secrets.token_urlsafe(24)
        send = False
        with self.store.transaction():
            existing = self._account_by_email(email)
            if existing and existing.value.get("verified"):
                self.store.put("account_reset", code, {"account_id": existing.value["id"],
                    "auth_version": existing.value["auth_version"]}, expires=time.time() + CODE_SECONDS,
                    owner="account:" + existing.value["id"])
                send = True
        if send:
            try:
                self._send(email, "reset", code)
            except MailUnavailable:
                self.store.delete("account_reset", code)
                # Keep the response identical to an unknown address. A broken
                # mail transport must not become an account-enumeration oracle.

    def reset(self, email: str, password: str, code: str, peer: str) -> None:
        email = normalize_email(email)
        self._rate("reset_finish", email, peer)
        password_hash = hash_password(password)
        if not isinstance(code, str) or len(code) > 128:
            raise AccountError("The reset code is invalid or expired.")
        with self.store.transaction():
            existing = self._account_by_email(email)
            reset = self.store.get("account_reset", code)
            if (not existing or not existing.value.get("verified") or not reset
                    or reset["account_id"] != existing.value["id"]
                    or reset["auth_version"] != existing.value["auth_version"]):
                raise AccountError("The reset code is invalid or expired.")
            account = {**existing.value, "password": password_hash, "auth_version": secrets.token_urlsafe(24)}
            self.store.put("account", account["id"], account, expires=existing.expires, owner=existing.owner)
            self.store.delete("account_reset", code)
            # auth_version invalidates every old browser session; associated
            # assistant grants are revoked by the provider in this transaction.
            self._drop_sessions(account["id"])
            self.on_security_change(account["id"])

    def delete(self, session_token: str, password: str, peer: str) -> None:
        session = self.session(session_token)
        if not session:
            raise AccountError("Sign in again to delete your account.")
        self._rate("delete", session["email"], peer)
        existing = self.store.record("account", session["account_id"])
        if not verify_password(password, existing.value["password"] if existing else None):
            raise AccountError("Account deletion could not be verified.")
        with self.store.transaction():
            current = self.store.record("account", session["account_id"])
            if not current or current.version != existing.version or not self.session(session_token):
                raise AccountError("Account changed. Sign in again.")
            self.on_security_change(session["account_id"])
            self.store.revoke_owner("account:" + session["account_id"], retention=ACCOUNT_SECONDS)
