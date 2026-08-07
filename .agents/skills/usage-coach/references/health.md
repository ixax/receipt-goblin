# Data-health checks

Answers "did the statistics themselves break", separately from "is spend bad".
Run this block before the spend analysis on every full run - a spend number computed on broken ingest is worse than no number.

`WINDOW` substitutes the same way as in `queries.md`.
Report a health finding before any cost finding, and say plainly which spend numbers it makes untrustworthy.

## HQ1 - ingest freshness

```sql
SELECT 'agent_usage' AS tbl, max(timestamp) AS last_event, max(ingested_at) AS last_ingest,
       dateDiff('minute', max(ingested_at), now()) AS ingest_age_min
FROM agent_usage
UNION ALL
SELECT 'agent_events', max(timestamp), max(ingested_at), dateDiff('minute', max(ingested_at), now())
FROM agent_events
UNION ALL
SELECT 'agent_messages', max(timestamp), max(ingested_at), dateDiff('minute', max(ingested_at), now())
FROM agent_messages
```

Fires when `ingest_age_min` > 30 while the user has been working.
Means the queue path is stalled somewhere between LiteLLM and the worker - `webhook`, `redis`, or `webhook-worker`.
Owner: `dev-ops` for the service state, `agent_docs/services/worker.md` for the path.

## HQ2 - dead-letter backlog

```sql
SELECT d.stage                       AS stage,
       substring(d.error, 1, 120)    AS error,
       count()                       AS rows_,
       max(d.occurred_at)            AS newest
FROM ingest_dlq AS d
LEFT JOIN ingest_dlq_resolved AS r
  ON d.litellm_call_id = r.litellm_call_id AND d.stage = r.stage
WHERE r.litellm_call_id = ''
  AND d.occurred_at >= now() - INTERVAL WINDOW DAY
GROUP BY stage, error
ORDER BY rows_ DESC
LIMIT 15
```

Fires on any unresolved row.
Means calls were dropped on the floor - every spend number for that period is under-counted by exactly those rows.
Fix path is `make reparse-dlq`; never propose it without naming the error that caused the backlog.

## HQ3 - volume gaps

```sql
SELECT toStartOfHour(timestamp) AS h,
       count()                  AS calls,
       round(sum(cost), 2)      AS cost
FROM agent_usage
WHERE timestamp >= now() - INTERVAL 72 HOUR
GROUP BY h
ORDER BY h
```

Fires when an hour inside an otherwise active stretch has zero calls.
Distinguish a real gap from the user simply not working - a gap bounded by busy hours on both sides is the suspicious shape.

## HQ4 - missing cost on priced calls

```sql
SELECT countIf(cost = 0 AND (input_tokens + output_tokens) > 0) AS zero_cost_calls,
       count()                                                 AS calls,
       round(countIf(cost = 0 AND (input_tokens + output_tokens) > 0) / count(), 4) AS zero_cost_share
FROM agent_usage
WHERE timestamp >= now() - INTERVAL WINDOW DAY
```

Fires above 1%.
Means LiteLLM returned no `response_cost` for those calls - usually an unpriced or newly added model.
Every cost total in the report is understated while this is non-zero, so say by how much in calls.

## HQ5 - duplicate rows

```sql
SELECT count()                                       AS rows_,
       uniqExact(litellm_call_id)                    AS calls,
       round(count() / nullIf(uniqExact(litellm_call_id), 0), 3) AS dup_factor
FROM agent_usage
WHERE timestamp >= now() - INTERVAL WINDOW DAY
```

Fires above 1.05.
`litellm_call_id` is unique per call, so anything above 1.0 is un-merged `ReplacingMergeTree` versions - normal right after a reparse, alarming otherwise.
Cost is inflated by roughly `dup_factor` while it lasts, so re-check before reporting a spend spike.

## HQ6 - classifier drift

```sql
SELECT if(timestamp >= now() - INTERVAL 24 HOUR, 'last24h', 'baseline') AS bucket,
       countIf(calculated_type = 'unknown')                             AS unknown_,
       count()                                                          AS calls,
       round(countIf(calculated_type = 'unknown') / count(), 4)         AS unknown_share
FROM agent_events
WHERE timestamp >= now() - INTERVAL WINDOW DAY
GROUP BY bucket
```

Fires when the recent share is more than double the baseline share.
Means the payload shape changed and `_classify_event` no longer recognises it - a client upgrade is the usual cause.
Old rows carry `unknown` permanently, so only the recent-vs-baseline comparison is meaningful.

## HQ7 - orphaned rows between tables

```sql
SELECT uniqExactIf(au.litellm_call_id, ae.litellm_call_id = '') AS usage_without_event,
       uniqExact(au.litellm_call_id)                            AS usage_calls
FROM agent_usage AS au
LEFT JOIN agent_events AS ae
  ON au.litellm_call_id = ae.litellm_call_id AND au.session_id = ae.session_id
WHERE au.timestamp >= now() - INTERVAL WINDOW DAY
```

Counted over distinct `litellm_call_id`, not over joined rows, so an un-merged duplicate on either side cannot masquerade as an orphan rate.

Fires above 2%.
Means one writer landed and the other did not, so every joined query (Q7, Q9, Q13) silently under-reports.

## HQ8 - broken attribution

```sql
SELECT round(countIf(session_id = '') / count(), 4)      AS no_session,
       round(countIf(user_id = '') / count(), 4)         AS no_user,
       round(countIf(event_client_id = 0) / count(), 4)  AS no_client,
       count()                                           AS calls
FROM agent_events
WHERE timestamp >= now() - INTERVAL WINDOW DAY
```

Fires above 5% on any column.
Means calls cannot be attributed to a session, person, or client - per-session and per-client reports become guesses.
A jump in `no_client` right after a client upgrade points at the `user_agent` parsing in `services/_common/src/ingest_parsing.py`.

## HQ9 - proxy alerts

```sql
SELECT event,
       event_group,
       count()                     AS hits,
       max(received_at)            AS newest,
       substring(anyLast(event_message), 1, 160) AS sample
FROM litellm_alerts
WHERE received_at >= now() - INTERVAL WINDOW DAY
GROUP BY event, event_group
ORDER BY newest DESC
LIMIT 15
```

Fires on any budget or exception alert in the window.
These come from LiteLLM itself, so they are independent evidence that something broke upstream of ClickHouse.

## HQ10 - replay coverage

```sql
SELECT round(uniqExactIf(ae.litellm_call_id, ir.litellm_call_id = '') / uniqExact(ae.litellm_call_id), 4) AS missing_raw,
       uniqExact(ae.litellm_call_id)                                                                     AS calls
FROM agent_events AS ae
LEFT JOIN ingest_raw AS ir
  ON ae.litellm_call_id = ir.litellm_call_id
WHERE ae.timestamp >= now() - INTERVAL WINDOW DAY
```

Distinct call ids on both sides, for the same reason as HQ7 - `ingest_raw` keeps one row per ingest attempt, so joined-row counts would drift on any replay.

Fires above 5%.
Means those calls can never be reparsed - a future classifier fix will not reach them.
Not a cost finding, but it caps what any later fix can repair, so it belongs in the report when it moves.

## HQ11 - instrumentation coverage

```sql
SELECT round(countIf(ttft_ms = 0) / count(), 4)    AS no_ttft,
       round(countIf(stop_reason = '') / count(), 4) AS no_stop_reason,
       round(countIf(provider = '') / count(), 4)  AS no_provider,
       count()                                     AS calls
FROM agent_usage
WHERE timestamp >= now() - INTERVAL WINDOW DAY
```

Fires above 20% on any column.
Means a field the playbook depends on is mostly empty, so the signals built on it are blind rather than quiet.
Say which playbook signals are blinded, and do not report those signals as "clean" in the same run.

## Reporting a health finding

State three things and stop: what broke, which numbers in this report it makes wrong, and who owns the fix.
Never propose a spend action that depends on a metric a health check just invalidated.
Record every fired check in `MEMO.md` so a recurring break is visible as a pattern instead of a surprise each run.
