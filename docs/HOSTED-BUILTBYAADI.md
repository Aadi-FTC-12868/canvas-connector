# Planned hosting at canvas.builtbyaadi.com

**Status: deployment intentionally pending.** No Canvas OAuth developer key is available yet, and no application host or DNS destination has been selected. These are planned settings and verification steps, not a live-service claim.

Canvas Connector remains independent, MIT-licensed software that anyone can run locally or self-host. Personal local `stdio` works without an OAuth developer key using the owner's private Canvas token; see [local setup](../README.md#run-locally). Hosted multi-user sign-in still requires an institution-enabled Canvas developer key and secret. Do not replace that flow with shared or user-pasted personal tokens.

## Fixed public addresses

| Purpose | Planned value |
| --- | --- |
| Public origin | `https://canvas.builtbyaadi.com` |
| Advertised OAuth issuer | `https://canvas.builtbyaadi.com/` |
| MCP endpoint | `https://canvas.builtbyaadi.com/mcp` |
| Canvas developer-key redirect URI | `https://canvas.builtbyaadi.com/canvas/callback` |
| Authorization-server metadata | `https://canvas.builtbyaadi.com/.well-known/oauth-authorization-server` |
| Protected-resource metadata | `https://canvas.builtbyaadi.com/.well-known/oauth-protected-resource/mcp` |

The Canvas redirect URI is separate from each MCP client's registered redirect URI. Keep client redirects exact; follow [client compatibility](COMPATIBILITY.md).

## Host and private configuration

The existing Built by Aadi website is static Cloudflare hosting. It can link to the connector but does not run this Python MCP/OAuth service. Select a future VM or container host with persistent storage, then place a TLS reverse proxy in front of the application. Keep the static website separate.

The prepared Linux VM reference consists of an [environment template](../packaging/hosted/builtbyaadi.env.example), [systemd service](../packaging/hosted/canvas-connector.service), and [Caddy configuration](../packaging/hosted/builtbyaadi.Caddyfile). It runs two workers as `canvas-connector` from `/opt/canvas-connector/.venv`, listens on `127.0.0.1:8000`, and uses persistent local SQLite storage. These templates do not select or provision a host.

Copy the environment template to `/etc/canvas-connector/operator.env`, as required by the service, and store the institution JSON alongside it. Both stay outside the checkout with restricted permissions. The JSON schema is in [DEPLOYMENT.md](DEPLOYMENT.md#hosted-prerequisites).

| Configuration | Planned value or source |
| --- | --- |
| `CONNECTOR_PUBLIC_URL` | `https://canvas.builtbyaadi.com` |
| `CONNECTOR_INSTITUTIONS_FILE` in the operator environment | `/etc/canvas-connector/institutions.json` |
| `CONNECTOR_DATABASE` for the Linux reference | `/var/lib/canvas-connector/connector.sqlite3` |
| `CONNECTOR_ENCRYPTION_KEY` | Persistent private output of `canvas-connector keygen`; never commit it |
| `CONNECTOR_TRUSTED_PROXIES` for same-host Caddy | `127.0.0.1,::1`; verify the actual connecting peers |

The institution file must contain the real Canvas origin, display name, developer-key ID, and secret. Enable only the required [Canvas scopes](CANVAS-SCOPES.md). Restrict credential-file access to the operator and service account. Keep secrets and database backups outside Git; back up the encryption key separately.

[compose.yaml](../compose.yaml) is a container alternative: it publishes the same loopback port, stores SQLite at `/data/connector.sqlite3` in `connector-data`, and mounts the institution file at `/run/secrets/institutions.json`. It needs a separate TLS proxy. [compose.postgres.yaml](../compose.postgres.yaml) supports a separately managed database/multiple hosts.

## Eventual deployment and verification

1. Obtain the institution-enabled developer key with the exact callback above; select a host and persistent storage. For the Linux reference, install Python 3.12, Caddy, and the repository dependencies into `/opt/canvas-connector/.venv`. Create the `canvas-connector` service account and private configuration files; fill the blank encryption key.
2. Once the host, credentials, and DNS destination are ready, install the service as `/etc/systemd/system/canvas-connector.service` and merge the supplied site block into `/etc/caddy/Caddyfile`. Validate and start:

   ```sh
   sudo systemctl daemon-reload
   sudo systemctl enable --now canvas-connector
   sudo caddy validate --config /etc/caddy/Caddyfile
   sudo systemctl enable --now caddy
   sudo systemctl reload caddy
   ```

   For the container alternative, use `docker compose --env-file /etc/canvas-connector/operator.env up --build -d` instead of the Python systemd service.
3. Confirm public TLS and proxy routing. Forward the entire origin, including `/mcp`, `/authorize`, `/token`, `/register`, `/revoke`, `/connect`, `/canvas/callback`, and `/.well-known/*`. Preserve `Host: canvas.builtbyaadi.com` and restrict backend access to the proxy.
4. After deployment, probe the public origin:

   ```sh
   curl --fail-with-body https://canvas.builtbyaadi.com/health
   curl --fail-with-body https://canvas.builtbyaadi.com/connection.json
   curl --fail-with-body https://canvas.builtbyaadi.com/.well-known/oauth-authorization-server
   curl --fail-with-body https://canvas.builtbyaadi.com/.well-known/oauth-protected-resource/mcp
   curl --include https://canvas.builtbyaadi.com/mcp
   ```

   Expect health `status: ok` with `canvas_authenticated: false`; connection endpoint and metadata resource must equal the planned MCP URL, and the advertised issuer must equal `https://canvas.builtbyaadi.com/` (including its trailing slash). Unauthenticated `/mcp` must return HTTP 401 with a `WWW-Authenticate` resource-metadata link. A health response does not prove Canvas sign-in.
5. Complete real OAuth/MCP connections using two distinct test users. Verify correct account identity, read-only denial of writes, separate account data/receipts, token revocation, restart persistence, and one explicitly authorized write plus independent readback in a test course. Run the intended clients' actual sign-in flows and record results in [VALIDATION.md](VALIDATION.md).

The origin configuration, metadata and unauthenticated 401 behavior were checked in process with synthetic credentials. Caddy and systemd have not been run on a target host. Caddy preserves the public Host and manages HTTPS when DNS and the host are ready; see its [reverse-proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy) and [automatic HTTPS](https://caddyserver.com/docs/automatic-https) documentation.

DNS changes, host provisioning, secret installation, and production deployment remain pending. Publish a working hosted-install link only after these checks pass.
