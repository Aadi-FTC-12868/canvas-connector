# Validation record

Validated November 1, 2026, in the local development environment. This is evidence
for a development release, not a claim of public deployment or complete Canvas
app parity.

## Completed

- Python 3.12: **510 tests passed**, including the inherited library/adapter
  regression suite and 69 new action, response, OAuth, isolation, and network
  security tests. Two upstream warnings remain: Starlette's test-client httpx
  deprecation and an MCP/Pydantic settings forward-reference warning.
- Real stdio MCP subprocess: initialize, tools/list, typed schemas, structured
  results, mutation annotations, and capabilities passed on protocol
  `2025-11-25`. The public surface has **38 tools**, seven separate execution
  permission groups, and 14 prepared action kinds. No generic write executor
  is exposed.
- Read-only live Canvas smoke check through that subprocess: authenticated
  profile and refreshed dashboard succeeded. The structured dashboard response
  was 2,729 bytes. No submissions, uploads, messages, comments, or other live
  writes were performed.
- Hosted HTTP integration with synthetic accounts: two-layer OAuth, S256 PKCE,
  resource/client binding, discovery, opt-in write access, refresh rotation,
  replay detection, revocation, CSRF, duplicate-header rejection, origin/host
  checks, and body/rate limits passed. Real MCP initialize/tools/list/tool calls
  ran through the application, not just Python handler calls.
- Concurrent synthetic users reached separate worker threads with the correct
  account credentials. Expired Canvas tokens refreshed once under concurrent
  access; revocation during refresh could not restore access.
- Prepared actions: target changes, expiry, file identity, upload-versus-submit
  separation, denied scopes, wrong execution groups, cross-account access,
  duplicate execution, encrypted persistence, restart recovery, consumed claims,
  and uncertain outcomes were tested with synthetic upstream responses.
- Public-only network guard: URL validation, private/mixed DNS rejection, IP
  pinning, anonymous upload client injection, and same-origin upload rebinding
  rejection passed. Secret-bearing HTML URLs are removed from tool output.
- Source package: allowlisted files, safe archive members, SHA-256 manifest,
  private-path/school-marker scan, and known private-token scan passed. The
  known-token scan reported zero matches without copying or printing the token.
- Clean source extraction into a separate Python environment installed and
  passed dependency checks, the real MCP protocol probe, and the test suite.
- Browser: setup-page assistant tabs, keyboard navigation, address validation,
  clipboard copy, and 390px/1440px layouts passed. No horizontal overflow or
  browser console errors remained. The existing Built by Aadi Next.js site
  built successfully with the setup page and project link.
- Python compilation, focused static checks, and `pip check` passed. The package
  builds as a wheel and includes the setup-page assets.

## Not yet verified

- Live institution OAuth sign-in requires an enabled Canvas developer key and
  public callback URL. Only synthetic hosted sign-in has been exercised.
- ChatGPT, Claude Desktop/web/Cowork, Codex, and Claude Code setup guides are
  grounded in official documentation. A real in-app connection to this new
  hosted server has not been completed in each client.
- Docker files are supplied; the local Docker daemon was unavailable, so a
  container build/run was not verified. The Python server itself was tested.
- No public hosting, DNS change, app-directory submission, GitHub publication,
  production load test, multi-replica deployment, or independent security audit
  has been completed.
- Canvas writes are covered by synthetic tests. Verify institution-specific
  write behavior in a test course before enabling public write access.
- This deployment remains one worker. Hosted action receipts are durable, but
  refresh locks, request limits, and result continuations are local. Identical
  actions are deduplicated for the seven-day retention period; intentional
  identical repeats require a future explicit repeat-intent mechanism.

## Reproduce

```sh
python -m pytest -q
python scripts/check_protocol.py
python scripts/release_check.py --build
python -m pip check
```

For the optional read-only live check, privately select your own credential file
with `CANVAS_ENV_PATH`, then run `python scripts/check_protocol.py --live`.
Never save a real account payload or credential as a test fixture.
