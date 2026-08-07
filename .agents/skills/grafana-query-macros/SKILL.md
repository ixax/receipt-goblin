---
name: grafana-query-macros
description: >
  Grafana-to-plain-SQL macro/variable substitution rulebook for running a dashboard panel's rawSql outside Grafana.
  TRIGGER - read before substituting any $__macro or $variable placeholder out of a panel's rawSql to run it directly against a database.
  v1.0.0
---

Panel `rawSql` is written for Grafana's ClickHouse plugin and contains macros and `$variable` placeholders that are not valid SQL on their own.
Substitute them with concrete literals before running the query directly - most raw-SQL tools only accept a single plain SELECT/WITH statement, no macros:

- `$__timeFilter(col)` -> `col >= now() - INTERVAL <N> HOUR` (default `N=24` unless a specific window is requested).
- `$__fromTime` / `$__toTime` -> `now() - INTERVAL <N> HOUR` / `now()` (same window as above).
- `$__interval` -> a concrete bucket, e.g. `INTERVAL 1 HOUR`, sized so the chosen time window produces a reasonable number of buckets.
- `${var:singlequote}` (multi-select template variables, used as `has([${var:singlequote}], '__all__') OR has([${var:singlequote}], col)`) -> replace with `'__all__'` so the "all values selected" branch is true.
  This matches the dashboard's default state and keeps the query semantically valid without needing real filter values.
- A bare single-select variable like `$provider` -> `'all'` (or whatever literal that variable's own OR-chain treats as "no filter" - check the surrounding SQL, e.g. `'$provider' = 'all' OR ...`).
- Drop or resolve anything else Grafana-specific the same way: read the surrounding SQL to see what value makes the clause a no-op filter, and use that.

After substitution, re-read the query and confirm it's a single SELECT/WITH statement before running it.
