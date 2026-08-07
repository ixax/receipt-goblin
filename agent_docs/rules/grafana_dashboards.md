# Grafana dashboard conventions

*Full rule content for `AGENTS.md`'s dashboard-skill routing pointers.*
*Read before creating/editing any panel, query, variable, or layout in a dashboard JSON under `services/grafana/dashboards/` or `dashboards-health/`.*
*Depth split: universal rules live here; panel-type-specific depth stays in the skills this file points to - don't duplicate their content, follow the pointer.*

## Schema & layout

- v2beta1 schema: top-level `apiVersion`/`kind`/`metadata`/`spec`; `spec.elements` holds panels keyed by `panel-<id>`; `spec.layout` is a `TabsLayout`/`GridLayout` tree of `GridLayoutItem`s.
- Some `dashboards-health/*.json` tabs use `RowsLayout` instead - `parse_dashboard.py`'s `list-tabs`/`list-panels` silently skip those tabs (no error), so cross-check against `summary`'s panel count (reads `spec.elements` directly) rather than trusting an empty/small list result.
- `agents_overview.json` reads go through the `dashboard-parser` agent; other dashboard files (`RowsLayout`/nested shapes it can't walk) get a direct `Read`.
- Every dashboard file is minified, one-key-per-line JSON in insertion order - never `Read` the whole file into context, and never `json.dump()` a full round-trip (reformats unrelated whitespace, huge diff).
- Read-edit-write is one atomic unit per change: read just the substring you're touching, make the one replacement, write immediately, then start the next edit's cycle fresh - never hold file content in memory across more than one edit, at any scale.
- Verify every edit: the `old` string occurs exactly the expected count before writing; `json.load()` still parses after; `git diff --stat` shows only the intended field; the change is live via `curl http://localhost:3000/api/dashboards/uid/<uid>` (provisioner reloads within ~30s); the panel's `description` is left non-empty and accurate.
- Mid-task mistakes are fixed forward with another scoped edit against the live file - never `git checkout`/`restore`/`reset`/`clean`, and never `git show :path` piped into the file to "start clean".
- Every `dashboards-health/*.json` file's `spec.timeSettings.from`/`to` defaults to `now-15m`/`now` - set/verify on every create or edit despite being a dashboard-level setting.
- A tab-scoped hidden variable (`TextVariable`, `hide: "hideVariable"`) passes per-click state (e.g. `trace_ts`, `trace_width_budget`) without touching the URL-visible filter variables - starts empty/default, read back by a companion panel or template arithmetic.

## Variable conventions

- `QueryVariable` - dropdown backed by a `rawSql` `SELECT DISTINCT col AS __value, col AS __text FROM ... ORDER BY __value`.
- `CustomVariable` - static option list, e.g. `window`'s `"30s : 30, 1m : 60, 5m : 300, ..."` query string.
- Standard multi-select "All" pattern: `multi: true`, `includeAll: true`, `allValue: "'__all__'"`, `current: {"text": "All", "value": "$__all"}`.
- Matching `rawSql` filter clause: `has([${var:singlequote}], '__all__') OR has([${var:singlequote}], col)`.
- A single-select "no filter" variable uses its own sentinel convention instead (e.g. `'$provider' = 'all' OR ...`) - read the surrounding SQL for what value makes the clause a no-op, don't assume `'__all__'` applies.
- `${window}`-style bare-brace variables (no `:singlequote` suffix) aren't detected as unresolved by `query_perf.py resolve` - it reports zero unresolved vars but the SQL still literally contains `${window}`, failing later at `profile_query` with an unrelated-looking syntax error; substitute by hand before profiling.

## Panel & query conventions

- `rawSql` reads like a formatted query file - real newlines, tab/2-space indentation for CTEs/subqueries/`AND` chains, never a minified one-liner.
- Standard filter clauses: `$__timeFilter(col)` is the plugin's time-range macro, applied to the panel's own primary timestamp column (`timestamp`/`u.timestamp`/`spawned_at`/`captured_at` depending on the table); `${window}` is the bucket-size `CustomVariable` for timeseries grouping; both resolve client-side in Grafana, never hand-substitute them into a deployed panel's `rawSql`.
- Test a panel's literal `rawSql` via `mcp__dev__query` with only `${...}` template placeholders substituted - never a simplified/reconstructed rewrite; a trimmed query that drops a join/column that looks like template-variable plumbing can pass while the real query fails.
- Every panel `description` is a fixed two-line `Goal:`/`Description:` format, <=100 words combined, surface-level only (grouping/filter/join/time-window a viewer already sees - never ClickHouse quirks or plugin internals), never a changelog.
  Mandatory on creation and on any edit to a panel's query or options - a stale or bloated description is worse than none.
- Table panel habits:
  - `vizConfig.spec.options.sortBy` names the same field(s)/direction as the SQL `ORDER BY`.
  - Long free-text/JSON columns get `custom.cellOptions: {"type": "auto"}` + `custom.inspect: true` - never the older `"json-view"` mechanism, and never a length-limiting SQL function on that column.
  - Column order follows the `SELECT` list, eye-icon columns last.
  - Hide an id column next to its display-name column (`custom.hidden: true`, not `hideFrom.viz`) and put the link on the display-name column instead.
  - A qualified column with no alias needs none added.
  - Every alias is named `<source>_<direction-or-qualifier>`, never a shortened/decorative name - rename every reference to it (`ORDER BY`, `sortBy`, an override, a `${__data.fields[...]}` link) together.
- `dataLink` general principles:
  - Build the path off `${__dashboard.uid}`, never a hardcoded slug.
  - Use the field's `dataLink` (`title`+`url`+`targetBlank`), never a raw `<a href>` baked into `rawSql` output - that's for Dynamic Text panels' freeform HTML only.
  - A single `dataLink` can only target one unambiguous value - never link an aggregated multi-value column.
- Chart colors: hardcode only for a fixed, small set of series, never a dynamic/unbounded one (leave `color.mode: "palette-classic"`).
  One series -> `fixedColor: "blue"`; two fixed known series -> blue then white.
- Legend: hide (`options.legend.showLegend: false`) when a panel plots exactly one value field; keep shown once it can have more than one series.
- Pie chart legends render as a table (`options.legend.displayMode: "table"`) with value and percent columns shown, never a plain list - forward-looking convention, don't retrofit an existing pie panel to match it.

`agents_overview.json`-specific conventions - never carried onto another dashboard's panels.
Full detail: `Skill(dashboard-panels)`.

- `unit: "locale"` for token columns, `unit: "currencyUSD"` for cost columns; a table never shows a bare `tokens` column, always the `tokens_input`/`tokens_output`(/`tokens_total`) split.
- A visible `session_id` column shows a mapped `"open ↗"` label via `${__value.raw}`, not the raw UUID.
- `custom.width` is fixed per semantic column type, not per-panel guessing:
  - session_id / user display-name -> 130
  - token count -> 150
  - cost/dollar -> 100
  - duration/elapsed-time -> 130
  - timestamp -> 170
  - issue -> 130
  - tool / agent / skill / command name -> 200
- dataLink URL patterns filter to a specific value, all via `var-<name>=${__value.text}` unless noted:
  - `var-user_id`, `var-issue_id`, `var-agent_name`, `var-skill_name`, `var-command_name`, `var-model` - direct.
  - `var-session_id` - also sets `dtab`/sub-`dtab` params (URL-encoded tab titles) to land on the Trace sub-tab.
  - `var-git_repo` + `var-git_branch` - always set together, never branch alone (branch names aren't unique across repos).

## Query-performance benchmarking

- Toolkit: `services/grafana/scripts/query_perf.py` (`resolve`/`save-run`/`diff`/`report`) - its own docstring is the source of truth for exact syntax.
- Every `rawSql` rewrite, requested or a side effect of other work, gets a mandatory before/after diff - not optional, and "should obviously be faster" isn't a finding.
  Workflow: `query-perf-runner` runs a `before` pass, the edit lands, `query-perf-runner` runs an `after` pass, `sql-expert` diffs the two run files via `query_perf.py diff`.
  Exit code 1 means something regressed - report it plainly.
  A brand-new panel skips before/after but still gets a baseline run.
- If a rewrite changes what the query returns (not just how fast), diff actual `mcp__dev__query` result values on both versions before trusting the perf numbers.
- Panel-76 ("Trace") and companion panel-77 are excluded from `resolve --all` by default - don't override.
- `query-perf-runner` is a cheap, mechanical delegate (`resolve` -> loop `profile_query` -> `save-run`, or `diff`) - it never invents a result, and an unresolved `$variable` is skipped and reported, never guessed at.

## `query_performance.json` sync

Only `agents_overview.json` has this query-cost mirror; `dashboards-expert` self-triggers this on every panel create/edit/remove there, even unasked, as part of the same task.
Three scripts, run in order - tag, extract, render:

1. `tag_panel_queries.py <file> --id <id> [...]` - appends/refreshes `SETTINGS log_comment = 'agents_overview:panel_<id>'`, idempotent.
2. `extract_panel_tree.py --source <file> --out <tree>` - fresh tree of every panel's id/title/tagged state; always re-run before rendering, never reuse a stale tree.
3. `build_query_perf_dashboard.py --tree <tree> --out <query_performance.json> [--tab "<title>"]` - `--tab` scopes the regeneration to that top-level tab; omit only for a full rebuild.

Per-edit scope: new panel or id change -> tag, extract, render its tab.
Moved to a different top-level tab -> one tag/extract, then render both origin and destination tabs.
Removed panel -> extract, render its former tab (mirror auto-prunes).
A query-content-only edit (same id, same tab) needs none of this.
Full-dashboard rebuild ("regenerate the whole dashboard", no panel named): extract fresh, tag every id marked untagged, extract again, render once with no `--tab`, confirm stderr has no `skipped untagged panels` warning, validate via `python -m json.tool`.

## Dynamic Text panels

Panel-76 ("Trace") and panel-99 ("Fork tree") in `agents_overview.json`; panel-77 ("Tool calls at $trace_ts", a plain `table` panel) is a named scope exception, grouped here because its query and click-through logic are inseparable from panel-76's.

- Plugin config that must not drift: `vizConfig.group` = `"marcusolsson-dynamictext-panel"`; `options.editor.format: "html"` (markdown mode escapes raw tags); `options.renderMode: "allRows"`; `options.defaultContent: ""`; the Handlebars `content` template stays a tiny `{{#each data}}...{{{this.tree}}}{{/each}}` wrapper (triple-stash required) - all real logic lives in the SQL.
- SQL shape: one row per session, not per event - tagged sub-selects `UNION ALL`ed, then `groupArray` + `arraySort` on `(sort_ts, tie, ts)` + `arrayStringConcat` into a single `tree` text column.
- Never `Read` the whole dashboard file or hand-edit with `Edit` for these panels - use the brace-matching splice procedure instead: find the unique `"panel-<N>": {` anchor, walk forward with string-aware brace-depth counting to the matching close, build the new panel dict in Python, `json.dumps(indent=2, ensure_ascii=False)` re-indented to the surrounding 6-space indent, splice it in as a string replacement, `json.load()` to confirm validity.
  One read-splice-write cycle per edit, never batched across edits.
- Styling uses a versioned external stylesheet (`dynamic-text-vN.css`, no mirror copy) attached via `vizConfig.spec.options.externalStyles` + `options.wrap: false`, with `t-`-prefixed CSS classes - never inline `style="..."` except `style="--t-depth: N"`.
  A stylesheet content change needs a new version file (`vN+1`), not an in-place edit - both Grafana and the plugin's loader cache aggressively.
- `query_performance.json` tagging/mirroring applies to Dynamic Text panels the same as any other panel, no exception.
- Deep panel-specific mechanics (byte-vs-UTF8 functions, truncation caps, pad-budget arithmetic, `sort_ts` ordering, schema-specific data-model facts) are reference material, not routine reading - open on demand: `Skill(dynamictext-panel-queries)`'s `references/{gotchas,data-model,width-budget,concurrent-ordering}.md`.
  Styling class/variable reference: `Skill(dynamictext-panel-design-system)`.
