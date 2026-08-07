# Metric pack

Numbered queries behind `/usage-coach`.
`WINDOW` is the scope window in days - substitute the literal number before sending.
Send Q1-Q11 as one batched `mcp__dev__query` list; Q12-Q14 only on demand.

Sums are taken directly off `agent_usage.cost` - no price table, no `ASOF JOIN`.

Three rules hold for every query in this pack, and for any query added to it.

- Never alias an aggregate with the name of a column the same SELECT aggregates again.
  A later `sum(cost)` resolves to the alias, not the column, and ClickHouse rejects the nested aggregate.
  Prefer the aggregate that needs no second pass at all (`avg(cost)` over `sum(cost) / count()`).
- Never join a `ReplacingMergeTree` dimension table (`clients`, `ai_gateway_users`, `ai_gateway_groups`, `agent_invocations`) directly.
  Each holds one un-merged row per write until a background merge collapses them, so a direct join multiplies that key's rows by its version count.
  Join `(SELECT key, argMax(col, version_col) AS col FROM dim GROUP BY key)` instead - the same dedup pattern `agents_overview.json`'s own CTEs use, since `FINAL` is not applied anywhere in this stack's queries.
- Cross-check any joined result against the unjoined table it came from.
  The grouped `calls` in a per-dimension breakdown must sum to `count()` on `agent_usage` for the same window; a mismatch means the join fanned out and the numbers are inflated.

The two fact tables (`agent_events`, `agent_usage`) are summed without `FINAL`, matching the dashboards and `mcp-stats`.
Their own duplicates only exist between a `make reparse` run and the next merge - HQ5 runs first precisely to catch that, and a joined figure is only trustworthy while its `dup_factor` sits at 1.0.

## Q1 - spend and volume per day

```sql
SELECT toDate(timestamp)             AS d,
       round(sum(cost), 2)           AS cost,
       sum(input_tokens)             AS fresh_in,
       sum(output_tokens)            AS out_tok,
       sum(cache_read_tokens)        AS cache_read,
       sum(cache_creation_tokens)    AS cache_write,
       count()                       AS calls,
       uniqExact(session_id)         AS sessions
FROM agent_usage
WHERE timestamp >= now() - INTERVAL WINDOW DAY
GROUP BY d
ORDER BY d
```

Headline series.
Read the direction over the window, and whether cost per session is drifting up independently of session count.

## Q2 - cache economics, current half-window vs previous

```sql
SELECT if(timestamp >= now() - INTERVAL HALF DAY, 'current', 'previous') AS bucket,
       round(sum(cost), 2)                                               AS cost,
       sum(cache_read_tokens)                                            AS cache_read,
       sum(input_tokens)                                                 AS fresh_in,
       sum(cache_creation_tokens)                                        AS cache_write,
       round(sum(cache_read_tokens) / nullIf(sum(cache_read_tokens) + sum(input_tokens) + sum(cache_creation_tokens), 0), 3) AS cache_read_share,
       round(avg(cache_hit), 3)                                          AS hit_rate,
       sum(cache_creation_1h_tokens)                                     AS write_1h,
       sum(cache_creation_5m_tokens)                                     AS write_5m
FROM agent_usage
WHERE timestamp >= now() - INTERVAL WINDOW DAY
GROUP BY bucket
```

`HALF` is `WINDOW / 2`.
Cache reads are billed at a fraction of fresh input, so `cache_read_share` is the single biggest lever on this stack's bill.
A high `cache_write` next to a low `cache_read` means the prefix is being rebuilt and thrown away.

## Q3 - model mix

```sql
SELECT model,
       provider,
       count()                                                          AS calls,
       round(sum(cost), 2)                                              AS cost,
       sum(output_tokens)                                               AS out_tok,
       round(avg(input_tokens + cache_read_tokens + cache_creation_tokens)) AS avg_ctx
FROM agent_usage
WHERE timestamp >= now() - INTERVAL WINDOW DAY
GROUP BY model, provider
ORDER BY cost DESC
```

Compute each model's share of spend in the analysis, not in SQL.
A frontier model carrying high-volume mechanical calls is the classic finding here.

## Q4 - cost by operation

```sql
SELECT multiIf(command_name  != '', concat('/', command_name),
               skill_name    != '', concat('skill:', skill_name),
               agent_name    != '', concat('agent:', agent_name),
               mcp_tool_name != '', concat('mcp:', mcp_tool_name),
               concat('main:', model))                                  AS op,
       count()                                                          AS calls,
       round(sum(cost), 2)                                              AS total_cost,
       round(avg(cost), 4)                                              AS cost_per_call,
       round(avg(output_tokens))                                        AS avg_out,
       round(avg(input_tokens + cache_read_tokens + cache_creation_tokens)) AS avg_ctx
FROM agent_usage
WHERE timestamp >= now() - INTERVAL WINDOW DAY
GROUP BY op
ORDER BY total_cost DESC
LIMIT 20
```

Same labelling precedence `mcp-stats`'s `me` tool uses, so the two reports agree.
Per-call cost is `avg(cost)`, not `sum(cost) / count()` - the same value, without a second aggregate over a column an alias in this SELECT is already named after.
`cost_per_call` next to `avg_ctx` separates "called too often" from "called with too much context".

## Q5 - session cost distribution

```sql
SELECT round(quantile(0.5)(c), 2)  AS p50,
       round(quantile(0.9)(c), 2)  AS p90,
       round(max(c), 2)            AS worst,
       round(avg(c), 2)            AS mean,
       count()                     AS sessions
FROM (
    SELECT session_id, sum(cost) AS c
    FROM agent_usage
    WHERE timestamp >= now() - INTERVAL WINDOW DAY
    GROUP BY session_id
)
```

A p90 far above p50 means a few marathon sessions carry the bill.
That is a `/clear` discipline finding, not a model-choice finding.

## Q6 - most expensive sessions

```sql
SELECT session_id,
       round(sum(cost), 2)                     AS cost,
       count()                                 AS calls,
       min(timestamp)                          AS started,
       round(max(input_tokens + cache_read_tokens + cache_creation_tokens)) AS peak_ctx
FROM agent_usage
WHERE timestamp >= now() - INTERVAL WINDOW DAY
GROUP BY session_id
ORDER BY cost DESC
LIMIT 5
```

`peak_ctx` says how large the context grew before the session ended.

## Q7 - failed-tool reactions

```sql
SELECT ae.failed_tool_name        AS tool,
       count()                    AS reactions,
       round(sum(au.cost), 2)     AS cost
FROM agent_events AS ae
LEFT JOIN agent_usage AS au
  ON ae.litellm_call_id = au.litellm_call_id AND ae.session_id = au.session_id
WHERE ae.timestamp >= now() - INTERVAL WINDOW DAY
  AND ae.failed_tool_name != ''
GROUP BY tool
ORDER BY cost DESC
LIMIT 15
```

One row per call that was reacting to a tool that just failed - the direct price of tool friction.
A tool at the top repeatedly is a harness fix (a rule, an allowlist entry, a wrapper), not a model problem.

## Q8 - call outcome mix

```sql
SELECT status, count() AS calls
FROM agent_events
WHERE timestamp >= now() - INTERVAL WINDOW DAY
GROUP BY status
```

Never group on `event_type` - it is always the literal `litellm_call`.

## Q9 - what the calls were doing

```sql
SELECT ae.calculated_type      AS kind,
       count()                 AS calls,
       round(sum(au.cost), 2)  AS cost
FROM agent_events AS ae
LEFT JOIN agent_usage AS au
  ON ae.litellm_call_id = au.litellm_call_id AND ae.session_id = au.session_id
WHERE ae.timestamp >= now() - INTERVAL WINDOW DAY
GROUP BY kind
ORDER BY cost DESC
```

`interrupted` spend is work paid for and thrown away.
A large `unknown` bucket is expected for old rows and is not a finding on its own.

## Q10 - truncation and stop reasons

```sql
SELECT stop_reason,
       count()             AS calls,
       round(sum(cost), 2) AS cost
FROM agent_usage
WHERE timestamp >= now() - INTERVAL WINDOW DAY
GROUP BY stop_reason
ORDER BY cost DESC
```

`max_tokens` means an answer was cut off and usually re-asked - paid twice for one answer.

## Q11 - context growth and delegation lane

```sql
SELECT toStartOfWeek(timestamp)                                          AS w,
       if(agent_invocation_id != '', 'subagent', 'main')                 AS lane,
       round(avg(input_tokens + cache_read_tokens + cache_creation_tokens)) AS avg_ctx,
       round(avg(output_tokens))                                        AS avg_out,
       round(sum(cost), 2)                                              AS cost,
       count()                                                          AS calls
FROM agent_usage
WHERE timestamp >= now() - INTERVAL 56 DAY
GROUP BY w, lane
ORDER BY w, lane
```

Fixed 8-week window regardless of scope - a bloat trend needs more history than one report window.
Rising `avg_ctx` in the `main` lane while the `subagent` lane stays flat means work that should have been delegated is being done inline.

## Q12 - per-version cost, on demand

```sql
SELECT agent_name,
       skill_name,
       agent_version,
       skill_version,
       count()                   AS calls,
       round(avg(cost), 4)       AS cost_per_call,
       round(avg(output_tokens)) AS avg_out,
       min(timestamp)            AS first_seen
FROM agent_usage
WHERE timestamp >= now() - INTERVAL 90 DAY
  AND (agent_name != '' OR skill_name != '')
GROUP BY agent_name, skill_name, agent_version, skill_version
ORDER BY agent_name, skill_name, first_seen
```

The verdict query.
There is no version registry table, so `first_seen` is what dates a version - compare `cost_per_call` across consecutive versions of the same agent or skill to grade an edit.

## Q13 - client split, on demand

```sql
SELECT c.value                  AS client,
       count()                  AS calls,
       round(sum(au.cost), 2)   AS cost
FROM agent_events AS ae
LEFT JOIN agent_usage AS au
  ON ae.litellm_call_id = au.litellm_call_id AND ae.session_id = au.session_id
LEFT JOIN (
    SELECT id, argMax(value, updated_at) AS value
    FROM clients
    GROUP BY id
) AS c
  ON ae.event_client_id = c.id
WHERE ae.timestamp >= now() - INTERVAL WINDOW DAY
GROUP BY client
ORDER BY cost DESC
```

Splits spend across the clients writing to this stack.
`clients` is joined through a `GROUP BY id` subquery, never directly: it holds un-merged `ReplacingMergeTree` versions of the same id, and a direct join multiplies that client's calls and cost by its version count.
Cross-check the result - the summed `calls` must equal `count()` on `agent_usage` for the same window, or the join is fanning out somewhere.

## Q14 - latency, on demand

```sql
SELECT model,
       round(avg(ttft_ms))              AS avg_ttft,
       round(quantile(0.9)(ttft_ms))    AS p90_ttft,
       count()                          AS calls
FROM agent_usage
WHERE timestamp >= now() - INTERVAL WINDOW DAY
  AND ttft_ms > 0
GROUP BY model
ORDER BY calls DESC
```

`ttft_ms` is time to first token; total call latency lives in `agent_events.latency_ms`.
Latency is a friction finding, never a cost finding.

## Session scope

For `session` scope, add this to every query's `WHERE`, using the value of `CLAUDE_CODE_SESSION_ID`:

```sql
AND session_id = 'THE_SESSION_ID'
```

Q5, Q6 and Q11 are meaningless at session scope - skip them and report Q4's operation table instead.
