"""Export setup UI plus a reviewed source download to a static website folder."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil

from release_check import ROOT, VERSION, run
from build_mcpb import build_bundle
from build_plugins import build_plugins

parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
run(build=True)
desktop_bundle = build_bundle()
plugin_bundle, _ = build_plugins()
args.output.mkdir(parents=True, exist_ok=True)
for path in (ROOT / "src/canvas_connector/web").iterdir():
    if path.is_file():
        shutil.copy2(path, args.output / path.name)
shutil.copy2(ROOT / f"dist/canvas-connector-{VERSION}-source.zip", args.output / "canvas-connector-source.zip")
shutil.copy2(desktop_bundle, args.output / "canvas-connector-personal.mcpb")
shutil.copy2(plugin_bundle, args.output / "canvas-connector-plugins.zip")
(args.output / "connection.json").write_text(json.dumps({"hosted": False, "endpoint": None,
    "source_url": "./canvas-connector-source.zip", "desktop_bundle_url": "./canvas-connector-personal.mcpb",
    "plugin_bundle_url": "./canvas-connector-plugins.zip"}) + "\n")
print("Static setup page, source, desktop extension, and local plugins exported.")
