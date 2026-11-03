"""Command line entry point. No credential values are emitted in diagnostics."""
from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
import sys
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
    commands.add_parser("owner-init", help="Provision the sole owner of a personal server interactively")
    commands.add_parser("owner-reset-password", help="Recover the personal owner password and revoke all sessions/apps")
    args = parser.parse_args()
    if args.command == "keygen":
        from cryptography.fernet import Fernet
        print(Fernet.generate_key().decode())
        return
    if args.command == "stdio":
        from .server import create_stdio_server
        create_stdio_server(allow_writes=args.allow_writes).run(transport="stdio")
        return
    if args.command in {"owner-init", "owner-reset-password"}:
        try:
            configure_personal_owner(reset=args.command == "owner-reset-password")
        except (Exception, KeyboardInterrupt):
            parser.error("Owner configuration failed. Use a private terminal, personal mode, and the configured encrypted database. Initialization requires an empty store; recovery requires an existing owner.")
        return
    if os.environ.get("CONNECTOR_AUTH_MODE") == "personal" and args.workers != 1:
        parser.error("Personal hosting uses one worker so all apps share its upstream request limit.")
    import uvicorn
    try:
        # Validate before spawning so invalid secret configuration fails once.
        trusted_proxies = parse_trusted_proxies(os.environ.get("CONNECTOR_TRUSTED_PROXIES"))
        check = provider_from_env()
        check.store.close()
    except Exception:
        parser.error("Invalid hosted configuration. Check CONNECTOR_AUTH_MODE, CONNECTOR_PUBLIC_URL, CONNECTOR_DATABASE, CONNECTOR_ENCRYPTION_KEY, the mode's onboarding settings and CONNECTOR_TRUSTED_PROXIES.")
    uvicorn.run("canvas_connector.__main__:create_app_from_env", factory=True,
                host=args.host, port=args.port, workers=args.workers,
                access_log=False, proxy_headers=bool(trusted_proxies),
                forwarded_allow_ips=trusted_proxies)


def provider_from_env():
    from .auth import CanvasOAuthProvider, Institution
    mode = os.environ.get("CONNECTOR_AUTH_MODE", "institution")
    if mode not in {"institution", "builtbyaadi", "personal"}:
        raise ValueError("Unknown hosted authentication mode")
    required = ("CONNECTOR_PUBLIC_URL", "CONNECTOR_DATABASE", "CONNECTOR_ENCRYPTION_KEY")
    if any(not os.environ.get(name) for name in required):
        raise ValueError("Missing hosted configuration")
    common = {
        "issuer_url": os.environ["CONNECTOR_PUBLIC_URL"],
        "database_path": os.environ["CONNECTOR_DATABASE"],
        "encryption_key": os.environ["CONNECTOR_ENCRYPTION_KEY"],
    }
    if mode == "personal":
        if common["database_path"] == "cloudflare://store":
            _validate_cloudflare_secrets(common["encryption_key"])
        from .personal import PersonalProvider
        return PersonalProvider(**common)
    if mode == "builtbyaadi":
        if common["database_path"] != "cloudflare://store":
            raise ValueError("Built by Aadi hosting requires the Cloudflare durable store")
        _validate_cloudflare_secrets(common["encryption_key"])
        from .broker import BuiltByAadiProvider
        # The mailer is opt-in. Without it, account creation and email-based
        # recovery are unavailable; no verification code is returned to users.
        mail_mode = os.environ.get("CONNECTOR_ACCOUNT_MAIL", "disabled")
        if mail_mode not in {"disabled", "cloudflare"}:
            raise ValueError("Unknown account mail transport")
        mailer = None
        if mail_mode == "cloudflare":
            from .cloudflare_mail import CloudflareMailer
            mailer = CloudflareMailer()
        return BuiltByAadiProvider(**common, mailer=mailer)
    if not os.environ.get("CONNECTOR_INSTITUTIONS_FILE"):
        raise ValueError("Institution mode requires institution configuration")
    institution_data = json.loads(Path(os.environ["CONNECTOR_INSTITUTIONS_FILE"]).read_text())
    return CanvasOAuthProvider(
        **common,
        institutions=[Institution(**entry) for entry in institution_data],
    )


def create_app_from_env():
    """Uvicorn worker factory; each process owns its database connection."""
    from .server import create_http_app
    provider = provider_from_env()
    edge_secret = os.environ.get("CONNECTOR_EDGE_SECRET") if (
        os.environ.get("CONNECTOR_AUTH_MODE") == "builtbyaadi" or
        (os.environ.get("CONNECTOR_AUTH_MODE") == "personal" and
         os.environ.get("CONNECTOR_DATABASE") == "cloudflare://store")) else None
    return create_http_app(provider, edge_secret=edge_secret)


def _validate_cloudflare_secrets(encryption_key):
    from .edge import validate_edge_secret
    validate_edge_secret(os.environ.get("CONNECTOR_EDGE_SECRET"))
    if os.environ["CONNECTOR_EDGE_SECRET"] == encryption_key:
        raise ValueError("Gateway and record-encryption secrets must be different")


def configure_personal_owner(*, reset: bool = False):
    """Operator-only interactive bootstrap; credentials never appear in argv."""
    if os.environ.get("CONNECTOR_AUTH_MODE") != "personal":
        raise ValueError("Owner commands require personal mode")
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise ValueError("Owner commands require a private interactive terminal")
    from .storage import open_store
    from .personal_admin import initialize_owner, reset_owner_password
    database, key = os.environ.get("CONNECTOR_DATABASE"), os.environ.get("CONNECTOR_ENCRYPTION_KEY")
    if not database or not key:
        raise ValueError("Missing encrypted store configuration")
    email = input("Owner email address (used only for sign-in; no email is sent): ").strip() if not reset else None
    password = getpass.getpass("New owner password (at least 12 characters): ")
    if password != getpass.getpass("Confirm owner password: "):
        raise ValueError("Passwords did not match")
    store = open_store(database, key)
    try:
        if reset:
            reset_owner_password(store, password)
        else:
            initialize_owner(store, email, password)
    finally:
        store.close()
    print("Owner password reset; previous sessions and assistant connections are revoked." if reset else
          "Personal owner provisioned. Start the server and sign in to connect your Canvas account.")


if __name__ == "__main__":
    main()
