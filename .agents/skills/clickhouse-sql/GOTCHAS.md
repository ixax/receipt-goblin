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

## `query_perf.py` bare-brace `${var}` bug

- `services/grafana/scripts/query_perf.py resolve` doesn't match `${window}` (bare braces, no `:singlequote` suffix) - it reports "resolved" with zero unresolved vars, but the SQL still literally contains `${window}`.
  Fails later, at `profile_query` time, with an unrelated-looking `Unmatched parentheses` syntax error.
  Fix: none from the query-authoring side.
  Substitute by hand using `BARE_VAR_DEFAULTS['window']` (3600) and profile via `mcp__dev__profile_query` directly.

## `mcp-dev` SQL validator (`services/mcp-dev/src/server.py`)

- The `;`/keyword/`SYSTEM`/table-function checks scan SQL text with string literals masked out first, via quote-aware `_mask_string_literals` - unmasked, a literal containing `;` (e.g. HTML entities like `&amp;`) or a forbidden-looking word as plain text would false-reject.
  If a query with such a literal gets rejected, suspect a regression of that masking.
- `ARRAY JOIN <expr>` (e.g. `ARRAY JOIN arrayMap(...)`) can get its expression's function name misparsed as a joined table by `_TABLE_REFS_RE`.
  If a validator rejection names a function as an unknown table, this is why.
- `query`/`profile_query` accept an explicit `max_duration_s` parameter (default 10s, server-clamped to a hard cap in `config.yml`).
  Pass it directly for a known-slow query instead of embedding `SETTINGS max_execution_time` in the SQL text.
