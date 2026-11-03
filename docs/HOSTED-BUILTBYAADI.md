# Built by Aadi hosted beta

**Status: implementation and local verification; public hosting is not enabled.**
The public download page remains available separately. This design replaces the
previous proposed Linux VM with a Cloudflare-only runtime and durable store.

## Account and connection flow

1. Create a Built by Aadi account with email and password, then verify the email.
   Existing accounts sign in. No Canvas password is requested.
2. Immediately above Canvas setup, show the [beta notice](BETA-NOTICE.md) and a
   prominent account-free [local setup](https://builtbyaadi.com/canvas-connector/local.html)
   recommendation with a copyable agent prompt linking to this repository.
3. Explain how to create a personal Canvas access token only when the school
   enables that control. Enter the Canvas HTTPS origin and token into the
   authenticated setup form. The server verifies the profile before saving it.
4. GPT, Claude or another MCP client starts OAuth with Built by Aadi. Consent
   shows the assistant, callback host, connected Canvas identity and requested
   permissions. Writes require an unchecked-by-default opt-in.
5. The assistant receives a separate connector token. The server resolves its
   grant to the account's exact Canvas connection and credential version.
   Replacing or disconnecting the credential revokes associated assistant grants.

This is Built by Aadi OAuth, not Canvas OAuth. Canvas's published documentation
explicitly prohibits multi-user applications asking users to generate and paste
personal tokens and requires obtaining Canvas tokens through its OAuth flow.
The beta implementation, open-source license and risk notice do not resolve
that policy issue or establish school approval.
[Canvas OAuth documentation](https://developerdocs.instructure.com/services/canvas/oauth2/file.oauth)

## Cloudflare architecture

The static portfolio stays on `builtbyaadi.com`. A separate Worker on
`canvas.builtbyaadi.com` forwards requests to a private Python Container managed
by a Durable Object. The existing connector runs inside that Container.

Account records, encrypted Canvas credentials, OAuth state, action receipts,
continuations, revocations and leases use a private SQLite-backed Durable Object
store. The Container accesses it through a fixed outbound handler; there is no
public storage endpoint or embedded Cloudflare API token. Container disk is
ephemeral and is not the source of truth.

The store stages multi-operation transactions under a short fenced lease and
commits them atomically. Nested rollback, compare-and-swap revisions, revocation
tombstones and execution leases remain part of its contract. Large encrypted
records are chunked below per-row limits. This initial backend serializes writes;
production capacity must be measured rather than inferred from synthetic tests.
See [Cloudflare runtime configuration](../cloudflare/README.md).

The gateway signs the observed client IP, timestamp, method and request target
with a separate private key. The Python server verifies that signature before
using the IP for limits. User-supplied forwarding headers cannot select an
identity. Gateway metadata never substitutes for a browser session or MCP grant.

The token is stored encrypted in Built by Aadi's Cloudflare infrastructure and
sent to the selected Canvas server to authenticate requests. It is not supplied
to GPT or Claude. Requested Canvas content is returned to the assistant. Do not
promise that the token literally never leaves Cloudflare or that encryption makes
application compromise impossible.

## Deployment configuration

| Setting | Value or source |
| --- | --- |
| Public origin | `https://canvas.builtbyaadi.com` |
| Advertised issuer | `https://canvas.builtbyaadi.com/` |
| MCP endpoint | `https://canvas.builtbyaadi.com/mcp` |
| `CONNECTOR_AUTH_MODE` | `builtbyaadi` |
| `CONNECTOR_DATABASE` | `cloudflare://store` |
| `CONNECTOR_ENCRYPTION_KEY` | Separate persistent Cloudflare secret; generated privately |
| `CONNECTOR_EDGE_SECRET` | Independent random gateway secret, at least 32 characters |
| `CONNECTOR_ACCOUNT_MAIL` | `cloudflare` only when the private email binding is configured |
| `HOSTED_ENABLED` | Disabled until deployment and access checks pass |
| `MAIL_ENABLED` | Disabled until sender/domain onboarding and delivery checks pass |

The institution configuration file is not used in this mode. The existing
institution OAuth mode remains available separately through
[DEPLOYMENT.md](DEPLOYMENT.md); its Linux/Caddy/Compose templates are not this
Cloudflare deployment.

The Cloudflare Email Sending binding sends fixed verification and recovery
messages from the configured sender. Signup and email recovery fail closed
without delivery configuration. Real delivery must be verified separately from
synthetic mailer tests. No verification codes, reset codes or tokens belong in
logs, URLs, source files or public browser responses.

## Remaining launch work

The connected Cloudflare account currently reports that Containers requires a
Workers Paid plan. No plan upgrade, new Container deployment or public token
collection was performed. Deploying also requires secret installation, email
sender/domain setup and review of the unresolved Canvas policy issue.

Before enabling the public flow, verify the actual deployed service: distinct
accounts and credentials, restart persistence, verified-email delivery, password
recovery, token replacement, disconnect, OAuth refresh/revocation, and account
deletion. Run real assistant sign-ins separately; a health response or protocol
test does not establish a successful ChatGPT or Claude connection.

Only then publish reviewed hosted configuration. `scripts/export_website.py`
preserves an existing validated hosting choice, accepts `--hosted-config` for an
explicit public configuration file, and supports `--disable-hosted`. It rejects
unknown fields and credential-bearing endpoint URLs. Never pass a secrets file.

## Sources

- [Cloudflare Containers](https://developers.cloudflare.com/containers/)
- [Container storage lifetime](https://developers.cloudflare.com/containers/faq/)
- [Private Worker binding access](https://developers.cloudflare.com/containers/configuration/workers-connections/)
- [Durable Object storage](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/)
- [Email Sending binding](https://developers.cloudflare.com/email-service/api/send-emails/workers-api/)
- [OpenAI connector authentication](https://developers.openai.com/plugins/build/auth)
