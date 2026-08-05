#!/usr/bin/env python3
"""Waits for every service `docker compose up`/`start` brings up to finish
starting, then reports pass/fail - what `make status` runs instead of a bare
`docker ps` (see Makefile). Run directly any time, not just from `make
status`: `uv run python3 scripts/wait_for_stack_healthy.py -f docker-compose.yml -f
docker-compose.dev.yml`.

Stdlib-only, no venv/pip install needed - same convention as
services/init/init_clickhouse_users.py/resolve_image_version.py. The live,
in-place-redrawing status table (like `docker compose up` itself shows) is
plain ANSI cursor-movement escape codes, gated on `sys.stdout.isatty()` - no
package (rich/colorama/etc.) needed for that either. Piped output (a log
file, `| cat`) falls back to one compact summary line per poll instead of
redrawing, so it stays readable there instead of dumping the whole table
every 2s.

"Done" for a service means either:
- its healthcheck reports `healthy` (every long-running service in this
  stack's default profile has one), or
- it exited with code 0 (covers one-shot services like clickhouse-migrate,
  which has no healthcheck and `restart: "no"` - generic on purpose, so this
  doesn't need to know which services are one-shot ahead of time).

Fails immediately (prints that service's `docker compose logs`, exit 1) the
moment any service reports `unhealthy` or exits with a nonzero code, rather
than waiting out the full timeout on an already-known failure. Also fails
(with logs for whatever's still not done) if the timeout elapses first.
"""
import json
import pathlib
import subprocess
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_COMPOSE_FILES = ["-f", "docker-compose.yml", "-f", "docker-compose.dev.yml"]
TIMEOUT_S = 180
POLL_INTERVAL_S = 1

# Our own three-state vocabulary, shown to the user.
STATUS_HEALTHY = "Healthy"
STATUS_FAILED = "Failed"
STATUS_WAITING = "Waiting"

# `docker compose ps --format json`'s own literal field values we parse
# against - not ours to rename, these come straight from Docker.
DOCKER_HEALTH_HEALTHY = "healthy"
DOCKER_HEALTH_UNHEALTHY = "unhealthy"
DOCKER_STATE_EXITED = "exited"


def _compose_services(compose_files: list[str]) -> list[str]:
    result = subprocess.run(
        ["docker", "compose", *compose_files, "config", "--services"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _compose_ps(compose_files: list[str]) -> dict[str, dict]:
    result = subprocess.run(
        ["docker", "compose", *compose_files, "ps", "-a", "--format", "json"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
    )
    containers = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        containers[row["Service"]] = row
    return containers


def _colored(text: str, code: str, stream) -> str:
    """Wraps text in an ANSI color code, but only if stream is a real
    terminal - piped output (a log file, CI) stays plain."""
    return f"\033[{code}m{text}\033[0m" if stream.isatty() else text


def _print_logs(compose_files: list[str], service: str) -> None:
    print(f"\n--- docker compose logs {service} (last 80 lines) ---", file=sys.stderr)
    subprocess.run(
        ["docker", "compose", *compose_files, "logs", "--tail", "80", service],
        cwd=REPO_ROOT, stdin=subprocess.DEVNULL,
    )


def _classify(expected: list[str], containers: dict[str, dict]) -> tuple[dict[str, str], list[str], list[str]]:
    """Per service: a short display label - Healthy/Failed/Waiting, nothing
    finer-grained - plus which are failed/pending."""
    status: dict[str, str] = {}
    failed: list[str] = []
    pending: list[str] = []
    for service in expected:
        row = containers.get(service)
        if row is None:
            status[service] = STATUS_WAITING
            pending.append(service)
            continue
        exit_code = row.get("ExitCode")
        health = row.get("Health", "")
        state = row.get("State", "")
        if (exit_code not in (0, None)) or health == DOCKER_HEALTH_UNHEALTHY:
            status[service] = STATUS_FAILED
            failed.append(service)
        elif health == DOCKER_HEALTH_HEALTHY or (state == DOCKER_STATE_EXITED and exit_code == 0):
            status[service] = STATUS_HEALTHY
        else:
            status[service] = STATUS_WAITING
            pending.append(service)
    return status, failed, pending


class _LiveTable:
    """Redraws in place on a real terminal - the same ANSI cursor-up +
    clear-to-end trick `docker compose up` itself uses to show its own
    per-container "Started"/"Healthy" progress lines - matching that look:
    ` ✔ Container <name>   <Label>   <elapsed>s`, one line per service,
    padded into columns, elapsed frozen once a line reaches a terminal state.
    On piped/non-tty output it prints one compact line per poll instead, so a
    log file doesn't get the whole table dumped every 2s."""

    def __init__(self, expected: list[str], containers: dict[str, dict]):
        self._is_tty = sys.stdout.isatty()
        self._drawn_lines = 0
        self._start = time.monotonic()
        self._frozen_elapsed: dict[str, float] = {}
        names = {service: containers.get(service, {}).get("Name", service) for service in expected}
        self._name_width = max(len(n) for n in names.values())

    def update(self, expected: list[str], status: dict[str, str], containers: dict[str, dict]) -> None:
        now = time.monotonic()
        names = {service: containers.get(service, {}).get("Name", service) for service in expected}
        self._name_width = max(self._name_width, max(len(n) for n in names.values()))

        for service in expected:
            if service not in self._frozen_elapsed and status[service] in (STATUS_HEALTHY, STATUS_FAILED):
                self._frozen_elapsed[service] = now - self._start

        if self._is_tty:
            lines = []
            for service in expected:
                elapsed = self._frozen_elapsed.get(service, now - self._start)
                label = status[service]
                padded_label = f"{label:<7}"  # pad the plain text first - ANSI codes below would throw off :< padding otherwise
                if label == STATUS_HEALTHY:
                    mark, colored_label = "\033[32m✔\033[0m", f"\033[32m{padded_label}\033[0m"
                elif label == STATUS_FAILED:
                    mark, colored_label = "\033[31m✘\033[0m", f"\033[31m{padded_label}\033[0m"
                else:
                    mark, colored_label = " ", padded_label
                name = names[service]
                lines.append(f" {mark} Container {name:<{self._name_width}}   {colored_label} {elapsed:>5.1f}s")
            if self._drawn_lines:
                sys.stdout.write(f"\x1b[{self._drawn_lines}A")
            sys.stdout.write("\x1b[J")
            sys.stdout.write("\n".join(lines) + "\n")
            sys.stdout.flush()
            self._drawn_lines = len(lines)
        else:
            waiting = [s for s in expected if status[s] != STATUS_HEALTHY]
            print(f"waiting on: {', '.join(waiting) if waiting else '(none)'}")


def main() -> None:
    if "-h" in sys.argv[1:] or "--help" in sys.argv[1:]:
        print(__doc__)
        sys.exit(0)
    compose_files = sys.argv[1:] or DEFAULT_COMPOSE_FILES

    # `config --services` only lists default-profile services, so
    # profile-gated stacks (observability, langfuse) are invisible here even
    # when their containers are already running.
    # `docker compose ps -a` isn't profile-filtered - that only gates
    # `up`/`create` - so any already-up profile service shows up there too.
    # But `ps -a` also includes long-exited one-shot job containers from the
    # `tools` profile (loadtest-fixtures, metrics-reparse, ...) - those ran
    # once in the past via `docker compose run` and aren't part of the
    # current stack, so a stale nonzero exit code from months ago shouldn't
    # fail `make status` today.
    # State=="running" is what separates "an extra service stack the user
    # actually brought up" (observability/langfuse) from "leftover container
    # of a finished one-off job" - only the former belongs in expected.
    expected = _compose_services(compose_files)
    extra = sorted(
        service for service, row in _compose_ps(compose_files).items()
        if service not in expected and row.get("State") == "running"
    )
    expected = expected + extra
    print(f"waiting for {len(expected)} service(s) to become healthy:")

    deadline = time.monotonic() + TIMEOUT_S
    pending: list[str] = expected
    table: "_LiveTable | None" = None
    while time.monotonic() < deadline:
        containers = _compose_ps(compose_files)
        if table is None:
            table = _LiveTable(expected, containers)
        status, failed, pending = _classify(expected, containers)
        table.update(expected, status, containers)

        if failed:
            print(f"\n{_colored(STATUS_FAILED, '31', sys.stderr)}: {', '.join(failed)}", file=sys.stderr)
            for service in failed:
                _print_logs(compose_files, service)
            sys.exit(1)

        if not pending:
            print(f"\nAll services {_colored(STATUS_HEALTHY, '32', sys.stdout)}.")
            sys.exit(0)

        time.sleep(POLL_INTERVAL_S)

    print(
        f"\n{_colored('Timed out', '31', sys.stderr)} after {TIMEOUT_S}s waiting on: {', '.join(pending)}",
        file=sys.stderr,
    )
    for service in pending:
        _print_logs(compose_files, service)
    sys.exit(1)


if __name__ == "__main__":
    main()
