#!/usr/bin/env python3
"""Auto-provisions a personal LiteLLM team + virtual key for a fresh clone.
Run via `make init` (or directly: `python3 services/init/init_litellm_key.py
-f docker-compose.yml -f docker-compose.dev.yml`), after
init_clickhouse_users.py.

Stdlib-only - loads services/init/init_common.py (shared .env/docker-compose
helpers, also used by init_clickhouse_users.py) via
importlib.util.spec_from_file_location rather than a package import, since
this script runs on the host, outside any container.

`litellm` publishes no host port (only `load-balancer` does, once the full
stack including load-balancer is up - which it isn't yet during `make init`),
so its Admin API can't be reached over HTTP from the host at this point.
Instead this brings up just the `litellm` service (and its `litellm-db`
dependency, via `depends_on: condition: service_healthy`), waits for its
existing healthcheck, then pipes services/init/litellm_provision.py into
`docker compose exec -T litellm python3 -` (same idiom
init_clickhouse_users.py uses to pipe schema.sql into clickhouse-client) so
the actual /team/new and /key/generate calls happen from inside the
container against its own http://localhost:4000.
LITELLM_MASTER_KEY doesn't need to be injected separately - it's already set
in that container's own environment
(docker-compose.yml's litellm.environment.LITELLM_MASTER_KEY).

Safe to re-run: skips entirely if .env already has a non-empty
LITELLM_VIRTUAL_KEY. If .env has LITELLM_TEAM_ID but not yet
LITELLM_VIRTUAL_KEY (e.g. a previous run crashed between the two API calls),
that team is reused instead of creating a second one.

`litellm` is only stopped again on the way out if this script is the one
that started it - same "leave it up if it was already running" logic as
init_clickhouse_users.py's clickhouse handling.
"""
import importlib.util
import json
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
INIT_COMMON_PATH = pathlib.Path(__file__).resolve().parent / "init_common.py"
PROVISION_SCRIPT_PATH = pathlib.Path(__file__).resolve().parent / "litellm_provision.py"

DEFAULT_COMPOSE_FILES = ["-f", "docker-compose.yml", "-f", "docker-compose.dev.yml"]


def _load_init_common():
    spec = importlib.util.spec_from_file_location("init_common", INIT_COMMON_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


init_common = _load_init_common()


def _provision(compose_files: list[str], existing_team_id: str) -> dict:
    cmd = ["docker", "compose", *compose_files, "exec", "-T"]
    if existing_team_id:
        cmd += ["-e", f"EXISTING_TEAM_ID={existing_team_id}"]
    cmd += ["litellm", "python3", "-"]
    # Piping litellm_provision.py's actual text via input= (not stdin=DEVNULL
    # like init_common.run_compose's other calls) - see
    # init_clickhouse_users.py's _apply_schema for why that's safe here.
    result = subprocess.run(
        cmd, cwd=REPO_ROOT, check=True, input=PROVISION_SCRIPT_PATH.read_text(),
        capture_output=True, text=True, timeout=30,
    )
    # litellm_provision.py prints exactly one JSON line; take the last
    # non-empty line defensively in case anything else lands on stdout.
    last_line = [line for line in result.stdout.splitlines() if line.strip()][-1]
    return json.loads(last_line)


def main() -> None:
    if "-h" in sys.argv[1:] or "--help" in sys.argv[1:]:
        print(__doc__)
        sys.exit(0)
    compose_files = sys.argv[1:] or DEFAULT_COMPOSE_FILES

    existing = init_common.read_existing_env(init_common.ENV_PATH)
    if existing.get("LITELLM_VIRTUAL_KEY"):
        print("=== LiteLLM key setup (make init) ===")
        print("LITELLM_VIRTUAL_KEY already set in .env - skipping.")
        return

    print("=== LiteLLM key setup (make init) ===")
    was_already_running = init_common.service_already_running(compose_files, "litellm")

    print("starting litellm...")
    init_common.run_compose(compose_files, "up", "-d", "--build", "litellm", quiet=True)
    init_common.wait_for_service_healthy(compose_files, "litellm")

    try:
        print("creating personal team + virtual key...")
        provisioned = _provision(compose_files, existing.get("LITELLM_TEAM_ID", ""))
    finally:
        if was_already_running:
            print("\nlitellm was already running before this script started - leaving it up.")
        else:
            print("\nstopping litellm...")
            init_common.run_compose(compose_files, "stop", "litellm", check=False)

    updates = {"LITELLM_TEAM_ID": provisioned["team_id"], "LITELLM_VIRTUAL_KEY": provisioned["key"]}
    init_common.write_env(init_common.ENV_PATH, updates)

    print("\n✅ === done ===")
    print(f"Team: {updates['LITELLM_TEAM_ID']}")
    print(f"Key written to {init_common.ENV_PATH.name} as LITELLM_VIRTUAL_KEY. Next: run `make up` then `make setup-client`.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted - re-run `make init` to resume, it's safe to repeat.", file=sys.stderr)
        sys.exit(130)
    except subprocess.TimeoutExpired as e:
        print(f"\ntimed out waiting on: {' '.join(e.cmd)}", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"\ncommand failed (exit {e.returncode}): {' '.join(e.cmd)}", file=sys.stderr)
        if e.stderr:
            print(e.stderr, file=sys.stderr)
        sys.exit(1)
