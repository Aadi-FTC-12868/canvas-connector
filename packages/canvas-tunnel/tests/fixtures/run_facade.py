"""Internal synthetic launcher: not installed as a public command."""
import logging
import sys
from pathlib import Path

import anyio
from mcp.client.stdio import StdioServerParameters

from canvas_tunnel.bridge import child_environment, serve

logging.getLogger("mcp").setLevel(logging.CRITICAL)
python, folder, mode, timeout = sys.argv[1:]
parameters = StdioServerParameters(
    command=python,
    args=[str(Path(__file__).with_name("legacy_child.py")), folder, mode],
    env=child_environment(),
)


async def run():
    await serve(parameters, timeout=float(timeout))


try:
    anyio.run(run)
except Exception:  # noqa: BLE001 - mirrors the sanitized production process boundary
    print("Synthetic facade stopped; no replay", file=sys.stderr)
    raise SystemExit(1) from None

