# Contributing

Install Python 3.12, both packages under `packages/`, and the root package with
`[dev]`. Use synthetic accounts and HTTP fixtures for automated tests. Never
commit a real Canvas payload, school material, token, or OAuth secret.

Run `python -m pytest`, `python scripts/check_protocol.py`, and
`python scripts/release_check.py`. A live protocol check is read-only, opt-in,
and uses your own explicitly selected private environment file.

New capabilities should have typed inputs, accurate MCP annotations, bounded
structured results, fresh state checks for writes, and an explicit access or
partial-coverage result when unsupported. Add the Canvas endpoint scopes to
the institutional setup document. Don't introduce an arbitrary authenticated
URL proxy or ask hosted users to generate personal tokens.

Keep host-app installation claims separate from protocol tests. Publishing a
website, deploying the server, and app-directory review are separate steps.
