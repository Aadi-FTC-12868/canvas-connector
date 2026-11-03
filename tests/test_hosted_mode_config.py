import json
from types import SimpleNamespace

import pytest

from canvas_connector.__main__ import provider_from_env


@pytest.fixture
def configured(monkeypatch):
    for name in ("CONNECTOR_AUTH_MODE", "CONNECTOR_INSTITUTIONS_FILE", "CONNECTOR_ACCOUNT_MAIL", "CONNECTOR_EDGE_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CONNECTOR_PUBLIC_URL", "https://canvas.example.test")
    monkeypatch.setenv("CONNECTOR_DATABASE", "cloudflare://store")
    monkeypatch.setenv("CONNECTOR_ENCRYPTION_KEY", "synthetic-record-encryption-key")
    monkeypatch.setenv("CONNECTOR_EDGE_SECRET", "synthetic-separate-edge-secret-123456")
    return monkeypatch


def test_default_institution_mode_keeps_its_explicit_config_requirement(configured):
    with pytest.raises(ValueError, match="Institution mode requires"):
        provider_from_env()


@pytest.mark.parametrize("mode", ["", "personal-hosted", "built-by-aadi", "unknown"])
def test_unknown_modes_do_not_silently_select_personal_token_hosting(configured, mode):
    configured.setenv("CONNECTOR_AUTH_MODE", mode)
    with pytest.raises(ValueError, match="Unknown hosted authentication mode"):
        provider_from_env()


@pytest.mark.parametrize("database", ["/tmp/ephemeral.sqlite3", "postgresql://other.example/db", "cloudflare://other"])
def test_builtbyaadi_mode_cannot_move_credentials_to_an_unapproved_backend(configured, database):
    configured.setenv("CONNECTOR_AUTH_MODE", "builtbyaadi")
    configured.setenv("CONNECTOR_DATABASE", database)
    with pytest.raises(ValueError, match="Cloudflare durable store"):
        provider_from_env()


@pytest.mark.parametrize("secret", [None, "short", "synthetic-record-encryption-key"])
def test_builtbyaadi_requires_separate_strong_gateway_secret(configured, secret):
    configured.setenv("CONNECTOR_AUTH_MODE", "builtbyaadi")
    if secret is None:
        configured.delenv("CONNECTOR_EDGE_SECRET")
    else:
        configured.setenv("CONNECTOR_EDGE_SECRET", secret)
    with pytest.raises(ValueError):
        provider_from_env()


@pytest.mark.parametrize("mail", [None, "disabled", "cloudflare"])
def test_builtbyaadi_mode_has_no_institution_file_and_mail_is_explicit(configured, mail):
    import canvas_connector.broker as broker
    from canvas_connector.cloudflare_mail import CloudflareMailer
    configured.setenv("CONNECTOR_AUTH_MODE", "builtbyaadi")
    if mail is not None:
        configured.setenv("CONNECTOR_ACCOUNT_MAIL", mail)
    captured = {}
    def make(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(**kwargs)
    configured.setattr(broker, "BuiltByAadiProvider", make)
    provider_from_env()
    assert captured["database_path"] == "cloudflare://store"
    assert "institutions" not in captured
    assert isinstance(captured["mailer"], CloudflareMailer) if mail == "cloudflare" else captured["mailer"] is None


def test_institution_factory_remains_separate(configured, tmp_path):
    import canvas_connector.auth as auth
    config = tmp_path / "institutions.json"
    config.write_text(json.dumps([{"origin": "https://school.example.test", "client_id": "test-client", "client_secret": "test-secret"}]))
    configured.setenv("CONNECTOR_INSTITUTIONS_FILE", str(config))
    configured.setenv("CONNECTOR_DATABASE", str(tmp_path / "institution.sqlite3"))
    configured.setattr(auth, "CanvasOAuthProvider", lambda **kwargs: kwargs)
    value = provider_from_env()
    assert value["institutions"][0].origin == "https://school.example.test"
    assert "mailer" not in value
