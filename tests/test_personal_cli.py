import sys

from cryptography.fernet import Fernet
import pytest

import canvas_connector.__main__ as cli
from canvas_connector.personal import PersonalProvider


@pytest.fixture
def environment(monkeypatch, tmp_path):
    database = tmp_path / "personal.db"
    monkeypatch.setenv("CONNECTOR_AUTH_MODE", "personal")
    monkeypatch.setenv("CONNECTOR_PUBLIC_URL", "https://connector.example")
    monkeypatch.setenv("CONNECTOR_DATABASE", str(database))
    monkeypatch.setenv("CONNECTOR_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("CONNECTOR_INSTITUTIONS_FILE", raising=False)
    monkeypatch.delenv("CONNECTOR_EDGE_SECRET", raising=False)
    return database


def terminal(monkeypatch, passwords):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "owner@example.test")
    values = iter(passwords)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: next(values))


def test_owner_command_requires_private_terminal(environment, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(ValueError, match="terminal"):
        cli.configure_personal_owner()
    assert not environment.exists()


def test_password_mismatch_does_not_open_store(environment, monkeypatch):
    terminal(monkeypatch, ["Synthetic-password-1!", "Synthetic-password-2!"])
    with pytest.raises(ValueError, match="match"):
        cli.configure_personal_owner()
    assert not environment.exists()


def test_operator_cli_and_runtime_use_same_owner_without_email(environment, monkeypatch, capsys):
    password = "Synthetic-owner-password-1!"
    terminal(monkeypatch, [password, password])
    cli.configure_personal_owner()
    provider = cli.provider_from_env()
    try:
        assert isinstance(provider, PersonalProvider)
        session = provider.accounts.login("owner@example.test", password, "synthetic")
        assert provider.accounts.session(session)
        owner_id = provider.owner_id
    finally:
        provider.store.close()
    replacement = "Synthetic-owner-password-2!"
    terminal(monkeypatch, [replacement, replacement])
    cli.configure_personal_owner(reset=True)
    provider = cli.provider_from_env()
    try:
        assert provider.owner_id == owner_id
        assert provider.accounts.session(session) is None
        assert provider.accounts.login("owner@example.test", replacement, "synthetic")
    finally:
        provider.store.close()
    output = capsys.readouterr()
    assert password not in output.out + output.err
    assert replacement not in output.out + output.err


def test_personal_runtime_refuses_missing_owner(environment):
    with pytest.raises(ValueError, match="owner"):
        cli.provider_from_env()


def test_personal_cli_refuses_independent_worker_gates(environment, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["canvas-connector", "serve", "--workers", "2"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "one worker" in capsys.readouterr().err
    assert not environment.exists()


def test_admin_errors_do_not_echo_sensitive_store_diagnostics(environment, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["canvas-connector", "owner-init"])
    def failure(**kwargs):
        raise RuntimeError("synthetic-private-secret-diagnostic")
    monkeypatch.setattr(cli, "configure_personal_owner", failure)
    with pytest.raises(SystemExit):
        cli.main()
    output = capsys.readouterr()
    assert "synthetic-private-secret-diagnostic" not in output.out + output.err


def test_cloudflare_personal_runtime_still_requires_signed_gateway(environment, monkeypatch):
    monkeypatch.setenv("CONNECTOR_DATABASE", "cloudflare://store")
    with pytest.raises(ValueError):
        cli.provider_from_env()
