"""Tests for config loading."""
from __future__ import annotations

import pytest

from canvas_py._config import load_config


def test_load_config_from_explicit_args():
    cfg = load_config(base_url="https://example.instructure.com", token="abc123")
    assert cfg.base_url == "https://example.instructure.com"
    assert cfg.token == "abc123"
    assert cfg.api_base == "https://example.instructure.com/api/v1"


def test_load_config_strips_trailing_slash():
    cfg = load_config(base_url="https://example.instructure.com/", token="t")
    assert cfg.base_url == "https://example.instructure.com"
    assert cfg.api_base == "https://example.instructure.com/api/v1"


def test_load_config_from_env(monkeypatch):
    monkeypatch.setenv("CANVAS_BASE_URL", "https://env.instructure.com")
    monkeypatch.setenv("CANVAS_API_TOKEN", "env_token")
    cfg = load_config()
    assert cfg.base_url == "https://env.instructure.com"
    assert cfg.token == "env_token"


def test_missing_base_url_raises():
    with pytest.raises(ValueError, match="CANVAS_BASE_URL"):
        load_config(token="t")


def test_missing_token_raises():
    with pytest.raises(ValueError, match="CANVAS_API_TOKEN"):
        load_config(base_url="https://x.instructure.com")


def test_explicit_args_override_env(monkeypatch):
    monkeypatch.setenv("CANVAS_BASE_URL", "https://env.instructure.com")
    monkeypatch.setenv("CANVAS_API_TOKEN", "env_token")
    cfg = load_config(base_url="https://override.instructure.com", token="override_token")
    assert cfg.base_url == "https://override.instructure.com"
    assert cfg.token == "override_token"


def test_selected_file_is_authoritative_and_does_not_mutate_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CANVAS_BASE_URL", "https://stale.test")
    monkeypatch.setenv("CANVAS_API_TOKEN", "stale-token")
    selected = tmp_path / "selected.env"
    selected.write_text("CANVAS_BASE_URL=https://selected.test\nCANVAS_API_TOKEN=selected-token\n")
    cfg = load_config(env_path=selected)
    assert (cfg.base_url, cfg.token) == ("https://selected.test", "selected-token")
    import os
    assert os.environ["CANVAS_BASE_URL"] == "https://stale.test"
    assert os.environ["CANVAS_API_TOKEN"] == "stale-token"


def test_environment_path_selected_without_ambient_credentials(tmp_path, monkeypatch):
    selected = tmp_path / "selected.env"
    selected.write_text("CANVAS_BASE_URL=https://selected.test\nCANVAS_API_TOKEN=selected-token\n")
    monkeypatch.setenv("CANVAS_ENV_PATH", str(selected))
    assert load_config().token == "selected-token"


def test_explicit_path_and_arguments_win(tmp_path, monkeypatch):
    selected = tmp_path / "selected.env"
    selected.write_text("CANVAS_BASE_URL=https://selected.test\nCANVAS_API_TOKEN=selected-token\n")
    monkeypatch.setenv("CANVAS_ENV_PATH", str(tmp_path / "missing"))
    cfg = load_config(env_path=selected, base_url="https://explicit.test", token="explicit-token")
    assert (cfg.base_url, cfg.token) == ("https://explicit.test", "explicit-token")


def test_missing_selected_file_fails_even_with_ambient_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("CANVAS_BASE_URL", "https://stale.test")
    monkeypatch.setenv("CANVAS_API_TOKEN", "stale-token")
    with pytest.raises(ValueError, match="Selected Canvas configuration file"):
        load_config(env_path=tmp_path / "missing.env")


def test_incomplete_selected_file_cannot_mix_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("CANVAS_API_TOKEN", "stale-token")
    selected = tmp_path / "selected.env"
    selected.write_text("CANVAS_BASE_URL=https://selected.test\n")
    with pytest.raises(ValueError, match="CANVAS_API_TOKEN"):
        load_config(env_path=selected)


def test_cwd_dotenv_never_discovered(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("CANVAS_BASE_URL=https://unselected.test\nCANVAS_API_TOKEN=unselected-token\n")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="CANVAS_BASE_URL"):
        load_config()


def test_dotenv_never_interpolates_ambient_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("OTHER_SECRET", "ambient-secret")
    selected = tmp_path / "selected.env"
    selected.write_text("CANVAS_BASE_URL=https://selected.test\nCANVAS_API_TOKEN=${OTHER_SECRET}\n")
    assert load_config(env_path=selected).token == "${OTHER_SECRET}"


@pytest.mark.parametrize("base", [
    "http://canvas.test", "https://canvas.test/api/v1", "https://user:pass@canvas.test",
    "https://canvas.test?x=1", "https://canvas.test#x", "https://canvas.test:99999",
    "file:///tmp/x", "https://", "https://canvas.test\\@evil.test", " https://canvas.test",
])
def test_config_rejects_non_origin_urls(base):
    from canvas_py._config import CanvasConfig
    with pytest.raises(ValueError, match="CANVAS_BASE_URL"):
        CanvasConfig(base_url=base, token="secret")


def test_local_http_requires_explicit_development_option():
    with pytest.raises(ValueError):
        load_config(base_url="http://localhost:8000", token="secret")
    assert load_config(base_url="http://localhost:8000", token="secret", allow_insecure_localhost=True).api_base.endswith("/api/v1")
    with pytest.raises(ValueError):
        load_config(base_url="http://remote.test", token="secret", allow_insecure_localhost=True)


def test_config_repr_never_contains_token():
    cfg = load_config(base_url="https://canvas.test", token="must-stay-secret")
    assert "must-stay-secret" not in repr(cfg)
