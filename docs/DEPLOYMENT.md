# Run the connector

This release supports personal local stdio and an OAuth-protected hosted service. The hosted implementation uses one process and a persistent SQLite database. Container files prepare it for deployment; they do not mean a public service has been deployed. See [VALIDATION.md](VALIDATION.md) for verified checks.

## Install from source

Use Python 3.12 in a virtual environment. From the repository root:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock -e ./packages/canvas-py -e ./packages/canvas-mcp-v2 -e '.[dev]'
```

## Personal local use

Set `CANVAS_ENV_PATH` to a private file outside the repository containing your own `CANVAS_BASE_URL` and `CANVAS_API_TOKEN`, or supply those environment variables directly. Protect the file with mode `600`. Start:

```sh
canvas-connector stdio
```

Reads are the default. `canvas-connector stdio --allow-writes` enables prepared actions for your personal account; the assistant must still act within your authorization. Configure the MCP client to launch the executable in the installed environment, with the private configuration path in its environment. See [COMPATIBILITY.md](COMPATIBILITY.md) for client choices.

Manual Canvas tokens are for personal testing. They are not the hosted onboarding flow: Canvas explicitly requires multi-user applications to obtain user tokens through OAuth, and prohibits asking other users to generate and paste tokens into an application. [Canvas OAuth overview](https://developerdocs.instructure.com/services/canvas/oauth2/file.oauth)

## Hosted prerequisites

The operator needs a stable HTTPS origin, persistent storage, and a Canvas OAuth developer key enabled for each supported institution. A global developer key also requires institutions to enable it. A student's Canvas URL alone cannot grant a public application this capability. [Canvas developer keys](https://developerdocs.instructure.com/services/canvas/oauth2/file.developer_keys)

For each institution, register the connector's exact upstream callback:

```text
https://YOUR-CONNECTOR-HOST/canvas/callback
```

The institution's developer-key scopes must cover the Canvas endpoints used by the enabled tools. Use the [reviewed scope manifest](CANVAS-SCOPES.md). The connector's `canvas:read` and `canvas:write` permissions are its own permissions, not Canvas API scope strings. Restrict the institution key to the capabilities you intend to offer, and test with that key before onboarding users.

Store operator configuration outside the source checkout. The institution file is a JSON array:

```json
[
  {
    "origin": "https://school.instructure.com",
    "name": "Example School",
    "client_id": "YOUR_INSTITUTION_DEVELOPER_KEY_ID",
    "client_secret": "YOUR_INSTITUTION_DEVELOPER_KEY_SECRET"
  }
]
```

Only configured institutions are accepted. Hosted users select an institution and sign in on that institution's Canvas site; the connector does not request their Canvas password or a pasted personal token.

## Configuration

| Variable | Meaning |
| --- | --- |
| `CONNECTOR_PUBLIC_URL` | Public HTTPS origin, such as `https://canvas.example.org`. Keep it stable because OAuth metadata and callback URLs depend on it. |
| `CONNECTOR_DATABASE` | Writable path for the persistent SQLite database. The container uses `/data/connector.sqlite3`. |
| `CONNECTOR_ENCRYPTION_KEY` | Persistent Fernet key for stored credentials and OAuth records. Generate with `canvas-connector keygen`; keep it secret and back it up separately from the database. |
| `CONNECTOR_INSTITUTIONS_FILE` | Absolute path to the private JSON file above. |

Use a secret manager or a private environment file; do not commit keys, institution secrets, account data, or the database. Replacing the encryption key does not migrate existing records. Losing it prevents decryption of those records; planned key rotation needs an explicit migration.

With those variables set, run behind a TLS reverse proxy:

```sh
canvas-connector serve --host 127.0.0.1 --port 8000
```

The MCP URL is `https://YOUR-CONNECTOR-HOST/mcp`. Forward `/mcp`, the OAuth routes, and `/.well-known/*` to the same application. Preserve the exact public `Host` header: the MCP transport checks it against `CONNECTOR_PUBLIC_URL`. The shipped CLI disables proxy-header interpretation and request access logs. Configure the public origin explicitly rather than deriving it from arbitrary forwarded request headers. Restrict the backend port to the proxy.

For the bundled Docker Compose configuration, place the three operator values `CONNECTOR_PUBLIC_URL`, `CONNECTOR_ENCRYPTION_KEY`, and `CONNECTOR_INSTITUTIONS_FILE` in a private environment file outside this checkout, then run:

```sh
docker compose --env-file /ABSOLUTE/PRIVATE/PATH/connector.env up --build -d
```

The compose file binds port 8000 on loopback, mounts the institution file read-only, and persists the database in a named volume. You must supply the HTTPS reverse proxy separately. The container has not been built or run in the development environment because a Docker daemon was unavailable. Container startup and health responses alone do not establish successful Canvas authorization.

## OAuth and permissions

The connector acts as an authorization server for MCP clients and separately as an OAuth client for Canvas. MCP tokens identify grants to this connector; upstream Canvas tokens stay server-side. The request's validated grant selects the account. A tool argument must never select another user's credentials. [MCP authorization model](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)

The implementation exposes authorization-server discovery, protected-resource discovery, `/authorize`, `/token`, `/register`, and `/revoke`. It supports dynamic client registration with PKCE S256. It does not advertise CIMD support. `/connect` presents institution choice and consent, and `/canvas/callback` finishes the upstream flow.

Discovery advertises `canvas:read` and `canvas:write`; default client registration requests both so write consent is reachable. Reading is required. The write checkbox is unchecked by default, so continuing without selecting it grants only `canvas:read`. A client explicitly requesting only reads cannot receive writes. To add write access to an existing read-only connection, reconnect and opt in; refresh cannot expand a grant. Refresh and revocation are supported, with rotating MCP refresh tokens and grant-family revocation on detected refresh-token reuse.

Allow the exact redirect URI supplied by each client registration. For ChatGPT, use the redirect shown in its connection management page. Hosted Claude uses its documented callback; local coding clients use loopback redirects. Do not accept arbitrary redirect URLs or wildcard internet origins. See [COMPATIBILITY.md](COMPATIBILITY.md) for the verified client requirements.

The hosted implementation encrypts stored record payloads and hashes bearer/code lookup keys. Its default MCP access-token lifetime is one hour; its refresh/grant lifetime is 30 days. Expired or revoked access requires refresh or reconnection. Canvas access tokens are refreshed separately. These are implementation defaults, not a promise that an institution will keep access enabled for that duration.

## Capacity and operational boundaries

Run one application worker. Hosted preparations and receipts use the encrypted SQLite store, including atomic execution claims that survive restart. Prepared payloads expire after 10 minutes, and receipts remain for up to seven days. Receipts can contain the reviewed text and target information. A restart during execution makes the outcome uncertain; inspect Canvas before retrying. Personal stdio actions remain process-local and are lost on exit.

The application caps concurrent requests at 16, authorization request bodies at 64 KiB, and MCP bodies at 12 MiB. Authenticated MCP traffic is limited to 600 requests per minute per verified grant. Authorization requests use a 120-per-minute limit by direct client IP; unauthenticated MCP requests also use a direct-IP limit. Behind a reverse proxy, these IP limits are shared by clients arriving from that proxy. Add appropriate public-edge limits and measure behavior for the intended deployment.

Pending actions are limited to 32 per grant, 256 overall, and 64 MiB of staged JSON. The encrypted store has a shared 20,000-record cap for authorization and action records; capacity errors require operational attention. Continuation results use a 16 MiB, process-local cache and expire after 10 minutes. A restart can require re-reading a source even when its action receipt remains available.

Horizontal scaling still requires coordinated token refresh, shared limits and result storage, and testing of concurrent execution across instances. Those are separate implementation tasks, not solved by increasing the worker count. SQLite and encrypted storage are useful for a small single-instance deployment; they are not a claim of public-service scale testing.

Before opening a hosted instance to the public, exercise the complete OAuth and MCP path with at least two distinct test users. Verify isolation, revoked tokens, denied write scopes, rejected redirects, expired preparations, read-after-write verification, and recovery after a server restart. Use a test course for write checks. Also verify the proxy's actual public HTTPS routing and the chosen clients' sign-in flows. Record measured results in [VALIDATION.md](VALIDATION.md).

Keep operational logs free of authorization headers, tokens, OAuth codes, signed file URLs, and course payloads. Backups contain private records and need restricted access. The repository's license covers its code; it does not authorize publishing students' or instructors' content.

Directory publication and GitHub publication are separate from deployment. Once a service and listing actually exist, put their real URLs into the website's install flow; never use a placeholder endpoint as if it were live.
