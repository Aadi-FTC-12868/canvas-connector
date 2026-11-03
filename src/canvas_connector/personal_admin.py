"""Trusted operator provisioning for a private, single-owner deployment.

These functions have no HTTP routes. The caller must already control the server
and its encrypted store. Password derivation stays outside storage transactions.
"""
from __future__ import annotations

import re
import secrets
import time

from .accounts import AccountError, AccountService, hash_password, normalize_email
from .storage import digest

OWNER_KIND = "personal_owner"
OWNER_KEY = "owner"
# The owner identity is deployment metadata, not an expiring browser account.
OWNER_EXPIRY = 253402300799.0
_ID = re.compile(r"[A-Za-z0-9_-]{32}\Z")


class PersonalConfigurationError(ValueError):
    """Safe operator error with no credentials or account identifiers."""


def _valid_password_record(value):
    if (not isinstance(value, dict) or value.get("algorithm") != "scrypt-v1"
            or (value.get("n"), value.get("r"), value.get("p")) != (2**17, 8, 1)):
        return False
    try:
        return len(bytes.fromhex(value["salt"])) == 16 and len(bytes.fromhex(value["hash"])) == 32
    except (KeyError, TypeError, ValueError):
        return False


def load_owner(store, *, expected_id=None, expected_email=None):
    """Validate the deployment binding and return its owner account record."""
    metadata = store.record(OWNER_KIND, OWNER_KEY)
    value = metadata.value if metadata else None
    if (not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1
            or value.get("enabled") is not True or not isinstance(value.get("id"), str)
            or not _ID.fullmatch(value["id"]) or metadata.owner is not None):
        raise PersonalConfigurationError("Personal owner is not initialized or is disabled.")
    try:
        email = normalize_email(value.get("email"))
    except AccountError:
        raise PersonalConfigurationError("Personal owner configuration is inconsistent.") from None
    if (email != value["email"] or expected_id is not None and value["id"] != expected_id
            or expected_email is not None and email != expected_email):
        raise PersonalConfigurationError("Personal owner configuration is inconsistent.")
    account = store.record("account", value["id"])
    index = store.record("account_email", email)
    owner = "account:" + value["id"]
    if (not account or account.owner != owner or account.value.get("id") != value["id"]
            or account.value.get("email") != email or account.value.get("verified") is not True
            or account.value.get("admission") != "operator" or not index or index.owner != owner
            or index.value != {"id": value["id"]} or store.is_owner_revoked(owner)
            or not isinstance(account.value.get("auth_version"), str)
            or not _ID.fullmatch(account.value["auth_version"])
            or not _valid_password_record(account.value.get("password"))):
        raise PersonalConfigurationError("Personal owner configuration is inconsistent.")
    return account


def initialize_owner(store, email: str, password: str) -> str:
    """Provision exactly one owner in a fresh store; never overwrite identity."""
    email = normalize_email(email)
    password_hash = hash_password(password)
    owner_id = secrets.token_urlsafe(24)
    with store.transaction():
        if not store.is_empty():
            raise PersonalConfigurationError("Owner initialization requires a fresh, empty store.")
        owner = "account:" + owner_id
        account = {"id": owner_id, "email": email, "password": password_hash,
                   "verified": True, "admission": "operator",
                   "auth_version": secrets.token_urlsafe(24), "created_at": time.time()}
        store.put("account", owner_id, account, expires=OWNER_EXPIRY, owner=owner)
        store.put("account_email", email, {"id": owner_id}, expires=OWNER_EXPIRY, owner=owner)
        store.put(OWNER_KIND, OWNER_KEY, {"schema": 1, "id": owner_id, "email": email,
                                       "enabled": True}, expires=OWNER_EXPIRY)
    return owner_id


def revoke_owner_grants(store, owner_id: str) -> None:
    """Revoke all owner's grants, including any missing their index entry.

    Caller holds a transaction. Foreign grants are never selected through a
    stale or corrupt account index. Tombstones fence actions already in flight.
    """
    grants = set()
    for row in store.scan("grant"):
        if (row.value.get("mode") == "personal" and row.value.get("account_id") == owner_id
                and isinstance(row.owner, str) and row.key_hash == digest(row.owner)):
            grants.add(row.owner)
    for row in store.scan("account_grant", owner="account:" + owner_id):
        grant_id = row.value.get("grant_id")
        if not isinstance(grant_id, str) or row.key_hash != digest(grant_id):
            continue
        grant = store.get("grant", grant_id)
        if not grant or grant.get("mode") == "personal" and grant.get("account_id") == owner_id:
            grants.add(grant_id)
            store.delete("account_grant", grant_id)
    for grant_id in grants:
        store.revoke_owner(grant_id)


def reset_owner_password(store, password: str) -> None:
    """Operator recovery: replace the password and revoke all existing access."""
    with store.transaction():
        previous = load_owner(store)
    password_hash = hash_password(password)
    with store.transaction():
        current = load_owner(store, expected_id=previous.value["id"], expected_email=previous.value["email"])
        if current.version != previous.version:
            raise PersonalConfigurationError("Owner changed during recovery. Try again.")
        account = {**current.value, "password": password_hash, "auth_version": secrets.token_urlsafe(24)}
        store.put("account", account["id"], account, expires=current.expires, owner=current.owner)
        AccountService(store)._drop_sessions(account["id"])
        revoke_owner_grants(store, account["id"])
