# Validation record

Development release 0.10.0, validated November 2, 2026 UTC.
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

- Python 3.12 check for 0.10.0: **3,279 tests passed; 13 PostgreSQL
  tests skipped** without a local PostgreSQL instance. New coverage includes
  appointments, self-profile/settings changes, native dashboard ordering,
  advanced Inbox authoring/controls/attachments, passive page reads and existing student actions. Version 0.9.0 Linux CI passed 2,908 tests,
  including all 13 PostgreSQL cases, with two native OCR cases skipped; both
  OCR tests passed locally. Two upstream warnings remain: Starlette's test-client
  httpx deprecation and MCP/Pydantic's settings forward reference.
- Real stdio MCP subprocess: initialize, tools/list, schemas, structured results,
  mutation annotations, and capabilities passed on protocol `2025-11-25`.
  The surface has **83 tools**, **83 prepared action kinds**, and **30 execution
  permission groups**. No generic write executor is exposed.
- The optional personal tunnel facade uses MCP 2.3.0 in an isolated environment;
  the core stays on MCP 1.27.0. A fresh locked install passed 32 dedicated tests,
  including both protocol lifecycles, exact tool/result forwarding, credential
  isolation, bounds, cancellation before dispatch, draining dispatched calls,
  timeout, backpressure, child death and unavailable old receipts after restart.
  It supports macOS/Linux; Windows is explicitly refused pending lifecycle work.
- The personal setup runner passed 63 offline cases and an independent native
  `--setup-only` bootstrap with the real UV and tunnel clients. Core/facade
  environments remain separate, installed adapter imports are independent of the
  source directory, paths with spaces retain literal command arguments, and
  private files/directories use 600/700 permissions. Repeating setup preserves
  the profile and credential file. No runtime key, doctor/live-run command,
  hosted registration or Canvas request was used in this bootstrap check.
- The real official tunnel client 0.0.19 passed local in-memory dev-proxy tests
  for legacy initialization and modern self-contained calls. Both exposed the
  current 83/83/30 contract. Tests verified read-only zero writes, enabled exact
  write plus terminal replay, five continuation windows reconstructed by hash,
  sanitized errors, timeout after an applied write, and process recovery with
  no replay. OS policy blocked external networking; only synthetic Canvas data
  was used. This is local transport evidence, not actual ChatGPT account access,
  hosted tunnel registration or a completed ChatGPT plugin installation.
- Inbox controls bind the current user's complete bounded view, preserve native
  subscription read-state/timestamp effects, and verify own-view deletion using
  acknowledgements plus independent state. Whole-thread deletion explicitly
  includes concurrent arrivals and never claims message recall. Legacy pending
  and completed Inbox receipts remain compatible across persistence restart.
- Inbox authoring checks native forwarding, exact recipient discovery, selective
  audiences/history, original-author copy restoration and instructor/account
  context boundaries. New-message delivery and membership verify independently;
  recipient-side historical visibility retains incomplete coverage. Actual
  upload-to-send tests use verified owner-bound receipts, multipart bytes, saved
  native folder/name/size/hash and independent downloads, with separate MCP
  upload/send permissions. No real Canvas messages or files were created.
- Appointment checks cover self/student-group participants, complete bounded
  past/future slot discovery, group/section eligibility, exact reservation-child
  cancellation, capacity and limits, preserved comments, stale preflight and
  uncertain readback. Real MCP tests verify separate calendar/appointment routes.
  A concurrent-booking case demonstrates native replace-all rescheduling: known
  deletion/new booking/final inventory can be verified while full cancelled-set
  coverage remains false. No upstream expected-count condition is claimed.
- Self-profile checks exclude calendar capabilities and administrative fields,
  preserve returned primary email, verify explicit rename permission and three
  exact names, and test native SIS behavior. Five supported boolean settings
  have independent readback and preservation checks. Real MCP tests cover scopes,
  wrong executors and replay; locale/timezone and avatars remain separate.
- Dashboard checks use the native card collection, complete requested permutation,
  merged saved positions, legacy numeric values, current-user/K5 state and exact
  readback. Native course K5 flags remain snapshot-bound without incorrectly
  deciding the user's layout. Saved order does not prove the active dashboard
  view; receipts retain that limitation. Encrypted receipt restart and older
  module-progress receipt regressions pass. No mutation retry/redirect is allowed.
- Inbox send regressions model native string recipient parameters and the distinct
  direct-group versus forced-new individual batch paths. Independent non-marking
  reads verify exact per-thread audiences, participants, current-author message,
  subject and absence of attachments/forwarded content. Group sends must produce
  one thread; individual sends must produce one new thread per recipient. Malformed
  acknowledgements, unexpected audiences and uncertain results cannot be replayed.
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
- A clean extracted 0.10.0 MCPB installed its bundled packages through UV and
  launched a real stdio session with all 83 tools, 83 actions and 30 execution
  groups, using synthetic credentials, an isolated Python 3.12 install/cache and temporary storage. The cold launch installed 37 packages and completed protocol validation in 10.598 seconds; a network guard recorded zero Canvas requests. All 30 execution-group boundaries passed with writes disabled and enabled. This
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
  has its MIT license, tracked source history, and [passing 0.9.0 Linux CI](https://github.com/Aadi-FTC-12868/canvas-connector/actions/runs/41405028567).
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
uv --no-config sync --python 3.12 --project packages/canvas-tunnel --locked
CANVAS_CORE_PYTHON="$(python -c 'import sys; print(sys.executable)')" \
  uv --no-config run --project packages/canvas-tunnel --locked --no-sync \
  pytest -c packages/canvas-tunnel/pyproject.toml packages/canvas-tunnel/tests
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
