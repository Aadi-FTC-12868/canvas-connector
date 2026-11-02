# Validation record

Development release 0.8.1, validated November 2, 2026 UTC.
Historical installation, container and benchmark evidence below is identified by release; those checks are not implied to have been repeated for every new capability.
These checks do not establish complete Canvas app parity or a production service.

## Completed

- The `canvas.builtbyaadi.com` origin, metadata and unauthenticated MCP guard
  were checked in process using synthetic credentials. Linux/systemd/Caddy
  configuration is prepared; no DNS, host or service deployment was performed.
  See the [pending hosting plan](HOSTED-BUILTBYAADI.md). Eight issuer regression
  cases verify that successful and denied authorization responses exactly match
  discovery metadata, with or without a trailing slash in operator configuration.
  Endpoint construction and CSRF origin checks remain separately verified.

- Python 3.12 local check for 0.8.1: **2,511 tests passed; 13 PostgreSQL
  tests skipped** without a local PostgreSQL instance. New coverage includes
  Mastery Paths, self group membership, discussion attachment bytes, feedback
  markers, passive page reads and Horizon read safety. Version 0.8.0 Linux CI passed 2,394 tests,
  including all 13 PostgreSQL cases, with two native OCR cases skipped; both
  OCR tests passed locally. Two upstream warnings remain: Starlette's test-client
  httpx deprecation and MCP/Pydantic's settings forward reference.
- Real stdio MCP subprocess: initialize, tools/list, schemas, structured results,
  mutation annotations, and capabilities passed on protocol `2025-11-25`.
  The surface has **71 tools**, **69 prepared action kinds**, and **24 execution
  permission groups**. No generic write executor is exposed.
- Horizon regressions model single-item SHOW completing a must-view requirement.
  Public item opening, URL routes, preparation, preflight and readback never use
  SHOW. Collection lookup is bounded, closes pagination, pins its path and refuses
  redirects or ambiguous Link headers. Missing records are distinguished from
  incomplete/denied discovery. Old raw snapshots and persisted done/undo/read
  receipts remain compatible; ignored writes cannot be falsely verified by a GET.
- Page-read regressions cover course home, direct pages, module-linked pages,
  course/group URL routes and group membership. They model SHOW and body-list
  progress side effects and verify those requests are never dispatched. Latest
  saved HTML requires exact metadata, matching revision and fresh locks/access;
  nested results retain partial coverage. Pagination rejects body includes,
  changed ordering/page size, skipped pages and redirects before dispatch.
  Horizon/block content, denied metadata, null body and stale revision return
  unavailable; imported pages may legitimately omit author identity.
- Mastery Path checks bind exact offered assignment sets/associations to the own
  student, perform one selection and independently verify it. Assignment release
  can remain incomplete; coursework completion is not inferred. Membership checks
  cover nonmember discovery, pending/accepted states, category-switch prevention,
  leadership review, exact self readback and no automatic mutation replay.
- Discussion attachments exercise actual multipart encoding, immutable bytes,
  course/group audience, entry identity/parent, filename normalization, self-file
  metadata, bounded download hashes and a final attachment association read.
  Mutations do not follow redirects or retry on rate-limit/network errors.
- Feedback checks use a fixed GraphQL read without REST's implicit read marking.
  Aggregate read updates explicitly cover grade/comment/rubric participation;
  failures at each of the three steps stop without replay. Comment marking covers
  all visible attempts. Rubric-assessment and annotation preference flags remain
  independent; readback, identity, feedback versions and returned grades are checked.
- Group action checks preserve legacy course payloads, fingerprints and snapshots;
  twelve persisted course action variants execute and replay after a database
  restart with exactly one upstream write. Group membership, author/parent identity,
  initial-post gates and entry permissions are checked independently.
- Invitation checks cover future-course invitations, previously declined acceptance,
  current-user ownership, exact enrollment state, stale preparation and uncertain
  readback without retry. Bookmark checks cover individual ordering, neighboring
  positions, the 1,000-record verification bound and external links without fetching.
- What-if checks verify hypothetical scores separately from unchanged actual grades.
  A course reset's independent coverage is explicitly limited to visible submissions;
  incomplete coverage survives the MCP envelope. Hidden/unpublished rows are not
  claimed as independently verified. Canvas aggregate totals are response-sourced,
  not locally recomputed.
- The 0.6.1 read-only live Canvas subprocess check authenticated the profile
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
  attachment-preserving refusal are tested. Comment attachments check exact
  uploaded bytes, context, ownership, and saved attachment identity. Personal-file management checks owned ancestry, protected folders,
  empty-folder deletion, stale targets and readback. Group reads and new-topic
  creation check membership, initial-post gates, permissions and author identity.
  Preferences check exact channel/context ownership and effective saved values.
  Module progress checks require a matching requirement, known access state, and exact
  completion readback. API field availability was checked against Canvas source.
- Public-only network protection checks URL validation, private/mixed DNS
  rejection, pinned addresses, upload client injection, and same-origin upload
  rebinding. Secret-bearing HTML links are removed from output.
- Version 0.5.0 Linux container build and run passed with two workers, UID 10001, a read-only
  root filesystem, dropped capabilities, and no external network. Health and OAuth
  discovery returned successfully; unauthenticated MCP access returned 401.
  The smoke test used synthetic configuration and cleaned up its temporary image.
- Version 0.5.0 Claude Desktop MCPB validated with the official MCPB tooling. Portable plugin
  manifests validate against the official Agent Plugins schemas. Claude Code
  validates the plugin/catalog. Both Codex and Claude Code installed the
  marketplace/plugin into isolated temporary settings without changing normal
  user settings. A fresh UV launch from the installed cache ran a real MCP
  session with synthetic credentials. Both CLIs then installed from the public
  HTTPS GitHub URL without Git credentials and launched their own installed
  copies successfully with 42 tools, 27 actions, and 11 permission groups.
  Clean extracted packages also bootstrapped
  their bundled Python packages and ran a real MCP session.
- A clean extracted 0.8.1 MCPB installed its bundled packages through UV and
  launched a real stdio session with all 71 tools, 69 actions and 24 execution
  groups, using synthetic credentials, an isolated Python 3.12 install/cache and temporary storage. The cold launch installed 37 dependencies and completed protocol validation in 7.31 seconds; a network guard recorded zero Canvas requests. This
  validates the packaged runtime, separately from native client installation.
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

- The public [source repository](https://github.com/Aadi-FTC-12868/canvas-connector)
  has its MIT license, tracked source history, and [passing 0.8.0 Linux CI](https://github.com/Aadi-FTC-12868/canvas-connector/actions/runs/41398653457).
  The website serves the reviewed source and install packages.
- A reproducible [synthetic workload](BENCHMARKS.md) checked 1,000 authenticated
  profile requests across 64 accounts with one and two SQLite workers. Both runs
  returned the correct identity without errors; throughput did not improve with
  two workers. The same script supports PostgreSQL and runs in CI.

## Still requires live verification

- Institution OAuth sign-in needs an enabled Canvas developer key and public
  callback URL. Hosted sign-in has only been tested with synthetic accounts.
- The install guides and packages follow official host documentation. Actual
  installation and Canvas authorization in every ChatGPT, Claude Desktop/web/
  Cowork, Codex, and Claude Code client have not all been completed. Schema or
  CLI validation is not evidence of successful host-app installation.
- No public MCP service or provider app-directory listing,
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
