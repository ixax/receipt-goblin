"""Unit tests for ingest.py's webhook-only ingest entry points.
Moved out of _common/tests/test_ingest_parsing.py's `_issue_id_from_branch`
cases when that function moved here (see
plans/common-module-cleanup-refactor.md) - ingest_git_branch/
ingest_plan_proposal/ingest_litellm_alert themselves had no prior test
coverage in _common/tests/test_ingest_db.py to move."""

from src import ingest


# ---------------------------------------------------------------------------
# _issue_id_from_branch
# ---------------------------------------------------------------------------

def test_issue_id_from_branch_success_ticket_at_start():
    assert ingest._issue_id_from_branch("VIEW-12345-my-super-branch") == "VIEW-12345"


def test_issue_id_from_branch_success_ticket_at_end():
    assert ingest._issue_id_from_branch("my-super-branch-VIEW-12345") == "VIEW-12345"


def test_issue_id_from_branch_success_normalizes_case():
    assert ingest._issue_id_from_branch("fix-view-12345-typo") == "VIEW-12345"


def test_issue_id_from_branch_unsuccess_no_ticket_returns_empty():
    assert ingest._issue_id_from_branch("my-super-branch") == ""
