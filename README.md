# Canvas Connector

A free, MIT-licensed Canvas connector for AI assistants. Navigate courses, read
assignments and documents, inspect feedback and grades, and prepare student
actions through the Model Context Protocol (MCP). This independent project is
not affiliated with or endorsed by Instructure or Canvas.

**Status: development release.** Local stdio and a hosted OAuth reference server
are included. No public service or app-directory listing is claimed. See
[validation](docs/VALIDATION.md) for what has actually been tested and
[capabilities](docs/CAPABILITIES.md) for remaining Canvas app gaps.

Source and issues: [Aadi-FTC-12868/canvas-connector](https://github.com/Aadi-FTC-12868/canvas-connector).
Setup and downloads: [Built by Aadi](https://builtbyaadi.com/canvas-connector/).

## Run locally

Requires Python 3.12 or later. From this repository:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock -e ./packages/canvas-py -e ./packages/canvas-mcp-v2 -e '.[dev,postgres]'
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

## Personal hosted connection for multiple assistant apps

The unreleased personal mode serves one owner's Canvas account over OAuth-protected
Streamable HTTP. Each app has its own revocable grant. It has no public signup or
email dependency, and accepts the operator's own Canvas URL and token after a
private owner login. See [personal hosting](docs/PERSONAL-HOSTED.md) for provisioning,
deployment, supported client setup, and frequent-call limits. Hosting remains an
operator task; this repository is not itself a running service.

## Personal ChatGPT connection

If your OpenAI account can create/use Secure MCP Tunnels and add custom MCP
servers in ChatGPT, run the optional guided setup from this source checkout:

```sh
python3 scripts/start_personal_tunnel.py --profile canvas-personal
```

Install UV and the official tunnel client first. The runner supports macOS/Linux,
keeps credentials in private local input/configuration, defaults writes off, and
runs the isolated compatibility adapter. Your computer must remain running.
Use `--setup-only` to prepare locally without a runtime key or an active tunnel.
Keep the source folder in place for later runs; unchanged runner-owned profiles
are reused, and the OpenAI runtime key is never saved.
Keep tunnel access limited to you: it uses one Canvas identity. No public Canvas
OAuth service is needed for this personal route. Account permissions and actual
ChatGPT sign-in are separate from the tested local transport. See the
[personal ChatGPT guide](docs/PERSONAL-CHATGPT.md).

## Install from GitHub

Install UV and your assistant's command-line app first. For Codex:

```sh
codex plugin marketplace add https://github.com/Aadi-FTC-12868/canvas-connector.git
codex plugin add canvas-connector-personal@built-by-aadi
```

For Claude Code:

```sh
claude plugin marketplace add https://github.com/Aadi-FTC-12868/canvas-connector.git
claude plugin install canvas-connector-personal@built-by-aadi
```

Then follow the setup skill for your own account. See the
[client guide](docs/COMPATIBILITY.md) for secure configuration and desktop options.
A plugin installation does not establish Canvas authorization.

## Install packages

The release includes a personal Claude Desktop `.mcpb` extension and a local
plugin marketplace for Codex and Claude Code. These bundle the connector and
companion skill; their README and the [client guide](docs/COMPATIBILITY.md) explain
setup. Downloads do not install a hosted connection or enable writes automatically.

```sh
python scripts/build_mcpb.py
python scripts/build_plugins.py
```

## Host for multiple users

The server authenticates assistants with its own OAuth grants. Canvas credentials
are encrypted in persistent storage; assistant tokens are separate, revocable,
scoped, and never forwarded to Canvas. Two explicitly distinct hosting modes exist.

**Institution mode** uses a public HTTPS origin and a Canvas OAuth developer key
enabled by each supported institution. Students authorize through their school's
Canvas login. Follow [deployment](docs/DEPLOYMENT.md), then connect the HTTPS
`/mcp` endpoint using the host app's custom connector setup. App directories and
one-click install URLs require their own publication processes.

**Built by Aadi beta mode** adds verified email/password accounts and a private
per-user Canvas URL/token setup, with a separate assistant consent step. Its
production configuration requires Cloudflare Containers and durable storage;
signup is unavailable without verification-email delivery. This mode is under
development, not a live public service. Canvas's published policy prohibits
multi-user applications collecting manually generated tokens; Built by Aadi
OAuth and a beta notice do not resolve that upstream policy issue. See the
[hosting plan and launch constraints](docs/HOSTED-BUILTBYAADI.md) and
[Canvas policy documentation](https://developerdocs.instructure.com/services/canvas/oauth2/file.oauth).

```sh
canvas-connector keygen
# Save the generated key privately as CONNECTOR_ENCRYPTION_KEY.
# Configure the remaining CONNECTOR_* variables described in .env.example.
canvas-connector serve --host 127.0.0.1 --port 8000
```

The bundled Docker configurations bind to loopback for a TLS reverse proxy. Use
SQLite for workers on one host, or PostgreSQL for separate replicas. Refresh
coordination, request limits, continuations, action claims, and receipts share
the database. The CLI supports up to eight workers; production capacity has not
been established. See the [synthetic measurements](docs/BENCHMARKS.md) for a
reproducible starting point.

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
