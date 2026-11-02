"""Personal stdio only: no listener, hosted registration, or credential arguments."""
from __future__ import annotations

import argparse
import logging
import os
import sys

import anyio

from . import __version__
from .bridge import connector_parameters, serve


def main() -> None:
    parser = argparse.ArgumentParser(description="Personal single-user Canvas tunnel facade")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--connector-python", required=True,
                        help="Absolute Python executable from the existing core environment")
    parser.add_argument("--allow-writes", action="store_true",
                        help="Enable the core's reviewed prepare/execute workflow")
    args = parser.parse_args()
    if os.name != "posix":
        parser.error("The personal tunnel facade currently supports macOS and Linux only")
    try:
        parameters = connector_parameters(args.connector_python, allow_writes=args.allow_writes)
    except ValueError as exc:
        parser.error(str(exc))
    # Protocol/model errors can include argument values in SDK debug logs.
    logging.getLogger("mcp").setLevel(logging.CRITICAL)
    try:
        anyio.run(serve, parameters)
    except KeyboardInterrupt:
        pass
    except Exception:  # noqa: BLE001 - process boundary must not print credential-bearing errors
        print("Canvas tunnel stopped. Check the local installation and configuration. "
              "No request was replayed; a pending write may have completed. "
              "Check fresh Canvas state before creating another action.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
