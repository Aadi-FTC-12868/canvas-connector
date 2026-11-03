import test from "node:test";
import assert from "node:assert/strict";
import {createHmac} from "node:crypto";
import {gateway} from "../src/gateway.js";
import {sendAccountMail} from "../src/mail.js";

test("gateway overwrites untrusted proxy headers and signs exact request", async () => {
  let forwarded;
  const env = {HOSTED_ENABLED: "true", CONNECTOR_ENCRYPTION_KEY: "synthetic", CONNECTOR_EDGE_SECRET: "s".repeat(32),
    CANVAS_CONTAINER: {getByName(name) { assert.equal(name, "beta-v1"); return {fetch(r) {forwarded=r; return new Response("ok");}};}}};
  const request = new Request("https://canvas.builtbyaadi.com/mcp?q=%2F&a=1", {method: "POST", body: "{}", headers: {
    "CF-Connecting-IP": "192.0.2.4", "X-Forwarded-For": "1.1.1.1", "Forwarded": "for=1.1.1.1",
    "X-Canvas-Edge-Signature": "evil", "CF-Container-Target-Port": "22",
  }});
  assert.equal((await gateway(request, env)).status, 200);
  assert.equal(forwarded.headers.get("x-forwarded-for"), null);
  assert.equal(forwarded.headers.get("forwarded"), null);
  assert.equal(forwarded.headers.get("cf-container-target-port"), null);
  const time = forwarded.headers.get("x-canvas-edge-time");
  const expected = createHmac("sha256", env.CONNECTOR_EDGE_SECRET).update(`v1\n${time}\n192.0.2.4\nPOST\n/mcp?q=%2F&a=1`).digest("hex");
  assert.equal(forwarded.headers.get("x-canvas-edge-signature"), expected);
  assert.equal(await forwarded.text(), "{}");
});

test("public gateway never exposes storage/mail handlers or disabled beta", async () => {
  for (const url of ["https://canvas-store.internal/rpc", "https://canvas-mail.internal/send", "https://canvas.builtbyaadi.com/_internal/rpc", "http://canvas.builtbyaadi.com/"]) {
    assert.equal((await gateway(new Request(url), {})).status, 404);
  }
  assert.equal((await gateway(new Request("https://canvas.builtbyaadi.com/setup"), {})).status, 503);
});

test("mail disabled by default and uses only exact configured sender", async () => {
  const request = () => new Request("http://canvas-mail.internal/send", {method: "POST", body: JSON.stringify({email: "student@example.invalid", purpose: "verify", code: "synthetic-code-123"})});
  assert.equal((await sendAccountMail(request(), {})).status, 503);
  let message;
  const env = {MAIL_ENABLED: "true", MAIL_FROM: "accounts@builtbyaadi.com", EMAIL: {send(value) { message=value; return {messageId: "synthetic-id"};}}};
  assert.deepEqual(await (await sendAccountMail(request(), env)).json(), {sent: true});
  assert.equal(message.to, "student@example.invalid"); assert.equal(message.from, "accounts@builtbyaadi.com");
  assert.equal(message.html, undefined); assert.match(message.text, /synthetic-code-123/);
});

test("mail failures and invalid recipients never echo codes/provider diagnostics", async () => {
  const env = {MAIL_ENABLED: "true", MAIL_FROM: "accounts@builtbyaadi.com", EMAIL: {send() {throw new Error("secret provider diagnostic");}}};
  const r = value => new Request("http://canvas-mail.internal/send", {method: "POST", body: JSON.stringify(value)});
  assert.equal((await sendAccountMail(r({email:"evil\r\nbcc:x@y.z",purpose:"verify",code:"secretcode"}), env)).status, 400);
  const response = await sendAccountMail(r({email:"x@example.invalid",purpose:"reset",code:"secretcode"}), env);
  assert.equal(response.status, 503); assert.deepEqual(await response.json(), {error: "mail_not_confirmed"});
});
