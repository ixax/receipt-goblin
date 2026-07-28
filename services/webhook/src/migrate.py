"""Idempotent ClickHouse migration runner - applies every
services/clickhouse/migrations/*.sql file at most once, then exits. Explicit-
only: gated behind the `tools` compose profile, so a plain `docker compose
up` never starts it and no other service `depends_on` it - run it via `make
migrate` (after `make init` on a fresh clone, or after adding a new migration
file).

This module never touches ClickHouse users/roles/grants - that's `make
init`'s job alone (services/init/init_clickhouse_users.py, reading
services/init/config.yml), run once, explicitly, not on every stack start.
A fresh volume's `docker compose up` without having run `make init` first
fails loudly (ACCESS_DENIED/AUTHENTICATION_FAILED from whichever service
can't authenticate) - that's expected, not a case this module works around.

Applied migrations are recorded in schema_migrations and never re-executed.
Destructive recreate+swap migrations (like 001_replacing_mergetree.sql) also
get a SKIP_CHECKS guard so a fresh volume - already matching the
post-migration shape via schema.sql - skips running that SQL at all.

run_migration() passes CLICKHOUSE_DATABASE as a query parameter to every
statement in every migrations/*.sql file, so a migration can reference
{clickhouse_database:String} directly in an ordinary expression position
(e.g. a WHERE clause). A statement that doesn't reference it is unaffected -
unused named parameters are just ignored.

That substitution does NOT work everywhere, though - confirmed against a
live server: `{name:Type}` is rejected with a SYNTAX_ERROR
("Expected substitution type (identifier)") anywhere inside a Dictionary's
SOURCE(CLICKHOUSE(...)) clause, even for something as simple as its DB
parameter - that clause's USER/PASSWORD/DB/QUERY sub-grammar only accepts
literals, not the general expression position {name:Type} hooks into
(unlike e.g. CREATE USER ... IDENTIFIED BY {password:String}, which does
work - not used in this file, but relevant if you're comparing this against
init_clickhouse_users.py's own CREATE USER statements). So
_create_dictionaries_once() below builds its CREATE DICTIONARY statements as
an f-string with the ingest role's credentials embedded directly (via
_sql_string_literal()'s escaping, not client.query's parameters=) rather
than going through a migrations/*.sql file at all.

_create_dictionaries_once() uses its own bootstrap-superuser connection, the
same one this module uses for every other statement - schema/migrations
bookkeeping needs DDL rights no least-privilege role should hold, and this
module doesn't have (nor need) an ingest-role client of its own at all; it
only reads CLICKHOUSE_INGEST_USER/_PASSWORD as plain strings to embed in the
Dictionary's SOURCE(CLICKHOUSE(...)) clause.
"""
import logging
import os
from pathlib import Path

import clickhouse_connect

from .config import CLICKHOUSE_DATABASE, CLICKHOUSE_HOST, CLICKHOUSE_PORT

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("clickhouse.migrate")

MIGRATIONS_DIR = Path(os.environ.get("MIGRATIONS_DIR", "/app/migrations"))

CLICKHOUSE_BOOTSTRAP_USER = os.environ["CLICKHOUSE_BOOTSTRAP_USER"]
CLICKHOUSE_BOOTSTRAP_PASSWORD = os.environ["CLICKHOUSE_BOOTSTRAP_PASSWORD"]


def _bootstrap_client():
    return clickhouse_connect.get_client(
        host=CLICKHOUSE_HOST,
        port=CLICKHOUSE_PORT,
        username=CLICKHOUSE_BOOTSTRAP_USER,
        password=CLICKHOUSE_BOOTSTRAP_PASSWORD,
    )


def _already_replacing_mergetree(client) -> bool:
    """True once agent_events is already ReplacingMergeTree (migrated
    already, or created that way by schema.sql on a fresh volume)."""
    rows = client.query(
        "SELECT engine FROM system.tables WHERE database = currentDatabase() AND name = 'agent_events'"
    ).result_rows
    return bool(rows) and rows[0][0] == "ReplacingMergeTree"


def _ingest_dlq_already_partitioned(client) -> bool:
    """True once ingest_dlq already has a partition key (migrated already,
    or created that way by schema.sql on a fresh volume) - guards against
    011_ingest_dlq_partition_by.sql's recreate+rename running again on a
    stack that already has the target shape."""
    rows = client.query(
        "SELECT partition_key FROM system.tables WHERE database = currentDatabase() AND name = 'ingest_dlq'"
    ).result_rows
    return bool(rows) and rows[0][0] != ""


def _event_sources_already_renamed(client) -> bool:
    """True when event_sources doesn't exist under its old name - either a
    fresh volume (schema.sql already creates ingest_raw/ingest_dlq directly,
    the old name never existed) or a stack that already ran
    007_rename_ingest_tables. Both 006 (ALTER TABLE event_sources ...) and
    007 (RENAME TABLE event_sources TO ...) target the pre-rename name, so
    both fail outright on a fresh volume without this guard - confirmed via
    a from-scratch clickhouse-data volume: 006 raised UNKNOWN_TABLE on
    event_sources and blocked every migration after it, 007 included."""
    rows = client.query(
        "SELECT count() FROM system.tables WHERE database = currentDatabase() AND name = 'event_sources'"
    ).result_rows
    return rows[0][0] == 0


# Maps a migration stem to callable(client) -> bool: True means record as
# applied without running its SQL (target already has the shape it'd
# produce). Unlisted migrations are assumed safe `IF NOT EXISTS` DDL.
SKIP_CHECKS = {
    "001_replacing_mergetree": _already_replacing_mergetree,
    "006_event_sources_lower_compression": _event_sources_already_renamed,
    "007_rename_ingest_tables": _event_sources_already_renamed,
    "011_ingest_dlq_partition_by": _ingest_dlq_already_partitioned,
}


# Available to every statement in every migrations/*.sql file - see this
# module's docstring. A statement that doesn't reference this name is
# unaffected.
_MIGRATION_PARAMETERS = {
    "clickhouse_database": CLICKHOUSE_DATABASE,
}


def _statements(sql_text: str) -> list[str]:
    lines = []
    for line in sql_text.splitlines():
        idx = line.find("--")
        lines.append(line[:idx] if idx != -1 else line)
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def _mark_applied(client, version: str) -> None:
    client.insert("schema_migrations", [[version]], column_names=["version"])


def _is_recorded(client, version: str) -> bool:
    result = client.query(
        "SELECT count() FROM schema_migrations WHERE version = {version:String}",
        parameters={"version": version},
    )
    return result.result_rows[0][0] > 0


def run_migration(client, path: Path) -> None:
    version = path.stem

    if _is_recorded(client, version):
        logger.info("skip %s (already recorded in schema_migrations)", version)
        return

    skip_check = SKIP_CHECKS.get(version)
    if skip_check and skip_check(client):
        logger.info("skip %s (target already has the post-migration shape)", version)
        _mark_applied(client, version)
        return

    logger.info("applying %s", version)
    for statement in _statements(path.read_text()):
        client.command(statement, parameters=_MIGRATION_PARAMETERS)
    _mark_applied(client, version)
    logger.info("applied %s", version)


def _sql_string_literal(value: str) -> str:
    """Escapes value for embedding directly in SQL text as '...' - only
    needed where {name:Type} substitution doesn't parse (SOURCE(CLICKHOUSE(
    ...)) - see this module's docstring); everywhere else, prefer
    parameters= instead of this."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


# name -> (source query, column defs, PRIMARY KEY column, LAYOUT). Each
# source query is exactly the same LEFT JOIN subquery every dashboard panel
# in agents_overview.json currently repeats inline (session_git_branch,
# ai_gateway_users, ai_gateway_groups) - a Dictionary runs that same GROUP
# BY/argMax scan once per LIFETIME refresh (60-120s) instead of once per
# dashboard query. session_git_branch_dict keys on session_id_hash (added
# by migrations/008_session_id_hash.sql) rather than session_id itself:
# HASHED() on a UInt64 key avoids the packing overhead COMPLEX_KEY_HASHED
# needs for a String key, and this schema already established
# id = cityHash64(value) as its convention for a high-cardinality string
# join key (see `clients`). ai_gateway_users/groups stay on their plain
# String primary key - already LowCardinality, low cardinality, no
# measurable benefit from a surrogate key.
_DICTIONARIES = {
    "session_git_branch_dict": (
        "SELECT session_id_hash, "
        "argMax(git_branch, captured_at) AS git_branch, "
        "argMax(git_repo, captured_at) AS git_repo, "
        "argMax(issue_id, captured_at) AS issue_id "
        "FROM session_git_branch GROUP BY session_id_hash",
        "(session_id_hash UInt64, git_branch String, git_repo String, issue_id String)",
        "session_id_hash",
    ),
    "ai_gateway_users_dict": (
        "SELECT user_id, "
        "argMax(user_name, updated_at) AS user_name, "
        "argMax(group_id, updated_at) AS group_id "
        "FROM ai_gateway_users GROUP BY user_id",
        "(user_id String, user_name String, group_id String)",
        "user_id",
    ),
    "ai_gateway_groups_dict": (
        "SELECT group_id, argMax(group_name, updated_at) AS group_name "
        "FROM ai_gateway_groups GROUP BY group_id",
        "(group_id String, group_name String)",
        "group_id",
    ),
}


def _create_dictionaries_once(client) -> None:
    """One-time: creates the Dictionaries listed in _DICTIONARIES above.
    HOST/PORT are deliberately omitted from SOURCE(CLICKHOUSE(...)) - with
    them unset, ClickHouse reads its own local table directly instead of
    opening a native-protocol TCP connection to itself. USER/PASSWORD are
    still required even for that local path (confirmed: a plain
    SOURCE(CLICKHOUSE(QUERY '...')) with neither tries to connect as
    `default` with an empty password and fails AUTHENTICATION_FAILED
    against this stack's password-protected `default` user) - and, per this
    module's docstring, {name:Type} substitution isn't usable inside
    SOURCE(CLICKHOUSE(...)) at all, so they're embedded as escaped string
    literals instead. The embedded identity is the `ingest` role - the only
    role with SELECT on all three source tables (session_git_branch,
    ai_gateway_users, ai_gateway_groups).

    IF NOT EXISTS (not CREATE OR REPLACE): a dictionary already serving
    dashboard queries shouldn't be silently swapped by a migration re-run.
    """
    version = "009_dashboard_dictionaries"
    if _is_recorded(client, version):
        logger.info("skip %s (already recorded in schema_migrations)", version)
        return
    user_lit = _sql_string_literal(os.environ["CLICKHOUSE_INGEST_USER"])
    password_lit = _sql_string_literal(os.environ["CLICKHOUSE_INGEST_PASSWORD"])
    db_lit = _sql_string_literal(CLICKHOUSE_DATABASE)
    for name, (query, columns, primary_key) in _DICTIONARIES.items():
        client.command(
            f"CREATE DICTIONARY IF NOT EXISTS {name} {columns} "
            f"PRIMARY KEY {primary_key} "
            f"SOURCE(CLICKHOUSE(USER {user_lit} PASSWORD {password_lit} DB {db_lit} "
            f"QUERY {_sql_string_literal(query)})) "
            "LAYOUT(HASHED()) "
            "LIFETIME(MIN 60 MAX 120)"
        )
        logger.info("created dictionary %s", name)
    _mark_applied(client, version)
    logger.info("applied %s", version)


def main() -> None:
    client = _bootstrap_client()
    try:
        client.command(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version String, applied_at DateTime64(3) DEFAULT now64(3)) "
            "ENGINE = MergeTree ORDER BY version"
        )
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            run_migration(client, path)
        _create_dictionaries_once(client)
    finally:
        client.close()
    logger.info("all migrations up to date")


if __name__ == "__main__":
    main()
