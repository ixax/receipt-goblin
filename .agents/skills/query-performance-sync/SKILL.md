---
name: query-performance-sync
description: >
  TRIGGER - read/apply whenever any panel in services/grafana/dashboards/agents_overview.json is created, edited, or removed - regardless of whether the calling task mentions it.
  Keeps services/grafana/dashboards-health/query_performance.json (its query-performance mirror) from drifting, via the tag/extract/render scripts under services/grafana/scripts/.
  SKIP for any other dashboard file, and for a Dynamic Text panel's query-content-only edit (same id, same tab).
  <version>1.0.0</version>
---

Everything here is specific to `services/grafana/dashboards/agents_overview.json` - no other dashboard file has a `query_performance.json`-style mirror.
`services/grafana/dashboards-health/query_performance.json` mirrors tagged panels of `agents_overview.json` with 4 panels each (duration, memory, read rows/bytes, recent executions), sourced from `system.query_log` filtered by a `log_comment` tag.
This runs as part of finishing the same task that touched `agents_overview.json`, not as a separate follow-up someone has to remember to ask for.

## Sync scripts

Three scripts under `services/grafana/scripts/` drive it, in this order - tag, extract, render:

- `tag_panel_queries.py <agents_overview.json> --id <panel_id> [--id <id2> ...]` appends/refreshes `SETTINGS log_comment = 'agents_overview:panel_<id>'` on that panel's `rawSql`.
  Idempotent, surgical raw-text replacement - safe to re-run.
  Always runs first - it only tags, never reads tagged state, and the next step's tree snapshots whatever tagged state exists at the moment it runs.
- `extract_panel_tree.py --source <agents_overview.json> --out <tree path>` walks every top-level tab (and nested sub-tabs) of `agents_overview.json` and writes a tree JSON of every panel's id/title/tagged state.
  Default `--out` is `services/grafana/scripts/panel_tree.json`, which is gitignored - a regenerated intermediate artifact, not source of truth.
  Always re-run this fresh before rendering.
  Never reuse a leftover tree from a previous run, since a stale tree silently renders stale id/title/tagged data.
- `build_query_perf_dashboard.py --tree <tree path> --out <query_performance.json path> [--tab "<top-level tab title>"]` renders from that tree - never reads `agents_overview.json` directly.
  `--tab` omitted regenerates every tab in the tree in one call and writes the whole file.
  `--tab "<title>"` scopes the regeneration to just that one top-level tab (sub-tabs included), leaving every other tab in `--out` untouched, same merge/prune behavior as before.
  Warns and skips any panel the tree marked untagged.

## Per-edit sync procedure

After every `agents_overview.json` panel change other than a Dynamic Text panel's own content, tag first, then extract a fresh tree, then render with `--tab` scoped to the affected top-level tab(s), before considering the task done:

- New panel - tag it (`tag_panel_queries.py ... --id <new_id>`), extract, then render `--tab "<tab>"`.
- Edited panel, id changed - re-tag with the new id (`tag_panel_queries.py ... --id <new_id>`), extract, then render `--tab "<tab>"`.
  A query-content-only edit (same id, same tab) needs none of this - the mirror doesn't read the source query, only the id.
- Edited panel, moved to a different top-level tab - one tag/extract pass, then render both the origin and destination top-level tabs (two `--tab` calls against the same fresh tree).
  Only the tab passed to `--tab` gets its element list rebuilt.
  The other one keeps a stale reference to the panel until it's regenerated too.
  A move between sub-tabs of the same top-level tab needs only one render call, since that rebuilds the whole nested layout.
- Removed panel - extract, then render its former top-level tab.
  The generator prunes the now-orphaned mirror panels automatically, it just has to actually be re-run against a tree that reflects the removal.

The two files' panel ids must never drift apart - a `query_performance.json` panel filtering on a `log_comment` tag whose source panel no longer exists, or a new source panel with no mirrored counterpart at all, is the failure mode all of the above exists to prevent.
If a panel edit finishes without running the matching step(s), that drift is the direct result.

## Full-dashboard rebuild vs. single-panel sync

"Rebuild/regenerate `query_performance.json`" (or "the whole dashboard", "the health dashboard") with no specific panel named means every top-level tab, not just whichever tab was most recently edited or piloted.
Never hand-write ad-hoc Python to discover which panels are untagged or to validate the rendered output - both are already covered by the sanctioned scripts, in this order:

1. Run `extract_panel_tree.py` first, fresh, before any tagging.
   Its tree JSON already records each panel's `"tagged": true/false` - that's the answer to "which panels still need tagging," read from the tree, not derived by loading `agents_overview.json` and checking `log_comment` markers yourself.
2. For every id the tree marked `"tagged": false` (excluding any Dynamic Text panel - check `type` via the tree/`dashboard-parser`, don't assume from an id list - which stays untagged on purpose per the open profiling question flagged in `Skill(dynamictext-panel-queries)`, never tag it on your own initiative), run `tag_panel_queries.py <agents_overview.json> --id <id> [--id <id2> ...]`.
   Idempotent, safe even if some of those ids turn out already tagged.
3. Re-run `extract_panel_tree.py` again.
   The tree from step 1 is now stale re: tagged-state after step 2's edits - same "never reuse a stale tree" rule as elsewhere in this doc, it just means a rebuild that found untagged panels needs the extract step run twice: once to discover, once to refresh post-tagging.
   If step 1 found nothing untagged, this second extract is still required (fresh tree per rebuild), it just reflects no new tags.
4. Run `build_query_perf_dashboard.py --tree <path> --out <path>` once, with no `--tab` - the tree extraction already walked every tab, so the render step's own "no `--tab` = all tabs" behavior covers the whole dashboard in that one call.
   There's no per-tab loop and no need to fetch a tab list from `dashboard-parser` for this case (`dashboard-parser` is still the right tool for reading a specific panel's current state during a single-panel edit, per `dashboards-expert`'s own "Reading the current panel" section; don't remove it from that use).
   Confirm the run's stderr has no `warning: skipped untagged panels` line - any panel it names means step 2/3 missed something and needs another tag+extract pass.
5. Validate the output with `python3 -m json.tool <out> > /dev/null` (exits non-zero on invalid JSON) - the sanctioned verification step; don't write a custom `json.load`-and-print-count snippet for this, the stdlib CLI already does it.

This is a distinct request from the per-edit sync above, which stays scoped to just the affected tab(s) via `--tab` - don't widen a single-panel edit into a full rebuild (extra churn on untouched tabs), and don't narrow an explicit full-rebuild ask down to a `--tab`-scoped call just because only one tab changed recently.
