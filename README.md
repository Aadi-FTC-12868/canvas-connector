# Canvas Connector

A free, MIT-licensed Canvas connector for AI assistants. Navigate courses, read
assignments and documents, inspect feedback and grades, and prepare student
actions through the Model Context Protocol (MCP).

**Status: development release.** Local stdio and a hosted OAuth reference server
are included. No public service or app-directory listing is claimed. See
[validation](docs/VALIDATION.md) for what has actually been tested and
[capabilities](docs/CAPABILITIES.md) for remaining Canvas app gaps.

## Run locally

Requires Python 3.12 or later. From this repository:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock -e ./packages/canvas-py -e ./packages/canvas-mcp-v2 -e '.[dev]'
```

Put `CANVAS_BASE_URL` and `CANVAS_API_TOKEN` in a private environment file **outside
this repository**, restrict it to your user, and point `CANVAS_ENV_PATH` to it.
Use the full path to `.venv/bin/canvas-connector` as your MCP server command, with
`stdio` as its argument. Writes are disabled unless you add `--allow-writes`.
Never place a token in a chat or public configuration.

```json
{
  "mcpServers": {
    "canvas": {
      "command": "/absolute/path/canvas-connector/.venv/bin/canvas-connector",
      "args": ["stdio"],
      "env": {"CANVAS_ENV_PATH": "/absolute/private/path/canvas.env"}
    }
  }
}
```

This JSON format applies to clients accepting `mcpServers`; Codex uses its own
MCP configuration or `codex mcp add`. See the [client guide](docs/COMPATIBILITY.md).

## Host for multiple users

The server uses two separate OAuth flows: the assistant authenticates to this
connector, and each student authorizes Canvas through their school's login.
Canvas credentials are encrypted in persistent storage; assistant tokens are
separate, revocable, scoped, and never forwarded to Canvas.

You need a public HTTPS origin and a Canvas OAuth developer key enabled by each
supported institution. Asking other users for personal tokens is not the hosted
onboarding flow. Follow [deployment](docs/DEPLOYMENT.md), then connect the HTTPS
`/mcp` endpoint using the host app's custom connector setup. App directories and
one-click install URLs require their own publication processes.

```sh
canvas-connector keygen
# Save the generated key privately as CONNECTOR_ENCRYPTION_KEY.
# Configure the remaining CONNECTOR_* variables described in .env.example.
canvas-connector serve --host 127.0.0.1 --port 8000
```

The bundled Docker Compose configuration binds to loopback for a TLS reverse
proxy. This reference deployment uses one application worker and SQLite, with
encrypted durable action receipts and atomic execution claims. Multiple replicas
still require shared refresh coordination, limits, and continuation storage.

## Design

- One student navigation flow: dashboard, course, section, item, linked material.
- Structured results with access state, source/freshness, bounded content,
  warnings, and continuation information.
- Explicit read/write MCP annotations and OAuth scopes. Host clients own their
  approval UI. Prepared actions preserve exact targets and contents.
- PDF, DOCX, PPTX, HTML, CSV, and text extraction with coverage and continuation.
- Google/Microsoft links get connector-aware handoff instructions; school access
  restrictions remain in effect.
- No instructor materials, account exports, or credentials in this repository.

The companion [assistant skill](skills/canvas-assistant/SKILL.md) is optional and
portable. The connector works without a client-specific skill installation.

## Develop

```sh
.venv/bin/python -m pytest
.venv/bin/python scripts/check_protocol.py
.venv/bin/python scripts/release_check.py
```

See [security](SECURITY.md), [source provenance](docs/PROVENANCE.md), and
[contributing](CONTRIBUTING.md). Canvas is an Instructure product; this project
is independent and is not endorsed by Instructure, OpenAI, or Anthropic.
