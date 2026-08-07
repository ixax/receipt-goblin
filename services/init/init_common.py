"""Shared host-side helpers for services/init/*.py first-run provisioning
scripts (currently init_clickhouse_users.py and init_litellm_key.py).

Stdlib-only, loaded via importlib.util.spec_from_file_location (not a package
import) since these scripts run on the host, outside any container - see
init_clickhouse_users.py's own docstring for why.
"""
import pathlib
import subprocess
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
ENV_PATH = REPO_ROOT / ".env"
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"


def read_existing_env(path: pathlib.Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def write_env(path: pathlib.Path, updates: dict[str, str]) -> None:
    if not path.exists():
        if not ENV_EXAMPLE_PATH.exists():
            print(f"error: neither {path} nor {ENV_EXAMPLE_PATH} exist", file=sys.stderr)
            sys.exit(1)
        path.write_text(ENV_EXAMPLE_PATH.read_text())
        print(f"copied {ENV_EXAMPLE_PATH.name} -> {path.name}")

    lines = path.read_text().splitlines()
    remaining = dict(updates)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.partition("=")[0].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"
    for key, value in remaining.items():
        lines.append(f"{key}={value}")

    path.write_text("\n".join(lines) + "\n")


def run_compose(
    compose_files: list[str], *args: str, check: bool = True, timeout: float = 120, quiet: bool = False,
) -> subprocess.CompletedProcess:
    # stdin=DEVNULL, deliberately: leaving stdin attached to this script's own
    # controlling terminal is what let a docker-compose-exec'd child inherit a
    # terminal left half-raw by getpass's echo-control fallback, producing a
    # spurious SIGINT with nobody touching the keyboard (observed while
    # building init_clickhouse_users.py).
    # Timeout so a genuine hang fails loudly instead of sitting silent.
    cmd = ["docker", "compose", *compose_files, *args]
    if quiet:
        # `make init` sets quiet=True on its own build/startup calls (e.g.
        # `up --build`) so a fresh clone's first run isn't drowned out by
        # image-pull/build-layer noise between the step banners - captured
        # combined output is only printed (then the failure re-raised) if
        # the command itself failed, so a real error is never hidden.
        result = subprocess.run(
            cmd, cwd=REPO_ROOT, stdin=subprocess.DEVNULL, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        if check and result.returncode != 0:
            sys.stdout.write(result.stdout)
            raise subprocess.CalledProcessError(result.returncode, cmd, output=result.stdout)
        return result
    return subprocess.run(cmd, cwd=REPO_ROOT, check=check, stdin=subprocess.DEVNULL, timeout=timeout)


def service_already_running(compose_files: list[str], service: str) -> bool:
    result = subprocess.run(
        ["docker", "compose", *compose_files, "ps", "--format", "{{.State}}", service],
        cwd=REPO_ROOT, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30,
    )
    return result.stdout.strip() == "running"


def wait_for_service_healthy(compose_files: list[str], service: str, timeout_s: int = 90) -> None:
    print(f"waiting for {service} to become healthy...")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["docker", "compose", *compose_files, "ps", "--format", "{{.Health}}", service],
            cwd=REPO_ROOT, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30,
        )
        if result.stdout.strip() == "healthy":
            print(f"{service} is healthy")
            return
        time.sleep(2)
    print(f"error: {service} did not become healthy in time", file=sys.stderr)
    sys.exit(1)
