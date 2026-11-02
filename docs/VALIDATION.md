# Validation record

Development release 0.4.0, validated November 1, 2026 (America/Chicago).
These checks do not establish complete Canvas app parity or a production service.

## Completed

- Python 3.12: **656 tests passed**, including inherited library/adapter checks,
  student actions, network security, OAuth, shared storage, and PostgreSQL tests.
  Two upstream warnings remain: Starlette's test-client httpx deprecation and an
  MCP/Pydantic settings forward-reference warning.
- Real stdio MCP subprocess: initialize, tools/list, schemas, structured results,
  mutation annotations, and capabilities passed on protocol `2025-11-25`.
  The surface has **41 tools**, **25 prepared action kinds**, and **10 execution
  permission groups**. No generic write executor is exposed.
- The earlier read-only live Canvas subprocess check authenticated the profile
  and refreshed dashboard; the dashboard response was 2,729 bytes. No live
  submissions, uploads, messages, comments, or other Canvas writes were performed.
- Synthetic hosted HTTP OAuth checks cover PKCE S256, resource/client binding,
  discovery, opt-in writes, refresh rotation, replay revocation, CSRF, redirect
  validation, duplicate headers, host/origin checks, and body/rate limits.
  Trusted-proxy tests cover spoofed forwarding headers, proxy chains, IPv6,
  disabled defaults, and preservation of the configured public Host.
- Two actual HTTP worker processes shared an encrypted SQLite database. Twenty
  concurrent account checks retained the correct user; large results and action
  receipts could be continued from the other worker; another grant was denied.
- SQLite and PostgreSQL 17 tests use spawned processes to exercise refresh,
  counter, rate, revocation, and action races. Only one upstream token refresh
  occurs; revocation prevents stale record resurrection. PostgreSQL tests also
  exercise connection recovery, transaction rollback, continuation budgets,
  execution fencing, and database waits without blocking the async event loop.
- Prepared actions cover immutable review, exact ownership, target changes,
  expiry, scope denial, wrong permission groups, intentional repeated actions,
  idempotent intent IDs, upload-versus-submit separation, persistence, renewable
  leases, cancellation races, stale workers, and uncertain outcomes. New
  discussion/comment/calendar actions use both stateful fixtures and the actual
  Canvas HTTP client with a mock transport. Personal calendar restrictions and
  attachment-preserving refusal are tested.
- Public-only network protection checks URL validation, private/mixed DNS
  rejection, pinned addresses, upload client injection, and same-origin upload
  rebinding. Secret-bearing HTML links are removed from output.
- Linux container build and run passed with two workers, UID 10001, a read-only
  root filesystem, dropped capabilities, and no external network. Health and OAuth
  discovery returned successfully; unauthenticated MCP access returned 401.
  The smoke test used synthetic configuration and cleaned up its temporary image.
- Claude Desktop MCPB validates with the official MCPB tooling. Portable plugin
  manifests validate against the official Agent Plugins schemas. Claude Code
  validates the plugin/catalog; Codex discovers the local marketplace without
  changing the user's configuration. Clean extracted packages bootstrapped their
  bundled Python packages and ran a real MCP session with synthetic credentials.
- Source releases use an allowlist, SHA-256 manifests, safe archive paths, and a
  private-path/school-marker scan. A known private-token scan found zero matches
  without printing or copying that token. Clean source extraction installed,
  passed dependency/protocol checks, and ran the tests.
- Browser checks cover assistant tabs, keyboard navigation, endpoint validation,
  copy behavior, download visibility/HTTP responses, and 390px/1440px layouts.
  No horizontal overflow or console errors remained. The Built by Aadi Next.js
  site built successfully with the setup page and project link.
- Focused static checks, Python compilation, wheel packaging, and `pip check`
  passed. CI reproduces tests with PostgreSQL and builds the source, extension,
  plugin archives, and Docker image.

## Still requires live verification

- Institution OAuth sign-in needs an enabled Canvas developer key and public
  callback URL. Hosted sign-in has only been tested with synthetic accounts.
- The install guides and packages follow official host documentation. Actual
  installation and Canvas authorization in every ChatGPT, Claude Desktop/web/
  Cowork, Codex, and Claude Code client have not all been completed. Schema or
  CLI validation is not evidence of successful host-app installation.
- No public MCP service, provider app-directory listing, GitHub repository,
  production capacity benchmark, real multi-host deployment, or independent
  security audit has been completed. A downloadable setup page is separate from
  an authenticated hosted service.
- Canvas writes have synthetic coverage only. Exercise institution-specific
  permissions, uploads, submissions, and readback with an authorized test course
  before opening hosted write access.

## Reproduce

```sh
python -m pytest -q
python scripts/check_protocol.py
python scripts/release_check.py --build
python scripts/build_mcpb.py
python scripts/build_plugins.py
python -m pip check
docker build -t canvas-connector:verify .
python scripts/check_container.py
```

Set `CONNECTOR_TEST_POSTGRES_URL` to a disposable private PostgreSQL database to
include the optional integration tests. They create isolated temporary schemas
and remove them afterward; never point the tests at a production database.

For the optional read-only live check, select your private credential file with
`CANVAS_ENV_PATH`, then run `python scripts/check_protocol.py --live`. Never save
a real account payload or credential as a test fixture.
