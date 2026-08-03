# ClickHouse gotchas (receipt-goblin agent-tracking stack)

Grep this file for a keyword before reading it in full.
Entries are flat and terse: symptom, cause, fix.
Add new findings here, not in SKILL.md.
Keep each entry a few lines max, no essays.

## String literal escapes

- `\b` inside a SQL string literal is consumed by ClickHouse's lexer as a literal backspace byte before RE2 ever sees it, so a word-boundary regex silently matches nothing.
  Fix: write `\\b` (doubled backslash) in the SQL source.
  Same risk for any other `\<letter>` escape a regex relies on.
- RE2 (`match`/`replaceRegexpAll`/`replaceRegexpOne`/`extract`) has no lookahead/lookbehind - `(?=...)` etc. fail with `invalid perl operator`.
  Fix: two-pass replace (over-match, then a second `replaceRegexpAll` that positively re-matches and undoes the excluded case) instead of a lookahead rewrite.

## JOIN ON restrictions

- `ON x IN (a, b)` with a non-constant list (columns from the other side) fails with `UNSUPPORTED_METHOD`.
  `ON x=a OR x=b` (cross-table OR) fails with `INVALID_JOIN_ON_EXPRESSION` (needs `allow_experimental_join_condition`, avoided in this repo).
  Fix: `ARRAY JOIN [a,b] AS v, ['slot1','slot2'] AS tag`, then a plain equi-join, then pivot back with `maxIf`/conditional aggregation.
  Not automatically faster: halves `read_rows`/memory but showed higher, noisier wall-clock than the original on `agents_overview.json` panels 23/24 at ~100K-row scale - verify with repeated `profile_query` runs, variance is large.

## CTE re-execution multiplication

- ClickHouse doesn't materialize `WITH` CTEs - every reference re-runs the subquery from scratch.
  A CTE referenced 15-20x (common in wide `UNION ALL`/multi-JOIN dashboard queries) turns a ~500ms fragment into a >10s or OOM'ing whole query, even on tiny tables, while each fragment looks fast tested in isolation.
  Confirmed on `agents_overview.json` panel 76 ("Trace").
  Fix: reduce the reference count, not the per-reference cost - collapse downstream CTEs that each independently re-scan the same base CTE into fewer, wider CTEs computed once.

## Same-SELECT alias chains don't memoize - "Query tree is too big"

- Referencing a same-SELECT alias more than once does NOT create a shared node - each reference re-expands the alias's full defining expression at parse time.
  N levels of aliases, each referencing the previous level's alias twice, blows up combinatorially (~2^N nodes) into the internal `500000`-node limit (`Code: 36`, `BAD_ARGUMENTS`, "Query tree is too big") even on tiny data - the blowup is the alias chain itself, not base-table re-scans, so moving the expression into its own upstream CTE doesn't help.
  Fix: force real materialization boundaries - a strictly linear pipeline of separate, single-reference CTEs, each selecting only from the immediately preceding one, so later steps consume genuine upstream columns (cheap to re-project) instead of re-expandable alias expressions.
  Reference case: panel-99 ("Fork tree") - 15 chained single-purpose CTEs replaced a failing `groupArray()`+`indexOf()` alias chain, matched the 7-level self-join's output byte-for-byte, wall-clock ~35.7s -> ~9.2s, read_rows ~774k -> ~236k on the biggest real session.

## `query_perf.py` bare-brace `${var}` bug

- `services/grafana/scripts/query_perf.py resolve` doesn't match `${window}` (bare braces, no `:singlequote` suffix) - it reports "resolved" with zero unresolved vars, but the SQL still literally contains `${window}`.
  Fails later, at `profile_query` time, with an unrelated-looking `Unmatched parentheses` syntax error.
  Fix: none from the query-authoring side.
  Substitute by hand using `BARE_VAR_DEFAULTS['window']` (3600) and profile via `mcp__dev__profile_query` directly.

## `column_type_names` bypasses schema verification (`clickhouse_connect`)

- `services/_common/src/ingest_db.py`/`side_ingest.py` pass `column_type_names=...` on every `client.insert(...)` (`_INVOCATION_COLUMN_TYPES`, `_EVENT_COLUMN_TYPES`, `_USAGE_COLUMN_TYPES`, `_MESSAGE_COLUMN_TYPES`, `_SOURCE_COLUMN_TYPES`, `_GROUP_COLUMN_TYPES`, `_USER_COLUMN_TYPES`, `_CLIENT_COLUMN_TYPES`, `_FAILURE_COLUMN_TYPES` in `ingest_db.py`; `_GIT_BRANCH_COLUMN_TYPES`/`_PLAN_PROPOSAL_COLUMN_TYPES`/`_LITELLM_ALERT_COLUMN_TYPES` in `side_ingest.py`), skipping `clickhouse_connect`'s per-insert `DESCRIBE TABLE` round trip (was ~half of all `ingest`-user queries: 828 `DESCRIBE`/10min -> 0, per `system.query_log`).
- The list is a hint, not verified: `clickhouse_connect` encodes by whatever type the Python list says, never checking the live schema - hand-copied from `services/clickhouse/schema.sql`, so it drifts silently.
  Drift consequences: type changed incompatibly -> loud failure or silent truncation/corruption; column added -> harmless (gets its `DEFAULT`); column renamed/removed -> loud "column doesn't exist".
- Rule: a `schema.sql` type change on any column these lists cover updates the matching Python constant in the same change - nothing else catches the mismatch.

## Inline `SETTINGS` clause silently ignored when not on the truly outermost statement

- A `SETTINGS key = value` clause inside a subquery (anything in a `FROM (...)`) parses fine but has zero effect - ClickHouse honors `SETTINGS` only on the genuinely outermost statement as sent.
  Confirmed on 24.8.14.39 with `max_memory_usage`: `SETTINGS max_memory_usage = 100000000` on an inner subquery ran past ~400MB; the same clause top-level failed at exactly `maximum: 95.37 MiB`, and raising it top-level ran past the profile's 2.4 GiB default.
- `mcp__dev__query`'s `_do_query` wraps every submitted SQL as `SELECT * FROM (<your sql>) AS _query_result LIMIT n` (`services/mcp-dev/src/server.py`) - demoting any inline `SETTINGS` to non-top-level, so an override tested via `query()` always looks ignored/clamped even though nothing blocks it.
  `mcp__dev__profile_query`'s `_do_profile` does NOT wrap (`client.command(f"{sql} FORMAT Null", ...)`) - use `profile_query` to test whether an inline `SETTINGS` override takes effect.
- Panels: Grafana's datasource plugin sends `rawSql` unwrapped, so a `SETTINGS` clause on the panel query's own outermost `SELECT` (after its terminal `GROUP BY`/`ORDER BY`, as `agents_overview.json` panel 76 does) is genuinely top-level and respected - don't conclude infeasibility from a `query()` re-test.

## `mcp-dev` SQL validator (`services/mcp-dev/src/server.py`)

- The `;`/keyword/`SYSTEM`/table-function checks scan SQL text with string literals masked out first, via quote-aware `_mask_string_literals` - unmasked, a literal containing `;` (e.g. HTML entities like `&amp;`) or a forbidden-looking word as plain text would false-reject.
  If a query with such a literal gets rejected, suspect a regression of that masking.
- `ARRAY JOIN <expr>` (e.g. `ARRAY JOIN arrayMap(...)`) can get its expression's function name misparsed as a joined table by `_TABLE_REFS_RE`.
  If a validator rejection names a function as an unknown table, this is why.
- `query`/`profile_query` accept an explicit `max_duration_s` parameter (default 10s, server-clamped to a hard cap in `config.yml`).
  Pass it directly for a known-slow query instead of embedding `SETTINGS max_execution_time` in the SQL text.
