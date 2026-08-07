"""CLI-only reparse tool - recomputes agent_events/agent_usage/agent_messages/
agent_invocations/ai_gateway_users/ai_gateway_groups for events already in
ingest_raw, reusing common.ingest_db's reparse_event() classification logic
directly.
Run via `make reparse-all` or `make reparse SESSION=<session_id>`; no HTTP
API, one-shot `python -m src.reparse` only.

ingest_raw is the only source read; .capture/*.json is out of scope
(see AGENTS.md).

Safe to re-run any number of times: the target tables are all
ReplacingMergeTree, keyed so this run's now() always wins.
"""
import argparse
from datetime import datetime, timezone

from common import fastjson as json
from common.ingest_adapters import CLAUDE_TRANSCRIPT_ADAPTER, build_ingest_event
from common.ingest_db import get_client, ingest_built_events, reparse_event
from common.logging_config import create_logger

from .config import REPARSE_CHUNK_SIZE

logger = create_logger("webhook.reparse")


def _reparse_one(client, litellm_call_id: str, source_session_id: str, raw_payload_full: str) -> None:
    try:
        payload = json.loads(raw_payload_full)
    except (TypeError, ValueError):
        logger.exception("failed to decode ingest_raw.raw_payload_full (litellm_call_id=%s)", litellm_call_id)
        return

    now = datetime.now(timezone.utc)
    try:
        if payload.get("schema_version") == 1 and payload.get("source") in {
            "claude_desktop",
            "claude_remote_control",
        }:
            ingest_built_events(
                client,
                [build_ingest_event(CLAUDE_TRANSCRIPT_ADAPTER, payload)],
            )
        else:
            reparse_event(client, payload, litellm_call_id, source_session_id, now)
    except Exception:
        logger.exception("failed to reparse event (litellm_call_id=%s)", litellm_call_id)


def reparse(session_id: str = "") -> int:
    """session_id="" reparses every row in ingest_raw.
    Returns rows processed.

    Pages REPARSE_CHUNK_SIZE rows at a time (keyset pagination on
    litellm_call_id) instead of pulling every raw_payload_full in one
    query, which used to OOM-kill the container.
    """
    client = get_client()
    query = (
        "SELECT litellm_call_id, session_id, raw_payload_full FROM ingest_raw "
        "WHERE ({session_id:String} = '' OR session_id = {session_id:String}) "
        "AND litellm_call_id > {cursor:String} "
        "ORDER BY litellm_call_id "
        "LIMIT {chunk_size:UInt32}"
    )

    count = 0
    cursor = ""
    while True:
        result = client.query(query, parameters={
            "session_id": session_id, "cursor": cursor, "chunk_size": REPARSE_CHUNK_SIZE,
        })
        rows = result.result_rows
        if not rows:
            break
        for call_id, row_session_id, raw_payload_full in rows:
            _reparse_one(client, call_id, row_session_id, raw_payload_full)
            count += 1
            if count % 500 == 0:
                logger.info("reparsed %d events so far...", count)
        cursor = rows[-1][0]

    logger.info("reparse complete (n=%d, session_id=%r)", count, session_id or "<all>")
    if count:
        logger.info(
            "run `OPTIMIZE TABLE agent_events FINAL`, `OPTIMIZE TABLE agent_usage FINAL`, "
            "`OPTIMIZE TABLE agent_messages FINAL`, `OPTIMIZE TABLE agent_invocations FINAL`, "
            "`OPTIMIZE TABLE ai_gateway_users FINAL`, `OPTIMIZE TABLE ai_gateway_groups FINAL` "
            "to force the dedup merge immediately - most dashboard queries don't use FINAL "
            "(for performance) and would otherwise see stale rows until a background merge happens."
        )
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--session-id", default="",
        help="Reparse only this session_id's events. Omit (or set SESSION_ID='') to reparse all of ingest_raw.",
    )
    args = parser.parse_args()

    import os
    session_id = args.session_id or os.environ.get("SESSION_ID", "")
    reparse(session_id)


if __name__ == "__main__":
    main()
