#!/usr/bin/env python3
"""Optional personal setup runner; standard-library bootstrap for macOS/Linux.

This creates only local private configuration and isolated Python environments.
It never creates a hosted tunnel or edits an assistant's normal configuration.
The user creates the tunnel in Platform and connects it in ChatGPT Plugins.
Source contract: https://developers.openai.com/api/docs/guides/secure-mcp-tunnels
and tunnel-client v0.0.19 init/doctor/run help and sample_mcp_stdio_local.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
from urllib.parse import urlsplit
import warnings

PLATFORM_URL = "https://platform.openai.com/settings/organization/tunnels"
PLUGINS_URL = "https://chatgpt.com/plugins"
DOCS_URL = "https://developers.openai.com/api/docs/guides/secure-mcp-tunnels"
UV_URL = "https://docs.astral.sh/uv/getting-started/installation/"
_BASE_ENV = frozenset({"HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "TERM",
    "TMPDIR", "TMP", "TEMP", "SSL_CERT_FILE", "SSL_CERT_DIR"})
_REQUIRED_HELP = {
    "init": ("--profile", "--profile-dir", "--sample", "--tunnel-id", "--mcp-command",
             "--control-plane-api-key-ref", "--health-listen-addr"),
    "doctor": ("--json", "--profile", "--profile-dir", "--mcp.stdio-failure-policy"),
    "run": ("--profile", "--profile-dir", "--mcp.stdio-failure-policy", "--log.http-raw-unsafe", "--tracing.enabled"),
}


class SetupError(Exception):
    """Messages are fixed local guidance, never upstream output or secrets."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes unknown argument values, possibly pasted keys.
        raise SetupError("Invalid command options. Use --help; enter secrets only at hidden prompts.")


def _parser():
    parser = _Parser(description="Start a private, single-user Canvas tunnel on macOS or Linux.")
    parser.add_argument("--profile", required=True, help="Explicit local profile name, e.g. my-canvas")
    parser.add_argument("--tunnel-id", help="Existing tunnel ID from Platform; prompted when creating a profile")
    parser.add_argument("--canvas-env", help="Existing private Canvas env file, or a new file outside source/runtime trees")
    parser.add_argument("--allow-writes", action="store_true", help="Explicitly enable reviewed Canvas actions; off by default")
    parser.add_argument("--runtime-key-env", choices=("CONTROL_PLANE_API_KEY", "OPENAI_API_KEY"),
                        help="Explicitly reuse this existing environment secret instead of prompting")
    parser.add_argument("--setup-only", action="store_true", help="Prepare local environments/profile without asking for a runtime key or starting a tunnel")
    return parser


def _path(value, home):
    value = str(value)
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise SetupError("Choose a valid local path without control characters.")
    if value == "~" or value.startswith("~/"):
        value = str(home) + value[1:]
    path = Path(value)
    if not path.is_absolute():
        raise SetupError("Use an absolute path for private configuration and XDG directories.")
    return Path(os.path.abspath(path))


def _no_symlinks(path):
    for candidate in [*reversed(path.parents), path]:
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise SetupError("Private setup paths must not traverse symlinks.")


def _outside(path, roots):
    _no_symlinks(path)
    if any(path == root or root in path.parents for root in roots):
        raise SetupError("Canvas credentials must be outside repository/source and runtime trees.")


def _private_file(path):
    _no_symlinks(path)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 1024 * 1024):
        raise SetupError("Use a regular owner-only private file (chmod 600), owned by the current user, without links.")


def _directory_fd(path, *, create=False):
    """Walk each absolute directory component with no-follow openat calls."""
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _private_dir(path):
    _no_symlinks(path)
    descriptor = _directory_fd(path, create=True)
    try:
        info = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SetupError("The dedicated setup directory must be owner-only (chmod 700) and owned by the current user.")


def _exclusive_file(path, data):
    _no_symlinks(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    parent = _directory_fd(path.parent)
    try:
        descriptor = os.open(path.name, flags, 0o600, dir_fd=parent)
    except FileExistsError as exc:
        raise SetupError("A setup file already exists; it was not overwritten.") from exc
    finally:
        os.close(parent)
    with os.fdopen(descriptor, "wb") as file:
        file.write(data)


def _environment(source):
    return {**{key: value for key, value in source.items() if key in _BASE_ENV and not value.startswith("()")},
            "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "UV_NO_PROGRESS": "1"}


def _quiet(command, env, stage, *, capture=False, timeout=900, cwd=None):
    try:
        result = subprocess.run(command, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetupError(f"{stage} could not complete. Check the local installation and try again; subprocess output was suppressed.") from exc
    if result.returncode:
        raise SetupError(f"{stage} failed. Check the local installation or configuration; subprocess output was suppressed.")
    output = result.stdout or b""
    if len(output) > 1024 * 1024:
        raise SetupError(f"{stage} returned unexpectedly large output.")
    return output.decode("utf-8", errors="replace")


def _prerequisites(env):
    found = {}
    for name, url in (("uv", UV_URL), ("tunnel-client", PLATFORM_URL)):
        executable = shutil.which(name, path=env.get("PATH"))
        if not executable:
            raise SetupError(f"Install {name} first, then run this command again. Official setup: {url}")
        found[name] = str(Path(executable).absolute())
    for subcommand, required in _REQUIRED_HELP.items():
        help_text = _quiet([found["tunnel-client"], subcommand, "--help"], env,
                           "Tunnel client compatibility check", capture=True, timeout=30)
        if any(not re.search(r"(?<![\w-])" + re.escape(flag) + r"(?=[\s=]|$)", help_text) for flag in required):
            raise SetupError(f"This tunnel-client lacks required setup options. Update it using {PLATFORM_URL}")
    return found["uv"], found["tunnel-client"]


def _secret(prompt):
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise SetupError("Hidden credential prompts require a local terminal. Never paste tokens into chat or command arguments.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            value = getpass.getpass(prompt)
    except getpass.GetPassWarning as exc:
        raise SetupError("This terminal cannot hide credential input. No token was read; use a secure local terminal.") from exc
    return _valid_secret(value)


def _valid_secret(value):
    if not isinstance(value, str) or not value or len(value) > 8192 or any(char.isspace() or ord(char) < 32 for char in value):
        raise SetupError("Enter a nonempty single-line token without whitespace at the hidden prompt.")
    return value


def _canvas_origin(value):
    try:
        parsed = urlsplit(value)
        parsed.port
    except (ValueError, TypeError) as exc:
        raise SetupError("Enter only your Canvas HTTPS origin, such as https://school.instructure.com.") from exc
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or "\\" in value
            or any(char.isspace() or ord(char) < 32 for char in value)):
        raise SetupError("Enter only your Canvas HTTPS origin, without credentials, a path, query or fragment.")
    return value.rstrip("/")


def _dotenv(value):
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _canvas_file(path, source_root, runtime_root):
    _outside(path, (source_root, runtime_root))
    print(f"Private Canvas settings file: {path}")
    if path.exists():
        _private_file(path)
        return
    print("Create a private Canvas configuration locally. Your Canvas token will be saved only in that owner-only file.")
    base = _canvas_origin(input("Canvas HTTPS origin: ").strip())
    token = _secret("Canvas personal access token (hidden): ")
    _private_dir(path.parent)
    content = f"CANVAS_BASE_URL={_dotenv(base)}\nCANVAS_API_TOKEN={_dotenv(token)}\n".encode()
    _exclusive_file(path, content)
    del token, content


def _install(source_root, runtime, uv, env):
    core, facade = runtime / "core", runtime / "facade"
    core_python, facade_python = core / "bin/python", facade / "bin/python"
    for directory in (core, facade):
        _no_symlinks(directory)
    print("Preparing separate core and tunnel Python environments. This can take a few minutes.")
    if not core.exists():
        _quiet([uv, "--no-config", "venv", "--python", "3.12", str(core)], env, "Core environment creation")
    _quiet([uv, "--no-config", "pip", "sync", "--python", str(core_python), str(source_root / "requirements-runtime.lock")],
           env, "Pinned core dependency installation")
    _quiet([uv, "--no-config", "pip", "install", "--python", str(core_python), "--no-deps",
            str(source_root / "packages/canvas-py"), str(source_root / "packages/canvas-mcp-v2"), str(source_root)],
           env, "Local core package installation")
    _quiet([uv, "--no-config", "sync", "--project", str(source_root / "packages/canvas-tunnel"), "--locked", "--no-editable", "--python", "3.12"],
           {**env, "UV_PROJECT_ENVIRONMENT": str(facade)}, "Isolated tunnel facade installation")
    for python in (core_python, facade_python):
        if not python.is_file() or not os.access(python, os.X_OK):
            raise SetupError("An isolated Python environment is incomplete; no tunnel was started.")
    return core_python, facade_python


def _manifest(path):
    _private_file(path)
    try:
        result = json.loads(path.read_text())
    except (ValueError, UnicodeError) as exc:
        raise SetupError("This runner's profile manifest is invalid; it was not changed.") from exc
    if not isinstance(result, dict):
        raise SetupError("This runner's profile manifest is invalid; it was not changed.")
    return result


def _start(command, env):
    print("Starting the outbound personal tunnel. Keep this terminal open; Ctrl-C stops it.")
    print(f"In ChatGPT, open {PLUGINS_URL}, choose Add custom MCP server, then Connection: Tunnel.")
    print("Select your tunnel and complete the account's access/authentication steps. Test a Canvas dashboard read there.")
    print("Local checks do not prove hosted connectivity or ChatGPT account access. Runtime logs are suppressed to protect credentials.")
    try:
        child = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        raise SetupError("The tunnel client could not start. No request was replayed.") from exc
    try:
        code = child.wait()
    except KeyboardInterrupt:
        try:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)
        except ProcessLookupError:
            pass
        print("Tunnel stopped. Pending Canvas writes may have completed; inspect Canvas before creating a replacement action.")
        return 0
    if code:
        raise SetupError("Tunnel stopped with an error. Check tunnel permissions, workspace association and network access. Pending Canvas writes may have completed; do not automatically repeat them.")
    print("Tunnel stopped. Re-run this command to start the same local profile.")
    return 0


def setup(args, *, source_root, home, environ):
    if sys.platform not in {"darwin", "linux"}:
        raise SetupError("This optional setup runner supports macOS and Linux only.")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,39}", args.profile):
        raise SetupError("Choose a profile name of 1–40 letters, digits, hyphens or underscores.")
    source_root = source_root.resolve()
    for required in ("requirements-runtime.lock", "packages/canvas-py/pyproject.toml",
                     "packages/canvas-mcp-v2/pyproject.toml", "packages/canvas-tunnel/uv.lock"):
        if not (source_root / required).is_file():
            raise SetupError("Run this script from the complete Canvas Connector source distribution.")
    env = _environment(environ)
    uv, tunnel = _prerequisites(env)
    config_root = _path(environ.get("XDG_CONFIG_HOME", str(home / ".config")), home) / "canvas-connector"
    runtime_root = _path(environ.get("XDG_DATA_HOME", str(home / ".local/share")), home) / "canvas-connector/tunnel-runtime"
    _outside(runtime_root, (source_root,))
    runtime = runtime_root / args.profile
    profiles = config_root / "tunnel-profiles"
    manifest_path = profiles / (args.profile + ".runner.json")
    profile_path = profiles / (args.profile + ".yaml")
    owner_path = runtime / "runner.json"
    for path in (runtime, profiles):
        _outside(path, (source_root,))
    _no_symlinks(manifest_path)
    _no_symlinks(profile_path)
    existing = _manifest(manifest_path) if manifest_path.exists() else None
    pending = _manifest(owner_path) if owner_path.exists() else None
    owned = existing or pending or {}
    if profile_path.exists() and not existing:
        raise SetupError("That profile already exists and was not created by this runner. Choose a new name; nothing was overwritten.")
    canvas_path = _path(args.canvas_env or environ.get("CANVAS_ENV_PATH") or
                        owned.get("canvas_env") or str(config_root / "canvas" / (args.profile + ".env")), home)
    tunnel_id = args.tunnel_id or owned.get("tunnel_id")
    print("This is a personal, single-user connection: anyone authorized to use this tunnel acts as your one Canvas identity.")
    print("Restrict tunnel access to yourself. It is not a hosted multi-user connector; account and workspace support must be checked separately.")
    print(f"Create/select a tunnel manually at {PLATFORM_URL} and associate the intended ChatGPT workspace.")
    print("Tunnel setup needs Platform Tunnels Read + Manage; runtime use needs Read + Use. ChatGPT custom-server permission is separate.")
    if not tunnel_id:
        tunnel_id = input("Existing Platform tunnel ID: ").strip()
    # Native runtimeconfig.ValidateTunnelID, tunnel-client v0.0.19 config.go:182.
    if not isinstance(tunnel_id, str) or not re.fullmatch(r"tunnel_(?:[a-z0-9]{4}_)?[a-z0-9]{32}", tunnel_id):
        raise SetupError("Enter the existing tunnel ID from Platform; no hosted tunnel was created.")
    metadata = {"runner": "canvas-personal-tunnel-v1", "profile": args.profile, "source_root": str(source_root),
                "runtime": str(runtime), "canvas_env": str(canvas_path), "tunnel_id": tunnel_id, "allow_writes": args.allow_writes}
    if pending is not None and pending != metadata:
        raise SetupError("The owned runtime settings differ. Use the original options or choose a new profile; nothing was overwritten.")
    if existing:
        if {key: value for key, value in existing.items() if key != "profile_sha256"} != metadata:
            raise SetupError("Existing runner profile settings differ. Use the original options or choose a new profile; nothing was overwritten.")
        _private_file(profile_path)
        if hashlib.sha256(profile_path.read_bytes()).hexdigest() != existing.get("profile_sha256"):
            raise SetupError("The existing profile was modified. It was not used or overwritten; choose a new profile.")
        _private_dir(runtime)
    elif runtime.exists() and not pending:
        raise SetupError("That runtime directory already exists without a matching runner profile. Choose a new name; nothing was overwritten.")
    _canvas_file(canvas_path, source_root, runtime_root)
    _private_dir(runtime)
    _private_dir(profiles)
    if pending is None:
        _exclusive_file(owner_path, (json.dumps(metadata, sort_keys=True, indent=2) + "\n").encode())
    core_python, facade_python = _install(source_root, runtime, uv, env)
    canvas_env = {**env, "CANVAS_ENV_PATH": str(canvas_path)}
    _quiet([str(core_python), "-c", "from canvas_py._config import load_config; load_config()"],
           canvas_env, "Local Canvas configuration validation", timeout=30)
    command = [str(facade_python), "-m", "canvas_tunnel", "--connector-python", str(core_python)]
    if args.allow_writes:
        command.append("--allow-writes")
    if not existing:
        _quiet([tunnel, "init", "--sample", "sample_mcp_stdio_local", "--profile", args.profile,
            "--profile-dir", str(profiles), "--tunnel-id", tunnel_id, "--mcp-command", shlex.join(command),
            "--control-plane-api-key-ref", "env:CONTROL_PLANE_API_KEY", "--health-listen-addr", "127.0.0.1:0"],
            env, "Local tunnel profile creation", timeout=30)
        _private_file(profile_path)
        metadata["profile_sha256"] = hashlib.sha256(profile_path.read_bytes()).hexdigest()
        _exclusive_file(manifest_path, (json.dumps(metadata, sort_keys=True, indent=2) + "\n").encode())
    print(f"Local profile {args.profile} is ready. Canvas writes are {'enabled through reviewed actions' if args.allow_writes else 'disabled'}.")
    if args.setup_only:
        print("Setup-only completed. No runtime key was requested and no tunnel was started. Re-run without --setup-only to connect.")
        return 0
    key = (_valid_secret(environ.get(args.runtime_key_env)) if args.runtime_key_env
           else _secret("OpenAI tunnel runtime API key (hidden; not saved): "))
    runtime_env = {**canvas_env, "CONTROL_PLANE_API_KEY": key}
    del key
    common = ["--profile", args.profile, "--profile-dir", str(profiles), "--mcp.stdio-failure-policy", "exit",
              "--log.http-raw-unsafe=false", "--tracing.enabled=false", "--allow-remote-ui=false", "--log.level", "warn"]
    try:
        report = _quiet([tunnel, "doctor", *common, "--json"], runtime_env, "Tunnel configuration check", capture=True, timeout=60)
        try:
            checked = json.loads(report)
        except ValueError as exc:
            raise SetupError("Tunnel doctor returned an unknown report; no tunnel was started.") from exc
        if not isinstance(checked, dict) or checked.get("result") != "ok":
            raise SetupError("Tunnel doctor did not confirm the local configuration; no tunnel was started.")
        print("Local tunnel configuration check passed. The runtime key remains in memory only.")
        return _start([tunnel, "run", *common], runtime_env)
    finally:
        runtime_env.pop("CONTROL_PLANE_API_KEY", None)


def main(argv=None):
    previous_umask = os.umask(0o077)
    try:
        args = _parser().parse_args(argv)
        return setup(args, source_root=Path(__file__).resolve().parents[1], home=Path.home(), environ=os.environ)
    except (SetupError, OSError, EOFError) as exc:
        print(str(exc) if isinstance(exc, SetupError) else "Local setup could not complete. Check private file permissions and available disk space; no error details were printed.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Setup cancelled. Existing files were not overwritten.", file=sys.stderr)
        return 130
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
