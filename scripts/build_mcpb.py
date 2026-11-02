"""Build a personal-local MCPB from the reviewed source-release allowlist.

The archive contains source, not a developer machine's environment. UV resolves
the bundled local packages and installs pinned registry dependencies on first use.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_check

ROOT = Path(__file__).resolve().parents[1]
NOTICE = """# Canvas Connector personal desktop bundle

Install this .mcpb through Claude Desktop's extension settings. Configure your
own Canvas HTTPS origin and personal token there. Writes default to disabled.
This local single-user package does not provide public hosted OAuth onboarding.

The host's UV runtime manages Python 3.12 and installs dependencies on first use;
internet access is required. Platform declarations describe the package format,
not completed installation tests on every operating system. Optional document
OCR still needs its external tools. The assistant skill is included as readable
source; the extension does not automatically install a separate host skill.

No credentials are included. Stored settings are managed by the host. Use its
uninstall and credential controls to remove your connection. Refer to LICENSE,
SECURITY.md, and the source release for implementation and security boundaries.
"""


def bundle_contents() -> dict[str, bytes]:
    release_check.run(False)
    allowed = {p.relative_to(ROOT).as_posix(): p for p in release_check.source_files()}
    required = {"pyproject.toml", "requirements.lock", "README.md", "LICENSE", "SECURITY.md",
                "packaging/mcpb/manifest.json", "packaging/mcpb/entrypoint.py"}
    if missing := required - allowed.keys():
        raise ValueError("Required packaging source is not allowlisted: " + ", ".join(sorted(missing)))
    result = {}
    for name, path in allowed.items():
        parts = Path(name).parts
        runtime = parts[0] in {"src", "skills"}
        dependency = (parts[0] == "packages" and len(parts) >= 3 and
                      (parts[2] == "src" or (len(parts) == 3 and parts[2] in {"pyproject.toml", "README.md"})))
        if runtime or dependency or name in {"pyproject.toml", "README.md", "LICENSE", "SECURITY.md", "requirements.lock"}:
            result[name] = path.read_bytes()
    manifest = json.loads(allowed["packaging/mcpb/manifest.json"].read_text())
    project = tomllib.loads(result["pyproject.toml"].decode())
    manifest["version"] = project["project"]["version"]
    if "uv" in project.get("tool", {}):
        raise ValueError("Review existing tool.uv configuration before packaging")
    constraints = [line.strip() for line in result["requirements.lock"].decode().splitlines()
                   if line.strip() and not line.lstrip().startswith("#")]
    if any("==" not in item or any(c in item for c in "\r\n") for item in constraints):
        raise ValueError("Expected exact dependency pins")
    uv_config = "\n[tool.uv]\nconstraint-dependencies = " + json.dumps(constraints) + "\n"
    uv_config += '\n[tool.uv.sources]\ncanvas-py = { path = "packages/canvas-py" }\ncanvas-mcp-v2 = { path = "packages/canvas-mcp-v2" }\n'
    result["pyproject.toml"] += uv_config.encode()
    result["manifest.json"] = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode()
    result["entrypoint.py"] = allowed["packaging/mcpb/entrypoint.py"].read_bytes()
    result["README-BUNDLE.md"] = NOTICE.encode()
    result["BUNDLE-MANIFEST.json"] = (json.dumps({name: hashlib.sha256(data).hexdigest()
        for name, data in sorted(result.items())}, indent=2) + "\n").encode()
    return result


def build_bundle(output: Path | None = None) -> Path:
    contents = bundle_contents()
    version = json.loads(contents["manifest.json"])["version"]
    destination = output or ROOT / "dist" / f"canvas-connector-{version}-personal.mcpb"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".mcpb", delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with zipfile.ZipFile(temporary_path, "w", zipfile.ZIP_DEFLATED) as bundle:
            for name, data in sorted(contents.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                bundle.writestr(info, data)
        temporary_path.replace(destination)
    finally:
        temporary_path.unlink(missing_ok=True)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    print(build_bundle(parser.parse_args().output))
