# Client compatibility

Documentation checked November 1, 2026. A supported protocol is not proof of a completed sign-in in every client. See [VALIDATION.md](VALIDATION.md) for the integration tests actually run.

The portable interface is an MCP server at an HTTPS `/mcp` endpoint with OAuth. Local use has a separate stdio entrypoint. The same Canvas account permissions apply in either mode.

| Client | Connection path | Installation and limitations |
| --- | --- | --- |
| ChatGPT | Remote Streamable HTTP with OAuth | Add a custom MCP server in ChatGPT Plugins, enter its URL, finish authorization, then install the resulting plugin. Workspace controls can limit availability. |
| Codex | Remote Streamable HTTP with OAuth, or local stdio/plugin | Configure the server, then run `codex mcp login canvas` for remote OAuth. The GitHub marketplace or downloadable ZIP bundles the connector and skill for personal use. |
| Claude web, Desktop, mobile, Cowork | Remote Streamable HTTP with OAuth | Add the endpoint through Customize → Connectors. Remote requests originate from Anthropic's infrastructure, including when using Desktop or Cowork. |
| Claude Desktop local server | Local stdio through the `.mcpb` extension | Download the personal extension and install it in Desktop's extension settings. This is a separate installation from an account's remote connector. |
| Claude Code | Remote HTTP with OAuth, or local stdio | Use `claude mcp add --transport http canvas https://YOUR-HOST/mcp`, then authenticate through `/mcp`. Connectors from a signed-in claude.ai subscription can also be available automatically; API-key and third-party-provider sessions differ. |

Sources: [ChatGPT custom servers](https://developers.openai.com/api/docs/guides/custom-mcp-server), [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli), [Claude custom connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp), [Claude Code MCP](https://code.claude.com/docs/en/mcp).

## Install the personal plugin from GitHub

Install [UV](https://docs.astral.sh/uv/getting-started/installation/), Git, and the relevant assistant CLI first. The public [source repository](https://github.com/Aadi-FTC-12868/canvas-connector) contains both marketplace catalogs and the complete plugin at its root. No ZIP extraction is needed for this route.

Codex:

```sh
codex plugin marketplace add https://github.com/Aadi-FTC-12868/canvas-connector.git
codex plugin add canvas-connector-personal@built-by-aadi
```

Claude Code:

```sh
claude plugin marketplace add https://github.com/Aadi-FTC-12868/canvas-connector.git
claude plugin install canvas-connector-personal@built-by-aadi
```

Restart or reload the relevant local session. Codex's setup skill explains the private `~/.config/canvas-connector/canvas.env` file; Claude Code offers plugin configuration fields, including a sensitive token field. Never paste tokens into a chat. Writes default off. Verify the connected account before using course data. These commands install personal local software; they do not establish a hosted connection or a public directory listing. [Codex CLI commands](https://learn.chatgpt.com/docs/cli/reference), [Claude Code installation](https://code.claude.com/docs/en/discover-plugins).

Both catalogs use `./` to select the repository root. UV resolves `canvas-py` and `canvas-mcp-v2` from the bundled `packages/` directories through `tool.uv.sources`; the root project constrains registry dependencies to `requirements.lock`. Plugin updates need the host's marketplace update flow and a new session. [Codex packaging](https://developers.openai.com/plugins/build/plugins), [Claude marketplace sources](https://code.claude.com/docs/en/plugins/marketplace-reference), [UV local dependencies](https://docs.astral.sh/uv/concepts/projects/dependencies/).

## Packaged personal downloads

The source release can build two local artifacts. Both are for the user's own account. They do not provide a public hosted Canvas sign-in or connect cloud/mobile sessions. Institutions can restrict API access; hosted multi-user onboarding still needs institution-authorized OAuth.

| Artifact | Install path | Runtime and account setup |
| --- | --- | --- |
| `canvas-connector-<version>-personal.mcpb` | Claude Desktop → Settings → Desktop app → Extensions → Advanced settings → Install Extension | A host supporting UV bundles manages Python and dependencies. Enter the Canvas origin and personal token in the extension's configuration fields; the token is marked sensitive for host secure storage. Writes default off. |
| `canvas-connector-<version>-plugins.zip` | Extract, add its local marketplace in Codex or Claude Code, then install `canvas-connector-personal@built-by-aadi-local` | Requires UV on PATH. Includes the local source, assistant skill, and setup skill. Claude Code uses plugin configuration fields. Codex uses a private configuration file as explained in the included README. Writes default off. |

The Desktop extension uses manifest version `0.4` and the `uv` server type. Initial dependency installation needs internet access. Platform declarations do not establish successful installation on every operating system. Manual `.mcpb` installs receive updates by installing a newer file; directory publication is separate. [MCPB manifest](https://github.com/modelcontextprotocol/mcpb/blob/main/MANIFEST.md), [Claude Desktop extension installation and secure fields](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop)

Build with `python scripts/build_mcpb.py` and `python scripts/build_plugins.py`. The plugin ZIP contains `.agents/plugins/marketplace.json` and `.claude-plugin/marketplace.json`; its plugin includes portable `plugin.json`/`mcp.json` plus compatibility manifests. Its README provides exact local registration commands. Plugin registration and schema validation do not prove account access or successful host execution. [Codex package format](https://developers.openai.com/plugins/build/plugins), [Claude Code plugin format and configuration](https://code.claude.com/docs/en/plugins-reference)

Public directory submission of an OpenAI local-MCP plugin requires approved-partner access. Custom local marketplaces remain a separate path; this release has no public directory listing. [Desktop-only submission requirements](https://developers.openai.com/plugins/deploy/desktop-only)

## Website installation buttons

The website presents the GitHub marketplace commands for Codex and Claude Code. It exposes “Download for Claude Desktop” only when `desktop_bundle_url` identifies an actual exported `.mcpb`; optional `plugin_bundle_url` exposes the local plugin ZIP fallback. Users still open downloaded artifacts and complete the host's installation steps. The source download is separately configured by `source_url`.

Until a directory listing exists, hosted ChatGPT and Claude buttons open setup instructions with a copyable server URL. They must not report that the connector was installed. Use the actual listing URL after publication and review. A button cannot approve permissions or sign in for the user.

The website's “Open in Codex after adding marketplace” link is `codex://plugins/install/canvas-connector-personal?marketplace=built-by-aadi`. It requires a marketplace that the app already knows. If Codex cannot find it, the link opens the Plugins page; it cannot register the GitHub repository. See [desktop deep links](https://learn.chatgpt.com/docs/app/commands). No universal URL that installs an arbitrary custom server in ChatGPT or Claude with prefilled connection details is claimed.

For Claude, a plugin can bundle the MCP configuration and companion skill; custom connectors and reviewed directory listings remain separate distribution paths. See [Claude distribution options](https://claude.com/docs/connectors/building) and [plugin packaging](https://claude.com/docs/plugins/build). Merely hosting this repository does not create either directory listing.

## Authentication contract

There are two independent sign-ins: the AI client authorizes this connector; this connector authorizes access to the user's institution's Canvas. Canvas credentials are never MCP bearer credentials. Hosted onboarding requires an institution-enabled Canvas developer key; see [DEPLOYMENT.md](DEPLOYMENT.md).

The MCP side publishes protected-resource metadata and authorization-server metadata. Authorization uses PKCE S256, with the MCP resource bound to the resulting grant. DCR is supported by ChatGPT and Claude. CIMD is another client-registration mechanism, but should only be advertised by a server that implements its metadata fetching and validation. Our implementation's supported mechanisms are listed in the deployment guide. [MCP authorization specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)

ChatGPT's exact redirect URI can vary by callback mode. Copy it from the server management page; do not assume every connection uses the legacy stable callback. OAuth metadata must advertise S256. [OpenAI authentication](https://developers.openai.com/plugins/build/auth)

Claude requires a real HTTP 401 with a `WWW-Authenticate` metadata pointer. The protected resource must exactly match the entered MCP URL, including `/mcp`. Hosted surfaces use `https://claude.ai/api/mcp/auth_callback`; Claude Code uses loopback callbacks. The token endpoint accepts form-encoded requests, while DCR accepts JSON. Public-client refresh tokens need rotation or sender constraint. [Claude authentication](https://claude.com/docs/connectors/building/authentication)

## Permissions and assistant behavior

The connector marks read and write tools with MCP annotations and enforces its granted scopes. Submissions, comments, discussions, messages, inbox changes, planner changes, and uploads have separate execution tools so a host can apply permissions by action family. A host may additionally offer approvals, remembered decisions, or administrator restrictions. The connector cannot force every client to show identical “always allow / ask / never” controls.

Tool annotations describe behavior; they do not establish user consent. The companion [Canvas assistant skill](../skills/canvas-assistant/SKILL.md) instructs assistants to obtain authorization for the exact submission, message, or post before executing it. Existing explicit authorization for that action is sufficient. Prepared content and a successful upload do not prove that an assignment was submitted.
