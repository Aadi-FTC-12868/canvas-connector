"""Allowlisted source release and privacy check; never crawls private workspaces."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
ROOT_FILES = {"README.md", "AGENTS.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md", "pyproject.toml",
              ".gitignore", ".dockerignore", ".env.example", "Dockerfile", "compose.yaml", "compose.postgres.yaml", "requirements.lock", "requirements-runtime.lock"}
PATTERNS = [re.compile(r"/Users/"), re.compile(r"bmcss\.instructure\.com", re.I),
            re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")]


def source_files():
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        if path.is_symlink():
            if relative.parts[0] in {"src", "packages", "tests", "docs", "scripts", "skills", "packaging"}:
                raise ValueError(f"Symlink not allowed: {relative}")
            continue
        if not path.is_file() or any(p.startswith(".") and p not in {".github"} for p in relative.parts[:-1]):
            continue
        if any(p in {"__pycache__", "node_modules", "dist", "build"} or p.endswith(".egg-info") for p in relative.parts):
            continue
        allowed = str(relative) in ROOT_FILES
        if relative.parts[0] in {"src", "tests", "scripts", "docs", "skills", "packaging", ".github"}:
            allowed = path.suffix in {".py", ".md", ".json", ".html", ".css", ".js", ".yml", ".yaml"}
        if relative.parts[0] == "packages" and len(relative.parts) >= 3:
            allowed = (len(relative.parts) == 3 and path.name in {"pyproject.toml", "README.md"}) or (
                relative.parts[2] in {"src", "tests"} and path.suffix == ".py")
        if allowed:
            yield path


def run(build: bool):
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
                bundle.write(path, "canvas-connector/" + str(path.relative_to(ROOT)))
            bundle.writestr("canvas-connector/RELEASE-MANIFEST.json", json.dumps(manifest, indent=2) + "\n")
        print(str(destination))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", action="store_true")
    run(parser.parse_args().build)
