import io
import sys
from types import SimpleNamespace

import pytest

from canvas_tunnel.bridge import child_environment, connector_parameters
from canvas_tunnel.transport import BoundedInput, BoundedOutput, MessageTooLarge


def test_child_environment_allowlist_preserves_only_explicit_local_settings():
    values = {"CANVAS_ENV_PATH": "/synthetic/config", "CANVAS_API_TOKEN": "synthetic",
              "OPENAI_API_KEY": "private", "OPENAI_ADMIN_KEY": "private",
              "MCP_TUNNEL_API_KEY": "private", "TUNNEL_AUTH_TOKEN": "private",
              "CONNECTOR_ENCRYPTION_KEY": "private", "HTTP_PROXY": "private",
              "OTEL_EXPORTER_OTLP_HEADERS": "private", "PYTHONPATH": "/untrusted",
              "PATH": "/usr/bin", "HOME": "/synthetic", "SHELL": "() { malicious; }"}
    result = child_environment(values)
    assert result == {"CANVAS_ENV_PATH": "/synthetic/config", "CANVAS_API_TOKEN": "synthetic",
                      "PATH": "/usr/bin", "HOME": "/synthetic",
                      "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"}


def test_cli_child_is_fixed_personal_stdio_and_writes_are_explicit():
    params = connector_parameters(sys.executable)
    assert params.command == sys.executable
    assert params.args == ["-m", "canvas_connector", "stdio"]
    assert connector_parameters(sys.executable, allow_writes=True).args[-1] == "--allow-writes"
    with pytest.raises(ValueError):
        connector_parameters("python")
    with pytest.raises(ValueError):
        connector_parameters("/does/not/exist")


def test_windows_launch_is_explicitly_unavailable(monkeypatch, capsys):
    import canvas_tunnel.__main__ as cli

    monkeypatch.setattr(cli, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(sys, "argv", ["canvas-connector-tunnel", "--connector-python", sys.executable])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "macOS and Linux only" in capsys.readouterr().err


@pytest.mark.parametrize("value", [b"x" * 11 + b"\n", "é" * 6, b"x" * 20])
async def test_input_bound_is_bytes_before_parse_and_does_not_read_entire_line(value):
    raw = value.encode() if isinstance(value, str) else value
    stream = io.BytesIO(raw)
    with pytest.raises(MessageTooLarge):
        await anext(BoundedInput(stream, maximum=10))
    assert stream.tell() <= 12


async def test_boundary_allows_exact_size_and_clean_eof():
    stream = BoundedInput(io.BytesIO(b"x" * 10 + b"\n"), maximum=10)
    assert await anext(stream) == "x" * 10 + "\n"
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


async def test_output_refuses_oversize_before_writing_any_bytes():
    stream = io.BytesIO()
    writer = BoundedOutput(stream, maximum=10)
    with pytest.raises(MessageTooLarge):
        await writer.write("é" * 6 + "\n")
    assert stream.getvalue() == b""
    await writer.write("x" * 10 + "\n")
    await writer.flush()
    assert stream.getvalue() == b"x" * 10 + b"\n"
