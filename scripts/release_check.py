"""Allowlisted source release and privacy check; never crawls private workspaces."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import tomllib
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
ROOT_FILES = {"README.md", "AGENTS.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md", "pyproject.toml",
              ".gitignore", ".dockerignore", ".env.example", "Dockerfile", "compose.yaml", "compose.postgres.yaml", "requirements.lock", "requirements-runtime.lock"}
PLUGIN_FILES = {"plugin.json", "mcp.json", "mcp.codex.json", ".mcp.json",
                ".agents/plugins/marketplace.json", ".codex-plugin/plugin.json",
                ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json"}
HOSTED_FILES = {"packaging/hosted/builtbyaadi.env.example",
                "packaging/hosted/builtbyaadi.Caddyfile", "packaging/hosted/canvas-connector.service"}
CLOUDFLARE_FILES = {"cloudflare/package.json", "cloudflare/package-lock.json",
                    "cloudflare/tsconfig.json", "cloudflare/wrangler.jsonc",
                    "cloudflare/README.md", "cloudflare/vitest.config.ts"}
PATTERNS = [re.compile(r"/Users/"), re.compile(r"bmcss\.instructure\.com", re.IGNORECASE),
            re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")]


def release_timestamp() -> tuple[int, int, int, int, int, int]:
    """One reviewed UTC timestamp for reproducible archives and safe upgrades.

    Advancing it with each release invalidates local dependency/build caches
    when an installer preserves ZIP timestamps while replacing source files.
    """
    metadata = json.loads((ROOT / "packaging/release.json").read_text())
    if not isinstance(metadata, dict) or set(metadata) != {"version", "source_date_epoch"}:
        raise ValueError("Release metadata must contain version and source_date_epoch")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    if metadata["version"] != version:
        raise ValueError("Release metadata version must match the root project version")
    epoch = metadata["source_date_epoch"]
    if type(epoch) is not int or epoch % 2 or not 315532800 <= epoch <= 4354819198:
        raise ValueError("Release source_date_epoch must be an even UTC second within the ZIP range")
    value = datetime.fromtimestamp(epoch, UTC)
    return value.year, value.month, value.day, value.hour, value.minute, value.second


def archive_entry(name: str, timestamp: tuple[int, int, int, int, int, int]) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=timestamp)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def source_files():
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        # Optional packages own isolated environments. Exclude those trees
        # before inspecting their interpreter symlinks as release sources.
        if any(p in {".venv", "__pycache__", "node_modules", "dist", "build"} or p.endswith(".egg-info") for p in relative.parts):
            continue
        if path.is_symlink():
            if relative.parts[0] in {"src", "packages", "tests", "docs", "scripts", "skills", "packaging",
                                     ".agents", ".codex-plugin", ".claude-plugin", "cloudflare"} or str(relative) in PLUGIN_FILES:
                raise ValueError(f"Symlink not allowed: {relative}")
            continue
        if not path.is_file() or (str(relative) not in PLUGIN_FILES and
                any(p.startswith(".") and p not in {".github"} for p in relative.parts[:-1])):
            continue
        if any(p in {"__pycache__", "node_modules", "dist", "build"} or p.endswith(".egg-info") for p in relative.parts):
            continue
        allowed = str(relative) in ROOT_FILES | PLUGIN_FILES | HOSTED_FILES | CLOUDFLARE_FILES
        if relative.parts[0] in {"src", "tests", "scripts", "docs", "skills", "packaging", ".github"}:
            allowed = allowed or path.suffix in {".py", ".md", ".json", ".html", ".css", ".js", ".yml", ".yaml"}
        if relative.parts[0] == "packages" and len(relative.parts) >= 3:
            allowed = (len(relative.parts) == 3 and path.name in {"pyproject.toml", "README.md", "uv.lock"}) or (
                relative.parts[2] in {"src", "tests"} and path.suffix == ".py")
        if relative.parts[0] == "cloudflare" and len(relative.parts) >= 3:
            allowed = relative.parts[1] in {"src", "test", "tests"} and path.suffix in {".ts", ".js", ".mjs"}
        if allowed:
            yield path


def run(build: bool):
    timestamp = release_timestamp()
    files = list(source_files())
    failures = []
    for path in files:
        contents = path.read_text()
        # The scanner itself necessarily names private-path patterns.
        if path.name != "release_check.py" and any(p.search(contents) for p in PATTERNS):
            failures.append(str(path.relative_to(ROOT)))
        if path.stat().st_size > 2_000_000:
            failures.append(str(path.relative_to(ROOT)) + " (unexpectedly large source)")
    if failures:
        raise SystemExit("Release scan failed: " + ", ".join(failures))
    manifest = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    print(json.dumps({"files": len(files), "privacy_marker_findings": 0, "allowlisted": True}))
    if build:
        destination = ROOT / "dist" / f"canvas-connector-{VERSION}-source.zip"
        destination.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in files:
                name = "canvas-connector/" + str(path.relative_to(ROOT))
                bundle.writestr(archive_entry(name, timestamp), path.read_bytes())
            bundle.writestr(archive_entry("canvas-connector/RELEASE-MANIFEST.json", timestamp),
                            json.dumps(manifest, indent=2) + "\n")
        print(str(destination))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", action="store_true")
    run(parser.parse_args().build)
