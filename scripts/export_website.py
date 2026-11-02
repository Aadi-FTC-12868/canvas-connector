"""Export setup UI plus a reviewed source download to a static website folder."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil

from release_check import ROOT, run

parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
run(build=True)
args.output.mkdir(parents=True, exist_ok=True)
for path in (ROOT / "src/canvas_connector/web").iterdir():
    if path.is_file():
        shutil.copy2(path, args.output / path.name)
shutil.copy2(ROOT / "dist/canvas-connector-0.3.0-source.zip", args.output / "canvas-connector-source.zip")
(args.output / "connection.json").write_text(json.dumps({"hosted": False, "endpoint": None,
    "source_url": "./canvas-connector-source.zip"}) + "\n")
print("Static setup page and source download exported.")
