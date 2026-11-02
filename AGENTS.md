# Canvas Connector

This is the public source distribution. Never copy school materials, credentials,
account exports, logs, upstream mobile-app source trees, or private workspace
history into it. The original private connector remains separate.

Use Python 3.12. Install the two local packages before this root package.
Run `.venv/bin/python -m pytest` and the protocol smoke checks after changing
the MCP contract. Test writes with synthetic fixtures, never real coursework.

For each release, match `packaging/release.json` to the root version and advance
its even UTC `source_date_epoch`; bump changed local package versions and pins.

Hosted mode requires institution-authorized Canvas OAuth. Personal tokens are
for local single-user stdio only. A request's authenticated grant selects its
Canvas client; model parameters must never select another user's credentials.

Keep structured responses bounded, preserve access/completeness/freshness,
annotate mutations accurately, and never treat a failed read as an empty result.
Deployment and successful tests do not prove host-app installation or app parity.
