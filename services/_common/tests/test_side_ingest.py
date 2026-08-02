"""Unit tests for side_ingest.py - the row-builders and batch-inserters
behind the side-channel stream (session-git-branch, plan-proposal, LiteLLM
native alerts).
_issue_id_from_branch cases moved from the now-deleted
services/webhook/tests/test_ingest.py when ingest.py's logic relocated to
common/ - see plans/side-channel-redis-and-describe-fix.md."""

from datetime import datetime, timezone

from common import side_ingest


class _FakeClient:
    def __init__(self):
        self.inserts = []

    def insert(self, table, rows, column_names, column_type_names=None):
        self.inserts.append((table, rows, column_names))


_NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# _issue_id_from_branch
# ---------------------------------------------------------------------------

def test_issue_id_from_branch_success_ticket_at_start():
    assert side_ingest._issue_id_from_branch("VIEW-12345-my-super-branch") == "VIEW-12345"


def test_issue_id_from_branch_success_ticket_at_end():
    assert side_ingest._issue_id_from_branch("my-super-branch-VIEW-12345") == "VIEW-12345"


def test_issue_id_from_branch_success_normalizes_case():
    assert side_ingest._issue_id_from_branch("fix-view-12345-typo") == "VIEW-12345"


def test_issue_id_from_branch_unsuccess_no_ticket_returns_empty():
    assert side_ingest._issue_id_from_branch("my-super-branch") == ""


# ---------------------------------------------------------------------------
# row-builders
# ---------------------------------------------------------------------------

def test_git_branch_row_success_builds_row_with_issue_id():
    payload = {"session_id": "sess-1", "git_branch": "VIEW-12345-fix", "git_repo": "receipt-goblin"}
    assert side_ingest._git_branch_row(payload, _NOW) == ["sess-1", "VIEW-12345-fix", "receipt-goblin", "VIEW-12345", _NOW]


def test_git_branch_row_success_missing_fields_default_to_empty_string():
    assert side_ingest._git_branch_row({}, _NOW) == ["", "", "", "", _NOW]


def test_plan_proposal_row_success_builds_row():
    payload = {"session_id": "sess-1", "plan_text": "do the thing"}
    assert side_ingest._plan_proposal_row(payload, _NOW) == ["sess-1", "do the thing", _NOW]


def test_litellm_alert_row_success_builds_row_and_keeps_raw_payload():
    payload = {
        "event": "budget_exceeded", "event_group": "budget", "key_alias": "key-1",
        "team_id": "team-1", "user_id": "user-1", "spend": 105.0, "max_budget": 100.0,
        "event_message": "over budget",
    }
    row = side_ingest._litellm_alert_row(payload, _NOW)
    assert row[:9] == [
        _NOW, "budget_exceeded", "budget", "key-1", "team-1", "user-1", 105.0, 100.0, "over budget",
    ]
    assert "budget_exceeded" in row[9]


def test_litellm_alert_row_success_missing_fields_default_sensibly():
    row = side_ingest._litellm_alert_row({}, _NOW)
    assert row[:9] == [_NOW, "", "", "", "", "", None, None, ""]


# ---------------------------------------------------------------------------
# batch-inserters
# ---------------------------------------------------------------------------

def test_insert_git_branch_batch_success_inserts_rows():
    client = _FakeClient()
    rows = [side_ingest._git_branch_row({"session_id": "s1"}, _NOW)]

    side_ingest.insert_git_branch_batch(client, rows)

    assert client.inserts == [("session_git_branch", rows, side_ingest._GIT_BRANCH_COLUMNS)]


def test_insert_git_branch_batch_unsuccess_empty_rows_skips_insert():
    client = _FakeClient()

    side_ingest.insert_git_branch_batch(client, [])

    assert client.inserts == []


def test_insert_plan_proposal_batch_success_inserts_rows():
    client = _FakeClient()
    rows = [side_ingest._plan_proposal_row({"session_id": "s1"}, _NOW)]

    side_ingest.insert_plan_proposal_batch(client, rows)

    assert client.inserts == [("plan_proposals", rows, side_ingest._PLAN_PROPOSAL_COLUMNS)]


def test_insert_litellm_alert_batch_success_inserts_rows():
    client = _FakeClient()
    rows = [side_ingest._litellm_alert_row({"event": "budget_exceeded"}, _NOW)]

    side_ingest.insert_litellm_alert_batch(client, rows)

    assert client.inserts == [("litellm_alerts", rows, side_ingest._LITELLM_ALERT_COLUMNS)]
