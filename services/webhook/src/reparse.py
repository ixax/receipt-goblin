"""CLI-only reparse tool - recomputes agent_events/agent_usage/agent_messages/
agent_invocations for events already in event_sources, using whatever the
current classification/parsing logic in clickhouse_ingest.py knows how to do
(reused directly, never re-derived here). Run via `make reparse-all` or
`make reparse SESSION=<session_id>` (see Makefile/docker-compose.yml
"webhook-reparse", profiles: ["tools"]) - no HTTP API, this only ever runs
as a one-shot `python -m src.reparse` invocation.

event_sources is the only source this reads from - .capture/*.json is
explicitly out of scope as a parsing/backfill input, now and always (see
schema.sql's event_sources comment and AGENTS.md).

Safe to re-run for the same session (or all data) any number of times:
agent_events/agent_usage/agent_messages/agent_invocations are all
ReplacingMergeTree, keyed so this run's `ingested_at`/`spawned_at` (now())
always wins over whatever was there before - see clickhouse/schema.sql.
"""
import argparse
import json
import logging

from .clickhouse_ingest import (
    _active_command_name_and_version,
    _agent_invocation_id,
    _agent_invocation_rows,
    _agent_name_and_version_for_invocation,
    _event_row,
    _insert_agent_invocations,
    _insert_event,
    _insert_message,
    _insert_usage,
    _message_row,
    _session_and_trace_id,
    _skill_name_and_version,
    _usage_row,
    get_client,
)
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("webhook.reparse")


def _reparse_one(client, litellm_call_id: str, source_session_id: str, raw_payload_full: str) -> None:
    try:
        payload = json.loads(raw_payload_full)
    except (TypeError, ValueError):
        logger.exception("failed to decode event_sources.raw_payload_full (litellm_call_id=%s)", litellm_call_id)
        return

    now = datetime.now(timezone.utc)
    try:
        session_id, trace_id = _session_and_trace_id(payload)
        session_id = session_id or source_session_id
        messages = payload.get("messages")

        _insert_agent_invocations(client, _agent_invocation_rows(session_id, messages, now=now))

        agent_invocation_id = _agent_invocation_id(payload)
        agent_name, agent_version = _agent_name_and_version_for_invocation(client, agent_invocation_id)
        skill_name, skill_version = _skill_name_and_version(payload)
        command_name, command_version = _active_command_name_and_version(messages)

        _insert_event(client, _event_row(
            payload, session_id, trace_id,
            agent_name, agent_version, skill_name, skill_version,
            command_name, command_version, agent_invocation_id, now,
        ))

        if payload.get("status") == "success":
            usage_row = _usage_row(
                payload, session_id, trace_id,
                agent_name, agent_version, skill_name, skill_version,
                command_name, command_version, agent_invocation_id, now,
            )
            if usage_row is not None:
                _insert_usage(client, usage_row)

            message_row = _message_row(
                payload, session_id, trace_id,
                agent_name, agent_version, skill_name, skill_version,
                command_name, command_version, agent_invocation_id, now,
            )
            if message_row is not None:
                _insert_message(client, message_row)
    except Exception:
        logger.exception("failed to reparse event (litellm_call_id=%s)", litellm_call_id)


def reparse(session_id: str = "") -> int:
    """session_id="" (the default) reparses every row in event_sources.
    Returns the number of rows processed."""
    client = get_client()
    query = (
        "SELECT litellm_call_id, session_id, raw_payload_full FROM event_sources "
        "WHERE {session_id:String} = '' OR session_id = {session_id:String} "
        "ORDER BY ingested_at"
    )
    result = client.query(query, parameters={"session_id": session_id})

    count = 0
    for call_id, row_session_id, raw_payload_full in result.result_rows:
        _reparse_one(client, call_id, row_session_id, raw_payload_full)
        count += 1
        if count % 500 == 0:
            logger.info("reparsed %d events so far...", count)

    logger.info("reparse complete (n=%d, session_id=%r)", count, session_id or "<all>")
    if count:
        logger.info(
            "run `OPTIMIZE TABLE agent_events FINAL`, `OPTIMIZE TABLE agent_usage FINAL`, "
            "`OPTIMIZE TABLE agent_messages FINAL`, `OPTIMIZE TABLE agent_invocations FINAL` "
            "to force the dedup merge immediately - most dashboard queries don't use FINAL "
            "(for performance) and would otherwise see stale rows until a background merge happens."
        )
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--session-id", default="",
        help="Reparse only this session_id's events. Omit (or set SESSION_ID='') to reparse all of event_sources.",
    )
    args = parser.parse_args()

    import os
    session_id = args.session_id or os.environ.get("SESSION_ID", "")
    reparse(session_id)


if __name__ == "__main__":
    main()
