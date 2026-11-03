"""Encrypted Store contract over a private Cloudflare Container binding.

The Durable Object owns committed state. A short fenced lease stages a complete
Python transaction; its commit is one SQLite transaction in the Durable Object.
Container restarts and failed requests cannot expose partial staged records.
This initial backend serializes transactions and is intended for a bounded beta,
not a claim of production capacity. No Cloudflare API credential is used here.
"""
from __future__ import annotations

import json
import math
import secrets
import threading
import time
from contextlib import suppress
from typing import Any

import httpx
from cryptography.fernet import Fernet

from .storage import Store, StoredRecord, Lease, digest

ENDPOINT = "http://canvas-store.internal/rpc"
CHUNK_BYTES = 128 * 1024
MAX_CIPHERTEXT_BYTES = 32 * 1024 * 1024


class CloudflareStorageError(RuntimeError):
    """A durable transaction failed or could not be independently confirmed."""


class _CloudflareTransaction:
    def __init__(self, store):
        self.store = store
        self.nested = False
        self.seq = 0

    def __enter__(self):
        store = self.store
        store.lock.acquire()
        try:
            if store._tx is not None:
                self.nested = True
                self.seq = store._rpc("savepoint")["seq"]
            else:
                store._begin()
        except BaseException:
            store.lock.release()
            raise
        return store

    def __exit__(self, exc_type, exc_value, traceback):
        store = self.store
        try:
            if self.nested:
                if exc_type is not None:
                    store._rpc("rollback", seq=self.seq)
            elif exc_type is None:
                store._commit()
            else:
                with suppress(Exception):
                    store._rpc("abort")
        finally:
            if not self.nested:
                store._tx = None
            store.lock.release()
        return False


class CloudflareStore(Store):
    """Drop-in durable backend. Inject transport only for synthetic tests."""
    def __init__(self, encryption_key: str | bytes, *, capacity: int = 20000,
                 transport: httpx.BaseTransport | None = None, acquire_timeout: float = 10):
        if type(capacity) is not int or not 1 <= capacity <= 20000:
            raise ValueError("Cloudflare capacity must be between 1 and 20000")
        if not math.isfinite(acquire_timeout) or not 0 < acquire_timeout <= 30:
            raise ValueError("Cloudflare acquisition timeout must be bounded")
        self.cipher = Fernet(encryption_key)
        self.capacity = capacity
        self.acquire_timeout = acquire_timeout
        self.lock = threading.RLock()
        self._tx: str | None = None
        self._client = httpx.Client(transport=transport, trust_env=False,
                                    follow_redirects=False, timeout=10)

    def transaction(self):
        return _CloudflareTransaction(self)

    def _rpc(self, op: str, **values) -> dict:
        if self._tx is None:
            raise CloudflareStorageError("Storage transaction is unavailable")
        try:
            response = self._client.post(ENDPOINT, json={"op": op, "tx": self._tx, **values})
            # The private virtual hostname must never redirect into the Internet.
            if response.status_code == 423:
                raise BlockingIOError("Storage transaction is busy")
            if response.status_code != 200:
                raise CloudflareStorageError("Durable storage operation failed")
            data = response.json()
            if not isinstance(data, dict):
                raise CloudflareStorageError("Invalid durable storage response")
            return data
        except (httpx.HTTPError, ValueError):
            raise CloudflareStorageError("Durable storage operation was not confirmed") from None

    def _begin(self):
        self._tx = secrets.token_urlsafe(32)
        deadline = time.monotonic() + self.acquire_timeout
        while True:
            try:
                result = self._rpc("begin")
                if result.get("state") != "active":
                    raise CloudflareStorageError("Durable transaction was not started")
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self._tx = None
                    raise CloudflareStorageError("Durable storage is busy") from None
                time.sleep(0.025)
            except BaseException:
                # A lost begin response may have acquired a lease. Best-effort
                # abort only this unpredictable transaction ID; never reacquire.
                with suppress(Exception):
                    self._rpc("abort")
                self._tx = None
                raise

    def _commit(self):
        try:
            result = self._rpc("commit")
            if result.get("state") == "committed":
                return
        except CloudflareStorageError:
            pass
        # Never replay the transaction or the commit. A durable status query can
        # confirm an already-applied commit after a lost response. Otherwise fail.
        try:
            if self._rpc("status").get("state") == "committed":
                return
        except Exception:
            pass
        with suppress(Exception):
            self._rpc("abort")
        raise CloudflareStorageError("Durable commit outcome could not be confirmed")

    def _get(self, category, namespace, key):
        return self._rpc("get", category=category, namespace=namespace, key=key).get("row")

    def _scan(self, category, namespace, owner=None):
        rows = self._rpc("scan", category=category, namespace=namespace, owner=owner).get("rows")
        if not isinstance(rows, list) or len(rows) > self.capacity:
            raise CloudflareStorageError("Invalid durable storage inventory")
        return rows

    def _write(self, category, namespace, key, *, expires, owner=None, metadata=None, blob=None):
        return self._rpc("write", category=category, namespace=namespace, key=key,
                         expires=expires, owner=owner, metadata=metadata or {}, blob=blob,
                         capacity=self.capacity)["version"]

    def _delete(self, category, namespace, key):
        self._rpc("delete", category=category, namespace=namespace, key=key)

    def _blob(self, payload: bytes) -> str:
        if len(payload) > MAX_CIPHERTEXT_BYTES:
            raise ValueError("Encrypted record exceeds Cloudflare storage bound")
        blob = secrets.token_urlsafe(32)
        self._rpc("blob_start", id=blob, size=len(payload), count=math.ceil(len(payload) / CHUNK_BYTES))
        for index, start in enumerate(range(0, len(payload), CHUNK_BYTES)):
            self._rpc("blob_chunk", id=blob, index=index,
                      data=payload[start:start + CHUNK_BYTES].decode("ascii"))
        return blob

    def _decode(self, row: dict) -> StoredRecord:
        blob = row["blob"]
        first = self._rpc("blob_read", id=blob, index=0)
        count, size = first["count"], first["size"]
        if (type(count) is not int or type(size) is not int or not 0 < size <= MAX_CIPHERTEXT_BYTES
                or count != math.ceil(size / CHUNK_BYTES)):
            raise CloudflareStorageError("Invalid ciphertext bounds")
        chunks = [first["data"]]
        for index in range(1, count):
            part = self._rpc("blob_read", id=blob, index=index)
            if part.get("count") != count or part.get("size") != size:
                raise CloudflareStorageError("Stored ciphertext changed")
            chunks.append(part["data"])
        payload = "".join(chunks).encode("ascii")
        if len(payload) != size:
            raise CloudflareStorageError("Stored ciphertext is incomplete")
        value = json.loads(self.cipher.decrypt(payload))
        if not isinstance(value, dict):
            raise CloudflareStorageError("Stored value is not an object")
        return StoredRecord(value, row["owner"], row["expires"], row["version"], row["key"])

    def put(self, kind: str, key: str, value: dict[str, Any], *, expires: float, owner: str | None = None) -> None:
        self._check_expiry(expires)
        payload = self._encoded(value)
        with self.transaction():
            self._check_owner(owner)
            blob = self._blob(payload)
            self._write("record", kind, digest(key), expires=expires, owner=owner, blob=blob)

    def record(self, kind: str, key: str) -> StoredRecord | None:
        with self.transaction():
            row = self._get("record", kind, digest(key))
            return self._decode(row) if row else None

    def scan(self, kind: str, *, owner: str | None = None) -> list[StoredRecord]:
        with self.transaction():
            return [self._decode(row) for row in self._scan("record", kind, owner)]

    def delete(self, kind: str, key: str) -> None:
        with self.transaction():
            self._delete("record", kind, digest(key))

    def is_owner_revoked(self, owner: str) -> bool:
        with self.transaction():
            return self._get("tombstone", "", digest(owner)) is not None

    def revoke_owner(self, owner: str, *, retention: float = 31 * 86400) -> None:
        if not math.isfinite(retention) or retention <= 0:
            raise ValueError("Revocation retention must be positive and finite")
        with self.transaction():
            rows = self._rpc("owned", owner=owner)["rows"]
            for row in rows:
                self._delete(row["category"], row["namespace"], row["key"])
            old = self._get("tombstone", "", digest(owner))
            expires = max(time.time() + retention, old["expires"] if old else 0)
            self._write("tombstone", "", digest(owner), expires=expires)

    def acquire_lease(self, namespace: str, key: str, *, ttl: float = 45, owner: str | None = None) -> Lease | None:
        if not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("Lease duration must be positive and finite")
        with self.transaction():
            if owner is not None and self.is_owner_revoked(owner):
                return None
            if self._get("lease", namespace, digest(key)) is not None:
                return None
            lease = Lease(namespace, key, secrets.token_urlsafe(32), time.time() + ttl, owner)
            self._write("lease", namespace, digest(key), expires=lease.expires, owner=owner,
                        metadata={"holder": lease.holder})
            return lease

    def lease_valid(self, lease: Lease) -> bool:
        with self.transaction():
            row = self._get("lease", lease.namespace, digest(lease.key))
            return row is not None and row["metadata"].get("holder") == lease.holder

    def renew_lease(self, lease: Lease, *, ttl: float = 45) -> bool:
        if not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("Lease duration must be positive and finite")
        with self.transaction():
            if lease.owner is not None and self.is_owner_revoked(lease.owner):
                return False
            if not self.lease_valid(lease):
                return False
            self._write("lease", lease.namespace, digest(lease.key), expires=time.time() + ttl,
                        owner=lease.owner, metadata={"holder": lease.holder})
            return True

    def release_lease(self, lease: Lease) -> None:
        with self.transaction():
            if self.lease_valid(lease):
                self._delete("lease", lease.namespace, digest(lease.key))

    def close(self) -> None:
        with self.lock:
            if self._tx:
                with suppress(Exception):
                    self._rpc("abort")
                self._tx = None
            self._client.close()
