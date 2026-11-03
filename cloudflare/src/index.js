import {DurableObject} from "cloudflare:workers";
import {Container, ContainerProxy} from "@cloudflare/containers";
import {StoreEngine, handleStoreRequest} from "./store.js";
import {sendAccountMail} from "./mail.js";
import {gateway} from "./gateway.js";

export {ContainerProxy};

export class CanvasStore extends DurableObject {
  constructor(ctx, env) { super(ctx, env); this.engine = new StoreEngine(ctx.storage); }
  fetch(request) { return handleStoreRequest(request, this.engine); }
}

export class CanvasContainer extends Container {
  defaultPort = 8000;
  sleepAfter = "10m";
  enableInternet = true;
  envVars = {
    CONNECTOR_PUBLIC_URL: "https://canvas.builtbyaadi.com",
    CONNECTOR_AUTH_MODE: "builtbyaadi",
    CONNECTOR_DATABASE: "cloudflare://store",
    CONNECTOR_ENCRYPTION_KEY: this.env.CONNECTOR_ENCRYPTION_KEY,
    CONNECTOR_EDGE_SECRET: this.env.CONNECTOR_EDGE_SECRET,
    CONNECTOR_ACCOUNT_MAIL: this.env.MAIL_ENABLED === "true" ? "cloudflare" : "disabled",
  };
}

CanvasContainer.outboundByHost = {
  "canvas-store.internal": (request, env) => env.CANVAS_STORE.getByName("store-v1").fetch(request),
  "canvas-mail.internal": (request, env) => sendAccountMail(request, env),
};

export default {fetch: gateway};
