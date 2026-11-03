import http from "node:http";
import {StoreEngine, handleStoreRequest} from "../src/store.js";
import {sqliteStorage} from "./sqlite.mjs";
const storage = sqliteStorage(process.argv[2] ?? ":memory:");
let offset = 0;
const engine = new StoreEngine(storage, () => Date.now() + offset);
const server = http.createServer(async (req, res) => {
  if (req.url === "/test/advance" && req.method === "POST") {
    let input = ""; for await (const c of req) input += c;
    offset += JSON.parse(input).milliseconds;
    res.end("{}"); return;
  }
  const parts = []; for await (const c of req) parts.push(c);
  const body = Buffer.concat(parts);
  const result = await handleStoreRequest(new Request("http://canvas-store.internal" + req.url, {
    method: req.method, headers: req.headers, body: req.method === "POST" ? body : undefined,
  }), engine);
  res.writeHead(result.status, Object.fromEntries(result.headers)); res.end(Buffer.from(await result.arrayBuffer()));
});
server.listen(0, "127.0.0.1", () => process.stdout.write(JSON.stringify({port: server.address().port}) + "\n"));
process.on("SIGTERM", () => server.close(() => {storage.db.close(); process.exit(0);}));
