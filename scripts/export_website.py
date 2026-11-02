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
index = args.output / "index.html"
index.write_text(index.read_text().replace('href="./style.css"', f'href="./style.css?v={VERSION}"')
    .replace('src="./app.js"', f'src="./app.js?v={VERSION}"'))
source_bundle = ROOT / f"dist/canvas-connector-{VERSION}-source.zip"
for path, alias in ((source_bundle, "canvas-connector-source.zip"),
                    (desktop_bundle, "canvas-connector-personal.mcpb"),
                    (plugin_bundle, "canvas-connector-plugins.zip")):
    shutil.copy2(path, args.output / path.name)
    shutil.copy2(path, args.output / alias)
(args.output / "connection.json").write_text(json.dumps({"hosted": False, "endpoint": None,
    "release": VERSION, "source_url": "./" + source_bundle.name,
    "desktop_bundle_url": "./" + desktop_bundle.name,
    "plugin_bundle_url": "./" + plugin_bundle.name}) + "\n")
print("Static setup page, source, desktop extension, and local plugins exported.")
