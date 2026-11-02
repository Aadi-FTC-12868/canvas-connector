"""The desktop package must be complete, secret-free, and read-only by default."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import tomllib
import zipfile
from datetime import UTC, datetime
from pathlib import Path

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
                "packaging/release.json",
                "src/canvas_connector/server.py", "packages/canvas-py/pyproject.toml",
                "packages/canvas-mcp-v2/pyproject.toml", "packages/canvas-tunnel/uv.lock",
                "packages/canvas-tunnel/src/canvas_tunnel/bridge.py"} <= names
        assert not any(part in {".env", ".venv", ".git", "tests", "node_modules", "__pycache__"}
                       for name in names for part in Path(name).parts)
        project = tomllib.loads(bundle.read("pyproject.toml").decode())
        assert "canvas-py==0.2.1" in project["project"]["dependencies"]
        assert project["tool"]["uv"]["sources"] == {
            "canvas-py": {"path": "packages/canvas-py"},
            "canvas-mcp-v2": {"path": "packages/canvas-mcp-v2"}}
        assert "mcp==1.27.0" in project["tool"]["uv"]["constraint-dependencies"]
        tunnel = tomllib.loads(bundle.read("packages/canvas-tunnel/pyproject.toml").decode())
        assert tunnel["project"]["dependencies"] == ["mcp==2.3.0"]
        manifest = json.loads(bundle.read("manifest.json"))
        assert manifest["user_config"]["canvas_token"]["sensitive"] is True
        assert manifest["user_config"]["allow_writes"]["default"] is False
        for name, expected in json.loads(bundle.read("BUNDLE-MANIFEST.json")).items():
            assert hashlib.sha256(bundle.read(name)).hexdigest() == expected
        assert {info.date_time for info in bundle.infolist()} == {builder.release_check.release_timestamp()}


def test_source_allowlist_excludes_nested_environments_but_rejects_source_symlinks(tmp_path, monkeypatch):
    checker = load_file("release_allowlist_isolated", ROOT / "scripts/release_check.py")
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    package = tmp_path / "packages/canvas-tunnel"
    environment = package / ".venv/bin"
    environment.mkdir(parents=True)
    (environment / "python").symlink_to("/nonexistent/private-interpreter")
    (package / "uv.lock").write_text("version = 1\n")
    assert list(checker.source_files()) == [package / "uv.lock"]
    source = package / "src"
    source.mkdir()
    (source / "borrowed.py").symlink_to("/nonexistent/private-source")
    with pytest.raises(ValueError, match="Symlink"):
        list(checker.source_files())


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
        assert {info.date_time for info in bundle.infolist()} == {builder.release_check.release_timestamp()}
    for name, expected in json.loads((directory / "PLUGIN-MANIFEST.json").read_text()).items():
        assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == expected
    second, _ = builder.build_plugins(tmp_path / "second-plugins.zip")
    assert archive.read_bytes() == second.read_bytes()


def isolated_release(checker, tmp_path, monkeypatch, metadata):
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    monkeypatch.setattr(checker, "VERSION", "1.2.3")
    (tmp_path / "packaging").mkdir(exist_ok=True)
    (tmp_path / "packaging/release.json").write_text(json.dumps(metadata))
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n')


@pytest.mark.parametrize("metadata", [
    {}, {"version": "1.2.3"}, {"version": "1.2.4", "source_date_epoch": 1793607874},
    {"version": "1.2.3", "source_date_epoch": True},
    {"version": "1.2.3", "source_date_epoch": "1793607874"},
    {"version": "1.2.3", "source_date_epoch": 1793607874.0},
    {"version": "1.2.3", "source_date_epoch": 1793607875},
    {"version": "1.2.3", "source_date_epoch": 315532798},
    {"version": "1.2.3", "source_date_epoch": 4354819200},
    {"version": "1.2.3", "source_date_epoch": 1793607874, "unexpected": True},
])
def test_invalid_release_metadata_blocks_build_before_archive_creation(tmp_path, monkeypatch, metadata):
    checker = load_file("invalid_release_metadata", ROOT / "scripts/release_check.py")
    isolated_release(checker, tmp_path, monkeypatch, metadata)
    with pytest.raises(ValueError, match="Release"):
        checker.run(True)
    assert not (tmp_path / "dist").exists()


@pytest.mark.parametrize("epoch,expected", [
    (315532800, (1980, 1, 1, 0, 0, 0)),
    (1793607874, (2026, 11, 2, 8, 24, 34)),
    (4354819198, (2107, 12, 31, 23, 59, 58)),
])
def test_release_timestamp_uses_utc_and_supported_zip_bounds(tmp_path, monkeypatch, epoch, expected):
    checker = load_file("valid_release_metadata", ROOT / "scripts/release_check.py")
    isolated_release(checker, tmp_path, monkeypatch, {"version": "1.2.3", "source_date_epoch": epoch})
    assert checker.release_timestamp() == expected


def test_source_archive_has_reproducible_reviewed_timestamp_and_exact_manifest(tmp_path, monkeypatch):
    checker = load_file("source_release_timestamp", ROOT / "scripts/release_check.py")
    epoch = 1793607874
    isolated_release(checker, tmp_path, monkeypatch, {"version": "1.2.3", "source_date_epoch": epoch})
    readme = tmp_path / "README.md"
    readme.write_text("Synthetic release content\n")
    checker.run(True)
    archive = tmp_path / "dist/canvas-connector-1.2.3-source.zip"
    original = archive.read_bytes()
    # Local checkout/build timing is not a release input.
    readme.touch()
    checker.run(True)
    assert archive.read_bytes() == original
    with zipfile.ZipFile(archive) as bundle:
        expected = checker.release_timestamp()
        assert {info.date_time for info in bundle.infolist()} == {expected}
        manifest = json.loads(bundle.read("canvas-connector/RELEASE-MANIFEST.json"))
        assert set(bundle.namelist()) == {"canvas-connector/" + name for name in manifest} | {
            "canvas-connector/RELEASE-MANIFEST.json"}
        for name, digest in manifest.items():
            assert hashlib.sha256(bundle.read("canvas-connector/" + name)).hexdigest() == digest
    (tmp_path / "packaging/release.json").write_text(json.dumps({"version": "1.2.3", "source_date_epoch": epoch + 2}))
    checker.run(True)
    with zipfile.ZipFile(archive) as bundle:
        assert all(info.date_time > expected for info in bundle.infolist())


def test_changed_client_version_and_local_source_cache_inputs_are_consistent():
    client = tomllib.loads((ROOT / "packages/canvas-py/pyproject.toml").read_text())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "canvas-py==" + client["project"]["version"] in project["project"]["dependencies"]
    assert '__version__ = "' + client["project"]["version"] + '"' in (
        ROOT / "packages/canvas-py/src/canvas_py/__init__.py").read_text()
    for name in ("canvas-py", "canvas-mcp-v2", "canvas-tunnel"):
        package = tomllib.loads((ROOT / "packages" / name / "pyproject.toml").read_text())
        assert package["tool"]["uv"]["cache-keys"] == [
            {"file": "pyproject.toml"}, {"file": "README.md"}, {"file": "src/**/*.py"},
            {"git": {"commit": True}}]
    epoch = json.loads((ROOT / "packaging/release.json").read_text())["source_date_epoch"]
    assert datetime.fromtimestamp(epoch, UTC).year >= 2026


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
