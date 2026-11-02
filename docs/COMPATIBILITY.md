# Client compatibility

Documentation checked November 1, 2026. A supported protocol is not proof of a completed sign-in in every client. See [VALIDATION.md](VALIDATION.md) for the integration tests actually run.

The portable interface is an MCP server at an HTTPS `/mcp` endpoint with OAuth. Local use has a separate stdio entrypoint. The same Canvas account permissions apply in either mode.

| Client | Connection path | Installation and limitations |
| --- | --- | --- |
| ChatGPT | Remote Streamable HTTP with OAuth | Add a custom MCP server in ChatGPT Plugins, enter its URL, finish authorization, then install the resulting plugin. Workspace controls can limit availability. |
| Codex | Remote Streamable HTTP with OAuth, or local stdio | Configure the server, then run `codex mcp login canvas` for remote OAuth. Local stdio uses the local configuration instead. |
| Claude web, Desktop, mobile, Cowork | Remote Streamable HTTP with OAuth | Add the endpoint through Customize → Connectors. Remote requests originate from Anthropic's infrastructure, including when using Desktop or Cowork. |
| Claude Desktop local server | Local stdio | Configure a local MCP server or package a desktop extension. This is a separate installation from an account's remote connector. |
| Claude Code | Remote HTTP with OAuth, or local stdio | Use `claude mcp add --transport http canvas https://YOUR-HOST/mcp`, then authenticate through `/mcp`. Connectors from a signed-in claude.ai subscription can also be available automatically; API-key and third-party-provider sessions differ. |

Sources: [ChatGPT custom servers](https://developers.openai.com/api/docs/guides/custom-mcp-server), [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli), [Claude custom connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp), [Claude Code MCP](https://code.claude.com/docs/en/mcp).

## Website installation buttons

Until a directory listing exists, “Connect to ChatGPT” and “Connect to Claude” should open setup instructions with a copyable server URL. They must not report that the connector was installed. Use the actual listing URL after publication and review. A button cannot approve permissions or sign in for the user.

The documented desktop link `codex://plugins/install/<plugin-name>?marketplace=<marketplace-name>` requires a plugin in a marketplace that the app already knows. It is not an arbitrary MCP-server installation link. See [desktop deep links](https://learn.chatgpt.com/docs/app/commands). We have not verified a universal URL that installs an arbitrary custom server in ChatGPT or Claude with prefilled connection details.

For Claude, a plugin can bundle the MCP configuration and companion skill; custom connectors and reviewed directory listings remain separate distribution paths. See [Claude distribution options](https://claude.com/docs/connectors/building) and [plugin packaging](https://claude.com/docs/plugins/build). Merely hosting this repository does not create either directory listing.

## Authentication contract

There are two independent sign-ins: the AI client authorizes this connector; this connector authorizes access to the user's institution's Canvas. Canvas credentials are never MCP bearer credentials. Hosted onboarding requires an institution-enabled Canvas developer key; see [DEPLOYMENT.md](DEPLOYMENT.md).

The MCP side publishes protected-resource metadata and authorization-server metadata. Authorization uses PKCE S256, with the MCP resource bound to the resulting grant. DCR is supported by ChatGPT and Claude. CIMD is another client-registration mechanism, but should only be advertised by a server that implements its metadata fetching and validation. Our implementation's supported mechanisms are listed in the deployment guide. [MCP authorization specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)

ChatGPT's exact redirect URI can vary by callback mode. Copy it from the server management page; do not assume every connection uses the legacy stable callback. OAuth metadata must advertise S256. [OpenAI authentication](https://developers.openai.com/plugins/build/auth)

Claude requires a real HTTP 401 with a `WWW-Authenticate` metadata pointer. The protected resource must exactly match the entered MCP URL, including `/mcp`. Hosted surfaces use `https://claude.ai/api/mcp/auth_callback`; Claude Code uses loopback callbacks. The token endpoint accepts form-encoded requests, while DCR accepts JSON. Public-client refresh tokens need rotation or sender constraint. [Claude authentication](https://claude.com/docs/connectors/building/authentication)

## Permissions and assistant behavior

The connector marks read and write tools with MCP annotations and enforces its granted scopes. Submissions, comments, discussions, messages, inbox changes, planner changes, and uploads have separate execution tools so a host can apply permissions by action family. A host may additionally offer approvals, remembered decisions, or administrator restrictions. The connector cannot force every client to show identical “always allow / ask / never” controls.

Tool annotations describe behavior; they do not establish user consent. The companion [Canvas assistant skill](../skills/canvas-assistant/SKILL.md) instructs assistants to obtain authorization for the exact submission, message, or post before executing it. Existing explicit authorization for that action is sufficient. Prepared content and a successful upload do not prove that an assignment was submitted.
