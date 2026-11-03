# Canvas Connector

This is the public source distribution. Never copy school materials, credentials,
account exports, logs, upstream mobile-app source trees, or private workspace
history into it. The original private connector remains separate.

Use Python 3.12. Install the two local packages before this root package.
Run `.venv/bin/python -m pytest` and the protocol smoke checks after changing
the MCP contract. Test writes with synthetic fixtures, never real coursework.

For each release, match `packaging/release.json` to the root version and advance
its even UTC `source_date_epoch`; bump changed local package versions and pins.

Institution mode uses institution-authorized Canvas OAuth. The explicitly
requested Built by Aadi beta uses verified email/password accounts and an
encrypted per-account Canvas credential vault in Cloudflare. Keep these modes
distinct. Canvas's published policy disallows collecting manually generated
tokens in multi-user applications; document that unresolved launch constraint.
A request's authenticated grant selects its Canvas client; model parameters
must never select another user's credentials. Never claim a beta disclaimer
removes legal duties or makes a hosted token flow institution-approved.

Personal hosted mode is explicitly for one operator's own Canvas identity.
Provision its immutable owner through the operator command; never enable public
signup, first-visitor claiming, or an environment-driven password reset. Keep
separate revocable assistant grants, and keep deployment/account purchases
pending explicit authorization. The Cloudflare-only beta remains distinct from
the personal mode's selectable storage backends.

Keep structured responses bounded, preserve access/completeness/freshness,
annotate mutations accurately, and never treat a failed read as an empty result.
Deployment and successful tests do not prove host-app installation or app parity.
