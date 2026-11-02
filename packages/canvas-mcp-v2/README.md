# canvas-mcp-v2 compatibility package

The original personal MCP adapter and its regression tests are retained for
compatibility. The public connector entry point is **canvas-connector**, defined
by the root package. Use that entry point for the new structured tools, expanded
actions, and per-user hosted OAuth.

Do not deploy the legacy single-account bearer transport as a shared public
service. The root README and docs/DEPLOYMENT.md describe supported deployment.
This package's original Python source is MIT licensed under the root LICENSE.
