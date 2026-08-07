import pytest
from src import reset_data


class _FakeClient:
    def __init__(self):
        self.commands = []

    def command(self, sql):
        self.commands.append(sql)


def test_reset_tracking_data_rejects_wrong_confirmation():
    client = _FakeClient()

    with pytest.raises(ValueError, match="confirmation"):
        reset_data.reset_tracking_data(client, "wrong")

    assert client.commands == []


def test_reset_tracking_data_truncates_only_allowlisted_tracking_tables():
    client = _FakeClient()

    reset_data.reset_tracking_data(client, reset_data.RESET_CONFIRMATION)

    assert client.commands == [
        f"TRUNCATE TABLE IF EXISTS {table} SYNC"
        for table in reset_data.TRACKING_TABLES
    ]
    assert "schema_migrations" not in reset_data.TRACKING_TABLES
    assert "ingest_raw" in reset_data.TRACKING_TABLES
    assert "agent_usage" in reset_data.TRACKING_TABLES
    assert "agent_events_old" in reset_data.TRACKING_TABLES
    assert "ingest_dlq_old" in reset_data.TRACKING_TABLES
