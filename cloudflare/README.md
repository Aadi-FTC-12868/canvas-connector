# Cloudflare hosted beta

This is a deployable reference for the Built by Aadi account/credential-broker
mode. It has not been deployed. `HOSTED_ENABLED` and `MAIL_ENABLED` default to
`false`; a valid Worker build is not evidence of a working public service.

The public Worker runs on `canvas.builtbyaadi.com` and forwards requests to the
existing non-root Python image in Cloudflare Containers. A separate SQLite-backed
Durable Object holds encrypted records, OAuth state, revocations, action receipts
and continuation data. Container disk is never the authoritative database.
The account's Canvas token is decrypted only by the connector and sent to the
verified Canvas HTTPS origin; it is not sent to GPT, Claude or an email provider.

## Storage contract

Set `CONNECTOR_DATABASE=cloudflare://store`. The Python `CloudflareStore` reaches
the fixed virtual URL `http://canvas-store.internal/rpc`. A Container
`outboundByHost` handler resolves this privately through its Durable Object
binding. There is no public storage route, database password or Cloudflare API
token in the Container. The same pattern provides the private mail bridge.

Each outer Python transaction acquires a fenced write lease. Changes enter
staging tables; one Durable Object `transactionSync` commits the complete change
set. Nested transactions use sequence savepoints. Other transactions cannot read
staging or acquire the lock. Every operation verifies the lease, which renews up
to a 180-second absolute deadline. An idle lease expires after 60 seconds; an
expired transaction cannot reacquire itself or commit later. New work discards
expired staging. Revisions monotonically advance even when a transaction aborts,
preventing ABA errors. Lost commit responses are reconciled through a separate
durable status query; operations and commit are never automatically replayed.
Commit outcomes remain queryable for one hour.

Payloads are Fernet encrypted before leaving Python, including account/email
records. Ciphertext is stored in 128 KiB chunks, with a 32 MiB encrypted-record
limit. A record cannot reference an incomplete blob. The production reader
checks bounds and Fernet integrity before returning the decoded object. This
supports the connector's 8 MiB uploads and 16 MiB continuation records without
exceeding Durable Object's 2 MiB row limit. Blob cleanup, record replacement and
revocation are included in the final atomic commit.

This first backend deliberately serializes transactions across the beta service.
It is a correctness-first single-writer implementation, with one Container and
20,000 live-record capacity. It is not a production scalability claim. Storage
round trips, long inventories and cleanup can limit throughput; benchmark the
deployed workload before increasing public access. The existing PostgreSQL
benchmark does not measure this backend. Transactions must not contain Canvas
network calls, password hashing, email sending or document extraction.

## Account email and edge identity

The private `http://canvas-mail.internal/send` bridge accepts only
`{email, purpose, code}`; `purpose` is `verify` or `reset`. The Worker creates a
fixed plain-text account message and uses Cloudflare Email Sending. Sender is
restricted to `accounts@builtbyaadi.com`. No arbitrary message body, attachments
or recipient lists are accepted. A missing binding or disabled mail setting
fails closed; local tests substitute a fake binding and send no email.

Cloudflare must onboard `builtbyaadi.com` for Email Sending, including its sender
DNS records, before `MAIL_ENABLED=true`. Arbitrary-recipient sending and
Containers require Workers Paid. A development `send_email` binding without
remote mode is simulated by Wrangler; that is not proof of delivery. Do not
enable a remote email binding while running the synthetic test suite.

The gateway strips incoming forwarded headers, Container-control headers and
`x-canvas-edge-*`. It signs Cloudflare's `CF-Connecting-IP` plus timestamp,
method and exact URL path/query using `CONNECTOR_EDGE_SECRET`. Python validates
that signature before using the IP for request limits. This is proxy identity
only; it never substitutes for account sessions or assistant OAuth grants.

## Local verification

Use Node.js 22.13 or newer (Node 24 is recommended for CI) and Python 3.12:

```sh
npm ci --ignore-scripts --prefix cloudflare
npm test --prefix cloudflare
npm run check --prefix cloudflare
python -m pytest -q tests/test_cloudflare_store.py
```

The Python tests run actual HTTP requests against the production JavaScript
storage engine and SQLite. They cover ciphertext persistence, large chunked
records, nested rollback, concurrent consumption, CAS, revocations, leases,
expired uploads and lost responses. Worker tests also run the engine inside
actual local workerd SQLite Durable Objects, including restart persistence.
Mail and routing use synthetic bindings.

`npm run check` is a Wrangler dry run with `--containers-rollout=none`: it checks
the Worker bundle/configuration without launching Docker or deploying resources.
Build and test the root Dockerfile separately. The Miniflare development tree
pins patched `undici` and `sharp` overrides; update them with compatibility tests
when upstream dependency constraints change.

## Deployment prerequisites

1. Review the upstream Canvas token policy and the beta's terms/privacy notice.
   This credential-broker mode does not make manually collected tokens into
   Canvas-issued application OAuth grants.
2. Enable Workers Paid for the intended Cloudflare account. Do not substitute an
   outside VPS while promising Cloudflare-only credential storage.
3. Generate a persistent `CONNECTOR_ENCRYPTION_KEY` using the connector's keygen,
   and an independent ASCII `CONNECTOR_EDGE_SECRET` of at least 32 random
   characters. Install both as Worker secrets through secure operator tooling.
   Do not put their values in Git, command arguments, screenshots or logs.
4. Onboard the mail domain, confirm sender restrictions and delivery through an
   explicitly authorized test. Set `MAIL_ENABLED=true` only after that succeeds.
5. Review the new custom-domain route and create the isolated Worker/Container/
   Durable Object resources from this configuration. Existing unrelated D1
   databases and the static portfolio Worker are not reused.
6. Before enabling public signup, test restart persistence, two-account isolation,
   credential replacement, disconnect/revocation, assistant OAuth and the actual
   ChatGPT/Claude clients. Set `HOSTED_ENABLED=true` only for the reviewed launch.

The encryption key must persist across Container/Worker upgrades. Backups and
Durable Object point-in-time restoration require a recovery procedure that
preserves revocation: restoring an old database can restore old grants. Revoke
or invalidate affected grants before opening traffic after disaster recovery.

References:

- [Container binding access](https://developers.cloudflare.com/containers/configuration/workers-connections/)
- [Container outbound handlers](https://developers.cloudflare.com/containers/configuration/outbound-traffic/)
- [Durable Object SQL transactions](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/)
- [Durable Object limits](https://developers.cloudflare.com/durable-objects/platform/limits/)
- [Container persistence](https://developers.cloudflare.com/containers/faq/)
- [Email Sending API](https://developers.cloudflare.com/email-service/api/send-emails/workers-api/)
- [Email sender bindings](https://developers.cloudflare.com/email-service/configuration/send-bindings/)
- [Canvas token policy](https://developerdocs.instructure.com/services/canvas/oauth2/file.oauth)
