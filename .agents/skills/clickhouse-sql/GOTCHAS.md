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
  Chaining N levels of aliases where each level's expression references the previous level's alias twice (e.g. `idx1 -> parent1` used by `idx2`'s definition, `idx2 -> parent2` used by `idx3`'s, ...) blows up combinatorially (~2^N nodes).
  This hits ClickHouse's internal `500000`-node limit (`Code: 36`, `BAD_ARGUMENTS`, "Query tree is too big") even when the underlying data is tiny.
  Hit this trying to replace panel-99's 7-level self-join with a `groupArray()`+`indexOf()` ancestor-walk - both embedding the window function directly in one CTE's SELECT list and splitting it into its own upstream CTE failed identically, because the blowup came from the alias chain itself, not from re-scanning a base table.
  Fix: force real materialization boundaries.
  Turn the alias chain into a strictly linear pipeline of separate, single-reference CTEs - each new CTE selects only from the immediately preceding CTE, so a later step's inputs are genuine upstream *columns* (cheap to re-project) rather than re-expandable in-SELECT alias expressions.
  Confirmed fix on panel-99 ("Fork tree"): 15 chained single-purpose CTEs replaced the failing alias-chain design, ran clean, and matched the original 7-level self-join's output byte-for-byte while cutting the biggest real session's wall-clock from ~35.7s to ~9.2s and read_rows from ~774k to ~236k.

## `query_perf.py` bare-brace `${var}` bug

- `services/grafana/scripts/query_perf.py resolve` doesn't match `${window}` (bare braces, no `:singlequote` suffix) - it reports "resolved" with zero unresolved vars, but the SQL still literally contains `${window}`.
  Fails later, at `profile_query` time, with an unrelated-looking `Unmatched parentheses` syntax error.
  Fix: none from the query-authoring side.
  Substitute by hand using `BARE_VAR_DEFAULTS['window']` (3600) and profile via `mcp__dev__profile_query` directly.

## `column_type_names` bypasses schema verification (`clickhouse_connect`)

- `services/_common/src/ingest_db.py` and `services/_common/src/side_ingest.py` pass `column_type_names=...` on every `client.insert(...)` call (e.g. `_INVOCATION_COLUMN_TYPES`, `_EVENT_COLUMN_TYPES`, `_USAGE_COLUMN_TYPES`, `_MESSAGE_COLUMN_TYPES`, `_SOURCE_COLUMN_TYPES`, `_GROUP_COLUMN_TYPES`, `_USER_COLUMN_TYPES`, `_CLIENT_COLUMN_TYPES`, `_FAILURE_COLUMN_TYPES` in `ingest_db.py`; `_GIT_BRANCH_COLUMN_TYPES`/`_PLAN_PROPOSAL_COLUMN_TYPES`/`_LITELLM_ALERT_COLUMN_TYPES` in `side_ingest.py`) so `clickhouse_connect` skips its per-insert `DESCRIBE TABLE` round trip (this was ~half of all queries from the `ingest` ClickHouse user - 828 `DESCRIBE` calls/10min before the fix, 0 after, confirmed via `system.query_log`).
- This is a hint, not a re-verified fact: `clickhouse_connect` trusts and encodes by whatever type the Python list says, and does NOT check it against the live schema.
  These lists are hand-copied from `services/clickhouse/schema.sql` at write time, so they can drift silently if the schema changes later without the matching constant being updated.
- Consequences of schema.sql changing a column's type without the Python constant following it in the same change:
  - Type narrowed/changed incompatibly -> insert can fail loudly, or in subtler cases silently truncate/corrupt data instead of erroring.
  - Column added to the table -> harmless, just unpopulated (existing lists stay valid, new column gets its `DEFAULT`) until code is updated to include it.
  - Column renamed/removed -> insert fails loudly with a clear "column doesn't exist" error, easy to catch.
- Fix/rule: whenever a column used by one of these `column_type_names` lists changes type in `schema.sql`, update the matching Python constant in the same change - `column_type_names` bypasses ClickHouse's own schema check, so nothing else will catch the mismatch.

## Inline `SETTINGS` clause silently ignored when not on the truly outermost statement

- A `SETTINGS key = value` clause attached to a subquery (anything inside a `FROM (...)`) parses without error but has zero effect on that setting for the query's actual execution.
  ClickHouse only honors `SETTINGS` from the genuinely outermost statement as sent to the server.
  Confirmed on 24.8.14.39 for `max_memory_usage`: a query with `SETTINGS max_memory_usage = 100000000` on an inner subquery ran past ~400MB with no error; the identical clause on the true top-level statement failed immediately with `maximum: 95.37 MiB` (exactly the requested value), and raising it on the true top-level statement let a query that normally hits the profile's 2.4 GiB default run right past that ceiling.
- `mcp__dev__query`'s own `_do_query` always wraps whatever SQL you pass it as `SELECT * FROM (<your sql>) AS _query_result LIMIT n` (`services/mcp-dev/src/server.py`).
  This demotes any `SETTINGS` clause inside your submitted SQL to non-top-level, so testing a `SETTINGS max_memory_usage = ...` override via `query()` will always look like it's being ignored/clamped back to the profile default, even though nothing is actually blocking it.
- `mcp__dev__profile_query`'s `_do_profile` does NOT wrap (`client.command(f"{sql} FORMAT Null", ...)`).
  Use `profile_query`, not `query`, to test whether an inline `SETTINGS` override actually takes effect.
- Practical implication for dashboard panels: a panel's `rawSql` is sent to ClickHouse by Grafana's datasource plugin directly, with no wrapping.
  A `SETTINGS` clause on the panel query's own final/outermost `SELECT` (e.g. right after its terminal `GROUP BY`/`ORDER BY`, as `agents_overview.json` panel 76 already does) is genuinely top-level and is respected.
  Don't conclude an override is infeasible just because it appeared to fail when re-tested through `mcp__dev__query`.

## `mcp-dev` SQL validator (`services/mcp-dev/src/server.py`)

- The `;`/keyword/`SYSTEM`/table-function checks scan SQL text with string literals masked out first, via quote-aware `_mask_string_literals` - unmasked, a literal containing `;` (e.g. HTML entities like `&amp;`) or a forbidden-looking word as plain text would false-reject.
  If a query with such a literal gets rejected, suspect a regression of that masking.
- `ARRAY JOIN <expr>` (e.g. `ARRAY JOIN arrayMap(...)`) can get its expression's function name misparsed as a joined table by `_TABLE_REFS_RE`.
  If a validator rejection names a function as an unknown table, this is why.
- `query`/`profile_query` accept an explicit `max_duration_s` parameter (default 10s, server-clamped to a hard cap in `config.yml`).
  Pass it directly for a known-slow query instead of embedding `SETTINGS max_execution_time` in the SQL text.
