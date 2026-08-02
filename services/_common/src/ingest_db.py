"""ClickHouse-I/O half of ingesting LiteLLM's StandardLoggingPayload webhook
events - everything that opens a client and does INSERT/SELECT.
See ingest_parsing.py's docstring for the DB-free parsing/classification
half this module imports from, and AGENTS.md for the overall pipeline.
"""
import logging
import time
from datetime import datetime, timezone

import clickhouse_connect

from common import fastjson as json
from common.config.clickhouse import CLICKHOUSE_DATABASE, CLICKHOUSE_HOST, CLICKHOUSE_PORT
from common.config.clickhouse_credentials import CLICKHOUSE_PASSWORD, CLICKHOUSE_USER
from common.ingest_parsing import (
    EventContext,
    _EVENT_AGENT_NAME_IDX,
    _EVENT_AGENT_VERSION_IDX,
    _EVENT_CALL_ID_IDX,
    _EVENT_CLIENT_ID_IDX,
    _EVENT_COLUMNS,
    _EVENT_INGESTED_AT_IDX,
    _EVENT_SESSION_ID_IDX,
    _EVENT_SKILL_NAME_IDX,
    _EVENT_SKILL_VERSION_IDX,
    _EVENT_TIMESTAMP_IDX,
    _GROUP_COLUMNS,
    _GROUP_UPDATED_AT_IDX,
    _INVOCATION_COLUMNS,
    _INVOCATION_SPAWNED_AT_IDX,
    _MESSAGE_AGENT_NAME_IDX,
    _MESSAGE_AGENT_VERSION_IDX,
    _MESSAGE_CALL_ID_IDX,
    _MESSAGE_COLUMNS,
    _MESSAGE_INGESTED_AT_IDX,
    _MESSAGE_SESSION_ID_IDX,
    _MESSAGE_SKILL_NAME_IDX,
    _MESSAGE_SKILL_VERSION_IDX,
    _MESSAGE_TIMESTAMP_IDX,
    _SOURCE_COLUMNS,
    _SOURCE_INGESTED_AT_IDX,
    _USAGE_AGENT_NAME_IDX,
    _USAGE_AGENT_VERSION_IDX,
    _USAGE_CALL_ID_IDX,
    _USAGE_COLUMNS,
    _USAGE_INGESTED_AT_IDX,
    _USAGE_SESSION_ID_IDX,
    _USAGE_SKILL_NAME_IDX,
    _USAGE_SKILL_VERSION_IDX,
    _USAGE_TIMESTAMP_IDX,
    _USER_COLUMNS,
    _USER_UPDATED_AT_IDX,
    _agent_invocation_id,
    _agent_invocation_rows,
    _backfill_missing_skill_versions,
    _deserialize_row,
    _deserialize_row_multi,
    _derive_context,
    _event_row,
    _group_row,
    _message_row,
    _source_row,
    _usage_row,
    _user_row,
    session_and_trace_id,
)

logger = logging.getLogger("webhook.ingest_db")

# Type-name strings for each _X_COLUMNS list above, same order.
# Passed as client.insert()'s column_type_names so clickhouse_connect skips
# its per-insert DESCRIBE TABLE (driver/client.py:create_insert_context).
# Types taken from services/clickhouse/schema.sql.
_INVOCATION_COLUMN_TYPES = [
    "String", "String", "LowCardinality(String)", "LowCardinality(String)",
    "String", "Nullable(String)", "DateTime64(3)",
]
_EVENT_COLUMN_TYPES = [
    "DateTime64(3)", "LowCardinality(String)", "LowCardinality(String)", "LowCardinality(String)",
    "String", "String", "UInt32", "LowCardinality(String)", "LowCardinality(String)",
    "LowCardinality(String)", "LowCardinality(String)", "LowCardinality(String)", "LowCardinality(String)",
    "LowCardinality(String)", "LowCardinality(String)", "String", "LowCardinality(String)",
    "Nullable(UInt32)", "LowCardinality(String)", "String", "String",
    "String", "UInt64", "LowCardinality(String)", "String", "DateTime64(3)",
]
_USAGE_COLUMN_TYPES = [
    "DateTime64(3)", "LowCardinality(String)", "LowCardinality(String)", "LowCardinality(String)",
    "String", "String", "UInt32", "LowCardinality(String)",
    "LowCardinality(String)", "LowCardinality(String)", "LowCardinality(String)", "LowCardinality(String)",
    "LowCardinality(String)", "LowCardinality(String)", "String", "LowCardinality(String)",
    "UInt32", "UInt32", "UInt32", "UInt32",
    "LowCardinality(String)",
    "UInt32", "UInt32",
    "Float64", "Float64", "Float64", "UInt8", "UInt32",
    "String", "LowCardinality(String)", "DateTime64(3)",
]
_MESSAGE_COLUMN_TYPES = [
    "DateTime64(3)", "LowCardinality(String)", "LowCardinality(String)", "LowCardinality(String)",
    "String", "String", "UInt32",
    "LowCardinality(String)", "LowCardinality(String)", "LowCardinality(String)", "LowCardinality(String)",
    "LowCardinality(String)", "LowCardinality(String)", "String", "String", "String",
    "String", "DateTime64(3)",
]
_SOURCE_COLUMN_TYPES = ["String", "String", "DateTime64(3)", "String"]
_GROUP_COLUMN_TYPES = ["LowCardinality(String)", "LowCardinality(String)", "DateTime64(3)"]
_USER_COLUMN_TYPES = ["LowCardinality(String)", "LowCardinality(String)", "LowCardinality(String)", "DateTime64(3)"]
_CLIENT_COLUMN_TYPES = ["UInt64", "String", "DateTime64(3)"]
_FAILURE_COLUMN_TYPES = ["DateTime64(3)", "LowCardinality(String)", "String", "String", "String", "String"]

# Keyed by table name - _BatchWriter.insert_with_dlq_fallback's generic
# table/columns signature looks its column_type_names up here.
_COLUMN_TYPES_BY_TABLE = {
    "agent_events": _EVENT_COLUMN_TYPES,
    "agent_usage": _USAGE_COLUMN_TYPES,
    "agent_messages": _MESSAGE_COLUMN_TYPES,
}

_client = None


def get_client():
    global _client
    if _client is None:
        _client = clickhouse_connect.get_client(
            host=CLICKHOUSE_HOST,
            port=CLICKHOUSE_PORT,
            username=CLICKHOUSE_USER,
            password=CLICKHOUSE_PASSWORD,
            database=CLICKHOUSE_DATABASE,
        )
    return _client


def clickhouse_alive() -> None:
    """Raises if ClickHouse isn't reachable.
    Shared by server.py's /health and worker.py's startup dependency check,
    so the liveness probe itself (currently just SELECT 1) only has one
    place to change."""
    get_client().command("SELECT 1")


_CLIENT_COLUMNS = ["id", "value", "updated_at"]
_FAILURE_COLUMNS = ["occurred_at", "stage", "error", "litellm_call_id", "session_id", "raw_row"]


def _insert_agent_invocations(client, rows: list[list]) -> None:
    if not rows:
        return
    client.insert("agent_invocations", rows, column_names=_INVOCATION_COLUMNS, column_type_names=_INVOCATION_COLUMN_TYPES)


def _insert_event(client, row: list) -> None:
    client.insert("agent_events", [row], column_names=_EVENT_COLUMNS, column_type_names=_EVENT_COLUMN_TYPES)


def _insert_usage(client, row: list) -> None:
    client.insert("agent_usage", [row], column_names=_USAGE_COLUMNS, column_type_names=_USAGE_COLUMN_TYPES)


def _insert_message(client, row: list) -> None:
    client.insert("agent_messages", [row], column_names=_MESSAGE_COLUMNS, column_type_names=_MESSAGE_COLUMN_TYPES)


def _insert_source(client, row: list) -> None:
    client.insert("ingest_raw", [row], column_names=_SOURCE_COLUMNS, column_type_names=_SOURCE_COLUMN_TYPES)


def _insert_ai_gateway_groups(client, rows: list[list]) -> None:
    if not rows:
        return
    client.insert("ai_gateway_groups", rows, column_names=_GROUP_COLUMNS, column_type_names=_GROUP_COLUMN_TYPES)


def _insert_ai_gateway_users(client, rows: list[list]) -> None:
    if not rows:
        return
    client.insert("ai_gateway_users", rows, column_names=_USER_COLUMNS, column_type_names=_USER_COLUMN_TYPES)


def _insert_clients(client, rows: list[list]) -> None:
    if not rows:
        return
    client.insert("clients", rows, column_names=_CLIENT_COLUMNS, column_type_names=_CLIENT_COLUMN_TYPES)


def _agent_name_and_version_for_invocation(client, agent_invocation_id: str) -> tuple[str, str]:
    """Best-effort lookup - blank if the parent's Agent tool_use/tool_result
    hasn't been ingested yet (e.g. subagent's first call raced ahead of it)."""
    if not agent_invocation_id:
        return "", ""
    try:
        result = client.query(
            "SELECT subagent_type, agent_version FROM agent_invocations WHERE agent_id = {agent_id:String} "
            "ORDER BY spawned_at DESC LIMIT 1",
            parameters={"agent_id": agent_invocation_id},
        )
        rows = result.result_rows
        return (rows[0][0], rows[0][1]) if rows else ("", "")
    except Exception:
        logger.exception("failed to resolve agent_invocation_id=%s", agent_invocation_id)
        return "", ""


def _resolve_client_id(client, user_agent: str) -> int:
    """Best-effort id lookup for a calling-client user-agent string (e.g.
    "claude-cli/2.1.207 (external, cli)").
    The hash is always computed by ClickHouse itself (cityHash64), never
    reimplemented in Python, so this can never diverge from the one-off
    backfill SQL run separately against ingest_raw.
    0 (unknown) on any failure or empty input - never blocks ingestion."""
    if not user_agent:
        return 0
    try:
        result = client.query("SELECT cityHash64({v:String})", parameters={"v": user_agent})
        return result.result_rows[0][0]
    except Exception:
        logger.exception("failed to resolve client id for user_agent=%s", user_agent)
        return 0


class _ContextClient:
    """Adapts a raw ClickHouse client to the narrow interface
    ingest_parsing._derive_context needs (agent_name_and_version_for_invocation),
    so ingest_parsing.py never imports this module (see its docstring on
    the one-directional dependency arrow)."""

    def __init__(self, client):
        self._client = client

    def agent_name_and_version_for_invocation(self, agent_invocation_id: str) -> tuple[str, str]:
        return _agent_name_and_version_for_invocation(self._client, agent_invocation_id)


def _derive_context_with_client(payload: dict, messages, client) -> EventContext:
    return _derive_context(payload, messages, client=_ContextClient(client))


def ingest_standard_logging_payload(payload: dict) -> None:
    """Insert one LiteLLM StandardLoggingPayload into ClickHouse.
    Never raises - LiteLLM would retry a payload forever if this broke the
    ack.

    Has zero call sites today (leftover from before the Redis-queue split -
    see AGENTS.md); carried forward unchanged for parity rather than
    deleted outright."""
    session_id = trace_id = ""
    try:
        client = get_client()
        now = datetime.now(timezone.utc)

        messages = payload.get("messages")
        session_id, trace_id = session_and_trace_id(payload)
        # Insert this call's own spawned-subagent rows before doing
        # _derive_context's DB lookup below (for *this* call's own
        # agent_invocation_id) - minimizes the race window before the
        # spawned subagent's own first call arrives and looks its row up.
        parent_agent_id = _agent_invocation_id(payload)
        _insert_agent_invocations(
            client, _agent_invocation_rows(session_id, messages, parent_agent_id, now=now)
        )

        ctx = _derive_context_with_client(payload, messages, client)

        _insert_source(client, _source_row(payload, session_id, now))

        _insert_event(client, _event_row(payload, ctx, now))

        if payload.get("status") == "success":
            usage_row = _usage_row(payload, ctx, now)
            if usage_row is not None:
                _insert_usage(client, usage_row)

            message_row = _message_row(payload, ctx, now)
            if message_row is not None:
                _insert_message(client, message_row)
    except Exception:
        logger.exception(
            "failed to ingest LiteLLM payload into ClickHouse "
            "(litellm_call_id=%s trace_id=%s session_id=%s status=%s call_type=%s)",
            payload.get("litellm_call_id", ""), trace_id, session_id,
            payload.get("status", ""), payload.get("call_type", ""),
        )


def ingest_webhook_body(body) -> None:
    """Usually a list of StandardLoggingPayload dicts (log_format:
    json_array), but tolerate a single dict too.

    Has zero call sites today, same as ingest_standard_logging_payload -
    carried forward unchanged for parity."""
    payloads = body if isinstance(body, list) else [body]
    for payload in payloads:
        if isinstance(payload, dict):
            ingest_standard_logging_payload(payload)


def reparse_event(client, payload: dict, litellm_call_id: str, source_session_id: str, now: datetime) -> None:
    """Recomputes and re-inserts one ingest_raw row's downstream tables
    (agent_invocations, ai_gateway_groups/users, agent_events, agent_usage,
    agent_messages) from an already-decoded payload.

    Formalizes what reparse.py used to do by reaching into 14 of this
    module's/ingest_parsing's private helpers directly - see
    plans/webhook-worker-split.md.
    Raises on failure; reparse.py's own caller keeps the try/except-log
    wrapper (per-event, so one bad row doesn't stop a whole reparse run)."""
    messages = payload.get("messages")
    session_id, _trace_id = session_and_trace_id(payload)
    session_id = session_id or source_session_id
    # Insert this call's own spawned-subagent rows before doing
    # _derive_context's DB lookup below (for *this* call's own
    # agent_invocation_id) - minimizes the race window before the
    # spawned subagent's own first call arrives and looks its row up.
    parent_agent_id = _agent_invocation_id(payload)
    _insert_agent_invocations(
        client, _agent_invocation_rows(session_id, messages, parent_agent_id, now=now)
    )

    ctx = _derive_context_with_client(payload, messages, client)
    ctx.session_id = session_id

    group_row = _group_row(payload, now=now)
    if group_row is not None:
        _insert_ai_gateway_groups(client, [group_row])
    user_row = _user_row(payload, now=now)
    if user_row is not None:
        _insert_ai_gateway_users(client, [user_row])

    _insert_event(client, _event_row(payload, ctx, now))

    if payload.get("status") == "success":
        usage_row = _usage_row(payload, ctx, now)
        if usage_row is not None:
            _insert_usage(client, usage_row)

        message_row = _message_row(payload, ctx, now)
        if message_row is not None:
            _insert_message(client, message_row)


def ingest_events_batch(events: list[dict]) -> None:
    """Runs in webhook-worker.
    Writes a batch of build_event() outputs with exactly one client.insert()
    per table (vs up-to-4-per-payload in the old synchronous path) - the
    batching that takes load off ClickHouse under concurrent traffic.

    Never raises - the caller still needs to XACK/retry the batch's message
    ids regardless of a malformed event.
    """
    if not events:
        return
    try:
        writer = _BatchWriter(get_client())
        writer.write_dimensions(events)
        event_rows, usage_rows, message_rows = writer.patch_fact_rows(events)
        for table, rows, columns, call_id_idx, session_id_idx in (
            ("agent_events", event_rows, _EVENT_COLUMNS, _EVENT_CALL_ID_IDX, _EVENT_SESSION_ID_IDX),
            ("agent_usage", usage_rows, _USAGE_COLUMNS, _USAGE_CALL_ID_IDX, _USAGE_SESSION_ID_IDX),
            ("agent_messages", message_rows, _MESSAGE_COLUMNS, _MESSAGE_CALL_ID_IDX, _MESSAGE_SESSION_ID_IDX),
        ):
            writer.insert_with_dlq_fallback(table, rows, columns, call_id_idx, session_id_idx)
    except Exception:
        logger.exception("failed to ingest event batch (n=%d)", len(events))


class _BatchWriter:
    """Owns the per-batch state ingest_events_batch() needs across its three
    concerns - dimension rows, per-event agent/client resolution caches, and
    per-table insert-with-DLQ-fallback - so ingest_events_batch() itself
    stays a plain call sequence."""

    def __init__(self, client):
        self.client = client
        self._agent_fields_cache: dict[str, tuple[str, str]] = {}
        self._invocation_batch_map: dict[str, tuple[str, str]] = {}
        self._client_id_cache: dict[str, int] = {}

    def write_dimensions(self, events: list[dict]) -> None:
        """Inserts agent_invocations, ingest_raw, and the (deduped)
        ai_gateway_groups/ai_gateway_users rows for this batch."""
        client = self.client

        invocation_rows = [
            _deserialize_row(row, _INVOCATION_SPAWNED_AT_IDX)
            for event in events
            for row in (event.get("invocation_rows") or [])
        ]
        # Resolve same-batch spawn/first-call pairs from memory instead of
        # racing this insert (see _agent_fields).
        for row in invocation_rows:
            self._invocation_batch_map[row[0]] = (row[2], row[3])
        _insert_agent_invocations(client, invocation_rows)

        source_rows = [
            row for event in events
            if (row := _deserialize_row(event.get("source_row"), _SOURCE_INGESTED_AT_IDX)) is not None
        ]
        if source_rows:
            client.insert("ingest_raw", source_rows, column_names=_SOURCE_COLUMNS, column_type_names=_SOURCE_COLUMN_TYPES)

        # Dedup by id within the batch (last wins); ReplacingMergeTree would
        # collapse duplicates on merge anyway, this just skips redundant inserts.
        group_rows_by_id: dict[str, list] = {}
        for event in events:
            row = _deserialize_row(event.get("group_row"), _GROUP_UPDATED_AT_IDX)
            if row is not None:
                group_rows_by_id[row[0]] = row
        _insert_ai_gateway_groups(client, list(group_rows_by_id.values()))

        user_rows_by_id: dict[str, list] = {}
        for event in events:
            row = _deserialize_row(event.get("user_row"), _USER_UPDATED_AT_IDX)
            if row is not None:
                user_rows_by_id[row[0]] = row
        _insert_ai_gateway_users(client, list(user_rows_by_id.values()))

    def _agent_fields(self, agent_invocation_id: str) -> tuple[str, str]:
        """Resolves agent_name/agent_version for the (sub)agent that made
        this call, preferring an in-memory hit over a ClickHouse round trip.

        Order: this batch's own invocation_rows (the spawn event and its
        child's first call landing in the same batch is the dominant case -
        see migration 013's docstring), then a per-batch cache, then a
        ClickHouse lookup with a short bounded retry for the near-miss case
        (spawn row committed in the immediately preceding batch)."""
        if not agent_invocation_id:
            return "", ""
        if agent_invocation_id in self._invocation_batch_map:
            return self._invocation_batch_map[agent_invocation_id]
        if agent_invocation_id not in self._agent_fields_cache:
            fields = _agent_name_and_version_for_invocation(self.client, agent_invocation_id)
            for _ in range(2):
                if fields != ("", ""):
                    break
                time.sleep(0.3)
                fields = _agent_name_and_version_for_invocation(self.client, agent_invocation_id)
            self._agent_fields_cache[agent_invocation_id] = fields
        return self._agent_fields_cache[agent_invocation_id]

    def _client_id(self, user_agent: str) -> int:
        if not user_agent:
            return 0
        if user_agent not in self._client_id_cache:
            self._client_id_cache[user_agent] = _resolve_client_id(self.client, user_agent)
        return self._client_id_cache[user_agent]

    def patch_fact_rows(self, events: list[dict]) -> tuple[list[list], list[list], list[list]]:
        """Resolves each event's agent_name/version and event_client_id
        (both needed a DB round trip build_event() couldn't do), patches
        them into its event/usage/message rows, and inserts the resolved
        `clients` rows.
        Returns (event_rows, usage_rows, message_rows)."""
        client_rows_by_id: dict[int, list] = {}
        now = datetime.now(timezone.utc)
        event_rows, usage_rows, message_rows = [], [], []

        for event in events:
            agent_name, agent_version = self._agent_fields(event.get("agent_invocation_id") or "")

            user_agent = event.get("user_agent") or ""
            client_id = self._client_id(user_agent)
            if client_id:
                client_rows_by_id[client_id] = [client_id, user_agent, now]

            event_row = _deserialize_row_multi(event.get("event_row"), _EVENT_TIMESTAMP_IDX, _EVENT_INGESTED_AT_IDX)
            if event_row is not None:
                event_row[_EVENT_AGENT_NAME_IDX] = agent_name
                event_row[_EVENT_AGENT_VERSION_IDX] = agent_version
                event_row[_EVENT_CLIENT_ID_IDX] = client_id
                event_rows.append(event_row)

            usage_row = _deserialize_row_multi(event.get("usage_row"), _USAGE_TIMESTAMP_IDX, _USAGE_INGESTED_AT_IDX)
            if usage_row is not None:
                usage_row[_USAGE_AGENT_NAME_IDX] = agent_name
                usage_row[_USAGE_AGENT_VERSION_IDX] = agent_version
                usage_rows.append(usage_row)

            message_row = _deserialize_row_multi(event.get("message_row"), _MESSAGE_TIMESTAMP_IDX, _MESSAGE_INGESTED_AT_IDX)
            if message_row is not None:
                message_row[_MESSAGE_AGENT_NAME_IDX] = agent_name
                message_row[_MESSAGE_AGENT_VERSION_IDX] = agent_version
                message_rows.append(message_row)

        _insert_clients(self.client, list(client_rows_by_id.values()))
        _backfill_missing_skill_versions([
            (event_rows, _EVENT_SESSION_ID_IDX, _EVENT_SKILL_NAME_IDX, _EVENT_SKILL_VERSION_IDX),
            (usage_rows, _USAGE_SESSION_ID_IDX, _USAGE_SKILL_NAME_IDX, _USAGE_SKILL_VERSION_IDX),
            (message_rows, _MESSAGE_SESSION_ID_IDX, _MESSAGE_SKILL_NAME_IDX, _MESSAGE_SKILL_VERSION_IDX),
        ])
        return event_rows, usage_rows, message_rows

    def insert_with_dlq_fallback(
        self, table: str, rows: list[list], columns: list[str], call_id_idx: int, session_id_idx: int,
    ) -> None:
        """One table's insert.
        On a rejected batch (e.g. a value out of a column's numeric range),
        falls back to inserting row-by-row so only the actual poison row(s)
        are lost - those go to ingest_dlq (see schema.sql) for triage instead
        of silently vanishing with the rest of a good batch.
        Each table is independent: one table rejecting its batch must not
        drop another table's otherwise-good rows for this same set of
        events."""
        if not rows:
            return
        client = self.client
        column_types = _COLUMN_TYPES_BY_TABLE[table]
        try:
            client.insert(table, rows, column_names=columns, column_type_names=column_types)
            return
        except Exception:
            logger.exception("failed to insert into %s (n=%d) - retrying row-by-row", table, len(rows))

        failure_rows = []
        for row in rows:
            try:
                client.insert(table, [row], column_names=columns, column_type_names=column_types)
            except Exception as exc:
                failure_rows.append([
                    datetime.now(timezone.utc), table, str(exc),
                    row[call_id_idx], row[session_id_idx],
                    json.dumps(row, default=str).decode(),
                ])
        if failure_rows:
            logger.error("dropped %d row(s) into ingest_dlq (stage=%s)", len(failure_rows), table)
            try:
                client.insert("ingest_dlq", failure_rows, column_names=_FAILURE_COLUMNS, column_type_names=_FAILURE_COLUMN_TYPES)
            except Exception:
                logger.exception("failed to record ingest_dlq (stage=%s, n=%d)", table, len(failure_rows))
