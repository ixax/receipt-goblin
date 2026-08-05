"""CLI-only DLQ-replay tool - re-inserts ingest_dlq rows back into their original target table (agent_events/agent_usage/agent_messages), reusing common.ingest_db's replay_dlq_row() directly.
Run via `make reparse-dlq` or `make reparse-dlq STAGE=<table>`; no HTTP API, one-shot `python -m src.reparse_dlq` only.

Faster than `reparse.py` for routine incident recovery: it replays only the rows that actually failed (ingest_dlq.raw_row is the already-transformed row that insert_rows_with_dlq_fallback rejected), instead of rescanning all of ingest_raw.
Use reparse.py instead when the failure could have affected dimension/invocation resolution too, not just these three tables.

Safe to re-run any number of times: the target tables are all ReplacingMergeTree, keyed so this run's now() always wins.
ingest_dlq itself is never deleted from - replayed rows stay in it as an append-only forensic log.
"""
import argparse

from common import fastjson as json
from common.ingest_db import get_client, mark_dlq_rows_resolved, replay_dlq_row
from common.logging_config import create_logger

from .config import REPARSE_CHUNK_SIZE

logger = create_logger("webhook.reparse_dlq")


def _replay_one(client, stage: str, litellm_call_id: str, raw_row: str) -> bool:
    try:
        row = json.loads(raw_row)
    except (TypeError, ValueError):
        logger.exception("failed to decode ingest_dlq.raw_row (stage=%s, litellm_call_id=%s)", stage, litellm_call_id)
        return False

    try:
        replay_dlq_row(client, stage, row)
    except Exception:
        logger.exception("failed to replay DLQ row (stage=%s, litellm_call_id=%s)", stage, litellm_call_id)
        return False
    return True


def reparse_dlq(stage: str = "") -> int:
    """stage="" replays every not-yet-resolved row in ingest_dlq, across all stages.
    Returns rows processed (attempted, not necessarily succeeded).

    Pages REPARSE_CHUNK_SIZE rows at a time from ingest_dlq_unresolved (see
    migrations/015_ingest_dlq_resolved.sql) - no cursor needed.
    Each successfully replayed row gets marked resolved before the next page
    is fetched, so it permanently drops out of ingest_dlq_unresolved and the
    same plain LIMIT query naturally returns different rows next iteration.
    That's what makes pagination terminate: a plain ORDER BY/LIMIT cursor
    over occurred_at isn't reliable when many rows share the same
    millisecond timestamp (ClickHouse doesn't guarantee stable tie-breaking
    across pages under parallel execution), which caused the 2026-08-04 OOM
    by re-selecting the same backlog dozens of times.
    """
    client = get_client()
    query = (
        "SELECT occurred_at, stage, litellm_call_id, raw_row FROM ingest_dlq_unresolved "
        "WHERE ({stage:String} = '' OR stage = {stage:String}) "
        "LIMIT {chunk_size:UInt32}"
    )

    count = 0
    succeeded = 0
    while True:
        result = client.query(query, parameters={"stage": stage, "chunk_size": REPARSE_CHUNK_SIZE})
        rows = result.result_rows
        if not rows:
            break
        resolved_this_page = []
        for occurred_at, row_stage, litellm_call_id, raw_row in rows:
            count += 1
            if _replay_one(client, row_stage, litellm_call_id, raw_row):
                succeeded += 1
                resolved_this_page.append((occurred_at, row_stage, litellm_call_id))
        if not resolved_this_page:
            logger.warning(
                "stopping: a page of %d row(s) made zero progress (all still failing) - "
                "check the logged errors above rather than looping forever", len(rows),
            )
            break
        mark_dlq_rows_resolved(client, resolved_this_page)
        if count % 500 == 0:
            logger.info("replayed %d DLQ rows so far...", count)

    logger.info(
        "reparse-dlq complete (n=%d, succeeded=%d, still_failing=%d, stage=%r)",
        count, succeeded, count - succeeded, stage or "<all>",
    )
    if succeeded:
        logger.info(
            "run `OPTIMIZE TABLE agent_events FINAL`, `OPTIMIZE TABLE agent_usage FINAL`, "
            "`OPTIMIZE TABLE agent_messages FINAL` to force the dedup merge immediately - "
            "most dashboard queries don't use FINAL (for performance) and would otherwise "
            "see stale rows until a background merge happens."
        )
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage", default="",
        help="Replay only this stage's (target table's) rows. Omit (or set STAGE='') to replay all of ingest_dlq.",
    )
    args = parser.parse_args()

    import os
    stage = args.stage or os.environ.get("STAGE", "")
    reparse_dlq(stage)


if __name__ == "__main__":
    main()
