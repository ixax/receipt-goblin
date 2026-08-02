---
name: dashboard-panels
description: >
  Formatting/build conventions for panels, graphs, and widgets in this repo's Grafana dashboards.
  Two tiers: Universal conventions apply to every dashboard JSON under services/grafana/dashboards/
  and dashboards-health/ (rawSql formatting, panel-description discipline, SQL testing/perf-check,
  dataLink principles, chart color/legend policy, table-panel habits). agents_overview.json-specific
  conventions apply only to services/grafana/dashboards/agents_overview.json (column-width table,
  token/cost units, dataLink URL/template-variable patterns, tab structure) - never copy onto another
  dashboard's panels.
  TRIGGER - read BEFORE creating/editing a panel's query/options, or reviewing a panel edit, in any of
  those files.
  SKIP for non-panel dashboard edits (annotations, variables, dashboard-level settings).
  v1.6.1
---

Conventions for building or editing a panel/graph/widget in any dashboard JSON in this repo (`services/grafana/dashboards/*.json`, `services/grafana/dashboards-health/*.json`).
Two tiers: universal conventions apply to every dashboard file; agents_overview.json-specific conventions apply only to `services/grafana/dashboards/agents_overview.json` and must never be copied onto another dashboard's panels - e.g. `docker_containers.json` has no session_id/user_id/issue_id to build a width table or dataLink around, and no tokens/cost schema to derive units from.
`services/grafana/dashboards-health/query_performance.json` is `dashboards-expert`'s generated companion mirror of `agents_overview.json` (per-panel ClickHouse query cost from `system.query_log`); `tag_panel_queries.py`/`build_query_perf_dashboard.py` (`services/grafana/scripts/`) keep it in sync (see `dashboards-expert` for when to run them) - universal conventions still apply to it since it's a real table panel.
Its "Recent executions" tables link `query_id` to this dashboard's own Query Detail tab (`dtab=Query-Detail`), modeled on `agents_overview.json`'s `session_id` -> Trace-tab convention below, but deliberately sets `targetBlank: False` since a same-dashboard `dtab=` navigation always opens in the same tab regardless of that flag - don't "correct" it to match the new-tab `session_id` pattern.
Two repo-wide guardrails live in AGENTS.md's "Rules to not violate" instead of here, since they must fire even when an edit isn't recognized as panel work: reading `agents_overview.json` always goes through the `dashboard-parser` agent (other dashboard files use `RowsLayout`/nested-`TabsLayout` shapes, not the `TabsLayout` -> `GridLayout` shape `parse_dashboard.py` understands - read those with plain Read/Bash-python), and no dashboard is ever changed as a side effect of unrelated work.

## Universal conventions (any dashboard)

### SQL formatting

Write `rawSql` like a formatted query file - real newlines, tab/2-space indentation for nested clauses (CTEs, subqueries, `AND` chains), never a minified one-liner.
Every panel in every dashboard file already does this - keep new/edited queries consistent with it rather than collapsing them for a smaller diff.

### Testing a panel's SQL

Test the panel's literal `rawSql` with only `${...}` placeholders substituted - never a simplified/reconstructed rewrite.
A trimmed test query that drops a join/column that looks like template-variable plumbing can pass cleanly while the real query still fails (e.g. `AMBIGUOUS_IDENTIFIER` from a dropped join that was actually load-bearing).

### Perf-checking a rewrite

Any `rawSql` rewrite, in any dashboard, gets a before-run and after-run via `services/grafana/scripts/query_perf.py` + the `query-perf-runner` agent, driven by `sql-expert` (see AGENTS.md's "Rules to not violate" and `sql-expert`'s own workflow) - whether the caller asked for the rewrite or it happened as a side effect of other work.
Not optional, and not skippable because the change "should obviously be faster" - a diff table is a finding, a guess isn't.
A brand-new panel doesn't need a before/after but still deserves a baseline run.

### Panel descriptions

Every panel's `description` field follows a fixed two-line format:

```
Goal: <why this panel exists - the question a viewer is trying to answer>
Description: <how the query/visualization answers it - the key grouping, computation, join, or filter that isn't obvious from the column headers alone>
```

Keep both lines short but substantive - a sentence each, not a paragraph.
Goal is the viewer's intent, Description is the mechanism - don't blur the two or just restate the panel title/column names.
Hard limit: 100 words total across both lines - cut detail rather than exceed it if a rewrite doesn't fit.
Surface-level only, no special/internal knowledge: grouping, filter, join, time window a viewer already understands, never ClickHouse quirks, regex/CTE mechanics, data-model caveats, or plugin-sanitizer behavior - that belongs in a skill file or code comment.
Never a changelog: the description is the panel's current state, not a history of edits - each edit that touches it replaces it, never appends "previously X, now Y".
If the existing description already violates this (changelog-style, over-limit, implementation-heavy), rewrite it from scratch rather than editing around the violation.
Mandatory on creation, and on any edit to a panel's query or options (`rawSql`, field set, grouping/join/computation, `vizConfig` options) - leave the panel with a non-empty, accurate, in-format, in-limit description regardless of its state going in; a stale or bloated description is worse than none.

### Table panel habits

- If a panel's data is sorted (SQL `ORDER BY` or a `sortBy` transform), `vizConfig.spec.options.sortBy` must name the same field(s)/direction so Grafana renders the sort-direction arrow.
  `sortBy` is an array of `{"displayName": "<field>", "desc": true|false}`, can list more than one entry for a multi-column sort.
  If `ORDER BY` is a computed expression with no matching output column (e.g. `ORDER BY (a + b) DESC`), add it as a real aliased `SELECT` column instead (`a + b AS tokens_total`, then `ORDER BY tokens_total`).
- Long free-text columns (tool arguments, error text, excerpts, JSON payloads) get the cell-inspect eye icon instead of wrapping/truncating inline: set `custom.cellOptions: {"type": "auto"}` and `custom.inspect: true` on that field's `fieldConfig.overrides` entry.
  Don't use `custom.cellOptions: {"type": "json-view"}` for this - that's a different, older mechanism that only renders correctly for actual JSON values and has confirmed bugs hiding the eye icon on plain text; convert any remaining `json-view` fields to `auto` + `inspect: true`.
  Grafana's inspect drawer (11.3+) auto-detects valid JSON and pretty-prints it in a Code editor tab - no separate flag needed.
  Never wrap an eye-icon column's SQL in a length-limiting function (`substring(...)`, `left(...)`) - the drawer needs the full value, and cutting it at a character count usually breaks JSON auto-detection.
  These columns don't get a `custom.width` - the eye icon replaces a fixed width.
- Column order follows the `SELECT` list order (or an `Organize fields` transform): eye-icon columns always go last.
- Don't show a raw id column next to its display-name column - hide the id (`custom.hidden: true`, not `custom.hideFrom.viz`, which has no effect on tables) and put the link on the display-name column instead, referencing the id via `${__data.fields["<id field's output name>"]}` in the link URL.
- A qualified column reference with no alias (`SELECT u.user_id`) is valid and needs no `AS` - ClickHouse strips the table prefix, so the field's real output name is already `user_id`.
  Don't add a no-op alias "to make it explicit" - the actual bug this pattern causes is an override guessing the wrong field name; fix references to use the real output name, don't add an alias.
  Verify the real field name via `dashboard-parser` (for `agents_overview.json`) or a plain read (other dashboards) if unclear.
- Name every `SELECT`-list alias `<source>_<direction-or-qualifier>`, source word first - never a shortened/decorative name.
  This is global: rename any violating alias, and update every reference by string (`ORDER BY`, `sortBy[].displayName`, an override matcher, a `${__data.fields["..."]}` link) in the same edit.
  Don't alias a real column to a shortened name either (e.g. an id column `AS id`) - use the real semantic name unless the user asks for a specific different one.
  Never use `fieldConfig.overrides` `displayName` on a table column - the header is always the field's real SQL output name; if that name isn't a good enough header, rename the field, don't layer a `displayName` on top.
  This is distinct from `sortBy[].displayName`, which must equal the real field name for the sort arrow to render - that's a reference, not a label, and stays required regardless of this rule.

### Linking a table column to a dashboard filter - general principles

- Build a link's path off `${__dashboard.uid}`, never a hardcoded slug - a hardcoded `/d/<slug>/<slug>` path only works while the uid happens to match that string; renaming the dashboard silently breaks every occurrence.
- Set a link as a `dataLink` on the field (`title` + `url` + `targetBlank: true`), not a raw `<a href>` baked into `rawSql` output - that pattern is for Dynamic Text panels' freeform HTML only.
- A single dataLink can only target one unambiguous value - don't link an aggregated multi-value column (e.g. a comma-joined `groupUniqArray(...)`/`arrayStringConcat(...)` list); leave such columns unlinked.
  Check whether a column is one value or a joined list, not just its name - a singular vs. plural name is a hint, not proof.
- A visible id-like column that exists mainly to be a link can show a short mapped label instead of the raw value - see `agents_overview.json`'s `session_id` "open ↗" pattern below.

### Chart colors and legend - general policy

- Blue and white are the two primaries; colors are only ever hardcoded for a fixed, small set of series, never a dynamic one.
  One series -> `fixedColor: "blue"` (unless a dashboard-specific fixed-pair rule applies, see the tokens/cost convention below).
  Two fixed, known series -> `blue` first, `white` second.
  Dynamic/unbounded series (split by a per-category field) -> leave `fieldConfig.defaults.color.mode` as `"palette-classic"`, don't hardcode.
- Hide the legend (`options.legend.showLegend: false`) when a panel only ever plots one value field - a single-entry legend is redundant.
  Keep it shown once a panel can have more than one series (a fixed pair, or a dynamic split).

## agents_overview.json-specific conventions

Everything below applies only to `services/grafana/dashboards/agents_overview.json` (and its generated mirror, `query_performance.json`) - built around this dashboard's schema (session_id/user_id/issue_id, tokens/cost), template variables (`var-session_id`, `var-user_id`, `var-issue_id`, etc.), and tab structure (`Sessions & Debugging` -> `Trace`).
Don't reach for a width/unit/link pattern here when editing `docker_containers.json`, `clickhouse.json`, or `infra_overview.json`.

### Units

- Token columns get `"unit": "locale"` (comma-grouped, e.g. `1,234,567`); cost/dollar columns get `"unit": "currencyUSD"` (renders with a `$` prefix, never hand-formatted into the SQL string).
  Set per-field under `fieldConfig.defaults.unit` (or a `fieldConfig.overrides` entry if the panel has other columns) - an established convention across ~20 panels each, don't invent a third way.
- A table never shows a bare combined `tokens` column - always split into `tokens_input` and `tokens_output`, plus `tokens_total` if a total is genuinely useful (e.g. as the sort key), never instead of the split pair.
  This is a table-panel rule only - it doesn't force a chart/timeseries panel to split a `tokens` series (see the chart-color rule below, where `tokens` as one series is intentional).
  Name the three consistently: `tokens_input`, `tokens_output`, `tokens_total` - not `tokens_in`/`tokens_out`/`total_tokens`.

### Column widths

`custom.width` follows the column's semantic type, not per-panel guessing:

- `session_id` -> 130 (same as user display-name)
- user display-name (`user_name` etc.) -> 130
- token count -> 150
- cost/dollar -> 100
- duration/elapsed-time (any field measuring a time span - `duration_s`, `latency_ms`, `ttft_ms`, `session_duration_period_s`, etc., matched by meaning not name) -> 130
- timestamp -> 170
- issue -> 130
- `tool_name` in any form (`tool_name`, `mcp_tool_name`, ...), agent name / skill name / command name -> 200
- long free-text/JSON columns -> no width, eye icon instead (universal rule above)
- everything else -> leave `custom.width` unset, Grafana auto-sizes

### Column order and hide/show specifics

- Applying the universal hide-id/show-display-name rule to users specifically: the visible column is always `user_name`, never `user_id` - if a panel shows `user_id` instead, hide it and show `user_name` with the link on it.
- When a table shows both `session_id` and `user_name`, `session_id` must come before `user_name` - a relative constraint between just these two fields, don't reorder any other column to satisfy it.

### This dashboard's exact dataLink URL patterns

- Filter to a user, from a column whose own value is the user's id:
  ```
  /d/${__dashboard.uid}?${__url_time_range}&var-user_id=${__value.text}
  ```
- Filter to a user, from a different column (e.g. `user_name`) - reference the id field by its actual output name:
  ```
  /d/${__dashboard.uid}?${__url_time_range}&var-user_id=${__data.fields["<user_id column's output field name>"]}
  ```
- Jump to a session's Trace tab, from a `session_id` column:
  ```
  /d/${__dashboard.uid}?${__url_time_range}&var-session_id=${__value.text}&dtab=Sessions-%26-Debugging&Sessions-%26-Debugging-dtab=Trace
  ```
  Don't copy this blindly if the tab/sub-tab gets renamed - derive it fresh: `grep -o '"title": *"[^"]*"' agents_overview.json` for the top-level tab's real title (currently `"Sessions & Debugging"`) and the target sub-tab's title (currently `"Trace"`), encode each (spaces -> `-`, `&` -> `%26`), then set `dtab=<encoded top-tab title>` and `<encoded top-tab title>-dtab=<encoded sub-tab title>`.
- Filter to an issue, from any `issue_id`-like column - same mechanics, no value mapping (cell keeps showing the real text):
  ```
  /d/${__dashboard.uid}?${__url_time_range}&var-issue_id=${__value.text}
  ```
  `title: "Filter to this issue"`, `targetBlank: true`.
  Apply to every panel with an issue column.
- Filter to a repo, from a `git_repo`-like column, same no-mapping mechanics:
  ```
  /d/${__dashboard.uid}?${__url_time_range}&var-git_repo=${__value.text}
  ```
- Filter to a branch, from a `git_branch`-like column - must set both `var-git_repo` and `var-git_branch` together, never branch alone (branch names like `main` aren't unique across repos).
  Select `git_repo` as a real (optionally hidden via `custom.hidden: true`) column on the row so the link can reference it:
  ```
  /d/${__dashboard.uid}?${__url_time_range}&var-git_repo=${__data.fields["git_repo"]}&var-git_branch=${__value.text}
  ```
- Filter to an agent / skill / command / model, same no-mapping mechanics, one variable each:
  ```
  /d/${__dashboard.uid}?${__url_time_range}&var-agent_name=${__value.text}
  /d/${__dashboard.uid}?${__url_time_range}&var-skill_name=${__value.text}
  /d/${__dashboard.uid}?${__url_time_range}&var-command_name=${__value.text}
  /d/${__dashboard.uid}?${__url_time_range}&var-model=${__value.text}
  ```
  A plural `models` column holding a joined list for one row (e.g. a session using more than one model) doesn't get this link, per the aggregated-multi-value rule above - check whether the column is one value or a joined list, not just its name.
- A visible `session_id` column shows a short link label, never the raw UUID: add a `fieldConfig.overrides` `mappings` entry, `"regex"` pattern `".*"`, `result: {"text": "open ↗"}`, so every row shows `"open ↗"` instead of the UUID.
  Once a mapping is in play, the link's `url` must use `${__value.raw}` instead of `${__value.text}` (`.text` now resolves to the mapped label).
  The link's `title` stays the plain static string (`"Filter to this session"`).
  Roll this mapping + `__value.raw` pattern out to every panel with a visible `session_id` column.

### Chart-color specifics

- `tokens` is always `blue` and `cost` is always `white` when a panel's only possible fields are `tokens` and/or `cost` ("Spend by user", "Spend by branch", "Tokens by branch", "Session cost distribution") - overrides the general single-series-blue rule for this pair.
- Dynamic/unbounded-series panels ("Tokens by user per week", "Cost by scope per week", etc.) still follow the general `palette-classic` rule, no override.
