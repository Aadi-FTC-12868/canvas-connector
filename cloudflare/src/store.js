// Private Durable Object protocol. Only the Container outbound binding calls it.
// Records remain Fernet ciphertext. No Canvas credential is decrypted here.
const CHUNK = 128 * 1024;
const MAX_BLOB = 32 * 1024 * 1024;
const MAX_OPS = 4096;
const LEASE_MS = 60_000;
const MAX_TX_MS = 180_000;
const RETAIN_MS = 60 * 60 * 1000;
const TOKEN = /^[A-Za-z0-9_-]{32,128}$/;
const HASH = /^[a-f0-9]{64}$/;

class ProtocolError extends Error {
  constructor(code, status = 400) { super(code); this.code = code; this.status = status; }
}
const requireValue = (ok, code = "invalid_request") => { if (!ok) throw new ProtocolError(code); };
const finite = value => typeof value === "number" && Number.isFinite(value);

export class StoreEngine {
  constructor(storage, now = () => Date.now()) {
    this.storage = storage; this.sql = storage.sql; this.now = now;
    this.sql.exec(`
      CREATE TABLE IF NOT EXISTS meta (id INTEGER PRIMARY KEY, revision INTEGER NOT NULL);
      INSERT OR IGNORE INTO meta VALUES (1,0);
      CREATE TABLE IF NOT EXISTS transactions (id TEXT PRIMARY KEY, state TEXT NOT NULL,
        started REAL NOT NULL, deadline REAL NOT NULL, ended REAL, seq INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS entities (category TEXT NOT NULL, namespace TEXT NOT NULL,
        key TEXT NOT NULL, owner TEXT, expires REAL NOT NULL, version INTEGER NOT NULL,
        blob TEXT, metadata TEXT NOT NULL, PRIMARY KEY(category,namespace,key));
      CREATE INDEX IF NOT EXISTS entities_owner ON entities(owner);
      CREATE TABLE IF NOT EXISTS staged (tx TEXT NOT NULL, seq INTEGER NOT NULL,
        category TEXT NOT NULL, namespace TEXT NOT NULL, key TEXT NOT NULL, owner TEXT,
        expires REAL NOT NULL, version INTEGER NOT NULL, blob TEXT, metadata TEXT NOT NULL,
        deleted INTEGER NOT NULL, PRIMARY KEY(tx,seq));
      CREATE INDEX IF NOT EXISTS staged_key ON staged(tx,category,namespace,key,seq);
      CREATE TABLE IF NOT EXISTS blobs (id TEXT PRIMARY KEY, tx TEXT NOT NULL,
        count INTEGER NOT NULL, size INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS chunks (blob TEXT NOT NULL, idx INTEGER NOT NULL,
        data TEXT NOT NULL, PRIMARY KEY(blob,idx));
    `);
  }

  rows(sql, ...args) { return [...this.sql.exec(sql, ...args)]; }
  first(sql, ...args) { return this.rows(sql, ...args)[0] ?? null; }
  atomic(fn) { return this.storage.transactionSync(fn); }

  discard(id) {
    this.sql.exec("DELETE FROM chunks WHERE blob IN (SELECT id FROM blobs WHERE tx=?)", id);
    this.sql.exec("DELETE FROM blobs WHERE tx=?", id);
    this.sql.exec("DELETE FROM staged WHERE tx=?", id);
    this.sql.exec("UPDATE transactions SET state='aborted',ended=? WHERE id=? AND state='active'", this.now(), id);
  }

  cleanup() {
    const now = this.now();
    for (const row of this.rows("SELECT id FROM transactions WHERE state='active' AND deadline<=?", now)) this.discard(row.id);
    this.sql.exec("DELETE FROM transactions WHERE state!='active' AND ended<?", now - RETAIN_MS);
    this.sql.exec("DELETE FROM entities WHERE expires<=?", now / 1000);
    this.gc();
  }

  gc() {
    this.sql.exec("DELETE FROM chunks WHERE blob IN (SELECT id FROM blobs WHERE tx='' AND id NOT IN (SELECT blob FROM entities WHERE blob IS NOT NULL))");
    this.sql.exec("DELETE FROM blobs WHERE tx='' AND id NOT IN (SELECT blob FROM entities WHERE blob IS NOT NULL)");
  }

  active(id) {
    requireValue(typeof id === "string" && TOKEN.test(id));
    const tx = this.first("SELECT * FROM transactions WHERE id=?", id);
    if (!tx || tx.state !== "active" || tx.deadline <= this.now()) {
      if (tx?.state === "active") this.discard(id);
      throw new ProtocolError("transaction_expired", 409);
    }
    if (tx.seq >= MAX_OPS) throw new ProtocolError("transaction_limit", 409);
    this.sql.exec("UPDATE transactions SET deadline=? WHERE id=?", Math.min(this.now() + LEASE_MS, tx.started + MAX_TX_MS), id);
    return tx;
  }

  key(input) {
    requireValue(["record", "lease", "tombstone"].includes(input.category));
    requireValue(typeof input.namespace === "string" && input.namespace.length <= 200);
    requireValue(typeof input.key === "string" && HASH.test(input.key));
  }

  view(tx, category, namespace, key) {
    const pending = this.first("SELECT * FROM staged WHERE tx=? AND category=? AND namespace=? AND key=? ORDER BY seq DESC LIMIT 1", tx, category, namespace, key);
    const row = pending ?? this.first("SELECT * FROM entities WHERE category=? AND namespace=? AND key=?", category, namespace, key);
    if (!row || row.deleted || row.expires <= this.now() / 1000) return null;
    return {category, namespace, key, owner: row.owner, expires: row.expires,
      version: row.version, blob: row.blob, metadata: JSON.parse(row.metadata)};
  }

  scan(tx, category, namespace, owner = null) {
    const keys = this.rows("SELECT key FROM entities WHERE category=? AND namespace=? UNION SELECT key FROM staged WHERE tx=? AND category=? AND namespace=? ORDER BY key", category, namespace, tx, category, namespace);
    return keys.map(({key}) => this.view(tx, category, namespace, key)).filter(row => row && (owner === null || row.owner === owner));
  }

  stage(tx, row, deleted = false) {
    this.sql.exec("UPDATE transactions SET seq=seq+1 WHERE id=?", tx);
    this.sql.exec("UPDATE meta SET revision=revision+1 WHERE id=1");
    const {seq} = this.first("SELECT seq FROM transactions WHERE id=?", tx);
    const {revision} = this.first("SELECT revision FROM meta WHERE id=1");
    this.sql.exec("INSERT INTO staged VALUES (?,?,?,?,?,?,?,?,?,?,?)", tx, seq, row.category, row.namespace,
      row.key, row.owner ?? null, row.expires ?? 0, revision, row.blob ?? null,
      JSON.stringify(row.metadata ?? {}), deleted ? 1 : 0);
    return revision;
  }

  handle(input) {
    return this.atomic(() => {
      requireValue(input && typeof input === "object" && !Array.isArray(input));
      const {op, tx} = input;
      requireValue(typeof tx === "string" && TOKEN.test(tx));
      if (op === "status") {
        const row = this.first("SELECT state,deadline FROM transactions WHERE id=?", tx);
        if (row?.state === "active" && row.deadline <= this.now()) { this.discard(tx); return {state: "aborted"}; }
        return {state: row?.state ?? "unknown"};
      }
      if (op === "begin") {
        this.cleanup();
        const previous = this.first("SELECT state FROM transactions WHERE id=?", tx);
        if (previous) throw new ProtocolError("transaction_id_used", 409);
        if (this.first("SELECT id FROM transactions WHERE state='active'")) throw new ProtocolError("busy", 423);
        const now = this.now();
        this.sql.exec("INSERT INTO transactions(id,state,started,deadline) VALUES (?,'active',?,?)", tx, now, now + LEASE_MS);
        return {state: "active"};
      }
      if (op === "abort") {
        const existing = this.first("SELECT state FROM transactions WHERE id=?", tx);
        if (existing?.state === "active") this.discard(tx);
        return {state: existing?.state === "committed" ? "committed" : "aborted"};
      }
      const current = this.active(tx);
      if (op === "savepoint") return {seq: current.seq};
      if (op === "rollback") {
        requireValue(Number.isInteger(input.seq) && input.seq >= 0 && input.seq <= current.seq);
        this.sql.exec("DELETE FROM staged WHERE tx=? AND seq>?", tx, input.seq);
        // Sequence numbers never go backwards; future savepoints remain unique.
        return {ok: true};
      }
      if (op === "commit") {
        const rows = this.rows("SELECT * FROM staged WHERE tx=? ORDER BY seq", tx);
        for (const row of rows) {
          if (row.deleted) this.sql.exec("DELETE FROM entities WHERE category=? AND namespace=? AND key=?", row.category, row.namespace, row.key);
          else this.sql.exec("INSERT INTO entities VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(category,namespace,key) DO UPDATE SET owner=excluded.owner,expires=excluded.expires,version=excluded.version,blob=excluded.blob,metadata=excluded.metadata", row.category, row.namespace, row.key, row.owner, row.expires, row.version, row.blob, row.metadata);
        }
        this.sql.exec("UPDATE blobs SET tx='' WHERE tx=?", tx);
        this.sql.exec("DELETE FROM staged WHERE tx=?", tx);
        this.sql.exec("UPDATE transactions SET state='committed',ended=? WHERE id=?", this.now(), tx);
        this.gc();
        return {state: "committed"};
      }
      if (op === "blob_start") {
        requireValue(TOKEN.test(input.id) && Number.isInteger(input.size) && input.size > 0 && input.size <= MAX_BLOB);
        requireValue(Number.isInteger(input.count) && input.count === Math.ceil(input.size / CHUNK));
        this.sql.exec("INSERT INTO blobs VALUES (?,?,?,?)", input.id, tx, input.count, input.size);
        return {ok: true};
      }
      if (op === "blob_chunk") {
        const blob = this.first("SELECT * FROM blobs WHERE id=? AND tx=?", input.id, tx);
        requireValue(blob && Number.isInteger(input.index) && input.index >= 0 && input.index < blob.count);
        const wanted = input.index === blob.count - 1 ? blob.size - input.index * CHUNK : CHUNK;
        requireValue(typeof input.data === "string" && input.data.length === wanted && /^[A-Za-z0-9_=-]+$/.test(input.data));
        this.sql.exec("INSERT INTO chunks VALUES (?,?,?)", input.id, input.index, input.data);
        return {ok: true};
      }
      if (op === "blob_read") {
        const blob = this.first("SELECT * FROM blobs WHERE id=? AND (tx='' OR tx=?)", input.id, tx);
        requireValue(blob && Number.isInteger(input.index) && input.index >= 0 && input.index < blob.count);
        const chunk = this.first("SELECT data FROM chunks WHERE blob=? AND idx=?", input.id, input.index);
        requireValue(chunk, "incomplete_blob");
        return {data: chunk.data, count: blob.count, size: blob.size};
      }
      if (op === "scan") {
        requireValue(["record", "lease", "tombstone"].includes(input.category));
        requireValue(typeof input.namespace === "string" && input.namespace.length <= 200);
        requireValue(input.owner === null || (typeof input.owner === "string" && input.owner.length <= 200));
        // Metadata only, never an entire store or large ciphertext response.
        return {rows: this.scan(tx, input.category, input.namespace, input.owner)};
      }
      if (op === "owned") {
        requireValue(typeof input.owner === "string" && input.owner.length <= 200);
        const groups = this.rows("SELECT DISTINCT category,namespace FROM entities UNION SELECT DISTINCT category,namespace FROM staged WHERE tx=?", tx);
        return {rows: groups.flatMap(r => this.scan(tx, r.category, r.namespace, input.owner))};
      }
      this.key(input);
      if (op === "get") return {row: this.view(tx, input.category, input.namespace, input.key)};
      if (op === "delete") { this.stage(tx, input, true); return {ok: true}; }
      if (op === "write") {
        requireValue(finite(input.expires) && (input.owner === null || (typeof input.owner === "string" && input.owner.length <= 200)));
        requireValue(input.metadata && typeof input.metadata === "object" && JSON.stringify(input.metadata).length < 2048);
        if (input.category === "record") {
          requireValue(typeof input.blob === "string" && TOKEN.test(input.blob));
          const blob = this.first("SELECT * FROM blobs WHERE id=? AND tx=?", input.blob, tx);
          const chunks = this.first("SELECT COUNT(*) AS count,COALESCE(SUM(LENGTH(data)),0) AS size FROM chunks WHERE blob=?", input.blob);
          requireValue(blob && chunks.count === blob.count && chunks.size === blob.size, "incomplete_blob");
          const old = this.view(tx, input.category, input.namespace, input.key);
          const limit = input.capacity;
          requireValue(Number.isInteger(limit) && limit > 0 && limit <= 20000);
          if (!old) {
            const groups = this.rows("SELECT DISTINCT namespace FROM entities WHERE category='record' UNION SELECT DISTINCT namespace FROM staged WHERE tx=? AND category='record'", tx);
            const count = groups.reduce((n, r) => n + this.scan(tx, "record", r.namespace).length, 0);
            if (count >= limit) throw new ProtocolError("capacity", 507);
          }
        } else requireValue(input.blob === null);
        return {version: this.stage(tx, input)};
      }
      throw new ProtocolError("unknown_operation");
    });
  }
}

export async function handleStoreRequest(request, engine) {
  try {
    if (request.method !== "POST" || new URL(request.url).pathname !== "/rpc") return new Response(null, {status: 404});
    const limit = CHUNK + 4096;
    const reader = request.body?.getReader();
    requireValue(reader, "invalid_request");
    let size = 0;
    const parts = [];
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      size += value.length;
      if (size > limit) { await reader.cancel(); throw new ProtocolError("request_too_large", 413); }
      parts.push(value);
    }
    const bytes = new Uint8Array(size); let offset = 0;
    for (const part of parts) { bytes.set(part, offset); offset += part.length; }
    const content = new TextDecoder("utf-8", {fatal: true}).decode(bytes);
    const result = engine.handle(JSON.parse(content));
    return Response.json(result, {headers: {"Cache-Control": "no-store"}});
  } catch (error) {
    const known = error instanceof ProtocolError;
    return Response.json({error: known ? error.code : "storage_failure"}, {status: known ? error.status : 500, headers: {"Cache-Control": "no-store"}});
  }
}
