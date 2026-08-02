"""Webhook-only ingest entry points: session-metadata hooks
(session-git-branch, plan-proposal) and LiteLLM's native alerting webhook.
Moved out of common/ingest_db.py - each of these has exactly one caller
(server.py) and is self-contained, unlike ingest_events_batch()/
reparse_event() which share ~10-15 private helpers with the worker/reparse
paths (see plans/common-module-cleanup-refactor.md).
"""
import logging
import re
from datetime import datetime, timezone

from common import fastjson as json
from common.ingest_db import get_client

logger = logging.getLogger("webhook.ingest")

_GIT_BRANCH_COLUMNS = ["session_id", "git_branch", "git_repo", "issue_id", "captured_at"]
_PLAN_PROPOSAL_COLUMNS = ["session_id", "plan_text", "captured_at"]
_LITELLM_ALERT_COLUMNS = [
    "received_at", "event", "event_group", "key_alias", "team_id", "user_id",
    "spend", "max_budget", "event_message", "raw_payload",
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


def _insert_git_branch(client, row: list) -> None:
    client.insert("session_git_branch", [row], column_names=_GIT_BRANCH_COLUMNS)


def _insert_plan_proposal(client, row: list) -> None:
    client.insert("plan_proposals", [row], column_names=_PLAN_PROPOSAL_COLUMNS)


def _insert_litellm_alert(client, row: list) -> None:
    client.insert("litellm_alerts", [row], column_names=_LITELLM_ALERT_COLUMNS)


def ingest_git_branch(session_id: str, git_branch: str, git_repo: str = "") -> None:
    """Insert a session's git branch/repo (hooks/report_git_branch.py).
    Never raises - a tracking failure must not surface to the CLI session."""
    try:
        client = get_client()
        issue_id = _issue_id_from_branch(git_branch)
        _insert_git_branch(client, [session_id, git_branch, git_repo, issue_id, datetime.now(timezone.utc)])
    except Exception:
        logger.exception("failed to ingest git branch (session_id=%s)", session_id)


def ingest_plan_proposal(session_id: str, plan_text: str) -> None:
    """Insert an ExitPlanMode call's plan text (hooks/report_plan_proposal.py).
    Never raises - a tracking failure must not surface to the CLI session."""
    try:
        client = get_client()
        _insert_plan_proposal(client, [session_id, plan_text, datetime.now(timezone.utc)])
    except Exception:
        logger.exception("failed to ingest plan proposal (session_id=%s)", session_id)


def ingest_litellm_alert(payload: dict) -> None:
    """Insert one LiteLLM native-alerting webhook event (budget/outage/
    exception/hang signals - see general_settings.alerting in
    services/litellm/config.yaml).
    Direct-to-ClickHouse, not queued through Redis: these events are rare
    compared to per-call metrics traffic, same low-volume pattern as
    ingest_git_branch/ingest_plan_proposal.
    Never raises - a tracking failure must not surface to LiteLLM's own
    retry logic for its alerting webhook.

    Only the budget-event shape is fully documented by LiteLLM's own docs -
    other alert types (llm_exceptions/outage_alerts/db_exceptions/...)
    likely carry different fields, so raw_payload keeps the full body
    regardless of which fields above happen to be present."""
    try:
        client = get_client()
        _insert_litellm_alert(client, [
            datetime.now(timezone.utc),
            payload.get("event") or "",
            payload.get("event_group") or "",
            payload.get("key_alias") or "",
            payload.get("team_id") or "",
            payload.get("user_id") or "",
            payload.get("spend"),
            payload.get("max_budget"),
            payload.get("event_message") or "",
            json.dumps(payload, default=str).decode(),
        ])
    except Exception:
        logger.exception("failed to ingest LiteLLM alert (event=%s)", payload.get("event", ""))
