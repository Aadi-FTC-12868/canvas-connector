# Personal Canvas connection in ChatGPT

The optional setup runner connects your own local Canvas account to ChatGPT
through OpenAI Secure MCP Tunnel. It does not require a public Canvas OAuth
service. Keep the tunnel and plugin limited to your own access: anyone allowed
to use this process acts as the one configured Canvas user. This is not a
multi-user hosted connector.

The runner and compatibility adapter support **macOS and Linux**. Your computer
must remain awake and the runner must stay open for discovery and calls. The
software is free and MIT licensed; access to OpenAI's tunnel and ChatGPT custom
server features is controlled by your OpenAI organization/workspace.

## Check account access first

Open [Platform tunnel settings](https://platform.openai.com/settings/organization/tunnels)
and [ChatGPT Plugins](https://chatgpt.com/plugins). You need permission to create
and use a tunnel and to add a custom MCP server in the intended ChatGPT workspace.
These are separate permissions. Associate the tunnel with both the correct
Platform organization and ChatGPT workspace, keeping access limited to you.

OpenAI documents Tunnels Read + Manage for creation and Read + Use for running
or selecting a tunnel. A personal organization can be used where those features
are available; this guide does not promise access on every plan. If the controls
are absent or denied, resolve that account/workspace restriction before entering
credentials. [Official tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

## Start the guided runner

Install Python 3, [UV](https://docs.astral.sh/uv/getting-started/installation/),
and the [official OpenAI tunnel client](https://github.com/openai/tunnel-client).
On macOS, the official client recommends Homebrew rather than bypassing
Gatekeeper for an unnotarized ZIP:

```sh
brew install openai/tools/tunnel-client
```

On Linux, follow the official client's release installation instructions.
Download and extract this connector's source ZIP, or clone its public repository.
Open a terminal in the extracted source folder containing `scripts/` (or the
cloned `canvas-connector` folder), then run:

```sh
python3 scripts/start_personal_tunnel.py --profile canvas-personal
```

The runner checks prerequisites, guides private Canvas configuration, installs
separate pinned Python 3.12 core and adapter environments, and prepares a named
tunnel client profile. It does not register a hosted tunnel, install the ChatGPT
plugin, start a background service, or change normal Codex/Claude configuration.
Keep the extracted source folder at the same path for future runs. The installed
adapter is independent of checkout imports, but this runner binds each profile
to its source location and uses that source to refresh the environments.

Enter the tunnel ID from Platform. Use a restricted OpenAI **runtime** API key
with tunnel access, entered through the runner's hidden terminal input; never
use an admin key. The runner does not save that key or put it in command-line
arguments. It supplies the key only to the official tunnel client's check/run
processes. The Canvas connector child receives its own Canvas configuration,
not the OpenAI control-plane credentials.

If you already have a private Canvas environment file, select it explicitly:

```sh
python3 scripts/start_personal_tunnel.py --profile canvas-personal \
  --canvas-env /absolute/private/path/canvas.env
```

Without `--canvas-env`, an existing `CANVAS_ENV_PATH` is used in place. Otherwise
the runner guides creation of an owner-only file outside the source checkout and
runtime directories. Enter your school's HTTPS Canvas origin and your own
personal token locally. Existing files must be owned by you, have owner-only
permissions (`chmod 600`), and have no symlink or hardlink indirection. Existing
files and foreign/modified tunnel profiles are not overwritten. Keep secrets out
of chats, public files, screenshots and shell command arguments. School
restrictions on personal API tokens still apply.

By default, Canvas settings live at
`~/.config/canvas-connector/canvas/<profile>.env`; the runner prints the selected
path. Tunnel profiles and their nonsecret ownership manifests live under
`~/.config/canvas-connector/tunnel-profiles`. The two Python environments live
under `~/.local/share/canvas-connector/tunnel-runtime/<profile>`. Explicit
`XDG_CONFIG_HOME` and `XDG_DATA_HOME` values replace those base directories.

Writes default off. Add `--allow-writes` only when you want the normal prepared
action workflow available. Enabling it does not authorize a particular message,
submission or other action; the assistant still needs your authorization for
the exact content and target.

## Prepare or restart a local profile

To install and prepare everything locally without entering an OpenAI runtime key
or starting the tunnel, use:

```sh
python3 scripts/start_personal_tunnel.py --profile canvas-personal --setup-only
```

Run the same command without `--setup-only` when ready to connect. Subsequent
runs reuse an unchanged profile created by this runner and ask for the runtime
key again. Use the same source folder, Canvas settings and write setting;
`--allow-writes` must be supplied again for a profile created with writes enabled.
Choose a new profile name if those settings change or the source folder moves.

The nonsecret `--tunnel-id` option can supply the existing Platform ID instead of
prompting. If your runtime key is already securely provided through the local
environment, `--runtime-key-env CONTROL_PLANE_API_KEY` or
`--runtime-key-env OPENAI_API_KEY` explicitly selects it. The runner does not
implicitly reuse ambient OpenAI keys or save the selected key.

## Finish in ChatGPT

Keep the runner open. In ChatGPT Plugins, add a custom MCP server and choose
**Tunnel** under Connection. Select the tunnel or enter its ID. For this
restricted personal stdio setup, choose **No authentication**: the tunnel uses
OpenAI's access controls, and the Canvas token belongs only in the local private
configuration. Create and install the plugin, then select it in a chat.

Ask for the Canvas profile and confirm the account identity. Then ask for the
dashboard. Successful package installation, local health, or a visible
tool list does not establish actual Canvas access. Never test writes by sending
a real message or submitting coursework without specific authorization.
[ChatGPT custom MCP setup](https://developers.openai.com/api/docs/guides/custom-mcp-server)

If the tunnel is missing, check its workspace association and your Use permission.
If discovery fails, confirm the runner is still active and read its stage-specific
status. Detailed subprocess output is suppressed to avoid exposing credentials.
Follow the official account troubleshooting guide when Platform or ChatGPT
rejects access; local code cannot grant those permissions.

## Limits and recovery

Use at most **6 MiB of raw bytes per upload/attachment through this tunnel**.
Base64 expands that to 8 MiB before JSON. The adapter caps each request/response
at 9 MiB; large accompanying text can also exceed that bound. It refuses
oversized messages instead of truncating data. The ordinary connector's 8 MiB
file limit remains available in other clients.

OpenAI's documented tunnel limit is 10 MiB for the complete serialized envelope
and 120 seconds including queue time. The adapter stops waiting after 105
seconds. A timeout, cancellation or disconnect does not mean a Canvas write
rolled back. Already-dispatched calls are bounded and drained because cancelling
the legacy SDK's operation can corrupt its session. Inspect the original action
receipt and fresh Canvas state before authorizing a new intent; no write is
automatically retried. [Tunnel limits](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

If the core process dies, the adapter exits. This runner configures the tunnel
client to exit as well, so restart it manually with the same command. Local
receipts and continuation handles belong to the old process and become
unavailable after restart. Check Canvas directly before preparing a replacement
action. Stopping the runner disconnects the personal connection; it does not
recall a message or undo a completed action.

## What has been tested

The optional adapter uses MCP Python SDK 2.3.0 in its own environment and an
initialized MCP 1.27.0 connector child. It forwards the actual tools, schemas,
annotations and structured results; it does not reimplement Canvas operations.
The hosted server and existing local clients keep their current dependencies.

Synthetic tests through official `tunnel-client` 0.0.19 verified both legacy
`2025-11-25` initialization and self-contained `2026-07-28` calls, writes off/on,
continued results, credential isolation, timeout after an applied write, and
process recovery without replay. No real Canvas/OpenAI credentials, hosted
tunnel registration or actual ChatGPT account connection were used in those
checks. See [validation](VALIDATION.md) and the
[adapter package](../packages/canvas-tunnel/README.md) for the exact boundaries.

A successful connection in your own ChatGPT account remains the final setup
check. The personal tunnel is separate from public plugin distribution, which
OpenAI requires to use a stable publicly reachable HTTPS endpoint.
[Official tunnel distribution boundary](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
