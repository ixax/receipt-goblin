---
name: clickhouse-sql
description: >
  Knowledge base of ClickHouse-specific SQL gotchas and sanctioned tool/script pointers for this
  repo's agent-tracking stack.
  TRIGGER - read BEFORE writing/debugging non-trivial ClickHouse SQL: regex functions
  (replaceRegexpAll/replaceRegexpOne/match/extract), string-literal escapes, Map-type columns,
  CAST/type-conversion, WITH/CTE alias resolution, ASOF JOIN, or any query whose result doesn't
  match what the SQL visibly says. Also read reactively when a query behaves inexplicably, even
  if not flagged as risky.
  Points at sanctioned tools: mcp__dev__query/profile_query (read/profile), query_perf.py
  (benchmark tracking), parse_dashboard.py (agents_overview.json structure) - all under
  services/grafana/scripts/.
  SKIP for a trivial query: no regex, no string-literal escapes, no Map columns, no CTE aliasing,
  behaving as expected.
  <version>1.3.0</version>
---

# clickhouse-sql

Running knowledge base of ClickHouse behaviors that don't match naive expectations, discovered the hard way in this repo.
Consult this before writing risky SQL, and consult it again - first - the moment a query behaves inexplicably, before spending a debugging cycle rediscovering something already documented here.

**Adding an entry**: when you (or an agent you're reviewing) resolves a new ClickHouse surprise, add it under the matching category below, or a new category if none fits.
One entry = symptom, cause, fix - short, concrete, generalized (not tied to one specific query/panel).
Don't let this file balloon into essay-length entries.
Keep each one scannable.

## String literals and escaping

- **The SQL lexer applies C-style escape processing *inside single-quoted string literals*, before the string reaches any function - including regex functions.**
  `\b` inside a string literal is not passed through as the two characters backslash+`b`.
  ClickHouse's lexer folds it into a literal backspace control byte (`0x08`) at parse time, the same way `\n` becomes a newline and `\t` becomes a tab.
  This happens *before* the string ever reaches `replaceRegexpAll`/`replaceRegexpOne`/`match`/etc., which run on RE2 - so RE2 never even sees a word-boundary anchor, it sees a raw backspace byte, and the regex silently fails to match anything useful (no error - just wrong/empty results).
  **Fix**: write `\\b` in the SQL source text whenever you want RE2's `\b` word-boundary anchor to actually reach the regex engine as two characters (backslash, `b`).
  The same doubling applies to any other backslash escape you want RE2 to see literally rather than have the lexer consume it first (e.g. `\\d`, `\\s`, `\\w` are usually fine since ClickHouse's lexer has no single-character escape for those letters, but don't assume - if a regex using a `\<letter>` sequence silently under/over-matches, suspect lexer-level escape consumption first, before assuming the regex engine itself is wrong).
  **Recognize this bug by its symptom**: a regex that looks correct, passes a mental read-through, and works fine when tested against the same pattern in a non-ClickHouse regex tester, but matches nothing (or matches everything) when run inside ClickHouse specifically.
  That mismatch - correct-looking pattern, wrong-only-in-ClickHouse result - is the signature of the lexer eating an escape sequence before RE2 gets it.

- **RE2 (ClickHouse's regex engine for `match`/`replaceRegexpAll`/`replaceRegexpOne`/`extract`/etc.) has no lookahead or lookbehind support at all** - none of `(?=...)`, `(?!...)`, `(?<=...)`, `(?<!...)` work, in any position, for any purpose.
  Using one fails outright (not silently): `DB::Exception: The pattern argument is not a valid re2 pattern: invalid perl operator: (?=` (confirmed on ClickHouse 24.8.14.39) - so at least this is a loud, unambiguous error rather than a silent wrong-result bug, unlike the `\b`-becomes-backspace issue above.
  **Don't reach for a lookahead/lookbehind fix and expect it to just need syntax tweaking - it needs an entirely different, lookahead-free approach.**
  A common case: "match X only if NOT immediately followed by Y" (e.g. don't treat `/usr` as a highlightable token if it's immediately followed by another `/`, i.e. it's actually part of a longer path like `/usr/local/bin`).
  Workaround pattern that avoids lookahead entirely: run the original (over-eager) match/replace first, then run a *second* `replaceRegexpAll` pass over its own output that positively matches the specific "Y followed X" shape you want to undo/exclude, and reverts/strips just that - e.g. `replaceRegexpAll(replaceRegexpAll(text, 'X', '<mark>X</mark>'), '<mark>(X)</mark>(/)', '\1\2')` un-marks any match immediately followed by another `/`.
  This only works when the excluded case can be positively re-matched after the fact in its now-transformed form; if that's not possible for your case, escalate to `sql-expert` rather than trying to force lookahead syntax to work.

## `mcp__dev__query`/`mcp__dev__profile_query` validator false-positives

- **The `query`/`profile_query` validator scans the raw SQL *text* for a few restricted tokens - at least a bare `;` and the keyword `SYSTEM`.**
  It does not check the parsed statement.
  So it rejects a valid single SELECT/WITH if either substring merely appears *inside a string literal or comment*, with no awareness of quoting.
  Confirmed with a minimal repro: `SELECT 'a&amp;b' AS x` (a `;` inside a literal HTML entity, no second statement anywhere) fails with `Only a single statement is allowed (no ';' inside the query).`
  A query containing the literal text `<system-reminder>` (e.g. inside a regex pattern string or a `--` comment) fails with `'SYSTEM' is not allowed in read-only queries.`
  Same naive-scan cause, different keyword.
  **Symptom**: a query that is visibly one single read-only SELECT/WITH, with no second statement and no `SYSTEM ...` command anywhere in its actual grammar, still gets rejected by `query`/`profile_query`.
  **Where this actually bites**: `agents_overview.json` panel 99 ("Fork tree") legitimately builds HTML by concatenating entities like `&amp;`, `&lt;`, `&gt;`, `&#42;` into string literals, and each of those entities contains a `;`.
  Its prompt-cleaning CTEs also reference `<system-reminder>` tags, both in `-- comments` and inside `replaceRegexpAll` pattern literals.
  So panel 99's real rawSql cannot be run through `profile_query` at all, which blocks the standard before/after benchmarking workflow for that panel.
  **Panel 76 ("Trace: $session_id") had the exact same problem, first confirmed 2026-08-02, and the validator fix for it was confirmed deployed later the same day.** Panel 76 HTML-escapes via `replaceAll(..., '&', '&amp;')`/`'&lt;'`/`'&gt;'`/`'&quot;'` literals throughout (tool-arg escaping, reply/prompt markdown rendering, agent-spawn description escaping, etc.), so it hit the same `;`-in-literal false positive as panel 99 until `_mask_string_literals` (quote-aware masking) landed in `services/mcp-dev/src/server.py` and the `mcp-dev` container was restarted to pick it up. Re-verified 2026-08-02 by running panel 76's complete, unmodified rawSql (all 1350 lines, all 8 UNION ALL branches, small 438-row test session) through `mcp__dev__query`/`mcp__dev__profile_query` end-to-end: it passed validation cleanly (no `;`/`SYSTEM` false positive anywhere in the query) and reached real ClickHouse execution, where it hit a genuine, unrelated performance problem instead (see "CTE re-execution multiplication" below). If the same rejection resurfaces on a panel with `;`-containing literals, suspect a regression/rollback of that masking fix before re-deriving the bug from scratch.
  This is not unique to panel 99 - any Dynamic Text panel in this dashboard that HTML-escapes its own output (which is all of them, per the "editor.format: html" convention in `Skill(dynamictext-panel-queries)`) will hit this same block.
  **Do not "fix" this by stripping/renaming the offending substrings in the query text to dodge the validator.**
  The harness's own permission classifier will (correctly) flag that as a keyword-bypass attempt.
  Even if it didn't, silently mutating the SQL under benchmark defeats the point of measuring the *real* query.
  Report the block instead of routing around it.
  **Fix**: this needs a genuine bug fix to the validator itself - proper tokenizing/quote-awareness for the `;`/`SYSTEM` checks.
  It is not something an agent can work around from the query-authoring side.
  Until fixed, panel 99 (and any other panel whose SQL embeds `;` or `system`-containing text in a literal/comment) cannot be profiled via `mcp__dev__profile_query`/`query_perf.py`.
  Flag this limitation rather than fabricating or approximating numbers.

## JOIN ON clause restrictions

- **A JOIN's ON clause can't use `IN (col_a, col_b)` where the list contains columns from the other side of the join (non-constant arguments) - only a constant list or a table expression is accepted.**
  Trying `... ON uf.agent_version IN (t.current_version, t.prev_version)` fails with `DB::Exception: Function 'in' is supported only if second argument is constant or table expression. (UNSUPPORTED_METHOD)`.
  Rewriting it as `... ON uf.agent_version = t.current_version OR uf.agent_version = t.prev_version` doesn't help either - ClickHouse then rejects it with `DB::Exception: JOIN ... join expression contains column from left and right table, you may try experimental support of this feature by SET allow_experimental_join_condition = 1. (INVALID_JOIN_ON_EXPRESSION)`, because an OR of two equalities isn't reducible to the single-equi-join shape ClickHouse's planner wants (short of opting into the experimental setting, which this repo avoids relying on).
  **Symptom**: trying to join one row to "either of two possible matching values" (e.g. a row's current version OR its previous version) via a single JOIN with an IN/OR condition in the ON clause.
  **Fix**: use `ARRAY JOIN` to expand the "either of two values" side into two separate tagged rows first (e.g. `ARRAY JOIN [current_version, prev_version] AS ver, ['after', 'before'] AS slot`), then do a single plain equi-join (`ON uf.agent_version = te.ver`) against the expanded rows, and re-pivot the two slots back into columns afterward with `maxIf(value, slot = 'after')` / `maxIf(value, slot = 'before')` style conditional aggregation in a subsequent GROUP BY.
  This avoids the two separate LEFT JOINs (and the CTE double-evaluation that comes with referencing the same filtered CTE twice) that the naive before/after-in-two-CTEs pattern requires.
  But note it's not automatically a performance win: on `agents_overview.json` panels 23/24 (each joining a filtered `usage_f`/`events_f` CTE), this ARRAY JOIN + two-stage-GROUP-BY pivot roughly halved `read_rows`/memory (single join reference instead of two) yet showed *higher and noisier* wall-clock `query_duration_ms` than the original two-CTE version across repeated `profile_query` runs, at the table's current ~100K-row scale.
  The extra ARRAY JOIN row multiplication plus the second aggregation stage's fixed overhead outweighed the I/O savings.
  Verify with repeated before/after `profile_query` runs (not a single sample - variance was large enough to flip the apparent winner between runs) before trusting this rewrite as a win; it may only pay off once the table is large enough that the halved I/O dominates.

## CTE re-execution multiplication

- **ClickHouse does not materialize a `WITH ... AS (...)` CTE - every reference to its name re-runs the CTE's own subquery (including any JOINs/window functions inside it) from scratch, independently.**
  This is easy to miss because each *individual* reference can look perfectly fast in isolation - the cost only shows up once a query references the same (or a downstream-dependent) CTE many times across several `UNION ALL` branches and/or several `LEFT JOIN`s in one statement, multiplying what looked like a cheap sub-500ms fragment by 15-20x.
  **Confirmed live** on `agents_overview.json` panel 76 ("Trace: $session_id", a ~1350-line, 23-CTE, 8-branch-`UNION ALL` Dynamic Text query): a tiny 438-row test session (`4888e481-7724-4617-976a-ebc87b5c6ad5`) times out at `mcp__dev__query`'s 10-second `max_execution_time` cap running the query as deployed, yet every individual CTE/fragment tested standalone (the base `scoped_events` window-function CTE, the full 6-way-joined tie=3 branch's row cardinality check, the triple-redundant `reply_render` escaping/markdown chain) came back in 250-900ms with `read_rows` matching the base table's row count (no join-fanout blowup - ruled out as the cause). The only thing that explains a >10s total from several sub-1s fragments is each CTE being re-evaluated once per reference: `scoped_events` (or CTEs built on top of it, like `prompt_final`) is referenced roughly 15-20 times total across `tool_render`, `reply_trunc`, `reply_render`, `failure_error`, `tie2_ts`, `agent_spawn_events`, `child_anchor_raw`, `plan_proposals_match`, `session_header`, `stats_tokens`, `stats_agent_names`, `stats_skills`, `stats_time`, `prompt_calc`→`prompt_final`, and 3-4 more times directly inside the final `UNION ALL` branches - each reference re-running the same JOIN+window-function computation independently, multiplying a ~500ms unit cost into a >10s cumulative one even though the underlying table only has 438 rows for this session.
  **Symptom**: a query with many CTEs feeding a wide `UNION ALL`/multi-`JOIN` final `SELECT` is dramatically slower than its parts suggest - individually-profiled fragments are fast, the row counts are small, but the assembled whole times out or approaches a memory limit anyway.
  **Fix pattern (not yet applied/verified on panel 76 - this needs its own before/after benchmarking before landing)**: reduce the *reference count* of the expensive base CTE(s), not their per-reference cost - e.g. collapse several single-purpose downstream CTEs that each independently re-scan `scoped_events` into fewer, wider CTEs computed once each; or pre-aggregate everything `session_stats`/`stats_*` need in one pass over `scoped_events` instead of six separate CTEs each re-deriving from it. This is a materially larger rewrite than the JOIN-ON-clause ARRAY JOIN fix above (touches CTE structure throughout the query, not one join), so treat it as its own attempt-budgeted investigation, not a quick patch - and confirm via `mcp__dev__profile_query`/`query_perf.py` before/after, same discipline as any other rewrite.

## Sanctioned tools/scripts for this stack

- **`mcp__dev__query`** - the only sanctioned way to run a SELECT/WITH against live data (read-only by server-side validation, `mcp-dev` service).
  Accepts either a single SQL string or a list of independent SQL strings to run as one batch instead of looping - see the tool's own docstring for the exact response shape.
  Never `docker exec .../clickhouse-client` or any other direct connection - see AGENTS.md's base ClickHouse-access rule.
- **`mcp__dev__profile_query`** - same validation as `query`, but returns cost metrics (`memory_usage_bytes`/`read_rows`/`read_bytes`/`query_duration_ms`) instead of result rows, for comparing two versions of a query.
- **`services/grafana/scripts/query_perf.py`** - deterministic resolve/save-run/diff/report tooling for tracking a dashboard query's cost over time (before/after a rewrite).
  Driven by `sql-expert`, executed by `query-perf-runner`.
  Read its own docstring for exact syntax before guessing at flags.
- **`services/grafana/scripts/parse_dashboard.py`** - the only supported way to read `services/grafana/dashboards/agents_overview.json`'s panel structure (via the `dashboard-parser` agent) - don't hand-parse that file's large v2beta1 JSON inline.

- **`query_perf.py resolve` silently leaves bare-brace `${name}` template variables (no `:singlequote` suffix) unsubstituted, producing invalid SQL that only fails later, at `profile_query` time.**
  The script's two substitution regexes are `_SINGLEQUOTE_RE = r"\$\{(\w+):singlequote\}"` and `_BARE_VAR_RE = r"\$(\w+)\b"` (no `{`/`}`) - neither matches `${window}` (braces, no modifier), which is exactly how `agents_overview.json` writes the histogram-bucket-width variable (`INTERVAL ${window} SECOND`, 41 occurrences across the dashboard as of this writing).
  `resolve`'s `unresolved_names` check is itself regex-based (`set(_BARE_VAR_RE.findall(sql)) | set(_SINGLEQUOTE_RE.findall(sql))`), so it doesn't catch this either - `resolved_sql` comes back looking "resolved" (empty `unresolved_vars` list) while still literally containing `${window}` in the text.
  The failure only surfaces one step later, when that resolved_sql is actually run: `DB::Exception: Syntax error ... Unmatched parentheses` (ClickHouse's parser gives a confusing downstream error, not anything mentioning `${window}`).
  **Symptom**: `query_perf.py resolve`/`query-perf-runner` reports success (no unresolved vars) for a panel using `${window}`, but the very next `profile_query` call on that same resolved_sql throws a syntax error unrelated-looking to the real cause.
  **Fix**: none from the query-authoring side - like the panel-99 `;`/`SYSTEM` validator gap above, this needs `_BARE_VAR_RE` (or a new regex) extended to also match `\$\{(\w+)\}` with no modifier, in `query_perf.py` itself. Until that's fixed, any panel using `${window}` (or another bare-brace var) can't go through the standard `resolve`/`save-run` pipeline - substitute it by hand (using `BARE_VAR_DEFAULTS['window']`'s own value, `3600`, to stay consistent with what the script would have done) and profile via `mcp__dev__profile_query` directly instead, same fallback as the panel-99 case.

## Escalation

If a query is behaving inexplicably and nothing here (or in `services/clickhouse/schema.sql`) explains it, that's exactly the case `sql-expert` exists for - it's the agent that owns ClickHouse DBA-level investigation for this stack.
Don't burn a long debugging cycle rediscovering a ClickHouse-version-specific quirk solo.
Escalate once the obvious explanations (typo, wrong column, wrong table) are ruled out.
