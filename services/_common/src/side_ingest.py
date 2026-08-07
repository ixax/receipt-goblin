"""Session-metadata hooks (session-git-branch, plan-proposal) and LiteLLM's
native alerting webhook - the low-volume "side channel" queued onto
webhook:side-events (see services/_common/queue.yml) rather than
webhook:events.
Lives in common/ (not webhook/src/ or worker/src/) because both webhook
(server.py, to build the payload dict it enqueues) and worker (worker.py, to
build/insert the row on the way back out) need it - see
plans/side-channel-redis-and-describe-fix.md.

Row-builders are pure (no client, no I/O); the *_batch functions below are
the only part that touches ClickHouse, one multi-row client.insert() per
table per flush.
"""
import re
from datetime import datetime, timezone

from common import fastjson as json
from common.ingest_db import insert_rows_with_dlq_fallback

_GIT_BRANCH_COLUMNS = ["session_id", "git_branch", "git_repo", "issue_id", "captured_at"]
_GIT_BRANCH_COLUMN_TYPES = ["String", "String", "String", "String", "DateTime64(3)"]
_PLAN_PROPOSAL_COLUMNS = ["session_id", "plan_text", "captured_at"]
_PLAN_PROPOSAL_COLUMN_TYPES = ["String", "String", "DateTime64(3)"]
_LITELLM_ALERT_COLUMNS = [
    "received_at", "event", "event_group", "key_alias", "team_id", "user_id",
    "spend", "max_budget", "event_message", "raw_payload",
]
_LITELLM_ALERT_COLUMN_TYPES = [
    "DateTime64(3)", "LowCardinality(String)", "LowCardinality(String)", "String", "String", "String",
    "Nullable(Float64)", "Nullable(Float64)", "String", "String",
]

_ISSUE_ID_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9]{1,9}-\d+)(?![A-Za-z0-9])")


def _issue_id_from_branch(git_branch: str) -> str:
    """Ticket ID embedded in a branch name, e.g. "VIEW-12345", matched
    case-insensitively and uppercased.
    Trailing boundary is a negative lookahead, not \\b: \\b treats
    digit/underscore as the same word class and would miss
    "VIEW-100500_my-branch"."""
    match = _ISSUE_ID_RE.search(git_branch or "")
    return match.group(1).upper() if match else ""


def _git_branch_row(payload: dict, now: datetime) -> list:
    session_id = payload.get("session_id") or ""
    git_branch = payload.get("git_branch") or ""
    git_repo = payload.get("git_repo") or ""
    return [session_id, git_branch, git_repo, _issue_id_from_branch(git_branch), now]


def _plan_proposal_row(payload: dict, now: datetime) -> list:
    return [payload.get("session_id") or "", payload.get("plan_text") or "", now]


def _litellm_alert_row(payload: dict, now: datetime) -> list:
    """Only the budget-event shape is fully documented by LiteLLM's own
    docs - other alert types (llm_exceptions/outage_alerts/db_exceptions/...)
    likely carry different fields, so raw_payload keeps the full body
    regardless of which fields above happen to be present."""
    return [
        now,
        payload.get("event") or "",
        payload.get("event_group") or "",
        payload.get("key_alias") or "",
        payload.get("team_id") or "",
        payload.get("user_id") or "",
        payload.get("spend"),
        payload.get("max_budget"),
        payload.get("event_message") or "",
        json.dumps(payload, default=str).decode(),
    ]


def insert_git_branch_batch(client, rows: list[list]) -> None:
    """session_id is column 0 - see _git_branch_row."""
    insert_rows_with_dlq_fallback(
        client, "session_git_branch", rows, _GIT_BRANCH_COLUMNS, _GIT_BRANCH_COLUMN_TYPES, session_id_idx=0,
    )


def insert_git_branch_payloads(client, payloads) -> None:
    """The main-path counterpart to insert_git_branch_batch: session_git_branch rows for calls that had no hook.

    Codex has no session-git-branch hook, so ingest_parsing.codex_git_payload derives the same payload shape from
    the turn metadata already on its LiteLLM call - see that function for what it can and cannot recover.
    `payloads` may contain None (a call that carried no usable git metadata), which is skipped.
    Lives here rather than in ingest_db, which side_ingest already imports and so cannot be imported back from;
    the owners of a built-event batch (worker.py, reparse.py) call it themselves.
    Deduped by session_id within the batch, last one winning, matching how write_dimensions treats its own
    ReplacingMergeTree rows.
    """
    now = datetime.now(timezone.utc)
    payloads_by_session = {
        payload["session_id"]: payload
        for payload in payloads
        if isinstance(payload, dict) and payload.get("session_id")
    }
    insert_git_branch_batch(client, [_git_branch_row(p, now) for p in payloads_by_session.values()])


def insert_git_branch_from_events(client, events: list[dict]) -> None:
    """insert_git_branch_payloads over a batch of build_event() outputs, each carrying its payload under "git_branch"."""
    insert_git_branch_payloads(client, (event.get("git_branch") for event in events))


def insert_plan_proposal_batch(client, rows: list[list]) -> None:
    """session_id is column 0 - see _plan_proposal_row."""
    insert_rows_with_dlq_fallback(
        client, "plan_proposals", rows, _PLAN_PROPOSAL_COLUMNS, _PLAN_PROPOSAL_COLUMN_TYPES, session_id_idx=0,
    )


def insert_litellm_alert_batch(client, rows: list[list]) -> None:
    """No call_id/session_id on an alert row - see _litellm_alert_row."""
    insert_rows_with_dlq_fallback(client, "litellm_alerts", rows, _LITELLM_ALERT_COLUMNS, _LITELLM_ALERT_COLUMN_TYPES)
