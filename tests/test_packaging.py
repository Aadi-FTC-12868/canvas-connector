"""The desktop package must be complete, secret-free, and read-only by default."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tomllib
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bundle_is_allowlisted_reproducible_and_resolves_local_dependencies(tmp_path):
    builder = load_file("mcpb_builder", ROOT / "scripts/build_mcpb.py")
    first = builder.build_bundle(tmp_path / "first.mcpb")
    second = builder.build_bundle(tmp_path / "second.mcpb")
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as bundle:
        names = set(bundle.namelist())
        assert {"manifest.json", "entrypoint.py", "LICENSE", "README-BUNDLE.md",
                "src/canvas_connector/server.py", "packages/canvas-py/pyproject.toml",
                "packages/canvas-mcp-v2/pyproject.toml"} <= names
        assert not any(part in {".env", ".venv", ".git", "tests", "node_modules", "__pycache__"}
                       for name in names for part in Path(name).parts)
        project = tomllib.loads(bundle.read("pyproject.toml").decode())
        assert project["tool"]["uv"]["sources"] == {
            "canvas-py": {"path": "packages/canvas-py"},
            "canvas-mcp-v2": {"path": "packages/canvas-mcp-v2"}}
        assert "mcp==1.27.0" in project["tool"]["uv"]["constraint-dependencies"]
        manifest = json.loads(bundle.read("manifest.json"))
        assert manifest["user_config"]["canvas_token"]["sensitive"] is True
        assert manifest["user_config"]["allow_writes"]["default"] is False
        for name, expected in json.loads(bundle.read("BUNDLE-MANIFEST.json")).items():
            assert hashlib.sha256(bundle.read(name)).hexdigest() == expected


@pytest.mark.parametrize("setting,expected", [(None, False), ("false", False), ("true", True)])
def test_entrypoint_uses_host_account_and_explicit_write_option(monkeypatch, setting, expected):
    import canvas_connector.server
    entry = load_file("mcpb_entry", ROOT / "packaging/mcpb/entrypoint.py")
    monkeypatch.setenv("CANVAS_BASE_URL", "https://school.example")
    monkeypatch.setenv("CANVAS_API_TOKEN", "synthetic-local-token")
    monkeypatch.setenv("CANVAS_ENV_PATH", "/nonexistent/unrelated-account.env")
    if setting is None:
        monkeypatch.delenv("CANVAS_ALLOW_WRITES", raising=False)
    else:
        monkeypatch.setenv("CANVAS_ALLOW_WRITES", setting)
    captured = {}

    class Server:
        def run(self, **kwargs):
            captured.update(kwargs)

    def create(**kwargs):
        captured.update(kwargs)
        return Server()

    monkeypatch.setattr(canvas_connector.server, "create_stdio_server", create)
    entry.main()
    assert captured["canvas"].config.token == "synthetic-local-token"
    assert captured["allow_writes"] is expected
    assert captured["transport"] == "stdio"


def test_entrypoint_rejects_ambiguous_write_setting_without_exposing_token(monkeypatch, capsys):
    entry = load_file("mcpb_entry_bad", ROOT / "packaging/mcpb/entrypoint.py")
    monkeypatch.setenv("CANVAS_BASE_URL", "https://school.example")
    monkeypatch.setenv("CANVAS_API_TOKEN", "synthetic-secret")
    monkeypatch.setenv("CANVAS_ALLOW_WRITES", "yes")
    with pytest.raises(SystemExit) as error:
        entry.main()
    assert "synthetic-secret" not in str(error.value) + capsys.readouterr().err


def test_plugin_archive_contains_both_catalogs_and_local_runtime(tmp_path):
    builder = load_file("plugin_builder", ROOT / "scripts/build_plugins.py")
    archive, directory = builder.build_plugins(tmp_path / "local-plugins.zip")
    plugin = directory / "plugins/canvas-connector-personal"
    assert (plugin / "skills/canvas-assistant/SKILL.md").is_file()
    assert (plugin / "skills/setup/SKILL.md").is_file()
    assert json.loads((plugin / "mcp.json").read_text())["mcpServers"]["canvas"]["type"] == "stdio"
    assert (plugin / "packages/canvas-py/src/canvas_py/__init__.py").is_file()
    for catalog in [".agents/plugins/marketplace.json", ".claude-plugin/marketplace.json"]:
        entry = json.loads((directory / catalog).read_text())["plugins"][0]
        relative = entry["source"] if isinstance(entry["source"], str) else entry["source"]["path"]
        assert (directory / relative).resolve() == plugin.resolve()
    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
        assert all(name.startswith("canvas-connector-plugins/") for name in names)
        assert not any(Path(name).name.endswith(".env") for name in names)
    for name, expected in json.loads((directory / "PLUGIN-MANIFEST.json").read_text()).items():
        assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == expected


def test_repository_is_a_complete_root_plugin_with_both_catalogs():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    for path in ["plugin.json", ".codex-plugin/plugin.json", ".claude-plugin/plugin.json"]:
        assert json.loads((ROOT / path).read_text())["version"] == project["project"]["version"]
    for path in [".agents/plugins/marketplace.json", ".claude-plugin/marketplace.json"]:
        catalog = json.loads((ROOT / path).read_text())
        assert catalog["name"] == "built-by-aadi"
        source = catalog["plugins"][0]["source"]
        relative = source if isinstance(source, str) else source["path"]
        assert relative == "./"
        assert (ROOT / relative).resolve() == ROOT
    for path, variable in [("mcp.json", "${PLUGIN_ROOT}"), ("mcp.codex.json", "${PLUGIN_ROOT}"),
                           (".mcp.json", "${CLAUDE_PLUGIN_ROOT}")]:
        server = json.loads((ROOT / path).read_text())["mcpServers"]["canvas"]
        assert server["command"] == "uv"
        assert server["args"] == ["run", "--python", "3.12", "--directory", variable,
                                  "--no-dev", "packaging/plugin/entrypoint.py"]
        assert (ROOT / server["args"][-1]).is_file()
    assert (ROOT / "skills/setup/SKILL.md").read_bytes() == (ROOT / "packaging/plugin/setup-SKILL.md").read_bytes()
    secret = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())["userConfig"]
    assert secret["canvas_token"]["sensitive"] is True
    assert secret["allow_writes"]["default"] is False


def test_bundle_rejects_unreviewed_uv_dependency_sources(monkeypatch):
    builder = load_file("mcpb_invalid_sources", ROOT / "scripts/build_mcpb.py")
    original = builder.tomllib.loads

    def changed_project(content):
        project = original(content)
        project["tool"]["uv"]["sources"]["canvas-py"] = {"path": "../unreviewed"}
        return project

    monkeypatch.setattr(builder.tomllib, "loads", changed_project)
    with pytest.raises(ValueError, match="bundled local sources"):
        builder.bundle_contents()


def test_plugin_explicit_host_fields_never_mix_with_selected_private_file(monkeypatch, tmp_path):
    entry = load_file("plugin_entry_partial", ROOT / "packaging/plugin/entrypoint.py")
    private = tmp_path / "canvas.env"
    private.write_text("CANVAS_BASE_URL=https://old.example\nCANVAS_API_TOKEN=synthetic-old-token\n")
    monkeypatch.setenv("CANVAS_ENV_PATH", str(private))
    monkeypatch.setenv("CANVAS_BASE_URL", "https://new.example")
    monkeypatch.delenv("CANVAS_API_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        entry.main()


def test_plugin_uses_only_documented_private_file_without_interpolation(monkeypatch, tmp_path):
    import canvas_connector.server
    entry = load_file("plugin_entry_file", ROOT / "packaging/plugin/entrypoint.py")
    private = tmp_path / "canvas.env"
    private.write_text("CANVAS_BASE_URL=https://school.example\nCANVAS_API_TOKEN=literal-${OTHER_TOKEN}\nCANVAS_ALLOW_WRITES=false\n")
    monkeypatch.setenv("CANVAS_ENV_PATH", str(private))
    monkeypatch.setenv("OTHER_TOKEN", "must-not-substitute")
    for name in ["CANVAS_BASE_URL", "CANVAS_API_TOKEN", "CANVAS_ALLOW_WRITES"]:
        monkeypatch.delenv(name, raising=False)
    captured = {}

    class Server:
        def run(self, **kwargs):
            captured.update(kwargs)

    def create(**kwargs):
        captured.update(kwargs)
        return Server()

    monkeypatch.setattr(canvas_connector.server, "create_stdio_server", create)
    entry.main()
    assert captured["canvas"].config.token == "literal-${OTHER_TOKEN}"
    assert captured["allow_writes"] is False
