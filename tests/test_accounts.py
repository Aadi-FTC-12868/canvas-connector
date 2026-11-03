from __future__ import annotations

import hashlib
import time

from cryptography.fernet import Fernet
import pytest

from canvas_connector import accounts
from canvas_connector.accounts import AccountError, AccountService, MailUnavailable, RateLimited
from canvas_connector.storage import Store

EMAIL = "student@example.test"
PASSWORD = "correct horse battery staple"


@pytest.fixture
def service(tmp_path, monkeypatch):
    # Most cases exercise transactional account behavior; the real scrypt
    # parameters and password roundtrip have a separate non-substituted test.
    monkeypatch.setattr(accounts, "_derive", lambda value, salt: hashlib.sha256(salt + value).digest())
    store = Store(tmp_path / "accounts.db", Fernet.generate_key())
    deliveries, changes = [], []
    result = AccountService(store, mailer=lambda *args: deliveries.append(args), on_security_change=changes.append)
    yield result, deliveries, changes
    store.close()


def registered(service, email=EMAIL, password=PASSWORD):
    svc, deliveries, _ = service
    svc.signup(email, password, "peer")
    svc.verify(email, password, deliveries[-1][2], "peer")
    return svc.login(email, password, "peer")


def test_real_scrypt_roundtrip_and_distinct_salts():
    first, second = accounts.hash_password(PASSWORD), accounts.hash_password(PASSWORD)
    assert first["algorithm"] == "scrypt-v1"
    assert (first["n"], first["r"], first["p"]) == (2**17, 8, 1)
    assert first["salt"] != second["salt"] and first["hash"] != second["hash"]
    assert accounts.verify_password(PASSWORD, first)
    assert not accounts.verify_password(PASSWORD + "wrong", first)


@pytest.mark.parametrize("value", [None, "x", "a@b", "a\r\n@b.test", "a..b@example.test", "a@host..test", "a" * 65 + "@example.test"])
def test_email_validation(value):
    with pytest.raises(AccountError):
        accounts.normalize_email(value)


@pytest.mark.parametrize("value", [None, "short", "x" * 129, "\0" * 14])
def test_password_bounds(value):
    with pytest.raises(AccountError):
        accounts.validate_password(value)


def test_mail_unconfigured_fails_closed(service):
    svc, deliveries, _ = service
    svc.mailer = None
    with pytest.raises(MailUnavailable):
        svc.signup(EMAIL, PASSWORD, "peer")
    with pytest.raises(MailUnavailable):
        svc.request_reset(EMAIL, "peer")
    assert not deliveries and not svc.store.scan("account")


def test_signup_verify_login_session_and_encrypted_records(service, tmp_path):
    svc, deliveries, _ = service
    svc.signup("STUDENT@example.test", PASSWORD, "peer")
    pending = svc._account_by_email(EMAIL)
    assert 0 < pending.expires - time.time() <= accounts.PENDING_ACCOUNT_SECONDS
    code = deliveries[-1][2]
    assert len(code) == 32 and deliveries[-1][:2] == (EMAIL, "verify")
    with pytest.raises(AccountError):
        svc.login(EMAIL, PASSWORD, "peer")
    with pytest.raises(AccountError):
        svc.verify(EMAIL, PASSWORD + "wrong", code, "peer")
    svc.verify(EMAIL, PASSWORD, code, "peer")
    verified = svc._account_by_email(EMAIL)
    assert verified.expires - time.time() > accounts.ACCOUNT_SECONDS - 60
    assert svc.store.record("account_email", EMAIL).expires == verified.expires
    with pytest.raises(AccountError):
        svc.verify(EMAIL, PASSWORD, code, "peer")
    token = svc.login(EMAIL, PASSWORD, "peer")
    session = svc.session(token)
    assert session["email"] == EMAIL and len(session["csrf"]) >= 32
    raw = (tmp_path / "accounts.db").read_bytes()
    for secret in (PASSWORD, EMAIL, token, code, session["csrf"]):
        assert secret.encode() not in raw
    svc.logout(token)
    assert svc.session(token) is None


def test_unverified_reregistration_invalidates_attacker_password_and_code(service):
    svc, deliveries, _ = service
    svc.signup(EMAIL, "attacker chosen password", "attacker")
    old = deliveries[-1][2]
    svc.signup(EMAIL, PASSWORD, "student")
    current = deliveries[-1][2]
    with pytest.raises(AccountError):
        svc.verify(EMAIL, PASSWORD, old, "student")
    with pytest.raises(AccountError):
        svc.verify(EMAIL, "attacker chosen password", current, "attacker")
    svc.verify(EMAIL, PASSWORD, current, "student")
    with pytest.raises(AccountError):
        svc.login(EMAIL, "attacker chosen password", "attacker")
    assert svc.session(svc.login(EMAIL, PASSWORD, "student"))


def test_existing_verified_signup_is_generic_and_does_not_overwrite(service):
    svc, deliveries, _ = service
    session = registered(service)
    sent = len(deliveries)
    assert svc.signup(EMAIL, "someone else's password", "attacker") is None
    assert len(deliveries) == sent and svc.session(session)
    assert svc.login(EMAIL, PASSWORD, "peer")


def test_unknown_and_wrong_password_have_same_login_error(service):
    svc, _, _ = service
    registered(service)
    errors = []
    for email, password in ((EMAIL, PASSWORD + "bad"), ("unknown@example.test", PASSWORD)):
        with pytest.raises(AccountError) as exc:
            svc.login(email, password, "peer")
        errors.append(str(exc.value))
    assert errors[0] == errors[1]


def test_reset_one_use_revokes_sessions_and_calls_grant_revoker(service):
    svc, deliveries, changes = service
    old = registered(service)
    account_id = svc.session(old)["account_id"]
    svc.request_reset(EMAIL, "peer")
    code = deliveries[-1][2]
    assert deliveries[-1][1] == "reset"
    svc.reset(EMAIL, PASSWORD + "new", code, "peer")
    assert svc.session(old) is None and changes == [account_id]
    with pytest.raises(AccountError):
        svc.reset(EMAIL, PASSWORD, code, "peer")
    with pytest.raises(AccountError):
        svc.login(EMAIL, PASSWORD, "peer")
    assert svc.login(EMAIL, PASSWORD + "new", "peer")


def test_reset_code_cannot_cross_account(service):
    svc, deliveries, _ = service
    one = registered(service)
    two = registered(service, "other@example.test")
    svc.request_reset(EMAIL, "peer")
    code = deliveries[-1][2]
    with pytest.raises(AccountError):
        svc.reset("other@example.test", PASSWORD + "new", code, "peer")
    assert svc.session(one) and svc.session(two)


def test_expired_verification_and_reset_codes(service):
    svc, deliveries, _ = service
    svc.signup(EMAIL, PASSWORD, "peer")
    code = deliveries[-1][2]
    record = svc.store.record("account_verify", code)
    svc.store.put("account_verify", code, record.value, expires=time.time() - 1, owner=record.owner)
    with pytest.raises(AccountError):
        svc.verify(EMAIL, PASSWORD, code, "peer")
    session = registered(service)
    svc.request_reset(EMAIL, "peer")
    code = deliveries[-1][2]
    record = svc.store.record("account_reset", code)
    svc.store.put("account_reset", code, record.value, expires=time.time() - 1, owner=record.owner)
    with pytest.raises(AccountError):
        svc.reset(EMAIL, PASSWORD, code, "peer")
    assert svc.session(session)


def test_mail_error_is_redacted_and_code_unusable(service):
    svc, _, _ = service
    def broken(email, purpose, code):
        raise RuntimeError(f"secret delivery {email} {code}")
    svc.mailer = broken
    assert svc.signup(EMAIL, PASSWORD, "peer") is None
    assert not svc.store.scan("account_verify")


def test_rate_limit_precedes_password_hash(service, monkeypatch):
    svc, _, _ = service
    for _ in range(8):
        with pytest.raises(AccountError):
            svc.login(EMAIL, PASSWORD, "peer")
    monkeypatch.setattr(accounts, "_derive", lambda *_: pytest.fail("hash should not run"))
    with pytest.raises(RateLimited):
        svc.login(EMAIL, PASSWORD, "peer")


def test_delete_requires_password_revokes_account_and_all_sessions(service):
    svc, _, changes = service
    token = registered(service)
    account_id = svc.session(token)["account_id"]
    with pytest.raises(AccountError):
        svc.delete(token, PASSWORD + "bad", "peer")
    svc.delete(token, PASSWORD, "peer")
    assert svc.session(token) is None and svc.active(account_id) is None
    assert svc.store.is_owner_revoked("account:" + account_id)
    assert svc.store.get("account_email", EMAIL) is None
    assert changes == [account_id]
    replacement = registered(service)
    assert svc.session(replacement)["account_id"] != account_id


def test_hash_verification_does_not_accept_untrusted_work_parameters(service):
    record = accounts.hash_password(PASSWORD)
    record["n"] = 2**31
    assert not accounts.verify_password(PASSWORD, record)


def test_login_rechecks_account_after_password_work(service, monkeypatch):
    svc, _, _ = service
    registered(service)
    original = accounts.verify_password
    def changed(password, record):
        success = original(password, record)
        current = svc._account_by_email(EMAIL)
        value = {**current.value, "auth_version": "changed"}
        svc.store.put("account", value["id"], value, expires=current.expires, owner=current.owner)
        return success
    monkeypatch.setattr(accounts, "verify_password", changed)
    with pytest.raises(AccountError, match="Try again"):
        svc.login(EMAIL, PASSWORD, "peer")


def test_session_budget_retires_oldest_and_reset_deletes_session_records(service, monkeypatch):
    svc, deliveries, _ = service
    first = registered(service)
    monkeypatch.setattr(svc, '_rate', lambda *args: None)
    tokens = [first] + [svc.login(EMAIL, PASSWORD, 'peer') for _ in range(accounts.MAX_SESSIONS)]
    assert svc.session(first) is None
    assert all(svc.session(token) for token in tokens[1:])
    svc.request_reset(EMAIL, 'peer')
    svc.reset(EMAIL, PASSWORD + 'new', deliveries[-1][2], 'peer')
    assert not svc.store.scan('account_session') and not svc.store.scan('account_sessions')
