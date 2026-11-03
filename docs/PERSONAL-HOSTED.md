# Personal hosted Canvas MCP

This unreleased mode serves **one operator's own Canvas identity** to multiple
assistant apps over a stable HTTPS `/mcp` endpoint. Each app receives a separate
OAuth grant. There is no public signup, email verification, password-reset email,
or first-visitor account claim. The operator creates the sole account from a
private terminal before the service starts.

## Hosting choice

For frequent personal calls, start with one always-on Linux host and encrypted
SQLite on its local disk. This avoids the beta's network round trips for each
storage operation. The reference configuration uses one worker and one database;
it is not a multi-host service. Keep the database and encryption-key backups
private and separately recoverable. The provider storing your server also stores
your encrypted Canvas credential; using Cloudflare DNS does not make a VPS a
Cloudflare credential store.

As checked November 2, 2026, DigitalOcean's regular Basic instances cost $6/month
for 1 GiB or $12/month for 2 GiB, before tax and optional extras such as backups.
The 2 GiB option provides more headroom for Python and document parsing. These
are pricing references, not purchases or measured capacity guarantees.
[Official pricing](https://www.digitalocean.com/pricing/droplets)

The Cloudflare store remains supported by the personal provider, with signed
private-gateway traffic required in environment-based startup. The existing
Cloudflare Worker configuration still launches the separate multi-user beta;
this Linux guide does not deploy or automatically convert that Worker. Cloudflare
operation needs its own reviewed owner-bootstrap and runtime deployment.

## Provision the owner

Install the source as described in [DEPLOYMENT.md](DEPLOYMENT.md). Set these
variables using private operator tooling or a mode-600 environment file outside
Git. Keep the encryption key stable across restarts:

```text
CONNECTOR_AUTH_MODE=personal
CONNECTOR_PUBLIC_URL=https://canvas.example.org
CONNECTOR_DATABASE=/private/state/personal.sqlite3
CONNECTOR_ENCRYPTION_KEY=YOUR_PERSISTENT_FERNET_KEY
CONNECTOR_TRUSTED_PROXIES=127.0.0.1,::1
```

Generate the key with `canvas-connector keygen` in a private terminal and store
it directly in your secret configuration. Never paste it in chat or publish it.
The trusted-proxy values above are correct only for a native reverse proxy
connecting over loopback on the same host.

Then run, in an interactive private terminal with that environment loaded:

```sh
canvas-connector owner-init
```

Enter the owner's email identifier and a password of at least 12 characters.
Password entry is hidden; no email is sent. Initialization requires an empty
store and cannot overwrite an existing account. The email is a sign-in identifier,
not an assertion that an email verification occurred. The server refuses to start
without consistent owner metadata.

Start one process behind HTTPS:

```sh
canvas-connector serve --host 127.0.0.1 --port 8000 --workers 1
```

Personal CLI startup rejects multiple workers because all app grants must share
the same process's upstream concurrency gate. Do not bypass that invariant by
launching additional Uvicorn processes or replicas.

Open `https://canvas.example.org/setup`, sign in, and enter your own Canvas URL
and token privately. Setup performs one bounded read of your Canvas profile.
The first successful setup pins the Canvas origin and user ID. Later token
rotation must identify that same account. Saving a replacement revokes existing
assistant grants; reconnect the apps afterward. Removing the credential preserves
the identity pin and fences in-flight setup requests. Changing to another Canvas
identity requires a separately provisioned deployment.

## Linux Docker reference

`compose.personal.yaml` uses Linux host networking, binds the application to
loopback, persists `/data`, and runs as the image's non-root user. It is not a
Docker Desktop networking recipe. The native Caddy reference is
`packaging/hosted/personal.Caddyfile`; replace its example domain with the same
origin used in `CONNECTOR_PUBLIC_URL`, arrange DNS, and validate Caddy before
starting it. Do not expose port 8000 or turn on request access logging.

The private Compose environment file needs `CONNECTOR_PUBLIC_URL` and
`CONNECTOR_ENCRYPTION_KEY`. Compose supplies personal mode, the persistent SQLite
path, and loopback proxy trust:

```sh
docker compose --env-file /PRIVATE/operator.env -f compose.personal.yaml build
docker compose --env-file /PRIVATE/operator.env -f compose.personal.yaml run --rm connector canvas-connector owner-init
docker compose --env-file /PRIVATE/operator.env -f compose.personal.yaml up -d
```

Do not run `docker compose config` into logs: it can render resolved secrets.
Enable host updates and protect operator SSH access. Back up a consistent database
while the service is stopped, or use SQLite's supported online backup mechanism;
do not copy a changing database file and assume the copy is consistent. Restore
tests and public TLS/client verification remain deployment work.

## Connect assistant apps

Use the same `https://canvas.example.org/mcp` URL in each app, then complete the
owner sign-in and that app's consent flow. Read access is the default. Select
write access only for apps you intend to authorize for prepared Canvas actions.
The tool-specific host approvals and your actual instructions still apply.

- **Claude / Duet:** add a custom remote connector in Claude's connector settings.
  Use **Register automatically** when choosing the OAuth client: this server
  supports dynamic registration, not published-identity/CIMD registration.
  Duet uses connectors from the Claude account; verify an actual Duet read after
  connecting. [Claude custom connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)
- **ChatGPT:** add a custom MCP server using the HTTPS URL and OAuth, subject to
  the account/workspace's custom-connector access. Verify a read in ChatGPT;
  server tests alone do not establish installation.
- **Codex / Claude Code:** configure a remote HTTP MCP server and complete its
  OAuth login. See [COMPATIBILITY.md](COMPATIBILITY.md).

The endpoint must be reachable by the relevant assistant service. Owner
authentication controls access; an obscure URL is not the access control. A
separate login wall in front of every OAuth/MCP request can prevent assistant
clients from completing discovery and authentication.

The owner dashboard lists assistant connections and supports revoking one app
without revoking the others. Grants expire after 30 days, with one-hour access
tokens and rotating refresh tokens within that grant lifetime. Reconnect after
grant expiry. Disconnecting all apps, credential removal/replacement, or operator
password recovery invalidates the corresponding grants and their stored actions
and continuations.

## Frequent tool calls

There is no paid per-tool-call meter built into this connector. Hosting resources,
bandwidth, assistant plan limits, and Canvas's own request quota still apply.
One MCP call may make several Canvas requests for pagination or navigation.
All your apps use the same Canvas credential, so adding app connections does not
create new upstream quota. Canvas penalizes parallel requests and exposes cost
and remaining-quota headers. [Canvas throttling](https://developerdocs.instructure.com/services/canvas/basics/file.throttling)

The initial personal settings are:

- 16 concurrent MCP HTTP requests in the one application worker.
- At most 4 simultaneous upstream HTTP requests shared by the app grants,
  including response bodies and anonymous file transfers; excess calls wait up
  to 30 seconds before a bounded error. This is admission control, not an
  assurance Canvas will never throttle.
- 600 authenticated MCP HTTP requests per minute per grant, including protocol
  requests. This is not a daily or monthly tool-call allowance.
- Eligible reads retain bounded throttle retries. Writes are never automatically
  replayed. Existing grant-specific action receipts and result continuations
  remain isolated.

Authorization is rechecked rather than cached. Navigation state remains scoped
to each HTTP request; this first version does not claim cross-call result caching
or shared TCP pools. This preserves freshness and avoids using a cache after
credential rotation or an authorized write. Measure the actual workload before
adding those optimizations.

Run `python scripts/benchmark_personal.py --requests 1000 --output
/tmp/personal-benchmark.json` for a local synthetic workload with one owner and
five app grants. It exercises real loopback MCP, paginated reads, continuation
reconstruction, write denial, throttling, and revocation. It cannot establish
live Canvas speed or the capacity of a purchased VPS. Record the tested commit,
hardware, concurrency, upstream amplification, latency, errors, and memory when
comparing runs.

## Recovery and status

From a private operator terminal with the original database and encryption key:

```sh
canvas-connector owner-reset-password
```

This replaces the owner password and revokes existing browser sessions and app
grants. It does not expose the old password or change the Canvas identity. It
requires operator control and is not an HTTP password-reset endpoint.

Configuration and synthetic tests do not establish a deployed service. Before
relying on it, verify public HTTPS/OAuth discovery, a real authorized account read
in each intended app, restart persistence, and individual app revocation. No
real Canvas writes are required for initial setup.
