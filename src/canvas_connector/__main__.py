"""Command line entry point. No credential values are emitted in diagnostics."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Canvas Connector for MCP assistants")
    commands = parser.add_subparsers(dest="command", required=True)
    local = commands.add_parser("stdio", help="Personal local MCP connection")
    local.add_argument("--allow-writes", action="store_true")
    hosted = commands.add_parser("serve", help="Hosted OAuth MCP server (one worker)")
    hosted.add_argument("--host", default="127.0.0.1")
    hosted.add_argument("--port", default=8000, type=int)
    commands.add_parser("keygen", help="Generate a private encryption key; store it securely")
    args = parser.parse_args()
    if args.command == "keygen":
        from cryptography.fernet import Fernet
        print(Fernet.generate_key().decode())
        return
    if args.command == "stdio":
        from .server import create_stdio_server
        create_stdio_server(allow_writes=args.allow_writes).run(transport="stdio")
        return
    from .auth import CanvasOAuthProvider, Institution
    from .server import create_http_app
    import uvicorn
    required = ("CONNECTOR_PUBLIC_URL", "CONNECTOR_DATABASE", "CONNECTOR_ENCRYPTION_KEY", "CONNECTOR_INSTITUTIONS_FILE")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        parser.error("Set required configuration: " + ", ".join(missing))
    try:
        institution_data = json.loads(Path(os.environ["CONNECTOR_INSTITUTIONS_FILE"]).read_text())
        institutions = [Institution(**entry) for entry in institution_data]
        provider = CanvasOAuthProvider(
            issuer_url=os.environ["CONNECTOR_PUBLIC_URL"],
            database_path=os.environ["CONNECTOR_DATABASE"],
            encryption_key=os.environ["CONNECTOR_ENCRYPTION_KEY"],
            institutions=institutions,
        )
    except Exception:
        parser.error("Invalid hosted configuration. Check the HTTPS origin, encryption key, database, and institutions file.")
    uvicorn.run(create_http_app(provider), host=args.host, port=args.port, workers=1,
                access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
