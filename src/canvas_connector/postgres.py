"""PostgreSQL storage for workers sharing a database across hosts.

This adapter uses encrypted payloads and the same public contract as Store.
Short mutation transactions take a namespace-scoped advisory transaction lock;
that gives compound quotas/CAS/revocation the same serializable behavior as the
SQLite backend without holding locks during upstream calls. This deliberately
prioritizes correctness over maximum database write throughput. PostgreSQL's
clock governs storage leases. This is not a load-tested unlimited-scale service.
"""
from __future__ import annotations

from contextlib import suppress
import hashlib
import json
import math
import re
import secrets
import threading

from cryptography.fernet import Fernet

from .storage import Lease, Store, StoredRecord, digest


class _PostgresTransaction:
    def __init__(self, store):
        self.store = store
        self.name = "store_" + secrets.token_hex(8)
        self.nested = False

    def __enter__(self):
        from psycopg.pq import TransactionStatus
        self.store.lock.acquire()
        try:
            self.store._ensure_connection()
            self.nested = self.store.connection.info.transaction_status != TransactionStatus.IDLE
            if self.nested:
                self.store.connection.execute(f"SAVEPOINT {self.name}")
            else:
                self.store.connection.execute("BEGIN")
                self.store.connection.execute("SELECT pg_advisory_xact_lock(%s)", (self.store.advisory_key,))
            self.store._transaction_depth += 1
        except BaseException:
            try:
                if not self.nested:
                    with suppress(Exception):
                        self.store.connection.execute("ROLLBACK")
            finally:
                self.store.lock.release()
            raise
        return self.store

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is None:
                self.store.connection.execute(f"RELEASE SAVEPOINT {self.name}" if self.nested else "COMMIT")
            elif self.nested:
                self.store.connection.execute(f"ROLLBACK TO SAVEPOINT {self.name}")
                self.store.connection.execute(f"RELEASE SAVEPOINT {self.name}")
            else:
                self.store.connection.execute("ROLLBACK")
        except BaseException:
            if not self.nested:
                with suppress(Exception):
                    self.store.connection.execute("ROLLBACK")
            raise
        finally:
            self.store._transaction_depth -= 1
            self.store.lock.release()
        return False


class PostgresStore(Store):
    """One database connection per worker; shared state has database-wide locks.

    The optional namespace creates an isolated schema, useful for test suites or
    separate deployments. All workers of one deployment must use the same schema
    and encryption key. Install the `postgres` package extra to use this backend.
    """
    def __init__(self, dsn: str, encryption_key: str | bytes, *, capacity: int = 20000,
                 namespace: str = "canvas_connector"):
        try:
            from psycopg import sql
        except ImportError:
            raise RuntimeError("Install canvas-connector[postgres] to use PostgreSQL storage") from None
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", namespace):
            raise ValueError("Storage namespace must be a lowercase PostgreSQL identifier")
        self.cipher = Fernet(encryption_key)
        self.capacity = capacity
        self.lock = threading.RLock()
        self.namespace = namespace
        self.advisory_key = int.from_bytes(hashlib.sha256(("canvas-connector:" + namespace).encode()).digest()[:8], "big", signed=True)
        self._dsn = dsn
        self._closed = False
        self._transaction_depth = 0
        self.connection = self._connect()
        try:
            with self.transaction():
                self.connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(namespace)))
                self.connection.execute(sql.SQL("SET search_path TO {}, pg_catalog").format(sql.Identifier(namespace)))
                self.connection.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT NOT NULL,key TEXT NOT NULL,owner TEXT,expires DOUBLE PRECISION NOT NULL,payload BYTEA NOT NULL,version BIGINT NOT NULL,PRIMARY KEY(kind,key))")
                self.connection.execute("CREATE INDEX IF NOT EXISTS record_owner ON records(owner)")
                self.connection.execute("CREATE INDEX IF NOT EXISTS record_expiry ON records(expires)")
                self.connection.execute("CREATE TABLE IF NOT EXISTS revoked_owners (owner_hash TEXT PRIMARY KEY,expires DOUBLE PRECISION NOT NULL)")
                self.connection.execute("CREATE TABLE IF NOT EXISTS leases (namespace TEXT NOT NULL,key TEXT NOT NULL,holder TEXT NOT NULL,expires DOUBLE PRECISION NOT NULL,owner TEXT,PRIMARY KEY(namespace,key))")
                self.connection.execute("CREATE INDEX IF NOT EXISTS lease_owner ON leases(owner)")
                self.connection.execute("CREATE TABLE IF NOT EXISTS revisions (id INTEGER PRIMARY KEY CHECK(id=1),value BIGINT NOT NULL)")
                self.connection.execute("INSERT INTO revisions SELECT 1,COALESCE(MAX(version),0) FROM records ON CONFLICT(id) DO NOTHING")
        except BaseException:
            self.connection.close()
            raise

    def _connect(self):
        import psycopg
        from psycopg import sql
        try:
            connection = psycopg.connect(self._dsn, autocommit=True, connect_timeout=10,
                options="-c statement_timeout=15000 -c lock_timeout=10000")
            connection.execute(sql.SQL("SET search_path TO {}, pg_catalog").format(sql.Identifier(self.namespace)))
            return connection
        except psycopg.Error:
            raise RuntimeError("Could not connect to the configured PostgreSQL database") from None

    def _ensure_connection(self):
        if self._closed:
            raise RuntimeError("Storage backend is closed")
        if self.connection.closed:
            if self._transaction_depth:
                raise RuntimeError("Database connection lost during a transaction; operation was not retried")
            # Reconnect on a later operation, never retry an ambiguous write or
            # continue an interrupted transaction on a fresh connection.
            self.connection = self._connect()

    def transaction(self):
        return _PostgresTransaction(self)

    def is_empty(self) -> bool:
        """Check raw application state under the namespace transaction lock."""
        with self.transaction():
            return bool(self.connection.execute(
                "SELECT NOT (EXISTS(SELECT 1 FROM records) OR "
                "EXISTS(SELECT 1 FROM leases) OR EXISTS(SELECT 1 FROM revoked_owners))"
            ).fetchone()[0])

    def now(self) -> float:
        with self.lock:
            self._ensure_connection()
            return float(self.connection.execute("SELECT EXTRACT(EPOCH FROM clock_timestamp())").fetchone()[0])

    def _purge(self, now: float) -> None:
        self.connection.execute("DELETE FROM records WHERE expires<=%s", (now,))
        self.connection.execute("DELETE FROM revoked_owners WHERE expires<=%s", (now,))
        self.connection.execute("DELETE FROM leases WHERE expires<=%s", (now,))

    def _check_capacity(self, kind: str, key_hash: str) -> None:
        exists = self.connection.execute("SELECT 1 FROM records WHERE kind=%s AND key=%s", (kind, key_hash)).fetchone()
        if not exists and self.connection.execute("SELECT COUNT(*) FROM records").fetchone()[0] >= self.capacity:
            raise ValueError("Authorization storage is at capacity")

    def put(self, kind, key, value, *, expires, owner=None):
        self._check_expiry(expires)
        payload, hashed = self._encoded(value), digest(key)
        with self.transaction():
            self._purge(self.now())
            self._check_owner(owner)
            self._check_capacity(kind, hashed)
            revision = self.connection.execute("UPDATE revisions SET value=value+1 WHERE id=1 RETURNING value").fetchone()[0]
            self.connection.execute("INSERT INTO records(kind,key,owner,expires,payload,version) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(kind,key) DO UPDATE SET owner=excluded.owner,expires=excluded.expires,payload=excluded.payload,version=excluded.version",
                (kind, hashed, owner, expires, payload, revision))

    def record(self, kind, key):
        hashed = digest(key)
        with self.lock:
            self._ensure_connection()
            row = self.connection.execute("SELECT owner,expires,payload,version FROM records WHERE kind=%s AND key=%s AND expires>EXTRACT(EPOCH FROM clock_timestamp())", (kind, hashed)).fetchone()
        if not row:
            return None
        return StoredRecord(json.loads(self.cipher.decrypt(bytes(row[2]))), row[0], row[1], row[3], hashed)

    def scan(self, kind, *, owner=None):
        with self.lock:
            self._ensure_connection()
            query = "SELECT key,owner,expires,payload,version FROM records WHERE kind=%s AND expires>EXTRACT(EPOCH FROM clock_timestamp())"
            params = (kind,)
            if owner is not None:
                query += " AND owner=%s"
                params += (owner,)
            rows = self.connection.execute(query + " ORDER BY key", params).fetchall()
        return [StoredRecord(json.loads(self.cipher.decrypt(bytes(row[3]))), row[1], row[2], row[4], row[0]) for row in rows]

    def delete(self, kind, key):
        with self.transaction():
            self.connection.execute("DELETE FROM records WHERE kind=%s AND key=%s", (kind, digest(key)))

    def is_owner_revoked(self, owner):
        with self.lock:
            self._ensure_connection()
            return self.connection.execute("SELECT 1 FROM revoked_owners WHERE owner_hash=%s AND expires>EXTRACT(EPOCH FROM clock_timestamp())", (digest(owner),)).fetchone() is not None

    def revoke_owner(self, owner, *, retention=31 * 86400):
        with self.transaction():
            self.connection.execute("DELETE FROM records WHERE owner=%s", (owner,))
            self.connection.execute("DELETE FROM leases WHERE owner=%s", (owner,))
            self.connection.execute("INSERT INTO revoked_owners VALUES (%s,%s) ON CONFLICT(owner_hash) DO UPDATE SET expires=GREATEST(revoked_owners.expires,excluded.expires)", (digest(owner), self.now() + retention))

    def acquire_lease(self, namespace, key, *, ttl=45, owner=None):
        if not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("Lease duration must be positive and finite")
        with self.transaction():
            now = self.now()
            self._purge(now)
            if owner is not None and self.is_owner_revoked(owner):
                return None
            if self.connection.execute("SELECT 1 FROM leases WHERE namespace=%s AND key=%s AND expires>%s", (namespace, digest(key), now)).fetchone():
                return None
            lease = Lease(namespace, key, secrets.token_urlsafe(32), now + ttl, owner)
            self.connection.execute("INSERT INTO leases VALUES (%s,%s,%s,%s,%s) ON CONFLICT(namespace,key) DO UPDATE SET holder=excluded.holder,expires=excluded.expires,owner=excluded.owner",
                (namespace, digest(key), lease.holder, lease.expires, owner))
            return lease

    def lease_valid(self, lease):
        with self.lock:
            self._ensure_connection()
            return self.connection.execute("SELECT 1 FROM leases WHERE namespace=%s AND key=%s AND holder=%s AND expires>EXTRACT(EPOCH FROM clock_timestamp())", (lease.namespace, digest(lease.key), lease.holder)).fetchone() is not None

    def renew_lease(self, lease, *, ttl=45):
        if not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("Lease duration must be positive and finite")
        with self.transaction():
            if lease.owner is not None and self.is_owner_revoked(lease.owner):
                return False
            now = self.now()
            return self.connection.execute("UPDATE leases SET expires=%s WHERE namespace=%s AND key=%s AND holder=%s AND expires>%s", (now + ttl, lease.namespace, digest(lease.key), lease.holder, now)).rowcount == 1

    def release_lease(self, lease):
        with self.transaction():
            self.connection.execute("DELETE FROM leases WHERE namespace=%s AND key=%s AND holder=%s", (lease.namespace, digest(lease.key), lease.holder))

    def increment_window(self, key, limit, period):
        if limit < 1 or not math.isfinite(period) or period <= 0:
            raise ValueError("Rate limit and period must be positive")
        with self.transaction():
            now = self.now()
            current = self.get("__rate_window__", key)
            if current is None:
                self.put("__rate_window__", key, {"count": 1, "reset_at": now + period}, expires=now + period)
                return True, 0.0
            if current["count"] >= limit:
                return False, max(0.0, current["reset_at"] - now)
            current["count"] += 1
            self.put("__rate_window__", key, current, expires=current["reset_at"])
            return True, 0.0

    def close(self):
        with self.lock:
            self._closed = True
            self.connection.close()
