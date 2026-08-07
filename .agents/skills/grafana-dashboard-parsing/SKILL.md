---
name: grafana-dashboard-parsing
description: >
  Subcommand reference for services/grafana/scripts/parse_dashboard.py, the sanctioned reader for Grafana v2beta1 dashboard JSON under services/grafana/dashboards/ and dashboards-health/.
  TRIGGER - read before running parse_dashboard.py against any dashboard file, or before trusting a list-tabs/list-panels result.
  Covers subcommand args and which dashboard files silently undercount tabs/panels via RowsLayout.
  v1.0.0
---

Grafana v2beta1 dashboard JSON has a top-level `apiVersion`/`kind`/`metadata`/`spec`.
`spec.elements` holds panels keyed by `panel-<id>`.
`spec.layout` is a `TabsLayout`/`GridLayout` tree, recursing into nested tabs.

## Subcommands

Always run `services/grafana/scripts/parse_dashboard.py` from the repo root with whatever subcommand fits the request.
Don't hand-write jq or ad hoc python for this - the script already knows the schema:

- `list-tabs <file>` - tab titles and panel counts
- `list-panels <file> [--tab TITLE]` - id, title, panel kind, per panel
- `show-panel <file> --id ID` or `--title TITLE` - full panel spec (title, description, query, panel type)
- `show-variable <file> --name NAME` - full variable spec
- `summary <file>` - tab count, panel count, variable names, datasource(s) used

If the request doesn't map cleanly onto one of these, run `summary` first to orient, then pick the narrowest subcommand that answers the question.
Don't dump the whole file.

## RowsLayout gotcha

`show-panel`, `show-variable`, and the panel count in `summary` read `spec.elements` directly - reliable on any dashboard using this schema.
`list-tabs`, `list-panels`, and the tab count in `summary` walk `spec.layout` and only handle `TabsLayout`/`GridLayout`.
A tab whose own layout is a `RowsLayout` (or nests one) is silently skipped, not reported as an error - it just doesn't appear in the output, undercounting tabs/panels with no warning.

Per-file status in this repo:

| File                                        | `RowsLayout` status                                             |
|----------------------------------------------|------------------------------------------------------------------|
| `dashboards/agents_overview.json`             | none - fully supported                                            |
| `dashboards-health/clickhouse.json`           | none - fully supported                                            |
| `dashboards-health/docker_containers.json`    | mixed - some tabs vanish from `list-tabs`/`list-panels`            |
| `dashboards-health/infra_overview.json`       | mixed - some tabs vanish from `list-tabs`/`list-panels`            |
| `dashboards-health/query_performance.json`    | every tab - `list-tabs`/`list-panels` return empty despite panels |

Don't trust an empty or suspiciously small `list-tabs`/`list-panels` result at face value on the three mixed/unsupported files above.
Cross-check against the panel count in `summary` (reads `spec.elements` directly, unaffected by layout gaps), or fall back to inline Read for the missing tabs.
