---
name: dashboard-panels
description: >
  Formatting/build conventions for panels, graphs, and widgets in this repo's Grafana dashboard
  (services/grafana/dashboards/agents_overview.json - the "agents" dashboard, ONLY this file).
  TRIGGER - read this BEFORE creating a new panel, editing an existing panel's query/options, or
  reviewing a panel edit in this dashboard - table sort indicators, chart colors/legend, rawSql
  formatting, field-naming, column widths, token/cost units, user/session links, and long-text cell
  display all follow the conventions below.
  SKIP for panel-76 "Trace" and its companion panel-77 (those go through the dynamictext-panel-builder
  agent instead - see AGENTS.md), for any change to this dashboard that isn't actually about a panel's
  own content (annotations, variables, dashboard-level settings - those are plain edits, no skill
  needed), and for anything under `services/grafana/dashboards-health/` (clickhouse.json,
  docker_containers.json, infra_overview.json) - those are infra/health dashboards with their own,
  unrelated conventions; nothing in this skill applies to them.
---

# dashboard-panels

Conventions for building or editing a panel/graph/widget in `services/grafana/dashboards/agents_overview.json` - and only that file.
The sibling dashboards under `services/grafana/dashboards-health/` (ClickHouse, Docker containers, infra overview) are a separate, unrelated set with their own conventions; none of this skill's rules (naming, widths, links, colors) carry over to them.
Two repo-wide guardrails still live in `AGENTS.md`'s "Rules to not violate" (not here, since they must fire even when an edit isn't recognized as "panel work"): reading the dashboard always goes through the `dashboard-parser` agent, and this dashboard is never changed as a side effect of unrelated work.

## Table panels

- **If a panel's data is sorted** (via SQL `ORDER BY` or a `sortBy`
  transform), the table's `vizConfig.spec.options.sortBy` must name the same
  field(s)/direction, so Grafana renders the sort-direction arrow in the
  column header. `sortBy` is an array of `{"displayName": "<field>", "desc":
  true|false}` and can list more than one entry for a multi-column sort. If
  the `ORDER BY` is a computed expression with no matching output column
  (e.g. `ORDER BY (a + b) DESC`), add that expression as a real aliased
  column in the `SELECT` instead (e.g. `SELECT ..., a + b AS tokens_total`,
  then `ORDER BY tokens_total`) rather than leaving the sort unrepresentable.
- **Token columns get `"unit": "locale"`** (comma-grouped, e.g. `1,234,567`)
  - never leave a raw token-count column unitless. **Cost/dollar columns get
  `"unit": "currencyUSD"`** (renders with a `$` prefix) - never hand-format a
  `$` into the SQL string itself. Both are set per-field under
  `fieldConfig.defaults.unit` (or a `fieldConfig.overrides` entry targeting
  that one field by name if the panel has other non-money/non-token
  columns). These are established conventions already used across ~20
  panels each - match them, don't invent a third way.
- **Long free-text columns (tool arguments, error text, excerpts, JSON payloads) get the "Cell value inspect" eye icon** so the table row stays compact and the full value opens in a drawer on click, instead of wrapping/truncating inline: set two properties on that field's `fieldConfig.overrides` entry - `custom.cellOptions: {"type": "auto"}` and `custom.inspect: true`.
  Do **not** use `custom.cellOptions: {"type": "json-view"}` for this - that's a different, older mechanism (Grafana's "JSON View" cell display mode) that only renders correctly for actual JSON values; on plain text (tool arguments, prompt text, error strings - none of which are JSON) it has long-standing, confirmed bugs where the eye icon never appears.
  `custom.inspect: true` is the dedicated, value-type-agnostic toggle Grafana added for exactly this ("Improved cell inspect in tables", 2024-08-22) and is what actually works - if any field in this file still has `custom.cellOptions: {"type": "json-view"}`, convert it to `{"type": "auto"}` + `custom.inspect: true` rather than leaving old and new patterns mixed.
  Grafana's inspect drawer (shipped since 11.3, so covered by this dashboard's 13.1.0) auto-detects whether a cell's raw text is valid JSON and shows it pretty-printed in a "Code editor" tab if so - no separate flag needed beyond `custom.inspect: true` itself.
  **Never wrap an eye-icon column's SQL in a length-limiting function (`substring(...)`, `left(...)`, etc.)** - the whole point of the eye icon is that the full value opens in the drawer on click, so the inline cell no longer needs truncating, and cutting a JSON payload at an arbitrary character count almost always produces invalid JSON, which breaks the auto-detection above and falls back to plain text. Select the full underlying column instead.
- **Column widths (`custom.width`) follow the column's semantic type, not
  ad-hoc per-panel guessing:**
  - `session_id` -> 130 (same width as user display-name, next bullet)
  - user display-name (`user_name` etc.) -> 130
  - token count -> 150
  - cost/dollar -> 100
  - duration/elapsed-time columns -> 130. This bucket is matched by
    **meaning, not by a fixed field name** - anything measuring an elapsed
    time span (a duration, a latency, a time-to-first-token, a wall-clock
    length) gets 130 regardless of what it's called or what unit it's in
    (`duration_s`, `latency_ms`, `ttft_ms`, `session_duration_period_s`,
    a future `response_time_min` - all the same bucket). Don't skip this
    width just because the field's name doesn't literally contain the word
    "duration".
  - timestamp -> 170
  - issue -> 130
  - `tool_name` in any form (`tool_name`, `mcp_tool_name`, etc.), and
    likewise agent name / skill name / command name columns -> 200
  - long free-text/JSON columns don't get a width at all - they get the
    eye icon instead (see the rule above)
  - everything else (generic numeric/other columns): don't set
    `custom.width` - leave it unset so Grafana auto-sizes the column
- **Don't show a raw id column next to its own display-name column.** When a
  table has both a `user_id`/`session_id`-shaped id and a human-readable
  name for the same entity, hide the id column
  (`custom.hidden: true` on that field's override - not `custom.hideFrom.viz`,
  which doesn't exist anywhere in this file and has no effect on this table
  panel) and put the link (see below) on the display-name column instead,
  referencing the id via `${__data.fields["<id column's output field name>"]}`
  in the link URL - don't drop the id from the query, just don't render it as
  its own column.
  For a user specifically, this means the visible column is always
  `user_name`, never `user_id` - if a panel currently shows `user_id`
  instead of `user_name` (even with a correct filter link on it), that's
  this rule not being applied, not an acceptable variant: hide `user_id`
  and show `user_name` with the link on it instead.
- **Column order: long free-text/JSON columns (the ones getting the eye
  icon) always go last** in a table's column order, after every other
  column. Column order in this v2beta1/table setup follows the `SELECT`
  list's order (or an explicit `Organize fields` transform if one exists) -
  put the eye-icon column(s) at the end of the `SELECT` list rather than in
  the middle.
- **Relative column order: when a table shows both `session_id` and
  `user_name`, `session_id` must come before `user_name`** - this is a
  relative constraint between just these two fields, not a rule about where
  the pair sits in the table overall. Don't reorder or move any other
  column to satisfy it - if some other field already sits before
  `session_id`, leave it there; the only thing that must hold is
  `session_id` appears to the left of `user_name` whenever both exist.
- **A qualified column reference with no alias (`SELECT u.user_id`) is perfectly valid and needs no `AS` added - ClickHouse strips the table prefix, so the field's real output name is already the plain `user_id`.**
  Don't force a no-op `AS user_id` just to "make it explicit"; the bug this used to cause wasn't the missing alias, it was overrides guessing the wrong name (an override written for `u.user_id`, the qualified text, instead of `user_id`, the real output field, is dead and never matches).
  The fix is to use the field's real output name everywhere (override matcher, `${__data.fields[...]}` reference) - not to add an alias.
  Verify the real field name via the `dashboard-parser` agent rather than guessing if it's ever unclear.
- **Field naming: name every SELECT-list alias `<source>_<direction-or-qualifier>`, source word first - never invent a shortened/decorative name, and never leave inconsistent orderings/abbreviations in place.**
  E.g. the three token metrics should be named `tokens_input`, `tokens_output`, `tokens_total` - not an inconsistently-ordered/abbreviated form like `tokens_in`/`tokens_out`/`total_tokens`.
  This is a global rule: existing aliases that violate it get renamed to match, and every place that references the old name by string (`ORDER BY`, `vizConfig.spec.options.sortBy[].displayName`, a `fieldConfig.overrides` matcher, a `${__data.fields["..."]}` link reference) needs updating in the same edit or the reference silently stops matching.
  Also don't alias a real column to a shortened/invented display name (e.g. aliasing a user-id column to plain `AS user`, or a session-id column to plain `AS session`) - alias to the real semantic name (`user_id`, `session_id`) unless the user explicitly asks for a specific different name.
  A bare qualified reference needing no alias (previous bullet) is not "inventing a name" - it's just not renaming at all, which is fine since its natural output name is already correct.
  **Don't use `fieldConfig.overrides` `displayName` on a table column at all** (e.g. `displayName: "Tokens in"` on a `tokens_input` field) - the column header is always the field's real SQL output name, shown as-is, never a separate cosmetic label layered on top.
  If the real field name isn't good enough as a header, that's a signal to rename the field itself (per this rule), not to add a `displayName`.
  (This is distinct from `vizConfig.spec.options.sortBy[].displayName` mentioned above, which must literally equal the real field name for the sort arrow to render - that one isn't a label, it's a reference, and every panel needs it filled in correctly regardless of this no-`displayName`-override rule.)
- **A table never shows a bare combined `tokens` column - always show `tokens_input` and `tokens_output` as separate columns instead.**
  A single merged token count hides which direction (prompt vs. completion) actually drove the number, and this dashboard already has the standard split formula established (see "Top 10 users by tokens"/"Issue overview") - there's no reason for a table to fall back to the ephemeral combined form.
  If a total is also genuinely useful (e.g. as the sort key), add `tokens_total` alongside the two split columns, not instead of them.
  This rule is about **table panels** specifically - it doesn't force a chart/timeseries panel to split a `tokens` series into two, since those follow the separate chart-color convention below (`tokens` as one series is an intentional, established pattern there, not the same "ephemeral column" problem a table has).

## Linking a table column to a dashboard filter

Every dashboard link uses `${__dashboard.uid}` for the path, never a
hardcoded slug (`/d/agents-overview/agents-overview` was a real, silently
fragile bug fixed 2026-07-26 - it only worked because the uid happened to
equal that literal string; renaming the dashboard would have broken all 30
occurrences with no error). Always build links off `${__dashboard.uid}`, not
a copy-pasted path.

- **Filter to a user**, from a column whose own value is that user's id:
  ```
  /d/${__dashboard.uid}?${__url_time_range}&var-user_id=${__value.text}
  ```
- **Filter to a user**, from a different column (e.g. a `user_name` display
  column, per the hide-the-id-column rule above) - reference the id field by
  its actual output field name (the explicit `AS` alias you gave it in the
  `SELECT` - see the note above about why an unaliased qualified reference
  like `u.user_id` is NOT that name):
  ```
  /d/${__dashboard.uid}?${__url_time_range}&var-user_id=${__data.fields["<user_id column's output field name>"]}
  ```
- **Jump to a session's Trace tab** (used from a `session_id` column):
  ```
  /d/${__dashboard.uid}?${__url_time_range}&var-session_id=${__value.text}&dtab=Sessions-%26-Debugging&Sessions-%26-Debugging-dtab=Trace
  ```
  Don't copy that string blindly if the tab/sub-tab ever gets renamed -
  derive it fresh: find the top-level tab's real title (`grep -o
  '"title": *"[^"]*"' agents_overview.json` and look for the tab, currently
  `"Sessions & Debugging"`) and the target sub-tab's title (currently
  `"Trace"`), then encode each for the URL - spaces become `-`, and any
  character that isn't URL-safe in a query value (an `&` in the title
  itself, for instance) becomes its percent-encoded form (`&` -> `%26`) -
  and set both params to that same encoded slug: `dtab=<encoded top-tab
  title>` and `<encoded top-tab title>-dtab=<encoded sub-tab title>`.

- **Filter to an issue**, from any column displaying an issue (`issue_id` or
  similarly-named): every such column is a link, using the same mechanics as
  the user/session links above but **without** a value mapping - the cell
  keeps showing the actual issue text as-is, it just becomes clickable.
  ```
  /d/${__dashboard.uid}?${__url_time_range}&var-issue_id=${__value.text}
  ```
  `title: "Filter to this issue"`, `targetBlank: true` (same as
  user/session). Apply this to every table panel that has an issue/issue_id
  column, not just new ones.

- **Filter to a repo**, from a column displaying a git repo (`git_repo` or similarly-named) - same no-mapping, show-the-real-text mechanics as the issue rule above, and the variable is the repo alone, nothing else:
  ```
  /d/${__dashboard.uid}?${__url_time_range}&var-git_repo=${__value.text}
  ```
- **Filter to a branch**, from a column displaying a git branch (`git_branch` or similarly-named) - **must set both `var-git_repo` and `var-git_branch` together**, never `git_branch` alone.
  Branch names aren't unique across repos (e.g. `main` exists in every repo), so a branch-only filter would silently match the wrong repo's branch.
  This is a table-row concern, not a dashboard-variable concern - the top-level `$git_repo`/`$git_branch` dropdowns are independently selectable filters for the whole dashboard, that's unrelated and unaffected.
  What this rule is about: the query producing the table row already resolves both `git_repo` and `git_branch` for that row (both come from the same `session_git_branch_dict` lookup by `session_id`) - select `git_repo` as a real column on the row (hidden via `custom.hidden: true` if it's not meant to be its own visible column) so the link can reference it:
  ```
  /d/${__dashboard.uid}?${__url_time_range}&var-git_repo=${__data.fields["git_repo"]}&var-git_branch=${__value.text}
  ```
- **Filter to an agent / skill / command / model**, from a column displaying one (`agent_name`, `skill_name`, `command_name`, `model`) - same no-mapping mechanics, one variable each, no repo-style pairing needed (these aren't ambiguous across another dimension the way branch is):
  ```
  /d/${__dashboard.uid}?${__url_time_range}&var-agent_name=${__value.text}
  /d/${__dashboard.uid}?${__url_time_range}&var-skill_name=${__value.text}
  /d/${__dashboard.uid}?${__url_time_range}&var-command_name=${__value.text}
  /d/${__dashboard.uid}?${__url_time_range}&var-model=${__value.text}
  ```
- **None of the repo/branch/agent/skill/command/model/issue link rules above apply to an aggregated multi-value column** - e.g. a `models` column built with `arrayStringConcat(groupUniqArray(...), ', ')` joining several values into one comma-separated cell.
  A single dataLink can only target one unambiguous value; a joined list has no single value to filter on, so leave such columns unlinked rather than building a link that would silently point at the whole joined string.
  Note the naming collision this creates: a single-value `model` column (one model per row, e.g. "Top 10 models") gets the link above; a plural `models` column holding a joined list of several models for one row (e.g. a session that used more than one model) does not, even though the names look alike - check whether the column is one value or a joined list, not just its name.

Set these as a `dataLink` on the field (`title` + `url` + `targetBlank: true`
- these links open in a new tab rather than navigating away from the current
dashboard view), not a raw `<a href>` baked into `rawSql` output - that
pattern is for panel-76/77's freeform HTML tree only
(dynamictext-panel-builder's territory), not for ordinary table/stat panels.

- **A visible `session_id` column shows a short link label, never the raw UUID as text** - the raw id doesn't fit the column, isn't readable, and is awkward to copy, so displaying it as text carries no value once it's clickable anyway.
  Add a `fieldConfig.overrides` `mappings` property on the `session_id` field: a `"regex"` mapping with `pattern: ".*"` (matches every value) and `result: {"text": "open ↗"}`, so every row shows the literal label `"open ↗"` instead of the UUID.
  Once a mapping is in play, the link's `url` must use `${__value.raw}` instead of `${__value.text}` - `.text` now resolves to the mapped label (`"open ↗"`), not the real session_id, and the URL needs the real id.
  The link's `title` stays the plain static string (`"Filter to this session"`) - don't try to interpolate the real id into the title.
  Confirmed working live (2026-07-26) on "Top 10 slowest tool calls" - roll this same mapping+`__value.raw` pattern out to every panel with a visible `session_id` column.

## Chart colors and legend

- **Blue and white are the two primaries, and colors are only ever
  hardcoded for a fixed, small set of series - never for a dynamic one.**
  - A panel with exactly one series (and it isn't the `tokens`/`cost` case
    below) gets `fixedColor: "blue"`.
  - A panel with exactly two fixed, known series gets `blue` for the first
    and `white` for the second.
  - `tokens` is always `blue` and `cost` is always `white` when a panel's
    only possible fields are `tokens` and/or `cost` (see "Spend by user",
    "Spend by branch", "Tokens by branch", "Session cost distribution").
  - If the number of series is dynamic/unbounded (split by `user_id`,
    `git_branch`, or another per-category field - "Tokens by user per
    week", "Cost by scope per week", etc.), don't hardcode a color - leave
    `fieldConfig.defaults.color.mode` as `"palette-classic"` so Grafana
    auto-assigns per series.
- **Hide the legend** (`options.legend.showLegend: false`) when a panel
  only ever plots one value field (e.g. "Sessions by branch" plots only
  `sessions`) - a legend with a single fixed entry is redundant. Keep the
  legend shown once a panel can have more than one series (a fixed pair
  like `tokens`/`cost`, or a dynamic per-category split).

## SQL formatting

Write `rawSql` the same way you'd write a formatted query file: real
newlines (not one long line), tab/2-space indentation for nested clauses
(CTEs, subqueries, `AND` chains) - not a minified one-liner. This is what
every panel in this file already does; keep new/edited queries consistent
with that rather than collapsing them for a smaller diff.

When validating a fix against ClickHouse, test the panel's literal `rawSql`
with only `${...}` placeholders substituted - never a simplified/
reconstructed rewrite. A trimmed test query that drops a join/column that
looks like template-variable plumbing can pass cleanly while the real query
still fails (e.g. `AMBIGUOUS_IDENTIFIER` from a dropped join that was
actually load-bearing).

## Panel descriptions

Every panel's `description` field follows a fixed two-line format:

```
Goal: <why this panel exists - the question a viewer is trying to answer>
Description: <how the query/visualization answers it - the key grouping, computation, join, or filter that isn't obvious from the column headers alone>
```

Keep both lines short but substantive - a sentence each, not a paragraph.
"Goal" is about the viewer's intent (what decision or question this panel serves), "Description" is about the mechanism (what the query actually does to produce that answer) - don't blur the two into one line, and don't just restate the panel title or column names in different words.

**Mandatory on creation**: every new panel gets this description filled in at creation time, not left blank or added later as an afterthought.

**Mandatory on any change to the panel's field set or query logic**: if you add/remove/rename a column, change what's being grouped or joined, or otherwise alter what the query computes, update the description in the same edit so it keeps matching reality.
A stale description is worse than none - this was a real bug (found and fixed 2026-07-26): "Top repeating tool errors"' description said "the first 80 characters of failed_tool_error" after the underlying truncation had already been removed, because the query changed without the description being revisited.
