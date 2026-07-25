---
name: dashboard-panels
description: >
  Formatting/build conventions for panels, graphs, and widgets in this repo's Grafana dashboard
  (services/grafana/dashboards/agents_overview.json).
  TRIGGER - read this BEFORE creating a new panel, editing an existing panel's query/options, or
  reviewing a panel edit in this dashboard (or any other agent-tracking Grafana dashboard this repo
  owns) - table sort indicators, chart colors/legend, rawSql formatting, token/cost units, user/session
  links, and long-text cell display all follow the conventions below.
  SKIP for panel-76 "Trace" and its companion panel-77 (those go through the dynamictext-panel-builder
  agent instead - see AGENTS.md), and for any change to this dashboard that isn't actually about a
  panel's own content (annotations, variables, dashboard-level settings - those are plain edits, no
  skill needed).
---

# dashboard-panels

Conventions for building or editing a panel/graph/widget in
`services/grafana/dashboards/agents_overview.json`. Two repo-wide guardrails
still live in `AGENTS.md`'s "Rules to not violate" (not here, since they must
fire even when an edit isn't recognized as "panel work"): reading the
dashboard always goes through the `dashboard-parser` agent, and this
dashboard is never changed as a side effect of unrelated work.

## Table panels

- **If a panel's data is sorted** (via SQL `ORDER BY` or a `sortBy`
  transform), the table's `vizConfig.spec.options.sortBy` must name the same
  field(s)/direction, so Grafana renders the sort-direction arrow in the
  column header. `sortBy` is an array of `{"displayName": "<field>", "desc":
  true|false}` and can list more than one entry for a multi-column sort. If
  the `ORDER BY` is a computed expression with no matching output column
  (e.g. `ORDER BY (a + b) DESC`), add that expression as a real aliased
  column in the `SELECT` (see `total_tokens` in "Top 10 users by tokens")
  rather than leaving the sort unrepresentable.
- **Token columns get `"unit": "locale"`** (comma-grouped, e.g. `1,234,567`)
  - never leave a raw token-count column unitless. **Cost/dollar columns get
  `"unit": "currencyUSD"`** (renders with a `$` prefix) - never hand-format a
  `$` into the SQL string itself. Both are set per-field under
  `fieldConfig.defaults.unit` (or a `fieldConfig.overrides` entry targeting
  that one field by name if the panel has other non-money/non-token
  columns). These are established conventions already used across ~20
  panels each - match them, don't invent a third way.
- **Long free-text columns (tool arguments, error text, excerpts) get a
  "view" cell** so the table row stays compact and the full value opens on
  click, instead of wrapping/truncating inline: set
  `fieldConfig.overrides` for that field's
  `custom.cellOptions` to `{"type": "json-view"}` (Grafana's built-in
  "inspect value" cell - shows an eye icon, opens the full text in a
  modal). This is a new pattern as of writing (no prior panel in this
  dashboard needed it) - if a future Grafana version renames or replaces
  this cell type, update this note.
- **Don't show a raw id column next to its own display-name column.** When a
  table has both a `user_id`/`session_id`-shaped id and a human-readable
  name for the same entity, hide the id column
  (`custom.hideFrom.viz: true` on that field's override) and put the link
  (see below) on the display-name column instead, referencing the id via
  `${__data.fields["<id column's SQL alias>"]}` in the link URL - don't
  drop the id from the query, just don't render it as its own column.

## Linking to a user or session

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
  its SQL alias:
  ```
  /d/${__dashboard.uid}?${__url_time_range}&var-user_id=${__data.fields["<user_id column alias>"]}
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

Set these as a `dataLink` on the field (`title` + `url` +
`targetBlank: false`), not a raw `<a href>` baked into `rawSql` output - that
pattern is for panel-76/77's freeform HTML tree only (dynamictext-panel-builder's
territory), not for ordinary table/stat panels.

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
