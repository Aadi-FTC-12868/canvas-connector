from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import secrets
import time

from cryptography.fernet import Fernet
import pytest

from canvas_connector import accounts, personal_admin
from canvas_connector.accounts import AccountError
from canvas_connector.personal import PersonalProvider
from canvas_connector.personal_admin import (OWNER_EXPIRY, PersonalConfigurationError,
    initialize_owner, load_owner, reset_owner_password)
from canvas_connector.storage import Store

EMAIL = "owner@example.test"
PASSWORD = "synthetic personal owner password"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "_derive", lambda value, salt: hashlib.sha256(salt + value).digest())
    storage = Store(tmp_path / "owner.db", Fernet.generate_key())
    yield storage
    storage.close()


def test_owner_is_explicit_and_identity_does_not_reset(store):
    with pytest.raises(PersonalConfigurationError):
        PersonalProvider(issuer_url="https://connector.example", store=store)
    owner_id = initialize_owner(store, "OWNER@example.test", PASSWORD)
    assert load_owner(store).value["id"] == owner_id
    provider = PersonalProvider(issuer_url="https://connector.example", store=store)
    assert provider.owner_id == owner_id and provider.owner_email == EMAIL
    assert load_owner(store).value["admission"] == "operator"
    with pytest.raises(PersonalConfigurationError, match="fresh"):
        initialize_owner(store, "another@example.test", PASSWORD + "another")
    assert provider.accounts.login(EMAIL, PASSWORD, "peer")


@pytest.mark.parametrize("kind", ["account", "client", "action", "unknown-state"])
def test_bootstrap_rejects_existing_data_of_any_kind(store, kind):
    store.put(kind, "anything", {"state": "existing"}, expires=time.time() + 3600)
    with pytest.raises(PersonalConfigurationError, match="fresh"):
        initialize_owner(store, EMAIL, PASSWORD)
    assert not store.scan("personal_owner")


def test_password_hashing_happens_outside_transaction(store, monkeypatch):
    original = personal_admin.hash_password
    def observed(password):
        assert not store.db.in_transaction
        return original(password)
    monkeypatch.setattr(personal_admin, "hash_password", observed)
    initialize_owner(store, EMAIL, PASSWORD)
    reset_owner_password(store, PASSWORD + " changed")


def test_two_operators_cannot_both_initialize(store):
    def initialize(email):
        try:
            return initialize_owner(store, email, PASSWORD)
        except PersonalConfigurationError:
            return None
    with ThreadPoolExecutor(2) as workers:
        result = list(workers.map(initialize, [EMAIL, "other@example.test"]))
    assert len([value for value in result if value]) == 1
    assert len(store.scan("account")) == len(store.scan("personal_owner")) == 1


def test_failed_bootstrap_leaves_no_partial_owner(store, monkeypatch):
    original = store.put
    def fail_metadata(kind, *args, **kwargs):
        if kind == "personal_owner":
            raise RuntimeError("synthetic persistence failure")
        return original(kind, *args, **kwargs)
    monkeypatch.setattr(store, "put", fail_metadata)
    with pytest.raises(RuntimeError):
        initialize_owner(store, EMAIL, PASSWORD)
    assert store.is_empty()


@pytest.mark.parametrize("change", ["disabled", "wrong-email", "wrong-id", "missing-index", "revoked", "wrong-admission", "password", "auth-version"])
def test_inconsistent_or_disabled_owner_fails_closed(store, change):
    owner_id = initialize_owner(store, EMAIL, PASSWORD)
    provider = PersonalProvider(issuer_url="https://connector.example", store=store)
    session = provider.accounts.login(EMAIL, PASSWORD, "peer")
    metadata = store.get("personal_owner", "owner")
    if change == "disabled":
        metadata["enabled"] = False
    elif change == "wrong-email":
        metadata["email"] = "other@example.test"
    elif change == "wrong-id":
        metadata["id"] = secrets.token_urlsafe(24)
    elif change == "missing-index":
        store.delete("account_email", EMAIL)
    elif change == "revoked":
        store.revoke_owner("account:" + owner_id)
    else:
        row = store.record("account", owner_id)
        if change == "password":
            row.value["password"] = {"algorithm": "plaintext", "hash": "invalid"}
        elif change == "auth-version":
            row.value["auth_version"] = ""
        else:
            row.value["admission"] = "public-signup"
        store.put("account", owner_id, row.value, expires=row.expires, owner=row.owner)
    store.put("personal_owner", "owner", metadata, expires=OWNER_EXPIRY)
    assert provider.accounts.active(owner_id) is None
    assert provider.accounts.session(session) is None
    with pytest.raises(PersonalConfigurationError):
        PersonalProvider(issuer_url="https://connector.example", store=store)


def test_password_recovery_revokes_sessions_and_unindexed_grants(store):
    owner_id = initialize_owner(store, EMAIL, PASSWORD)
    provider = PersonalProvider(issuer_url="https://connector.example", store=store)
    session = provider.accounts.login(EMAIL, PASSWORD, "peer")
    expiry = time.time() + 3600
    for grant_id, indexed in [("indexed", True), ("unindexed", False)]:
        store.put("grant", grant_id, {"mode": "personal", "account_id": owner_id}, expires=expiry, owner=grant_id)
        store.put("action", grant_id, {"state": "prepared"}, expires=expiry, owner=grant_id)
        if indexed:
            store.put("account_grant", grant_id, {"grant_id": grant_id}, expires=expiry, owner="account:" + owner_id)
    store.put("grant", "foreign", {"mode": "personal", "account_id": "another"}, expires=expiry, owner="foreign")
    store.put("account_grant", "foreign", {"grant_id": "foreign"}, expires=expiry, owner="account:" + owner_id)
    reset_owner_password(store, PASSWORD + " changed")
    assert provider.accounts.session(session) is None
    for grant_id in ["indexed", "unindexed"]:
        assert store.get("grant", grant_id) is None and store.get("action", grant_id) is None
        assert store.is_owner_revoked(grant_id)
    assert store.get("grant", "foreign")
    with pytest.raises(AccountError):
        provider.accounts.login(EMAIL, PASSWORD, "peer")
    assert provider.accounts.login(EMAIL, PASSWORD + " changed", "peer")
    assert provider.owner_id == load_owner(store).value["id"]


def test_recovery_detects_concurrent_owner_change(store, monkeypatch):
    owner_id = initialize_owner(store, EMAIL, PASSWORD)
    before = load_owner(store).value
    original = personal_admin.hash_password
    def competing_change(password):
        row = store.record("account", owner_id)
        row.value["auth_version"] = secrets.token_urlsafe(24)
        store.put("account", owner_id, row.value, expires=row.expires, owner=row.owner)
        return original(password)
    monkeypatch.setattr(personal_admin, "hash_password", competing_change)
    with pytest.raises(PersonalConfigurationError, match="changed"):
        reset_owner_password(store, PASSWORD + " changed")
    assert load_owner(store).value["password"] == before["password"]


def test_owner_records_are_encrypted_and_persist_after_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "_derive", lambda value, salt: hashlib.sha256(salt + value).digest())
    path, key = tmp_path / "persist.db", Fernet.generate_key()
    store = Store(path, key)
    owner_id = initialize_owner(store, EMAIL, PASSWORD)
    store.close()
    assert EMAIL.encode() not in path.read_bytes() and PASSWORD.encode() not in path.read_bytes()
    store = Store(path, key)
    try:
        provider = PersonalProvider(issuer_url="https://connector.example", store=store)
        assert provider.owner_id == owner_id and provider.accounts.login(EMAIL, PASSWORD, "peer")
    finally:
        store.close()
