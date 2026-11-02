# Personal Canvas tunnel facade

This optional **macOS/Linux** package runs in a **separate Python environment** from Canvas
Connector. It uses the official MCP Python SDK 2.3.0 to accept self-contained
`2026-07-28` requests and legacy `initialize` handshakes on stdio. One initialized
MCP 1.27 connector child owns the existing tools, Canvas credentials, action
receipts, and continuation handles. The shared connector dependencies and
hosted OAuth server are unchanged.

Windows launch is explicitly unavailable until equivalent cancellation-safe
pipe I/O is implemented and verified; it does not fall back to a potentially
blocked stdio thread.

This is a **personal, single-user connection**. Anyone who can use the tunnel
can act as the configured Canvas user. Do not share it or use it as a hosted
multi-user service. This package creates no public HTTP listener, registers no
hosted tunnel, and does not configure ChatGPT or a user's account.

## Install and run

Install the normal connector first, using its own Python environment. Then run
this package separately; `--no-config` prevents the root project's MCP 1 pin
from contaminating the optional environment:

```sh
uv --no-config sync --python 3.12 --project packages/canvas-tunnel --locked
uv --no-config run --project packages/canvas-tunnel --locked --no-sync \
  canvas-connector-tunnel --connector-python /absolute/path/to/core/.venv/bin/python
```

Add `--allow-writes` only when enabling the connector's normal reviewed
prepare/execute workflow. The CLI accepts no arbitrary subprocess command,
HTTP address, Canvas token argument, or OpenAI API key argument. It launches
exactly the selected Python's `-m canvas_connector stdio` command. Configure
the normal `CANVAS_ENV_PATH`, or `CANVAS_BASE_URL` and `CANVAS_API_TOKEN`, as for
the core connector. A local launcher can supply this command to the official
tunnel client's `--mcp-command`. OpenAI account/tunnel registration is a
separate user-controlled setup step.

Only an explicit environment allowlist reaches the connector child. Canvas
configuration and standard OS paths are retained; OpenAI/tunnel keys, hosted
OAuth settings, arbitrary proxy credentials, tracing exports and Python import
path overrides are excluded. No secret values are printed in facade failures.

## Boundaries

- Each complete newline-delimited JSON request or response is limited to
  **9 MiB of UTF-8 bytes**, leaving room under the tunnel's documented 10 MiB
  serialized-envelope limit. Oversized input closes the connection before
  dispatch; oversized output is unavailable, never a truncated success.
- Use **at most 6 MiB of raw upload bytes** through this tunnel. Base64 expands
  that to 8 MiB before JSON. This is conservative transport guidance, not a
  changed Canvas tool schema; large accompanying text can still exceed the
  envelope cap. The core's normal 8 MiB upload bound remains unchanged for
  other clients. Exact tool schemas, annotations and structured tool results
  are forwarded from the child.
- A child request times out after 105 seconds, below the tunnel's documented
  120-second end-to-end limit. Queueing also counts against the tunnel limit.
  A timeout, cancellation or disconnect does **not** establish rollback.
  Check the action receipt and fresh Canvas state before creating another
  action. The facade never retries a call, including protocol errors.
- The SDK correlates caller IDs with independent child IDs. Cancellation stops
  the caller's wait. An already-dispatched child call continues and its response
  is drained so the original receipt can finish; a call cancelled before
  dispatch is not sent. A cancelled request does not get a fabricated success
  result. This avoids a verified MCP 1.27 cancellation failure that can
  double-answer a completed synchronous tool and corrupt its session. There
  are at most 16 outstanding child calls; excess calls are rejected before
  dispatch. A child call still stuck after 300 seconds terminates the facade
  with an uncertain outcome, without replaying it.
- Child exit terminates the facade, even while idle. There is no automatic
  child replacement or call replay here. The tunnel client may restart the
  whole facade; that creates a fresh core process. Old process-local action
  receipts and continuation handles then cease to exist. Inspect Canvas state
  before preparing a replacement action.
- The supported child has a fixed, single-page Canvas tool catalog. The
  facade rejects incomplete, duplicate, oversized or non-Canvas catalogs.
  Modern multi-round input, task execution and server-initiated interactions
  are not added to the core's contract.

## Isolated tests

The root test environment must not import this package's MCP 2 dependencies.
The dedicated test environment requires the already-installed MCP 1 core
environment through `CANVAS_CORE_PYTHON`:

```sh
uv --no-config sync --python 3.12 --project packages/canvas-tunnel --locked
CANVAS_CORE_PYTHON=/absolute/path/to/core/.venv/bin/python \
  uv --no-config run --project packages/canvas-tunnel --locked --no-sync \
  pytest -c packages/canvas-tunnel/pyproject.toml packages/canvas-tunnel/tests
```

Tests use a synthetic Canvas `MockTransport` and prohibit external network
access. Protocol tests launch real MCP processes, not a mocked SDK. Local
tunnel validation uses the official `tunnel-client dev proxy`, never a hosted
tunnel registration or real Canvas/OpenAI credentials. A local protocol pass
does not establish that a particular ChatGPT account has tunnel access or
that the app has been connected successfully.

## Primary sources

- [MCP Python SDK 2.3.0 release](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.3.0)
- [SDK v1 to v2 migration](https://py.sdk.modelcontextprotocol.io/migration/)
- [Self-contained protocol versioning](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning)
- [MCP transports](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports)
- [OpenAI Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
- [ChatGPT MCP Events: both protocol eras](https://developers.openai.com/plugins/build/mcp-events)
- [Official tunnel client configuration](https://github.com/openai/tunnel-client/blob/v0.0.19/docs/configuration.md)

