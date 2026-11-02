"""Command line entry point. No credential values are emitted in diagnostics."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path


def parse_trusted_proxies(value: str | None) -> list[str]:
    """Validate an explicit proxy allowlist; an absent setting disables trust.

    Only IP literals and strict CIDRs are accepted. No DNS names, wildcards,
    empty entries, zone identifiers, or networks covering a whole IP family.
    Uvicorn, rather than application code, interprets forwarded headers.
    """
    if value is None:
        return []
    if not value.strip() or len(value) > 4096:
        raise ValueError("CONNECTOR_TRUSTED_PROXIES must be a bounded nonempty IP allowlist")
    entries = value.split(",")
    if len(entries) > 64:
        raise ValueError("CONNECTOR_TRUSTED_PROXIES accepts at most 64 entries")
    normalized = []
    networks = {4: [], 6: []}
    for entry in entries:
        entry = entry.strip()
        if not entry or "*" in entry or "%" in entry:
            raise ValueError("Trusted proxies must be explicit IP addresses or CIDRs")
        try:
            network = ipaddress.ip_network(entry, strict=True)
        except ValueError:
            raise ValueError("Trusted proxies must be valid IP addresses or strict CIDRs") from None
        if network.prefixlen == 0 or network.is_multicast or network.is_unspecified:
            raise ValueError("Unbounded or unspecified trusted proxy addresses are forbidden")
        networks[network.version].append(network)
        normalized.append(str(network) if "/" in entry else str(network.network_address))
    for family in networks.values():
        if any(network.prefixlen == 0 for network in ipaddress.collapse_addresses(family)):
            raise ValueError("Trusted proxy entries must not cover an entire IP address family")
    return list(dict.fromkeys(normalized))


def main() -> None:
    parser = argparse.ArgumentParser(description="Canvas Connector for MCP assistants")
    commands = parser.add_subparsers(dest="command", required=True)
    local = commands.add_parser("stdio", help="Personal local MCP connection")
    local.add_argument("--allow-writes", action="store_true")
    hosted = commands.add_parser("serve", help="Hosted OAuth MCP server")
    hosted.add_argument("--host", default="127.0.0.1")
    hosted.add_argument("--port", default=8000, type=int)
    hosted.add_argument("--workers", default=1, type=int, choices=range(1, 9), help="Worker processes sharing the configured database (1..8)")
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
    import uvicorn
    try:
        # Validate before spawning so invalid secret configuration fails once.
        trusted_proxies = parse_trusted_proxies(os.environ.get("CONNECTOR_TRUSTED_PROXIES"))
        check = provider_from_env()
        check.store.close()
    except Exception:
        parser.error("Invalid hosted configuration. Check CONNECTOR_PUBLIC_URL, CONNECTOR_DATABASE, CONNECTOR_ENCRYPTION_KEY, CONNECTOR_INSTITUTIONS_FILE and CONNECTOR_TRUSTED_PROXIES.")
    uvicorn.run("canvas_connector.__main__:create_app_from_env", factory=True,
                host=args.host, port=args.port, workers=args.workers,
                access_log=False, proxy_headers=bool(trusted_proxies),
                forwarded_allow_ips=trusted_proxies)


def provider_from_env():
    from .auth import CanvasOAuthProvider, Institution
    required = ("CONNECTOR_PUBLIC_URL", "CONNECTOR_DATABASE", "CONNECTOR_ENCRYPTION_KEY", "CONNECTOR_INSTITUTIONS_FILE")
    if any(not os.environ.get(name) for name in required):
        raise ValueError("Missing hosted configuration")
    institution_data = json.loads(Path(os.environ["CONNECTOR_INSTITUTIONS_FILE"]).read_text())
    return CanvasOAuthProvider(
        issuer_url=os.environ["CONNECTOR_PUBLIC_URL"],
        database_path=os.environ["CONNECTOR_DATABASE"],
        encryption_key=os.environ["CONNECTOR_ENCRYPTION_KEY"],
        institutions=[Institution(**entry) for entry in institution_data],
    )


def create_app_from_env():
    """Uvicorn worker factory; each process owns its database connection."""
    from .server import create_http_app
    return create_http_app(provider_from_env())


if __name__ == "__main__":
    main()
