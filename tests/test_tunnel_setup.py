"""Offline setup-runner tests: fake installers/client, synthetic secrets only."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import signal
import stat
import subprocess
import warnings

import pytest

SPEC = importlib.util.spec_from_file_location("personal_tunnel_setup", Path(__file__).parents[1] / "scripts/start_personal_tunnel.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)

CANVAS_KEY = "synthetic-canvas-secret"
OPENAI_KEY = "synthetic-runtime-secret"
TUNNEL_ID = "tunnel_" + "0" * 32


class FakeSystem:
    def __init__(self, tmp_path):
        self.base = tmp_path.resolve()
        self.home = self.base / "home with spaces"
        self.source = self.base / "source with spaces"
        self.home.mkdir(mode=0o700)
        self.source.mkdir()
        for name in ("requirements-runtime.lock", "packages/canvas-py/pyproject.toml",
                     "packages/canvas-mcp-v2/pyproject.toml", "packages/canvas-tunnel/uv.lock"):
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("Synthetic lock fixture\n")
        self.canvas_env = self.home / "private.env"
        self.canvas_env.write_text(f"CANVAS_BASE_URL='https://school.example'\nCANVAS_API_TOKEN='{CANVAS_KEY}'\n")
        self.canvas_env.chmod(0o600)
        self.env = {"HOME": str(self.home), "PATH": "/fake/bin", "CANVAS_ENV_PATH": str(self.canvas_env),
            "OPENAI_API_KEY": "ambient-key-must-not-be-used", "CANVAS_API_TOKEN": "ambient-canvas-must-not-be-used",
            "CONTROL_PLANE_BASE_URL": "https://evil.example", "MCP_COMMAND": "wrong-server", "LOG_HTTP_RAW_UNSAFE": "true",
            "CONNECTOR_SECRET": "private-hosted-value", "PYTHONPATH": "/wrong/imports", "UV_PROJECT_ENVIRONMENT": "/wrong/env",
            "HTTPS_PROXY": "https://user:password@proxy.invalid", "TRACING_ENABLED": "true"}
        self.calls = []
        self.prompts = []
        self.missing = None
        self.failed = None
        self.doctor = {"result": "ok", "checks": [], "evidence": OPENAI_KEY}
        self.help_omit = None
        self.exit_code = 0
        self.interrupt = False
        self.kills = []

    def which(self, name, path=None):
        return None if self.missing == name else f"/fake/bin/{name}"

    def secret(self, prompt):
        self.prompts.append(prompt)
        return CANVAS_KEY if "Canvas personal" in prompt else OPENAI_KEY

    def run(self, command, **kwargs):
        self.calls.append((list(command), {**kwargs, "env": dict(kwargs["env"])}))
        assert kwargs["stderr"] == subprocess.DEVNULL and kwargs["stdin"] == subprocess.DEVNULL
        assert "shell" not in kwargs
        stdout = b""
        if command[-1] == "--help":
            stdout = "\n".join(flag + " string" for flag in runner._REQUIRED_HELP[command[1]] if flag != self.help_omit).encode()
        elif self.failed and self.failed in command:
            return subprocess.CompletedProcess(command, 1, stdout=OPENAI_KEY.encode(), stderr=CANVAS_KEY.encode())
        elif "venv" in command:
            core = Path(command[-1])
            (core / "bin").mkdir(parents=True, mode=0o700)
            (core / "bin/python").write_text("synthetic executable")
            (core / "bin/python").chmod(0o700)
        elif "sync" in command and "--project" in command:
            facade = Path(kwargs["env"]["UV_PROJECT_ENVIRONMENT"])
            (facade / "bin").mkdir(parents=True, mode=0o700, exist_ok=True)
            (facade / "bin/python").write_text("synthetic executable")
            (facade / "bin/python").chmod(0o700)
        elif "init" in command:
            directory = Path(command[command.index("--profile-dir") + 1])
            name = command[command.index("--profile") + 1]
            assert "--force" not in command
            profile = directory / (name + ".yaml")
            assert not profile.exists()
            profile.write_text("config_version: 1\napi_key: env:CONTROL_PLANE_API_KEY\ncommand: " +
                               command[command.index("--mcp-command") + 1] + "\n")
            profile.chmod(0o600)
        elif "doctor" in command:
            stdout = json.dumps(self.doctor).encode() if not isinstance(self.doctor, bytes) else self.doctor
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=CANVAS_KEY.encode())

    def popen(self, command, **kwargs):
        self.calls.append((list(command), {**kwargs, "env": dict(kwargs["env"])}))
        assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["start_new_session"] is True and kwargs["stdin"] == subprocess.DEVNULL
        system = self

        class Child:
            pid = 123456

            def wait(self, timeout=None):
                if system.interrupt:
                    system.interrupt = False
                    raise KeyboardInterrupt
                return system.exit_code

        return Child()

    def execute(self, *options):
        args = runner._parser().parse_args(["--profile", "my-canvas", "--tunnel-id", TUNNEL_ID, *options])
        return runner.setup(args, source_root=self.source, home=self.home, environ=self.env)

    @property
    def profiles(self):
        return self.home / ".config/canvas-connector/tunnel-profiles"

    @property
    def runtime(self):
        return self.home / ".local/share/canvas-connector/tunnel-runtime/my-canvas"


@pytest.fixture
def system(tmp_path, monkeypatch):
    instance = FakeSystem(tmp_path)
    monkeypatch.setattr(runner.shutil, "which", instance.which)
    monkeypatch.setattr(runner.subprocess, "run", instance.run)
    monkeypatch.setattr(runner.subprocess, "Popen", instance.popen)
    monkeypatch.setattr(runner, "_secret", instance.secret)
    monkeypatch.setattr(runner.os, "killpg", lambda pid, sig: instance.kills.append((pid, sig)))
    return instance


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_offline_complete_setup_uses_isolated_exact_envs_private_profile_and_no_secret_arguments(system, monkeypatch, capsys, platform):
    monkeypatch.setattr(runner.sys, "platform", platform)
    assert system.execute() == 0
    commands = [command for command, _ in system.calls]
    install = next(command for command in commands if "install" in command)
    assert "--no-deps" in install and "--no-config" in install
    assert install[-3:] == [str(system.source / "packages/canvas-py"), str(system.source / "packages/canvas-mcp-v2"), str(system.source)]
    sync = next(command for command in commands if "sync" in command and "pip" in command)
    assert sync[-1] == str(system.source / "requirements-runtime.lock")
    facade, facade_options = next((command, options) for command, options in system.calls if "--project" in command)
    assert facade[1:3] == ["--no-config", "sync"] and "--locked" in facade and "--no-editable" in facade
    assert facade_options["env"]["UV_PROJECT_ENVIRONMENT"] == str(system.runtime / "facade")
    init = next(command for command in commands if "init" in command and "--help" not in command)
    launch = shlex.split(init[init.index("--mcp-command") + 1])
    assert launch == [str(system.runtime / "facade/bin/python"), "-m", "canvas_tunnel", "--connector-python", str(system.runtime / "core/bin/python")]
    assert init[init.index("--health-listen-addr") + 1] == "127.0.0.1:0"
    assert init[init.index("--control-plane-api-key-ref") + 1] == "env:CONTROL_PLANE_API_KEY"
    for command, options in system.calls:
        assert CANVAS_KEY not in str(command) and OPENAI_KEY not in str(command)
        if command[1] in {"doctor", "run"} and "--help" not in command:
            assert options["env"]["CONTROL_PLANE_API_KEY"] == OPENAI_KEY
            assert options["env"]["CANVAS_ENV_PATH"] == str(system.canvas_env)
            assert command[command.index("--mcp.stdio-failure-policy") + 1] == "exit"
        else:
            assert "CONTROL_PLANE_API_KEY" not in options["env"]
        assert not any(key in options["env"] for key in ("OPENAI_API_KEY", "CANVAS_API_TOKEN", "CONNECTOR_SECRET", "PYTHONPATH", "HTTPS_PROXY", "MCP_COMMAND"))
    for path in system.profiles.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert CANVAS_KEY not in path.read_text() and OPENAI_KEY not in path.read_text()
    assert stat.S_IMODE(system.profiles.stat().st_mode) == 0o700
    output = capsys.readouterr()
    assert CANVAS_KEY not in output.out + output.err and OPENAI_KEY not in output.out + output.err
    assert runner.PLATFORM_URL in output.out and runner.PLUGINS_URL in output.out
    assert "writes are disabled" in output.out and "one Canvas identity" in output.out
    assert "do not prove hosted connectivity" in output.out


def test_writes_require_explicit_flag_and_match_saved_profile_on_reuse(system):
    system.execute("--allow-writes", "--setup-only")
    init = next(command for command, _ in system.calls if "init" in command and "--help" not in command)
    assert shlex.split(init[init.index("--mcp-command") + 1])[-1] == "--allow-writes"
    with pytest.raises(runner.SetupError, match="settings differ"):
        system.execute("--setup-only")
    assert system.execute("--allow-writes", "--setup-only") == 0
    assert len([c for c, _ in system.calls if "init" in c and "--help" not in c]) == 1


def test_setup_only_never_requests_runtime_key_or_launches_client(system):
    assert system.execute("--setup-only") == 0
    assert not system.prompts
    assert not any(command[1] in {"doctor", "run"} and "--help" not in command for command, _ in system.calls)


def test_resume_uses_only_unchanged_runner_owned_profile_without_overwriting(system):
    system.execute("--setup-only")
    original = {path.name: path.read_bytes() for path in system.profiles.iterdir()}
    args = runner._parser().parse_args(["--profile", "my-canvas", "--setup-only"])
    assert runner.setup(args, source_root=system.source, home=system.home, environ=system.env) == 0
    assert {path.name: path.read_bytes() for path in system.profiles.iterdir()} == original
    assert len([c for c, _ in system.calls if "init" in c and "--help" not in c]) == 1


@pytest.mark.parametrize("case", ["foreign", "modified", "manifest", "runtime"])
def test_foreign_modified_or_unowned_profile_and_runtime_are_never_overwritten(system, case):
    if case == "foreign":
        system.profiles.mkdir(parents=True, mode=0o700)
        target = system.profiles / "my-canvas.yaml"
        target.write_text("foreign profile")
    elif case == "runtime":
        system.runtime.mkdir(parents=True)
        target = system.runtime / "foreign.txt"
        target.write_text("foreign runtime")
    else:
        system.execute("--setup-only")
        target = system.profiles / ("my-canvas.yaml" if case == "modified" else "my-canvas.runner.json")
        target.write_text("modified profile" if case == "modified" else "[]")
    before = target.read_bytes()
    system.calls.clear()
    with pytest.raises(runner.SetupError):
        system.execute("--setup-only")
    assert target.read_bytes() == before
    assert not any(("init" in command or "sync" in command) and "--help" not in command for command, _ in system.calls)


def test_failed_install_can_resume_only_its_matching_owned_runtime(system):
    system.failed = "install"
    with pytest.raises(runner.SetupError, match="installation failed"):
        system.execute("--setup-only")
    assert (system.runtime / "runner.json").exists() and not (system.profiles / "my-canvas.yaml").exists()
    system.failed = None
    assert system.execute("--setup-only") == 0


def test_explicit_existing_runtime_environment_reference_does_not_prompt_or_persist_key(system):
    system.env["OPENAI_API_KEY"] = OPENAI_KEY
    assert system.execute("--runtime-key-env", "OPENAI_API_KEY") == 0
    assert not system.prompts
    assert all(OPENAI_KEY not in path.read_text() for path in system.profiles.iterdir())


def test_missing_explicit_environment_key_is_not_replaced_by_another_secret(system):
    system.env.pop("OPENAI_API_KEY")
    with pytest.raises(runner.SetupError, match="nonempty single-line"):
        system.execute("--runtime-key-env", "OPENAI_API_KEY")
    assert not system.prompts
    assert not any(c[1] == "run" and "--help" not in c for c, _ in system.calls)


@pytest.mark.parametrize("name", ["uv", "tunnel-client"])
def test_missing_prerequisite_gives_official_guidance_without_installing_or_registering(system, name):
    system.missing = name
    with pytest.raises(runner.SetupError, match=f"Install {name} first"):
        system.execute()
    assert not system.runtime.exists() and not system.profiles.exists()


def test_old_client_help_cannot_mistake_profile_dir_for_required_profile_flag(system):
    system.help_omit = "--profile"
    with pytest.raises(runner.SetupError, match="lacks required"):
        system.execute()
    assert not system.runtime.exists()


def test_windows_is_explicitly_unsupported_before_any_process(system, monkeypatch):
    monkeypatch.setattr(runner.sys, "platform", "win32")
    with pytest.raises(runner.SetupError, match="macOS and Linux"):
        system.execute()
    assert not system.calls


@pytest.mark.parametrize("tunnel_id", ["tunnel_short", "tunnel_" + "a" * 31, "tunnel_" + "a" * 33,
    "tunnel_" + "A" * 32, "tunnel_abc_" + "a" * 32, "tunnel_abcde_" + "a" * 32,
    "tunnel_ab-c_" + "a" * 32, "tunnel_" + "-" * 32, "tunnel_" + "a" * 32 + "/", "https://example.com/tunnel"])
def test_native_tunnel_id_grammar_rejected_before_config_or_expensive_install(system, tunnel_id):
    with pytest.raises(runner.SetupError, match="existing tunnel ID"):
        system.execute("--tunnel-id", tunnel_id, "--setup-only")
    assert not system.runtime.exists() and not system.profiles.exists()
    assert not any("venv" in command or "sync" in command for command, _ in system.calls)


@pytest.mark.parametrize("tunnel_id", [TUNNEL_ID, "tunnel_z9x1_" + "a1" * 16])
def test_native_legacy_and_namespaced_tunnel_ids_are_supported(system, tunnel_id):
    assert system.execute("--tunnel-id", tunnel_id, "--setup-only") == 0
    metadata = json.loads((system.profiles / "my-canvas.runner.json").read_text())
    assert metadata["tunnel_id"] == tunnel_id


@pytest.mark.parametrize("profile", ["../foreign", ".", "space name", "bad\nname", "-leading", "a" * 41])
def test_invalid_profile_name_is_not_used_in_paths_or_commands(system, profile):
    args = runner._parser().parse_args(["--profile", profile]) if not profile.startswith("-") else None
    with pytest.raises(runner.SetupError):
        if args:
            runner.setup(args, source_root=system.source, home=system.home, environ=system.env)
        else:
            runner._parser().parse_args(["--profile", profile])
    assert not system.calls


def test_create_missing_canvas_file_is_exclusive_private_outside_source_and_runtime(system, monkeypatch):
    system.env.pop("CANVAS_ENV_PATH")
    monkeypatch.setattr("builtins.input", lambda prompt: "https://school.example/")
    assert system.execute("--setup-only") == 0
    created = system.home / ".config/canvas-connector/canvas/my-canvas.env"
    assert created.read_text() == f"CANVAS_BASE_URL='https://school.example'\nCANVAS_API_TOKEN='{CANVAS_KEY}'\n"
    assert stat.S_IMODE(created.stat().st_mode) == 0o600
    assert stat.S_IMODE(created.parent.stat().st_mode) == 0o700
    before = created.read_bytes()
    system.execute("--setup-only")
    assert created.read_bytes() == before
    assert len(system.prompts) == 1


@pytest.mark.parametrize("case", ["source", "runtime", "symlink-file", "symlink-parent", "world-readable", "hardlink"])
def test_canvas_file_privacy_boundaries_fail_without_copy_or_install(system, case):
    if case == "source":
        path = system.source / "secret.env"
    elif case == "runtime":
        path = system.runtime / "secret.env"
    elif case == "symlink-file":
        path = system.home / "linked.env"
        path.symlink_to(system.canvas_env)
    elif case == "symlink-parent":
        parent = system.home / "linked"
        parent.symlink_to(system.home, target_is_directory=True)
        path = parent / "new.env"
    elif case == "hardlink":
        path = system.home / "hard.env"
        os.link(system.canvas_env, path)
    else:
        path = system.canvas_env
        path.chmod(0o644)
    with pytest.raises(runner.SetupError):
        system.execute("--canvas-env", str(path), "--setup-only")
    assert not any(("sync" in command or "init" in command) and "--help" not in command for command, _ in system.calls)


def test_new_file_creation_rechecks_parent_with_no_follow_openat(system, monkeypatch):
    parent = system.home / "safe"
    parent.mkdir(mode=0o700)
    target = parent / "secret.env"
    # Even a replaced parent after the initial path check cannot redirect the write.
    monkeypatch.setattr(runner, "_no_symlinks", lambda path: None)
    parent.rmdir()
    parent.symlink_to(system.source, target_is_directory=True)
    with pytest.raises(OSError):
        runner._exclusive_file(target, CANVAS_KEY.encode())
    assert not (system.source / "secret.env").exists()


def test_exclusive_create_never_overwrites_existing_secret(system):
    before = system.canvas_env.read_bytes()
    with pytest.raises(runner.SetupError, match="not overwritten"):
        runner._exclusive_file(system.canvas_env, b"replacement")
    assert system.canvas_env.read_bytes() == before


@pytest.mark.parametrize("url", ["http://school.example", "https://u:p@school.example", "https://school.example/courses",
    "https://school.example?token=secret", "https://school.example/#fragment", "https://school.example:99999", "https://bad host/"])
def test_canvas_origin_rejects_credentialed_or_non_origin_values(url):
    with pytest.raises(runner.SetupError):
        runner._canvas_origin(url)


@pytest.mark.parametrize("value", ["", "two words", "line\nbreak", "x" * 8193, None])
def test_secret_validation_does_not_echo_input(value):
    with pytest.raises(runner.SetupError) as error:
        runner._valid_secret(value)
    if value:
        assert value not in str(error.value)


def test_getpass_refuses_echo_fallback_and_nonterminal(monkeypatch):
    monkeypatch.setattr(runner.sys.stdin, "isatty", lambda: False)
    with pytest.raises(runner.SetupError, match="local terminal"):
        runner._secret("hidden: ")
    monkeypatch.setattr(runner.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(runner.sys.stderr, "isatty", lambda: True)

    def warning(prompt):
        warnings.warn("Cannot hide input", runner.getpass.GetPassWarning)
        raise AssertionError("Must never proceed to echoed fallback")

    monkeypatch.setattr(runner.getpass, "getpass", warning)
    with pytest.raises(runner.SetupError, match="cannot hide"):
        runner._secret("hidden: ")


@pytest.mark.parametrize("report", [{"result": "fail"}, {"result": "unknown"}, [], b"not-json"])
def test_unknown_or_failed_doctor_report_never_starts_tunnel_or_prints_output(system, report, capsys):
    system.doctor = report
    with pytest.raises(runner.SetupError):
        system.execute()
    assert not any(command[1] == "run" and "--help" not in command for command, _ in system.calls)
    assert OPENAI_KEY not in capsys.readouterr().out


def test_subprocess_failure_and_unsupported_arguments_never_echo_secrets(system, capsys):
    system.failed = "doctor"
    with pytest.raises(runner.SetupError) as error:
        system.execute()
    assert OPENAI_KEY not in str(error.value) and CANVAS_KEY not in str(error.value)
    with pytest.raises(runner.SetupError) as error:
        runner._parser().parse_args(["--profile", "ok", "--api-key", OPENAI_KEY])
    assert OPENAI_KEY not in str(error.value)
    output = capsys.readouterr()
    assert OPENAI_KEY not in output.out + output.err and CANVAS_KEY not in output.out + output.err


def test_interrupt_stops_entire_tunnel_process_group_without_replaying(system, capsys):
    system.interrupt = True
    assert system.execute() == 0
    assert system.kills == [(123456, signal.SIGTERM)]
    assert len([c for c, _ in system.calls if c[1] == "run" and "--help" not in c]) == 1
    assert "Pending Canvas writes may have completed" in capsys.readouterr().out


def test_runtime_failure_is_sanitized_and_not_restarted(system):
    system.exit_code = 2
    with pytest.raises(runner.SetupError, match="do not automatically repeat"):
        system.execute()
    assert len([c for c, _ in system.calls if c[1] == "run" and "--help" not in c]) == 1
