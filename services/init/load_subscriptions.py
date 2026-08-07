#!/usr/bin/env python3
"""Loads services/init/subscriptions.yml into ClickHouse's person_identities and subscriptions tables.

Run via `make subscriptions` (or directly: `python3 services/init/load_subscriptions.py -f docker-compose.yml`).
Also runs as the last step of `make init`, after `make migrate` - the tables have to exist first, and migrate is what creates them.

Stdlib-only, no venv/pip install needed - loads services/init/subscription_config.py (the parser and validator, itself reading services/init/subscriptions.yml) via importlib.util.spec_from_file_location rather than a package import, since this script runs on the host, outside any container.
Same arrangement as init_clickhouse_users.py/ch_roles.py next to it.

Both target tables are treated as a projection of subscriptions.yml, not as accumulated state: each run truncates and rewrites them.
That is what makes deleting a line from the YAML actually remove the subscription.
Insert-only against ReplacingMergeTree would leave the deleted row in place forever, still being summed by every spend panel, since ReplacingMergeTree collapses versions of a key but has no concept of a key that should no longer exist.
The whole config is parsed and validated before anything is truncated, so a malformed edit fails with the old rows still in place rather than emptying the tables.

This script never creates tables and never touches users/roles/grants - `make migrate` owns the schema and `make init` owns the roles.
Re-running it is always safe.
"""
import importlib.util
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
CONFIG_MODULE_PATH = pathlib.Path(__file__).resolve().parent / "subscription_config.py"
ENV_PATH = REPO_ROOT / ".env"


def _load_subscription_config():
    spec = importlib.util.spec_from_file_location("subscription_config", CONFIG_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_env(path: pathlib.Path) -> dict[str, str]:
    if not path.exists():
        print(f"error: {path} not found - run `make init` first", file=sys.stderr)
        sys.exit(1)
    values = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def _sql_string_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _clickhouse(compose_files: list[str], env: dict[str, str], query: str) -> None:
    """Runs one or more `;`-separated statements as the bootstrap superuser.

    TRUNCATE is DDL-adjacent and no least-privilege role in services/init/config.yml holds it, so this uses the same bootstrap identity `make migrate` does rather than the ingest role.
    """
    cmd = [
        "docker", "compose", *compose_files, "exec", "-T", "clickhouse", "clickhouse-client",
        "--user", env["CLICKHOUSE_BOOTSTRAP_USER"],
        "--password", env["CLICKHOUSE_BOOTSTRAP_PASSWORD"],
        "--database", env.get("CLICKHOUSE_DATABASE", "default"),
        "--multiquery", "--query", query,
    ]
    subprocess.run(cmd, cwd=REPO_ROOT, check=True, stdin=subprocess.DEVNULL, timeout=60)


def _identity_values(people) -> str:
    rows = []
    for person in people:
        for user_id in person.user_ids:
            rows.append(f"({_sql_string_literal(user_id)}, {_sql_string_literal(person.person_id)}, now64(3))")
    return ", ".join(rows)


def _subscription_values(subscriptions) -> str:
    rows = []
    for sub in subscriptions:
        rows.append(
            "("
            f"{_sql_string_literal(sub.person_id)}, "
            f"{_sql_string_literal(sub.provider)}, "
            f"{_sql_string_literal(sub.plan)}, "
            # Quoted, not bare: ClickHouse parses a bare decimal literal as Float64 first, which is the one place a declared price could pick up a representation error on the way into a Decimal column.
            f"toDecimal64({_sql_string_literal(sub.monthly_price)}, 2), "
            f"{_sql_string_literal(sub.currency)}, "
            f"{sub.seats}, "
            f"toDate({_sql_string_literal(sub.valid_from.isoformat())}), "
            f"toDate({_sql_string_literal(sub.valid_to.isoformat())}), "
            "now64(3))"
        )
    return ", ".join(rows)


def main() -> None:
    compose_files = sys.argv[1:]
    config = _load_subscription_config()

    try:
        people, subscriptions = config.load()
    except config.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)

    env = _read_env(ENV_PATH)
    for required in ("CLICKHOUSE_BOOTSTRAP_USER", "CLICKHOUSE_BOOTSTRAP_PASSWORD"):
        if not env.get(required):
            print(f"error: {required} missing from .env - run `make init` first", file=sys.stderr)
            sys.exit(1)

    identity_values = _identity_values(people)
    subscription_values = _subscription_values(subscriptions)

    # Each table's TRUNCATE and INSERT ride one clickhouse-client invocation: there is no between-process window where a crash leaves the table emptied for good.
    # A failure inside the pair still empties the table, but the pair is idempotent as a unit - re-running `make subscriptions` fully recovers.
    identity_load = "TRUNCATE TABLE IF EXISTS person_identities"
    if identity_values:
        identity_load += f"; INSERT INTO person_identities (user_id, person_id, updated_at) VALUES {identity_values}"
    _clickhouse(compose_files, env, identity_load)

    subscription_load = "TRUNCATE TABLE IF EXISTS subscriptions"
    if subscription_values:
        subscription_load += (
            "; INSERT INTO subscriptions "
            "(person_id, provider, plan, monthly_price, currency, seats, valid_from, valid_to, updated_at) "
            f"VALUES {subscription_values}"
        )
    _clickhouse(compose_files, env, subscription_load)

    identity_count = sum(len(person.user_ids) for person in people)
    print(f"loaded {len(people)} people ({identity_count} key identities) and {len(subscriptions)} subscriptions")


if __name__ == "__main__":
    main()
