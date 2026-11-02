"""Build local Codex/Claude Code marketplaces without installing them for a user."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_mcpb
import release_check

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = "plugins/canvas-connector-personal/"


def plugin_contents() -> tuple[str, dict[str, bytes]]:
    runtime = build_mcpb.bundle_contents()
    version = json.loads(runtime.pop("manifest.json"))["version"]
    for name in ("entrypoint.py", "README-BUNDLE.md", "BUNDLE-MANIFEST.json"):
        runtime.pop(name)
    files = {PLUGIN + name: data for name, data in runtime.items()}
    sources = {p.relative_to(ROOT).as_posix(): p for p in release_check.source_files()}

    def source(name):
        return sources["packaging/plugin/" + name].read_bytes()

    for original, target in {
        "portable-plugin.json": "plugin.json", "portable-mcp.json": "mcp.json",
        "codex-plugin.json": ".codex-plugin/plugin.json", "claude-plugin.json": ".claude-plugin/plugin.json",
        "claude-mcp.json": ".mcp.json",
    }.items():
        data = json.loads(source(original))
        if "version" in data:
            data["version"] = version
        files[PLUGIN + target] = (json.dumps(data, indent=2) + "\n").encode()
    legacy_mcp = json.loads(source("portable-mcp.json"))
    legacy_mcp.pop("$schema")
    files[PLUGIN + "mcp.codex.json"] = (json.dumps(legacy_mcp, indent=2) + "\n").encode()
    files[PLUGIN + "plugin_entrypoint.py"] = source("entrypoint.py")
    files[PLUGIN + "skills/setup/SKILL.md"] = source("setup-SKILL.md")
    files[".agents/plugins/marketplace.json"] = source("codex-marketplace.json")
    files[".claude-plugin/marketplace.json"] = source("claude-marketplace.json")
    files["README.md"] = source("README.md")
    files["LICENSE"] = runtime["LICENSE"]
    files["PLUGIN-MANIFEST.json"] = (json.dumps({name: hashlib.sha256(data).hexdigest()
        for name, data in sorted(files.items())}, indent=2) + "\n").encode()
    return version, files


def build_plugins(output: Path | None = None) -> tuple[Path, Path]:
    version, files = plugin_contents()
    destination = output or ROOT / "dist" / f"canvas-connector-{version}-plugins.zip"
    directory = destination.with_suffix("")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix="plugin-stage-") as temporary:
        stage = Path(temporary)
        with zipfile.ZipFile(stage / "archive.zip", "w", zipfile.ZIP_DEFLATED) as bundle:
            for name, data in sorted(files.items()):
                target = stage / "marketplace" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                info = zipfile.ZipInfo("canvas-connector-plugins/" + name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                bundle.writestr(info, data)
        # Replace only this builder's generated sibling output directory.
        if directory.exists():
            marker = directory / "PLUGIN-MANIFEST.json"
            if directory.is_symlink() or not marker.is_file():
                raise ValueError("Refusing to replace a directory without this builder's manifest")
            shutil.rmtree(directory)
        (stage / "marketplace").replace(directory)
        (stage / "archive.zip").replace(destination)
    return destination, directory


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    archive, directory = build_plugins(parser.parse_args().output)
    print(json.dumps({"archive": str(archive), "marketplace": str(directory)}))
