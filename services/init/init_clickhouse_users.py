#!/usr/bin/env python3
"""Interactive first-run ClickHouse provisioning for a fresh clone. Run via
`make init` (or directly: `python3 services/init/init_clickhouse_users.py -f
docker-compose.yml -f docker-compose.dev.yml`).

Stdlib-only, no venv/pip install needed - loads services/init/ch_roles.py
(the single source of truth for the role/grant model, itself reading
services/init/config.yml) and services/init/init_common.py (shared .env/
docker-compose helpers, also used by init_litellm_key.py) via
importlib.util.spec_from_file_location rather than a package import, since
this script runs on the host, outside any container.

This is the *only* place ClickHouse users/roles/grants get created - not on
every `docker compose up` (see services/migrate/src/migrate.py's own
docstring for why that changed). Re-running this script is always safe
(existing usernames/passwords already in .env are reused, not regenerated;
every `CREATE USER`/`GRANT` statement it issues is idempotent).

Flow: ask every question first (database name, bootstrap creds, then per
role - a database name too for any role whose database isn't the main one,
e.g. loadtest - username, password), THEN touch .env (copy from .env.example
if missing, rewrite in place), bring up just the `clickhouse` service, create
every role via `docker compose exec clickhouse clickhouse-client` (the exact
GRANT bodies services/init/config.yml defines), then hand control back to
`make up`.

`clickhouse` is only stopped again on the way out if this script is the one
that started it (a fresh clone's first run). Re-running this script later
against an already-up stack - e.g. to pick up a new grant added to
config.yml - leaves `clickhouse` running afterward instead of stopping it
out from under the rest of the running stack.
"""
import getpass
import importlib.util
import os
import pathlib
import secrets
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
CH_ROLES_PATH = pathlib.Path(__file__).resolve().parent / "ch_roles.py"
INIT_COMMON_PATH = pathlib.Path(__file__).resolve().parent / "init_common.py"
# Applied to any role's database that this script itself creates
# (role.create_database) - CLICKHOUSE_DATABASE gets its tables for free from
# docker-entrypoint-initdb.d on the clickhouse container's own first boot,
# but a database created later via CREATE DATABASE (e.g. loadtest's) never
# goes through that path, so nothing else would ever apply this schema to
# it - without this step that database exists but has zero tables.
SCHEMA_PATH = REPO_ROOT / "services" / "clickhouse" / "schema.sql"

DEFAULT_COMPOSE_FILES = ["-f", "docker-compose.yml", "-f", "docker-compose.dev.yml"]


def _load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_ch_roles():
    return _load_module("ch_roles", CH_ROLES_PATH)


init_common = _load_module("init_common", INIT_COMMON_PATH)


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def _prompt_password(label: str) -> str:
    value = getpass.getpass(f"{label} (blank = generate one): ").strip()
    if value:
        return value
    generated = secrets.token_urlsafe(18)
    print(f"  generated: {generated}")
    return generated


def _sql_string_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _clickhouse_client(compose_files: list[str], user: str, password: str, query: str) -> None:
    init_common.run_compose(
        compose_files, "exec", "-T", "clickhouse", "clickhouse-client",
        "--user", user, "--password", password, "--query", query,
        timeout=30,
    )


def _apply_schema(compose_files: list[str], user: str, password: str, database: str) -> None:
    # Piping schema.sql's actual text via input= (not stdin=DEVNULL like
    # init_common.run_compose's other calls) - subprocess.run(input=...) opens
    # its own pipe for this one call, so it doesn't reintroduce the
    # terminal-inheritance issue documented on run_compose; that issue was
    # specifically about *inheriting* the parent's controlling tty when stdin
    # wasn't redirected at all, not about deliberately piping known content
    # through a fresh pipe.
    cmd = [
        "docker", "compose", *compose_files, "exec", "-T", "clickhouse", "clickhouse-client",
        "--user", user, "--password", password, "--database", database, "--multiquery",
    ]
    subprocess.run(cmd, cwd=REPO_ROOT, check=True, input=SCHEMA_PATH.read_text(), text=True, timeout=60)


def main() -> None:
    # Not argparse: the real input here is `-f docker-compose.yml -f
    # docker-compose.dev.yml` (make init's $(COMPOSE_FILES), passed through
    # as-is) - argparse treats every `-f` as an unrecognized option rather
    # than a positional value, so this just takes argv verbatim instead.
    if "-h" in sys.argv[1:] or "--help" in sys.argv[1:]:
        print(__doc__)
        sys.exit(0)
    compose_files = sys.argv[1:] or DEFAULT_COMPOSE_FILES

    ch_roles = _load_ch_roles()
    existing = init_common.read_existing_env(init_common.ENV_PATH)

    print("=== ClickHouse setup (make init) ===")
    print("Answer the prompts below - nothing is written to .env until the end.\n")

    updates: dict[str, str] = {}

    updates["CLICKHOUSE_DATABASE"] = _prompt("Main database name", existing.get("CLICKHOUSE_DATABASE", "default"))

    print("\n-- Bootstrap superuser (used only to create the roles below) --")
    updates["CLICKHOUSE_BOOTSTRAP_USER"] = _prompt(
        "Bootstrap username", existing.get("CLICKHOUSE_BOOTSTRAP_USER", "clickhouse_bootstrap")
    )
    updates["CLICKHOUSE_BOOTSTRAP_PASSWORD"] = existing.get("CLICKHOUSE_BOOTSTRAP_PASSWORD") or _prompt_password(
        "Bootstrap password"
    )

    for role in ch_roles.ROLES:
        print(f"\n-- Role: {role.name} --")
        if role.create_database:
            updates[role.database_env] = _prompt(
                f"Database name for {role.name}", existing.get(role.database_env, role.default_user)
            )
        user = _prompt(f"{role.name} username", existing.get(role.user_env, role.default_user))
        updates[role.user_env] = user
        updates[role.password_env] = existing.get(role.password_env) or _prompt_password(f"{role.name} password")

    # docker-compose.yml requires these too (unrelated to ClickHouse) - avoid
    # a confusing Compose failure on `up -d clickhouse` if they're unset.
    for key in ("LITELLM_MASTER_KEY", "LITELLM_DB_PASSWORD"):
        if not existing.get(key):
            updates[key] = secrets.token_urlsafe(18)
            print(f"\n{key} was unset - generated a value (edit .env later if you need a specific one).")

    init_common.write_env(init_common.ENV_PATH, updates)
    print(f"\nwrote {init_common.ENV_PATH.name}")

    os.environ.update(updates)

    was_already_running = init_common.service_already_running(compose_files, "clickhouse")

    print("\nstarting clickhouse...")
    init_common.run_compose(compose_files, "up", "-d", "--build", "clickhouse", quiet=True)
    init_common.wait_for_service_healthy(compose_files, "clickhouse")

    bootstrap_user = updates["CLICKHOUSE_BOOTSTRAP_USER"]
    bootstrap_password = updates["CLICKHOUSE_BOOTSTRAP_PASSWORD"]

    # clickhouse is only meant to be left running for the duration of
    # provisioning below - always try to stop it again on the way out
    # (success, error, or Ctrl-C) if this script is the one that started it,
    # so a failure never silently leaves it up with no indication either way.
    # But only if it wasn't already running before this script touched it -
    # if the rest of the stack was already up (e.g. re-running `make init`
    # to pick up a new grant), stopping clickhouse out from under running
    # webhook/grafana/mcp-dev containers would just break them.
    try:
        for role in ch_roles.ROLES:
            database = updates.get(role.database_env, updates["CLICKHOUSE_DATABASE"])
            user = updates[role.user_env]
            password = updates[role.password_env]
            print(f"provisioning role {role.name} (user {user}, database {database})...")
            if role.create_database:
                _clickhouse_client(
                    compose_files, bootstrap_user, bootstrap_password,
                    f"CREATE DATABASE IF NOT EXISTS {database}",
                )
                _apply_schema(compose_files, bootstrap_user, bootstrap_password, database)
            _clickhouse_client(
                compose_files, bootstrap_user, bootstrap_password,
                f"CREATE USER OR REPLACE {user} IDENTIFIED BY {_sql_string_literal(password)} "
                f"DEFAULT DATABASE {database}",
            )
            for grant in role.grants:
                _clickhouse_client(
                    compose_files, bootstrap_user, bootstrap_password,
                    f"{grant.format(database=database)} TO {user}",
                )
            # Revokes run after grants by construction - they carve columns back
            # out of a broad grant, so the grant has to exist first.
            for revoke in role.revokes:
                _clickhouse_client(
                    compose_files, bootstrap_user, bootstrap_password,
                    f"{revoke.format(database=database)} FROM {user}",
                )
    finally:
        if was_already_running:
            print("\nclickhouse was already running before this script started - leaving it up.")
        else:
            print("\nstopping clickhouse...")
            init_common.run_compose(compose_files, "stop", "clickhouse", check=False)

    print("\n✅ === done ===")
    print(f"Database: {updates['CLICKHOUSE_DATABASE']}")
    for role in ch_roles.ROLES:
        print(f"  {role.name}: user={updates[role.user_env]}")
    print(f"\nCredentials are in {init_common.ENV_PATH.name}. Next: run `make up`.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted - clickhouse should already be stopped (see above); re-run `make init` to resume, "
              "it's safe to repeat (existing usernames/passwords in .env are reused, not regenerated).",
              file=sys.stderr)
        sys.exit(130)
    except subprocess.TimeoutExpired as e:
        print(f"\ntimed out waiting on: {' '.join(e.cmd)}", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"\ncommand failed (exit {e.returncode}): {' '.join(e.cmd)}", file=sys.stderr)
        sys.exit(1)
