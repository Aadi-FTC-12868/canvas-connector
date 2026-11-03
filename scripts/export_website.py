"""Export setup UI plus a reviewed source download to a static website folder."""
from __future__ import annotations

import argparse
import ipaddress
import json
from pathlib import Path
import shutil
from urllib.parse import urlsplit

from release_check import ROOT, VERSION, run
from build_mcpb import build_bundle
from build_plugins import build_plugins

def hosting_config(path: Path | None) -> dict:
    """Preserve an operator's explicit hosting choice across source releases.

    This validates configuration, not deployment or sign-in success. Unknown
    fields are rejected so credential/configuration files cannot be published.
    """
    offline = {"hosted": False, "endpoint": None}
    if path is None or not path.exists():
        return offline
    if path.stat().st_size > 8192:
        raise ValueError("Hosting configuration is too large")
    value = json.loads(path.read_text())
    allowed = {"hosted", "endpoint", "auth_mode", "account_setup_available", "release",
               "source_url", "desktop_bundle_url", "plugin_bundle_url"}
    if not isinstance(value, dict) or set(value) - allowed or type(value.get("hosted")) is not bool:
        raise ValueError("Invalid public hosting configuration")
    if value["hosted"] is False:
        if value.get("endpoint") is not None:
            raise ValueError("Disabled hosting must not advertise an endpoint")
        return offline
    endpoint = value.get("endpoint")
    if not isinstance(endpoint, str) or len(endpoint) > 2048:
        raise ValueError("Hosted endpoint is required")
    parts = urlsplit(endpoint)
    host = parts.hostname or ""
    if (parts.scheme != "https" or not host or parts.port not in {None, 443}
            or parts.path != "/mcp" or parts.query or parts.fragment or parts.username or parts.password
            or host.lower() in {"localhost", "localhost.localdomain"}
            or host.lower().endswith((".localhost", ".local", ".internal"))):
        raise ValueError("Hosted endpoint must be a public HTTPS /mcp URL")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            raise ValueError("Hosted endpoint must use a public hostname") from None
    else:
        if not address.is_global:
            raise ValueError("Hosted endpoint must be public")
    mode = value.get("auth_mode", "institution")
    if mode not in {"institution", "builtbyaadi"}:
        raise ValueError("Unknown public authentication mode")
    available = value.get("account_setup_available", False)
    if type(available) is not bool:
        raise ValueError("Account setup availability must be explicit")
    return {"hosted": True, "endpoint": endpoint, "auth_mode": mode,
            "account_setup_available": available}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--hosted-config", type=Path, help="Reviewed public endpoint configuration; never a secrets file")
    group.add_argument("--disable-hosted", action="store_true", help="Explicitly remove hosted setup links")
    args = parser.parse_args()
    if args.hosted_config is not None and not args.hosted_config.is_file():
        parser.error("The explicit hosted configuration file does not exist")
    config = hosting_config(None if args.disable_hosted else (args.hosted_config or args.output / "connection.json"))
    run(build=True)
    desktop_bundle = build_bundle()
    plugin_bundle, _ = build_plugins()
    args.output.mkdir(parents=True, exist_ok=True)
    for path in (ROOT / "src/canvas_connector/web").iterdir():
        if path.is_file():
            shutil.copy2(path, args.output / path.name)
    for page in args.output.glob("*.html"):
        text = page.read_text().replace('href="./style.css"', f'href="./style.css?v={VERSION}"')
        for script in ("app.js", "local.js"):
            text = text.replace(f'src="./{script}"', f'src="./{script}?v={VERSION}"')
        page.write_text(text)
    source_bundle = ROOT / f"dist/canvas-connector-{VERSION}-source.zip"
    for path, alias in ((source_bundle, "canvas-connector-source.zip"),
                        (desktop_bundle, "canvas-connector-personal.mcpb"),
                        (plugin_bundle, "canvas-connector-plugins.zip")):
        shutil.copy2(path, args.output / path.name)
        shutil.copy2(path, args.output / alias)
    config.update(release=VERSION, source_url="./" + source_bundle.name,
                  desktop_bundle_url="./" + desktop_bundle.name,
                  plugin_bundle_url="./" + plugin_bundle.name)
    (args.output / "connection.json").write_text(json.dumps(config) + "\n")
    print("Static setup page, source, desktop extension, and local plugins exported.")


if __name__ == "__main__":
    main()
