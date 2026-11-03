export async function gateway(request, env) {
  const url = new URL(request.url);
  if (url.hostname !== "canvas.builtbyaadi.com" || url.protocol !== "https:"
      || url.pathname.startsWith("/_internal") || ["CONNECT", "TRACE"].includes(request.method)) {
    return new Response("Not found", {status: 404});
  }
  if (env.HOSTED_ENABLED !== "true" || !env.CONNECTOR_ENCRYPTION_KEY
      || typeof env.CONNECTOR_EDGE_SECRET !== "string" || env.CONNECTOR_EDGE_SECRET.length < 32
      || !/^[\x21-\x7e]+$/.test(env.CONNECTOR_EDGE_SECRET)) {
    return new Response("Hosted setup is not available yet. Use https://builtbyaadi.com/canvas-connector/local.html", {
      status: 503, headers: {"Cache-Control": "no-store", "Content-Type": "text/plain; charset=utf-8"},
    });
  }
  // Cloudflare sets this header at its public edge. It is not sourced from an
  // arbitrary forwarded header or any model/tool parameter.
  const ip = request.headers.get("CF-Connecting-IP");
  if (!ip || !/^[0-9a-fA-F:.]{3,45}$/.test(ip)) return new Response("Unavailable", {status: 503});
  const headers = new Headers(request.headers);
  for (const name of [...headers.keys()]) {
    if (name.startsWith("cf-container-") || name.startsWith("x-canvas-edge-")
        || ["forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto"].includes(name)) headers.delete(name);
  }
  const timestamp = String(Math.floor(Date.now() / 1000));
  const message = `v1\n${timestamp}\n${ip}\n${request.method.toUpperCase()}\n${url.pathname}${url.search}`;
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey("raw", encoder.encode(env.CONNECTOR_EDGE_SECRET), {name: "HMAC", hash: "SHA-256"}, false, ["sign"]);
  const signature = await crypto.subtle.sign("HMAC", key, encoder.encode(message));
  headers.set("X-Canvas-Edge-Ip", ip);
  headers.set("X-Canvas-Edge-Time", timestamp);
  headers.set("X-Canvas-Edge-Signature", Array.from(new Uint8Array(signature), n => n.toString(16).padStart(2, "0")).join(""));
  return env.CANVAS_CONTAINER.getByName("beta-v1").fetch(new Request(request, {headers}));
}
