---
name: query-performance-sync
description: >
  TRIGGER - read/apply whenever any panel in services/grafana/dashboards/agents_overview.json is created, edited, or removed - regardless of whether the calling task mentions it.
  Keeps services/grafana/dashboards-health/query_performance.json (its query-performance mirror) from drifting, via the tag/extract/render scripts under services/grafana/scripts/.
  SKIP for any other dashboard file, and for a Dynamic Text panel's query-content-only edit (same id, same tab).
  <version>1.0.1</version>
---

Applies only to `services/grafana/dashboards/agents_overview.json` - no other dashboard has a `query_performance.json`-style mirror.
`services/grafana/dashboards-health/query_performance.json` mirrors tagged panels with 4 panels each (duration, memory, read rows/bytes, recent executions), sourced from `system.query_log` filtered by a `log_comment` tag.
Run this as part of finishing the same task that touched `agents_overview.json`, not as a separate follow-up.

## Sync scripts

Three scripts under `services/grafana/scripts/`, run in this order - tag, extract, render:

- `tag_panel_queries.py <agents_overview.json> --id <panel_id> [--id <id2> ...]` - appends/refreshes `SETTINGS log_comment = 'agents_overview:panel_<id>'` on that panel's `rawSql`.
  Idempotent, surgical raw-text replacement, safe to re-run.
  Always runs first - it only tags, never reads tagged state.
- `extract_panel_tree.py --source <agents_overview.json> --out <tree path>` - walks every top-level tab (and nested sub-tabs), writes a tree JSON of every panel's id/title/tagged state.
  Default `--out` is `services/grafana/scripts/panel_tree.json` (gitignored, a regenerated artifact).
  Always re-run fresh before rendering - never reuse a leftover tree from a previous run.
- `build_query_perf_dashboard.py --tree <tree path> --out <query_performance.json path> [--tab "<top-level tab title>"]` - renders from that tree, never reads `agents_overview.json` directly.
  `--tab` omitted regenerates every tab in one call.
  `--tab "<title>"` scopes the regeneration to just that top-level tab (sub-tabs included), leaving every other tab untouched.
  Warns and skips any panel the tree marked untagged.

## Per-edit sync procedure

After every `agents_overview.json` panel change other than a Dynamic Text panel's own content, tag, extract a fresh tree, then render scoped to the affected top-level tab(s):

- New panel - tag (`--id <new_id>`), extract, render `--tab "<tab>"`.
- Edited panel, id changed - re-tag with the new id, extract, render `--tab "<tab>"`.
  A query-content-only edit (same id, same tab) needs none of this - the mirror only reads the id, not the source query.
- Edited panel, moved to a different top-level tab - one tag/extract pass, then render both origin and destination tabs (two `--tab` calls against the same fresh tree).
  Only the tab passed to `--tab` gets its element list rebuilt - the other keeps a stale reference until regenerated too.
  A move between sub-tabs of the same top-level tab needs only one render call.
- Removed panel - extract, then render its former top-level tab; the generator prunes orphaned mirror panels automatically once re-run against a tree reflecting the removal.

The two files' panel ids must never drift apart - a `query_performance.json` panel filtering on a `log_comment` tag whose source panel no longer exists, or a new source panel with no mirrored counterpart, is the failure mode this procedure prevents.
Finishing a panel edit without running the matching step(s) is exactly how that drift happens.

## Full-dashboard rebuild vs. single-panel sync

"Rebuild/regenerate `query_performance.json`" (or "the whole dashboard"/"the health dashboard") with no specific panel named means every top-level tab, not just the most recently edited one.
Never hand-write ad-hoc Python to discover untagged panels or validate rendered output - use the sanctioned scripts, in this order:

1. Run `extract_panel_tree.py` fresh, before any tagging - its tree JSON already records each panel's `"tagged": true/false`.
2. For every id marked `"tagged": false` (excluding any Dynamic Text panel - check `type` via the tree/`dashboard-parser`, don't assume from an id list; stays untagged on purpose, see the open question in `Skill(dynamictext-panel-queries)`), run `tag_panel_queries.py <agents_overview.json> --id <id> [--id <id2> ...]`.
3. Re-run `extract_panel_tree.py` again - the step-1 tree is now stale re: tagged state.
   Still required even if step 1 found nothing untagged (fresh tree per rebuild).
4. Run `build_query_perf_dashboard.py --tree <path> --out <path>` once, no `--tab` - covers every tab in one call.
   Confirm stderr has no `warning: skipped untagged panels` line - any panel named there means step 2/3 missed something.
5. Validate with `python3 -m json.tool <out> > /dev/null` (exits non-zero on invalid JSON) - the sanctioned check, don't write a custom `json.load` script.

Distinct from the per-edit sync above, which stays scoped to the affected tab(s) via `--tab` - don't widen a single-panel edit into a full rebuild, and don't narrow an explicit full-rebuild ask down to one `--tab` just because only one tab changed recently.
