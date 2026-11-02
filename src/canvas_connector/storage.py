"""Encrypted SQLite records for a single-host connector deployment.

Opaque lookup values are hashed before storage. Payloads (including institution
refresh tokens) are encrypted. The key must be supplied separately from the DB.
SQLite transactions make one-use state/code/refresh consumption atomic.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

from cryptography.fernet import Fernet


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, path: str | Path, encryption_key: str | bytes, *, capacity: int = 20000):
        self.cipher = Fernet(encryption_key)
        self.capacity = capacity
        self.lock = threading.RLock()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise ValueError("Database must not be a symlink")
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        os.chmod(path, 0o600)
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT NOT NULL, key TEXT NOT NULL, owner TEXT, expires REAL NOT NULL, payload BLOB NOT NULL, PRIMARY KEY(kind,key))")
        self.db.execute("CREATE INDEX IF NOT EXISTS record_owner ON records(owner)")

    def put(self, kind: str, key: str, value: dict[str, Any], *, expires: float, owner: str | None = None) -> None:
        payload = self.cipher.encrypt(json.dumps(value, separators=(",", ":")).encode())
        with self.lock:
            self.db.execute("DELETE FROM records WHERE expires <= ?", (time.time(),))
            exists = self.db.execute("SELECT 1 FROM records WHERE kind=? AND key=?", (kind, digest(key))).fetchone()
            if not exists and self.db.execute("SELECT COUNT(*) FROM records").fetchone()[0] >= self.capacity:
                raise ValueError("Authorization storage is at capacity")
            self.db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?,?,?)", (kind, digest(key), owner, expires, payload))

    def get(self, kind: str, key: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute("SELECT payload FROM records WHERE kind=? AND key=? AND expires>?", (kind, digest(key), time.time())).fetchone()
        return json.loads(self.cipher.decrypt(row[0])) if row else None

    def consume(self, kind: str, key: str) -> dict[str, Any] | None:
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute("SELECT payload FROM records WHERE kind=? AND key=? AND expires>?", (kind, digest(key), time.time())).fetchone()
                self.db.execute("DELETE FROM records WHERE kind=? AND key=?", (kind, digest(key)))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        return json.loads(self.cipher.decrypt(row[0])) if row else None

    def delete(self, kind: str, key: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM records WHERE kind=? AND key=?", (kind, digest(key)))

    def revoke_owner(self, owner: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM records WHERE owner=?", (owner,))

    def close(self) -> None:
        with self.lock:
            self.db.close()
