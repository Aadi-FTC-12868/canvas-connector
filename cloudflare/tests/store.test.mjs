import test from "node:test";
import assert from "node:assert/strict";
import {randomBytes, createHash} from "node:crypto";
import {mkdtemp, rm} from "node:fs/promises";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {build} from "esbuild";
import {Miniflare} from "miniflare";
import {StoreEngine, handleStoreRequest} from "../src/store.js";
import {sqliteStorage} from "./sqlite.mjs";

const token = () => randomBytes(32).toString("base64url");
const hash = value => createHash("sha256").update(value).digest("hex");
const row = (key, blob) => ({category: "record", namespace: "test", key: hash(key), owner: "owner",
  expires: Date.now() / 1000 + 600, metadata: {}, blob, capacity: 20000});
function blob(engine, tx, text = "A".repeat(200)) {
  const id = token(); engine.handle({op: "blob_start", tx, id, size: text.length, count: Math.ceil(text.length / 131072)});
  for (let index = 0; index < Math.ceil(text.length / 131072); index++) engine.handle({op: "blob_chunk", tx, id, index, data: text.slice(index * 131072, (index + 1) * 131072)});
  return id;
}

test("committed data remains invisible until single SQLite commit", () => {
  const storage = sqliteStorage(); const engine = new StoreEngine(storage); const tx = token();
  engine.handle({op: "begin", tx}); const id = blob(engine, tx);
  engine.handle({op: "write", tx, ...row("a", id)});
  assert.equal(storage.db.prepare("SELECT COUNT(*) AS n FROM entities").get().n, 0);
  assert.throws(() => engine.handle({op: "begin", tx: token()}), /busy/);
  engine.handle({op: "commit", tx});
  assert.equal(storage.db.prepare("SELECT COUNT(*) AS n FROM entities").get().n, 1);
  assert.deepEqual(engine.handle({op: "status", tx}), {state: "committed"});
  storage.db.close();
});

test("commit failure rolls back every visible change", () => {
  const storage = sqliteStorage(); const engine = new StoreEngine(storage); const tx = token();
  engine.handle({op: "begin", tx});
  engine.handle({op: "write", tx, ...row("a", blob(engine, tx))});
  engine.handle({op: "write", tx, ...row("b", blob(engine, tx))});
  const execute = storage.sql.exec; let writes = 0;
  storage.sql.exec = (sql, ...args) => {
    if (sql.startsWith("INSERT INTO entities") && ++writes === 2) throw new Error("synthetic disk fault");
    return execute(sql, ...args);
  };
  assert.throws(() => engine.handle({op: "commit", tx}), /synthetic disk fault/);
  storage.sql.exec = execute;
  assert.equal(storage.db.prepare("SELECT COUNT(*) AS n FROM entities").get().n, 0);
  engine.handle({op: "abort", tx});
  assert.equal(storage.db.prepare("SELECT COUNT(*) AS n FROM chunks").get().n, 0);
  storage.db.close();
});

test("expired transaction cannot renew, commit or block next writer", () => {
  let now = Date.now(); const storage = sqliteStorage(); const engine = new StoreEngine(storage, () => now); const tx = token();
  engine.handle({op: "begin", tx}); engine.handle({op: "write", tx, ...row("a", blob(engine, tx))});
  now += 61000;
  assert.throws(() => engine.handle({op: "get", tx, category: "record", namespace: "test", key: hash("a")}), /transaction_expired/);
  assert.throws(() => engine.handle({op: "commit", tx}), /transaction_expired/);
  const next = token(); engine.handle({op: "begin", tx: next});
  assert.equal(engine.handle({op: "get", tx: next, category: "record", namespace: "test", key: hash("a")}).row, null);
  assert.equal(engine.handle({op: "status", tx}).state, "aborted");
  engine.handle({op: "abort", tx: next}); storage.db.close();
});

test("chunks stay below row bound and incomplete payload cannot be published", () => {
  const storage = sqliteStorage(); const engine = new StoreEngine(storage); const tx = token();
  engine.handle({op: "begin", tx}); const id = token();
  engine.handle({op: "blob_start", tx, id, size: 131073, count: 2});
  engine.handle({op: "blob_chunk", tx, id, index: 0, data: "A".repeat(131072)});
  assert.throws(() => engine.handle({op: "write", tx, ...row("a", id)}), /incomplete_blob/);
  assert.throws(() => engine.handle({op: "blob_chunk", tx, id, index: 1, data: "too long"}), /invalid_request/);
  engine.handle({op: "abort", tx}); storage.db.close();
});

test("transaction absolute deadline prevents indefinite lock renewal", () => {
  let now = Date.now(); const storage = sqliteStorage(); const engine = new StoreEngine(storage, () => now); const tx = token();
  engine.handle({op: "begin", tx});
  for (let i = 0; i < 4; i++) { now += 40000; engine.handle({op: "savepoint", tx}); }
  now += 21000;
  assert.throws(() => engine.handle({op: "savepoint", tx}), /transaction_expired/);
  storage.db.close();
});

test("private HTTP rejects redirect-style URLs, methods and malformed payloads", async () => {
  const storage = sqliteStorage(); const engine = new StoreEngine(storage);
  assert.equal((await handleStoreRequest(new Request("http://canvas-store.internal/rpc"), engine)).status, 404);
  assert.equal((await handleStoreRequest(new Request("http://canvas-store.internal/else", {method: "POST", body: "{}"}), engine)).status, 404);
  const response = await handleStoreRequest(new Request("http://canvas-store.internal/rpc", {method: "POST", body: "not json"}), engine);
  assert.equal(response.status, 500);
  assert.deepEqual(await response.json(), {error: "storage_failure"});
  storage.db.close();
});

test("actual workerd Durable Object commits chunked records and persists restart", async () => {
  const dir = await mkdtemp(join(tmpdir(), "canvas-cf-do-"));
  const entry = `import {DurableObject} from 'cloudflare:workers';
    import {StoreEngine,handleStoreRequest} from './src/store.js';
    export class TestStore extends DurableObject { constructor(ctx,env){super(ctx,env);this.engine=new StoreEngine(ctx.storage)} fetch(r){return handleStoreRequest(r,this.engine)} }
    export default {fetch(r,env){return env.STORE.getByName('synthetic').fetch(r)}};`;
  const bundle = await build({stdin: {contents: entry, resolveDir: process.cwd()}, bundle: true, write: false, format: "esm", external: ["cloudflare:workers"]});
  const options = {modules: true, script: bundle.outputFiles[0].text, compatibilityDate: "2026-07-30",
    durableObjects: {STORE: {className: "TestStore", useSQLite: true}}, durableObjectsPersist: dir};
  let mf = new Miniflare(options);
  const rpc = async value => {
    const response = await mf.dispatchFetch("http://test/rpc", {method: "POST", body: JSON.stringify(value)});
    assert.equal(response.status, 200, await response.clone().text()); return response.json();
  };
  try {
    const tx = token(), id = token(), content = "A".repeat(3 * 1024 * 1024);
    await rpc({op: "begin", tx}); await rpc({op: "blob_start", tx, id, size: content.length, count: content.length / 131072});
    for (let index = 0; index < content.length / 131072; index++) await rpc({op: "blob_chunk", tx, id, index, data: content.slice(index * 131072, (index + 1) * 131072)});
    await rpc({op: "write", tx, ...row("a", id)}); await rpc({op: "commit", tx});
    await mf.dispose(); mf = new Miniflare(options);
    assert.equal((await rpc({op: "status", tx})).state, "committed");
    const next = token(); await rpc({op: "begin", tx: next});
    assert.equal((await rpc({op: "get", tx: next, category: "record", namespace: "test", key: hash("a")})).row.blob, id);
    assert.equal((await rpc({op: "blob_read", tx: next, id, index: 23})).data.length, 131072);
    await rpc({op: "commit", tx: next});
  } finally { await mf.dispose(); await rm(dir, {recursive: true, force: true}); }
});
