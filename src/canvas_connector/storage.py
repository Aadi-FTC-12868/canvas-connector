"""Encrypted, transactional records shared by workers on one host.

SQLite uses a local disk file (not a network filesystem). Public methods form
our backend contract: callers must not depend on SQLite connections or encrypted
row layouts. Transactions are short synchronous sections; never hold one while
awaiting, calling the network, or running document extraction. Versioned writes
and durable leases fence stale workers. Revocation tombstones prevent a request
that started before disconnect from resurrecting that owner's records.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from typing import Any, Callable

from cryptography.fernet import Fernet

_UNSET = object()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class OwnerRevokedError(ValueError):
    """A stale request tried to recreate data for a disconnected grant."""


@dataclass(frozen=True)
class StoredRecord:
    value: dict[str, Any]
    owner: str | None
    expires: float
    version: int
    key_hash: str


@dataclass(frozen=True)
class Lease:
    namespace: str
    key: str
    holder: str
    expires: float
    owner: str | None


class _Transaction:
    # A class context manager also handles SDK's frozen dataclass exceptions;
    # contextlib.generator.__exit__ tries assigning their __traceback__.
    def __init__(self, store):
        self.store = store
        self.nested = False
        self.name = "store_" + secrets.token_hex(8)

    def __enter__(self):
        self.store.lock.acquire()
        try:
            self.nested = self.store.db.in_transaction
            self.store.db.execute(f"SAVEPOINT {self.name}" if self.nested else "BEGIN IMMEDIATE")
        except BaseException:
            self.store.lock.release()
            raise
        return self.store

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is None:
                self.store.db.execute(f"RELEASE SAVEPOINT {self.name}" if self.nested else "COMMIT")
            elif self.nested:
                self.store.db.execute(f"ROLLBACK TO SAVEPOINT {self.name}")
                self.store.db.execute(f"RELEASE SAVEPOINT {self.name}")
            else:
                self.store.db.execute("ROLLBACK")
        finally:
            self.store.lock.release()
        return False


class Store:
    """Encrypted SQLite backend; safe for independent processes on one host."""
    def __init__(self, path: str | Path, encryption_key: str | bytes, *, capacity: int = 20000):
        self.cipher = Fernet(encryption_key)
        self.capacity = capacity
        self.lock = threading.RLock()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise ValueError("Database must not be a symlink")
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=10)
        os.chmod(path, 0o600)
        self.db.execute("PRAGMA busy_timeout=10000")
        # DELETE journaling works across local processes without additional WAL
        # files. The backend deliberately makes no shared-network-disk claim.
        self.db.execute("PRAGMA journal_mode=DELETE")
        with self.transaction():
            self.db.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT NOT NULL, key TEXT NOT NULL, owner TEXT, expires REAL NOT NULL, payload BLOB NOT NULL, version INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(kind,key))")
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(records)")}
            if "version" not in columns:
                self.db.execute("ALTER TABLE records ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
            self.db.execute("CREATE INDEX IF NOT EXISTS record_owner ON records(owner)")
            self.db.execute("CREATE INDEX IF NOT EXISTS record_expiry ON records(expires)")
            self.db.execute("CREATE TABLE IF NOT EXISTS revoked_owners (owner_hash TEXT PRIMARY KEY, expires REAL NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS leases (namespace TEXT NOT NULL,key TEXT NOT NULL,holder TEXT NOT NULL,expires REAL NOT NULL,owner TEXT,PRIMARY KEY(namespace,key))")
            self.db.execute("CREATE INDEX IF NOT EXISTS lease_owner ON leases(owner)")
            self.db.execute("CREATE TABLE IF NOT EXISTS revisions (id INTEGER PRIMARY KEY CHECK(id=1), value INTEGER NOT NULL)")
            self.db.execute("INSERT OR IGNORE INTO revisions SELECT 1,COALESCE(MAX(version),0) FROM records")

    def transaction(self):
        """Atomic read/modify/write section; nested sections use savepoints."""
        return _Transaction(self)

    def _encoded(self, value: dict[str, Any]) -> bytes:
        if not isinstance(value, dict):
            raise TypeError("Stored values must be objects")
        return self.cipher.encrypt(json.dumps(value, separators=(",", ":"), allow_nan=False).encode())

    def _purge(self, now: float) -> None:
        self.db.execute("DELETE FROM records WHERE expires <= ?", (now,))
        self.db.execute("DELETE FROM revoked_owners WHERE expires <= ?", (now,))
        self.db.execute("DELETE FROM leases WHERE expires <= ?", (now,))

    @staticmethod
    def _check_expiry(expires: float) -> None:
        if not math.isfinite(expires):
            raise ValueError("Record expiry must be finite")

    def _check_owner(self, owner: str | None) -> None:
        if owner is not None and self.is_owner_revoked(owner):
            raise OwnerRevokedError("Connection was revoked; its records cannot be recreated")

    def _check_capacity(self, kind: str, key_hash: str) -> None:
        exists = self.db.execute("SELECT 1 FROM records WHERE kind=? AND key=?", (kind, key_hash)).fetchone()
        if not exists and self.db.execute("SELECT COUNT(*) FROM records").fetchone()[0] >= self.capacity:
            raise ValueError("Authorization storage is at capacity")

    def put(self, kind: str, key: str, value: dict[str, Any], *, expires: float, owner: str | None = None) -> None:
        self._check_expiry(expires)
        payload = self._encoded(value)
        hashed = digest(key)
        with self.transaction():
            self._purge(time.time())
            self._check_owner(owner)
            self._check_capacity(kind, hashed)
            # Database-wide revisions avoid ABA when a key is deleted and later
            # recreated: an old CAS token never matches the new record.
            revision = self.db.execute("SELECT value+1 FROM revisions WHERE id=1").fetchone()[0]
            self.db.execute("UPDATE revisions SET value=? WHERE id=1", (revision,))
            self.db.execute("INSERT INTO records(kind,key,owner,expires,payload,version) VALUES (?,?,?,?,?,?) ON CONFLICT(kind,key) DO UPDATE SET owner=excluded.owner,expires=excluded.expires,payload=excluded.payload,version=excluded.version",
                (kind, hashed, owner, expires, payload, revision))

    def record(self, kind: str, key: str) -> StoredRecord | None:
        hashed = digest(key)
        with self.lock:
            row = self.db.execute("SELECT owner,expires,payload,version FROM records WHERE kind=? AND key=? AND expires>?", (kind, hashed, time.time())).fetchone()
        if row is None:
            return None
        return StoredRecord(json.loads(self.cipher.decrypt(row[2])), row[0], row[1], row[3], hashed)

    def get(self, kind: str, key: str) -> dict[str, Any] | None:
        result = self.record(kind, key)
        return result.value if result else None

    def scan(self, kind: str, *, owner: str | None = None) -> list[StoredRecord]:
        """Snapshot of unexpired records. Payloads should carry any public IDs.

        Keys stay hashed; an API that returned raw keys could expose OAuth
        credentials. owner=None selects all owners, as used for global quotas.
        """
        with self.lock:
            sql = "SELECT key,owner,expires,payload,version FROM records WHERE kind=? AND expires>?"
            params: tuple = (kind, time.time())
            if owner is not None:
                sql += " AND owner=?"
                params += (owner,)
            rows = self.db.execute(sql + " ORDER BY key", params).fetchall()
        return [StoredRecord(json.loads(self.cipher.decrypt(row[3])), row[1], row[2], row[4], row[0]) for row in rows]

    def put_if_absent(self, kind: str, key: str, value: dict[str, Any], *, expires: float, owner: str | None = None) -> bool:
        with self.transaction():
            if self.record(kind, key) is not None:
                return False
            self.put(kind, key, value, expires=expires, owner=owner)
            return True

    def compare_and_swap(self, kind: str, key: str, expected_version: int, value: dict[str, Any], *,
                         expires: float | None = None, owner: Any = _UNSET) -> bool:
        with self.transaction():
            previous = self.record(kind, key)
            if previous is None or previous.version != expected_version:
                return False
            self.put(kind, key, value, expires=previous.expires if expires is None else expires,
                     owner=previous.owner if owner is _UNSET else owner)
            return True

    def update(self, kind: str, key: str, transform: Callable[[dict[str, Any]], dict[str, Any] | None], *,
               expires: float | None = None) -> dict[str, Any] | None:
        """Atomically transform an existing live record. None deletes it."""
        with self.transaction():
            record = self.record(kind, key)
            if record is None:
                return None
            result = transform(record.value)
            if result is None:
                self.delete(kind, key)
            else:
                self.put(kind, key, result, expires=record.expires if expires is None else expires, owner=record.owner)
            return result

    def consume(self, kind: str, key: str) -> dict[str, Any] | None:
        with self.transaction():
            result = self.get(kind, key)
            self.delete(kind, key)
            return result

    def delete(self, kind: str, key: str) -> None:
        with self.transaction():
            self.db.execute("DELETE FROM records WHERE kind=? AND key=?", (kind, digest(key)))

    def delete_if_version(self, kind: str, key: str, expected_version: int) -> bool:
        with self.transaction():
            record = self.record(kind, key)
            if record is None or record.version != expected_version:
                return False
            self.delete(kind, key)
            return True

    def is_owner_revoked(self, owner: str) -> bool:
        with self.lock:
            return self.db.execute("SELECT 1 FROM revoked_owners WHERE owner_hash=? AND expires>?", (digest(owner), time.time())).fetchone() is not None

    def revoke_owner(self, owner: str, *, retention: float = 31 * 86400) -> None:
        """Delete owned rows and prevent old requests from recreating them.

        Grants live at most 30 days; tombstones outlive every valid grant and
        pending action. Tombstones have reserved storage so a full record table
        cannot prevent disconnect/revocation.
        """
        with self.transaction():
            self.db.execute("DELETE FROM records WHERE owner=?", (owner,))
            self.db.execute("DELETE FROM leases WHERE owner=?", (owner,))
            self.db.execute("INSERT INTO revoked_owners VALUES (?,?) ON CONFLICT(owner_hash) DO UPDATE SET expires=MAX(expires,excluded.expires)", (digest(owner), time.time() + retention))

    def acquire_lease(self, namespace: str, key: str, *, ttl: float = 45, owner: str | None = None) -> Lease | None:
        if not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("Lease duration must be positive and finite")
        now = time.time()
        with self.transaction():
            self._purge(now)
            if owner is not None and self.is_owner_revoked(owner):
                return None
            if self.db.execute("SELECT 1 FROM leases WHERE namespace=? AND key=? AND expires>?", (namespace, digest(key), now)).fetchone():
                return None
            lease = Lease(namespace, key, secrets.token_urlsafe(32), now + ttl, owner)
            self.db.execute("INSERT OR REPLACE INTO leases VALUES (?,?,?,?,?)", (namespace, digest(key), lease.holder, lease.expires, owner))
            return lease

    def lease_valid(self, lease: Lease) -> bool:
        with self.lock:
            return self.db.execute("SELECT 1 FROM leases WHERE namespace=? AND key=? AND holder=? AND expires>?", (lease.namespace, digest(lease.key), lease.holder, time.time())).fetchone() is not None

    def renew_lease(self, lease: Lease, *, ttl: float = 45) -> bool:
        if not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("Lease duration must be positive and finite")
        with self.transaction():
            if lease.owner is not None and self.is_owner_revoked(lease.owner):
                return False
            cursor = self.db.execute("UPDATE leases SET expires=? WHERE namespace=? AND key=? AND holder=? AND expires>?", (time.time() + ttl, lease.namespace, digest(lease.key), lease.holder, time.time()))
            return cursor.rowcount == 1

    def release_lease(self, lease: Lease) -> None:
        with self.transaction():
            self.db.execute("DELETE FROM leases WHERE namespace=? AND key=? AND holder=?", (lease.namespace, digest(lease.key), lease.holder))

    def increment_window(self, key: str, limit: int, period: float) -> tuple[bool, float]:
        """Atomic fixed-window admission across processes; retry seconds on deny."""
        if limit < 1 or not math.isfinite(period) or period <= 0:
            raise ValueError("Rate limit and period must be positive")
        with self.transaction():
            now = time.time()
            current = self.get("__rate_window__", key)
            if current is None:
                self.put("__rate_window__", key, {"count": 1, "reset_at": now + period}, expires=now + period)
                return True, 0.0
            if current["count"] >= limit:
                return False, max(0.0, current["reset_at"] - now)
            current["count"] += 1
            self.put("__rate_window__", key, current, expires=current["reset_at"])
            return True, 0.0

    def close(self) -> None:
        with self.lock:
            self.db.close()


def open_store(path: str | Path, encryption_key: str | bytes, **options):
    """Select the durable backend without exposing its DSN in diagnostics."""
    if str(path).startswith("cloudflare:"):
        if str(path) != "cloudflare://store":
            raise ValueError("Cloudflare storage uses its fixed private binding")
        from .cloudflare_store import CloudflareStore
        return CloudflareStore(encryption_key, **options)
    if str(path).startswith(("postgres://", "postgresql://")):
        from .postgres import PostgresStore
        return PostgresStore(str(path), encryption_key, **options)
    return Store(path, encryption_key, **options)
