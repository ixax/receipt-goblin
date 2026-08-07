"""Explicit, destructive reset for agent-tracking data only."""

import argparse

import clickhouse_connect
from common.config.clickhouse import (
    CLICKHOUSE_DATABASE,
    CLICKHOUSE_HOST,
    CLICKHOUSE_PORT,
)
from common.logging_config import create_logger

from .config import CLICKHOUSE_BOOTSTRAP_PASSWORD, CLICKHOUSE_BOOTSTRAP_USER

logger = create_logger("clickhouse.reset_data")

RESET_CONFIRMATION = "RESET-TRACKING-DATA"

# Keep schema, migrations, users, dictionaries, and dashboard configuration.
# Legacy migration tables are included so a reset also removes retained data
# from old/recovery copies when they exist.
TRACKING_TABLES = (
    "agent_invocations",
    "session_git_branch",
    "plan_proposals",
    "ai_gateway_groups",
    "ai_gateway_users",
    "clients",
    "agent_events",
    "agent_usage",
    "agent_messages",
    "ingest_raw",
    "ingest_dlq",
    "ingest_dlq_resolved",
    "litellm_alerts",
    "event_sources",
    "ingest_failures",
    "agent_events_old",
    "agent_usage_old",
    "agent_messages_old",
    "ingest_dlq_old",
    "agent_events_new",
    "agent_usage_new",
    "agent_messages_new",
    "ingest_dlq_new",
)


def reset_tracking_data(client, confirmation: str) -> None:
    if confirmation != RESET_CONFIRMATION:
        raise ValueError("tracking reset confirmation does not match")

    for table in TRACKING_TABLES:
        client.command(f"TRUNCATE TABLE IF EXISTS {table} SYNC")
    logger.info("tracking data reset complete", extra={"table_count": len(TRACKING_TABLES)})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    if args.confirm != RESET_CONFIRMATION:
        parser.error(f"--confirm must equal {RESET_CONFIRMATION}")

    client = clickhouse_connect.get_client(
        host=CLICKHOUSE_HOST,
        port=CLICKHOUSE_PORT,
        username=CLICKHOUSE_BOOTSTRAP_USER,
        password=CLICKHOUSE_BOOTSTRAP_PASSWORD,
        database=CLICKHOUSE_DATABASE,
    )
    try:
        reset_tracking_data(client, args.confirm)
    finally:
        client.close()


if __name__ == "__main__":
    main()
