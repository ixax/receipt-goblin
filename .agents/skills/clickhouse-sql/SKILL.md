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
  <version>1.2.0</version>
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

## Sanctioned tools/scripts for this stack

- **`mcp__dev__query`** - the only sanctioned way to run a SELECT/WITH against live data (read-only by server-side validation, `mcp-dev` service).
  Accepts either a single SQL string or a list of independent SQL strings to run as one batch instead of looping - see the tool's own docstring for the exact response shape.
  Never `docker exec .../clickhouse-client` or any other direct connection - see AGENTS.md's base ClickHouse-access rule.
- **`mcp__dev__profile_query`** - same validation as `query`, but returns cost metrics (`memory_usage_bytes`/`read_rows`/`read_bytes`/`query_duration_ms`) instead of result rows, for comparing two versions of a query.
- **`services/grafana/scripts/query_perf.py`** - deterministic resolve/save-run/diff/report tooling for tracking a dashboard query's cost over time (before/after a rewrite).
  Driven by `sql-expert`, executed by `query-perf-runner`.
  Read its own docstring for exact syntax before guessing at flags.
- **`services/grafana/scripts/parse_dashboard.py`** - the only supported way to read `services/grafana/dashboards/agents_overview.json`'s panel structure (via the `dashboard-parser` agent) - don't hand-parse that file's large v2beta1 JSON inline.

## Escalation

If a query is behaving inexplicably and nothing here (or in `services/clickhouse/schema.sql`) explains it, that's exactly the case `sql-expert` exists for - it's the agent that owns ClickHouse DBA-level investigation for this stack.
Don't burn a long debugging cycle rediscovering a ClickHouse-version-specific quirk solo.
Escalate once the obvious explanations (typo, wrong column, wrong table) are ruled out.
