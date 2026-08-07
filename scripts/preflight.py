#!/usr/bin/env python3
"""Host readiness check, run first by `make bootstrap` (standalone: `make
preflight`, or `python3 scripts/preflight.py`).

Stdlib-only, no venv/pip install needed - same constraint as every other
`scripts/*` entry point (see AGENTS.md "Python"), because this has to be
runnable on a machine where nothing but Docker and Python exist yet.

Exists so an unattended `make bootstrap` fails on the *actual* missing
prerequisite with a one-line fix, instead of failing 4 minutes later inside
a `docker compose build` with a misleading error. The two real-world ones
this repo has already hit are documented in README "Prerequisites": a
missing compose plugin surfaces as `unknown shorthand flag: 'f' in -f`, and
a missing buildx plugin as `the --chmod option requires BuildKit` - neither
mentions the plugin that's actually absent.

Every check is read-only: nothing here starts, stops, or writes anything.

Exit codes: 0 = all hard checks passed (warnings may still be printed),
1 = at least one hard check failed.
"""
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"

# Floor from README "Minimal resource requirements" - the stack idles above
# this, so anything under it is reported as a warning rather than silently
# accepted and blamed on ClickHouse later.
MIN_CPUS = 4
MIN_MEMORY_GIB = 8

# Every host port docker-compose.yml publishes by default, with the .env var
# that overrides it. Checked for a conflict with something *other than* this
# stack - a port held by our own already-running container is fine.
PORTS = {
    "WEBHOOK_PORT": 8010,
    "LITELLM_PORT": 4000,
    "ANTHROPIC_PROXY_PORT": 4001,
    "OPENAI_PROXY_PORT": 4002,
    "GRAFANA_PORT": 3000,
    "MCP_SERVER_PORT": 8001,
    "CLICKHOUSE_HTTP_PORT": 8123,
    "CLICKHOUSE_NATIVE_PORT": 9000,
}

OK, WARN, FAIL = "ok", "warn", "fail"

_RESULTS: list[tuple[str, str, str]] = []


def _glyphs() -> dict[str, str]:
    """✔/✖ where the console can render them, ASCII where it can't. A stock
    Windows console still hands Python a cp1252 stdout, and printing ✔ there
    raises UnicodeEncodeError - a preflight script that itself crashes on its
    own output is the least useful possible failure mode."""
    encoding = sys.stdout.encoding or "ascii"
    try:
        "✔✖".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return {OK: "[ok]", WARN: "[! ]", FAIL: "[XX]"}
    return {OK: "✔", WARN: "!", FAIL: "✖"}


def _record(status: str, label: str, detail: str = "") -> None:
    _RESULTS.append((status, label, detail))


def _run(*args: str, timeout: float = 30) -> tuple[int, str]:
    """Returns (returncode, stdout+stderr). Never raises on a non-zero exit -
    every caller here treats failure as a check result, not an exception."""
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
        )
    except FileNotFoundError:
        return 127, f"{args[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{' '.join(args)}: timed out after {timeout}s"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _read_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    values = {}
    for line in ENV_PATH.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def _docker_install_hint() -> str:
    if sys.platform == "darwin":
        return "brew install colima docker docker-compose docker-buildx && colima start --edit"
    if sys.platform.startswith("win"):
        return "install Docker Desktop and make sure it's running (or `wsl --install` + Docker in WSL2)"
    return "install docker-ce + docker-compose-plugin + docker-buildx-plugin from your distro's repo"


def check_docker() -> bool:
    if shutil.which("docker") is None:
        _record(FAIL, "docker CLI", _docker_install_hint())
        return False

    code, out = _run("docker", "info", "--format", "{{json .}}", timeout=60)
    if code != 0:
        # Daemon down is by far the most common failure here, and its message
        # ("Cannot connect to the Docker daemon...") is long - keep the first
        # line only, the fix is the same regardless.
        first_line = out.splitlines()[0] if out else "docker info failed"
        _record(FAIL, "docker daemon", f"{first_line} - start it: {_docker_install_hint()}")
        return False

    _record(OK, "docker daemon", "reachable")

    try:
        info = json.loads(out)
    except json.JSONDecodeError:
        _record(WARN, "docker resources", "could not parse `docker info` output - skipped")
        return True

    cpus = info.get("NCPU") or 0
    mem_gib = (info.get("MemTotal") or 0) / (1024 ** 3)
    resource_detail = f"{cpus} CPU, {mem_gib:.1f} GiB"
    if cpus < MIN_CPUS or mem_gib < MIN_MEMORY_GIB - 0.5:
        _record(
            WARN,
            "docker resources",
            f"{resource_detail} - below the {MIN_CPUS} CPU / {MIN_MEMORY_GIB} GiB floor "
            "(README 'Minimal resource requirements'); ClickHouse merges and `make loadtest` will struggle",
        )
    else:
        _record(OK, "docker resources", resource_detail)
    return True


def check_docker_plugins() -> bool:
    ok = True
    for plugin, symptom in (
        ("compose", "every `docker compose -f ...` call fails with \"unknown shorthand flag: 'f' in -f\""),
        ("buildx", 'image builds fail with "the --chmod option requires BuildKit"'),
    ):
        code, out = _run("docker", plugin, "version")
        if code != 0:
            _record(FAIL, f"docker {plugin}", f"missing - {symptom}. See README 'Prerequisites'")
            ok = False
        else:
            _record(OK, f"docker {plugin}", out.splitlines()[0] if out else "present")
    return ok


def check_python() -> bool:
    # 3.9 is the floor for the `dict[str, str]` builtin generics used across
    # scripts/ and services/init/ without a `from __future__` import.
    if sys.version_info < (3, 9):
        _record(FAIL, "python3", f"{sys.version.split()[0]} - need 3.9+")
        return False
    _record(OK, "python3", sys.version.split()[0])
    return True


def check_git() -> bool:
    if shutil.which("git") is None:
        # Not fatal for the stack itself - only `make git-hooks-install`
        # (part of `make init`) needs it.
        _record(WARN, "git", "not found - tracked git hooks can't be installed")
        return True
    _record(OK, "git", "present")
    return True


def _port_owner(port: int) -> str:
    """Container name publishing `port`, or "" if none/docker unavailable."""
    code, out = _run("docker", "ps", "--filter", f"publish={port}", "--format", "{{.Names}}")
    if code != 0:
        return ""
    return out.strip().splitlines()[0] if out.strip() else ""


def _compose_working_dir(container: str) -> str:
    """The directory the running container's compose project was started
    from, per its own `com.docker.compose.project.working_dir` label."""
    code, out = _run(
        "docker", "inspect", "-f",
        '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}',
        container,
    )
    return out.strip() if code == 0 else ""


def _port_in_use(port: int) -> bool:
    # connect_ex, not bind: on Windows a bind test against a port held by
    # another process can succeed anyway (SO_EXCLUSIVEADDRUSE semantics
    # differ from Linux), so probe for a listener instead.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def check_ports(env: dict[str, str]) -> bool:
    conflicts = []
    ours = []
    for var, default in PORTS.items():
        port = int(os.environ.get(var) or env.get(var) or default)
        if not _port_in_use(port):
            continue
        owner = _port_owner(port)
        if owner.startswith("receipt-goblin"):
            ours.append(f"{port} ({owner})")
        else:
            conflicts.append(f"{port} ({var}){f' held by {owner}' if owner else ''}")

    if conflicts:
        _record(
            FAIL,
            "host ports",
            "in use by something else: " + ", ".join(conflicts)
            + " - free them, or override the listed var(s) in .env",
        )
        return False
    detail = "free" if not ours else "already served by this stack: " + ", ".join(ours)
    _record(OK, "host ports", detail)

    if ours:
        # Compose keys a project by name, not by path, so a second clone of
        # this repo shares the project name `receipt-goblin` with the first -
        # `make up` from here would recreate the *running* containers under
        # this directory's bind mounts instead of starting anything new.
        # Silent in the normal case (same directory), loud in the one that
        # quietly takes over someone else's running stack.
        container = ours[0].split("(")[1].rstrip(")")
        working_dir = _compose_working_dir(container)
        if working_dir and pathlib.Path(working_dir).resolve() != REPO_ROOT.resolve():
            _record(
                WARN,
                "compose project",
                f"the running stack was started from {working_dir}, not this clone "
                f"({REPO_ROOT}) - both share the compose project name, so `make up` here "
                "would recreate those containers against this directory's files",
            )
    return True


def check_env_file(env: dict[str, str]) -> bool:
    if not ENV_PATH.exists():
        # Expected on a fresh clone - `make init` copies .env.example over.
        _record(WARN, ".env", "missing - `make bootstrap`/`make init` will create it from .env.example")
        return True
    missing = [
        key
        for key in ("CLICKHOUSE_DATABASE", "CLICKHOUSE_BOOTSTRAP_USER", "LITELLM_MASTER_KEY", "LITELLM_DB_PASSWORD")
        if not env.get(key)
    ]
    if missing:
        _record(WARN, ".env", f"present but unset: {', '.join(missing)} - `make init` fills these in")
    else:
        _record(OK, ".env", "present, core vars set")
    return True


def main() -> None:
    if "-h" in sys.argv[1:] or "--help" in sys.argv[1:]:
        print(__doc__)
        sys.exit(0)

    env = _read_env()

    docker_ok = check_docker()
    plugins_ok = check_docker_plugins() if docker_ok else False
    python_ok = check_python()
    check_git()
    ports_ok = check_ports(env) if docker_ok else True
    check_env_file(env)

    print("=== preflight ===")
    glyph = _glyphs()
    for status, label, detail in _RESULTS:
        print(f"  {glyph[status]} {label:<18} {detail}")

    if not (docker_ok and plugins_ok and python_ok and ports_ok):
        print("\npreflight FAILED - fix the ✖ line(s) above, then re-run.", file=sys.stderr)
        sys.exit(1)
    print("\npreflight OK.")


if __name__ == "__main__":
    main()
