# Security and privacy

Canvas Connector handles student records and credentials. Its security boundary is the authenticated account, not a course ID supplied by a model. Hosted access uses separate MCP and Canvas OAuth grants; local personal use can use a private Canvas token. See [deployment](docs/DEPLOYMENT.md) and [validation](docs/VALIDATION.md) for configuration and completed tests.

## Implemented controls

- Each hosted request resolves its Canvas account from a verified MCP bearer grant. Tool arguments cannot provide an alternative token or institution.
- Institutions are configured by the operator. Hosted egress requires public HTTPS destinations on port 443 and validates DNS results at connection time, rejecting private and mixed public/private address sets. The original hostname remains the TLS identity.
- MCP authorization uses PKCE, registered redirect checks, browser-bound consent state, expiring single-use codes, rotating refresh tokens, and revocation. Detected refresh-token reuse revokes the grant family. Upstream Canvas tokens are kept server-side.
- The SQLite store encrypts record payloads with a separately supplied key; opaque lookup keys are hashed. Possession of the running process and its encryption key still permits access to its credentials.
- Request sizes, concurrent requests, authorization storage, and action storage are bounded. The shipped CLI disables request access logs so OAuth codes do not enter ordinary access-log lines.
- Write execution requires an explicitly granted write scope; the consent checkbox is unchecked by default. Actions preserve a reviewed payload, bind it to an account, recheck targets, and return a verification result. Hosted execution claims and receipts persist in the encrypted store. Course content is untrusted input and cannot establish consent.

These controls are not a claim of an independent security audit, complete threat coverage, regulatory certification, or production load testing. Record any real deployment review separately.

## Operator responsibilities

Keep institution secrets, the encryption key, backups, the database, and private environment files outside source control. Protect the public origin with HTTPS and configure the reverse proxy to avoid logging tokens, codes, signed download links, or course payloads. Keep the backend port private. An institution administrator controls whether its Canvas developer key and API scopes are available.

Run one application worker. Hosted action persistence supports restart recovery, but token refresh, request limits, and result continuation coordination remain local. This version is not validated for multiple replicas. Prepared payloads expire after 10 minutes; encrypted receipts, including reviewed text and target information, remain for up to seven days. Local stdio actions have no durable receipt store. Follow the public-deployment checks in [DEPLOYMENT.md](docs/DEPLOYMENT.md).

The application does not turn off an AI client's approvals. A server write grant permits execution technically; the assistant must still act within the user's exact authorization. A prompt-injection instruction in an assignment, document, or message must never authorize a write.

## Report a vulnerability

Use the repository's private vulnerability-reporting channel if the published repository has enabled it. Otherwise contact the maintainer through an established private channel before disclosing an exploit. This source distribution does not invent a security email address or promise a response deadline.

Include the affected version, a minimal reproduction using test accounts, expected and observed behavior, and the impact. Never include real Canvas tokens, student records, instructor documents, or signed URLs. Do not test against other users' accounts or perform destructive actions to demonstrate a problem.

## If credentials may be exposed

Revoke the affected connection and Canvas authorization, rotate the relevant institution secret or storage key as appropriate, and investigate the scope with private logs and test accounts. Storage-key replacement requires a migration or deliberate invalidation of existing encrypted records; changing an environment variable alone does not re-encrypt a database. Check Canvas for uncertain writes before attempting them again.
