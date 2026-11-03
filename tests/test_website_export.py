import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def exporter(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("website_export_test", scripts / "export_website.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_new_export_defaults_offline_and_hosting_choice_survives_version_change(exporter, tmp_path):
    config = tmp_path / "connection.json"
    assert exporter.hosting_config(config) == {"hosted": False, "endpoint": None}
    existing = {"hosted": True, "endpoint": "https://canvas.example.com/mcp", "auth_mode": "builtbyaadi",
                "account_setup_available": True, "release": "old", "source_url": "./old.zip"}
    config.write_text(json.dumps(existing))
    assert exporter.hosting_config(config) == {k: v for k, v in existing.items() if k not in {"release", "source_url"}}
    assert exporter.hosting_config(None) == {"hosted": False, "endpoint": None}


@pytest.mark.parametrize("change", [
    {"token": "synthetic-private-token"}, {"endpoint": "http://canvas.example.com/mcp"},
    {"endpoint": "https://canvas.example.com/mcp?token=secret"},
    {"endpoint": "https://user:pass@canvas.example.com/mcp"},
    {"endpoint": "https://127.0.0.1/mcp"}, {"endpoint": "https://[::1]/mcp"},
    {"endpoint": "https://canvas.internal/mcp"}, {"endpoint": "https://localhost/mcp"},
    {"endpoint": "https://canvas.example.com:444/mcp"},
    {"hosted": "true"}, {"auth_mode": "unknown"}, {"account_setup_available": "false"},
])
def test_private_invalid_or_ambiguous_hosting_config_is_not_exported(exporter, tmp_path, change):
    config = tmp_path / "connection.json"
    config.write_text(json.dumps({"hosted": True, "endpoint": "https://canvas.example.com/mcp", **change}))
    with pytest.raises(ValueError):
        exporter.hosting_config(config)


def test_disabled_hosting_cannot_keep_a_stale_public_endpoint(exporter, tmp_path):
    config = tmp_path / "connection.json"
    config.write_text(json.dumps({"hosted": False, "endpoint": "https://canvas.example.com/mcp"}))
    with pytest.raises(ValueError):
        exporter.hosting_config(config)
