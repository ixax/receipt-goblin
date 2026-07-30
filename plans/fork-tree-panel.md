# Fork tree panel for Sessions & Debugging

## Context

`services/grafana/dashboards/agents_overview.json` has a "Sessions & Debugging" tab
whose layout is a nested `TabsLayout` with two sub-tabs today: **Trace**
(panel-76 dynamic-text trace + panel-77 tool-call drill-down table) and
**Graph** (panel-86, a `nodeGraph` node/edge view of the same session).
Neither view gives a compact, purely hierarchical picture of "which agent
forked which agent, with what prompt, and at what cost" — Trace interleaves
everything into one chronological stream, Graph shows nodes/edges but not
prompts/stats per fork. The user wants a third sub-tab, **Fork tree**, sitting
between Trace and Graph, that renders exactly that: an indented tree, root =
`main`, one line per forked agent (bold name + tokens/duration/cost/model),
followed by its full un-truncated prompt text in gray, using the same
dynamic-text mechanism as panel-76.

Confirmed with the user:
- Placement: a **new sub-tab** in the nested TabsLayout, not squeezed into an
  existing grid.
- Depth: ClickHouse has no `WITH RECURSIVE`. Resolve parent/depth in **one SQL
  pass** via a bounded chain of self-joins (~8 levels, covers all realistic
  nesting), producing one flat, pre-sorted row set; the dynamic-text template
  draws the tree purely from that flat data (indentation glyphs computed in
  SQL) — no recursion anywhere, same pattern panel-76 already uses.
- Per-fork stats are **that fork's own numbers only** (not rolled up over its
  descendants) — each descendant fork already gets its own line in the tree.

## New panel

- New panel id: **99** (current max element id in the dashboard is 98).
- New sub-tab **"Fork tree"** inserted into the "Sessions & Debugging" nested
  `TabsLayout.spec.tabs` array between the existing "Trace" and "Graph"
  entries.
- Panel-99: `marcusolsson-dynamictext-panel`, full-width single panel in that
  sub-tab's grid (`gridPos: x=0 y=0 w=24 h=28`, matching panel-86's height).
- Reuses the `$session_id` dashboard variable exactly as panel-76 does (no new
  variable needed).

## Ownership / delegation

- The actual panel JSON is built/edited by the **`dynamictext-panel-builder`**
  subagent (owns every Dynamic Text panel edit in this file, knows the
  mandatory brace-matching JSON-splice procedure — never a full-file
  `Read`/`json.dump` reserialize). It also has `mcp__dev__query` to iterate the
  SQL against real data before finalizing.
- After the panel is created, delegate the `query_performance.json`
  mirror-sync step to **`dashboard-panels-builder`** (tags/mirrors every
  panel's `log_comment`) — `dynamictext-panel-builder` is barred from running
  that sync itself.
- New panel's SQL must end with `SETTINGS log_comment =
  'agents_overview:panel_99'` so the sync tooling picks it up.

## SQL design (handed to dynamictext-panel-builder as a starting sketch, to be iterated with `mcp__dev__query` against a real multi-fork session before finalizing)

Reuse verbatim from panel-76 (`services/grafana/dashboards/agents_overview.json`,
panel-76's `rawSql`):
- `selected` (session filter via `$session_id`)
- `session_header`, `stats_tokens`, `stats_time`, `stats_agent_names` — these
  produce exactly the header fields requested (`StartedAt`, `Duration`,
  `Cost`, `Tokens`, `Model(s)`, `Agents`, `Git`). No new logic needed for the
  header.

New CTEs:

1. **`fork_dedup`** — one row per `(session_id, agent_id)` from
   `agent_invocations`, `argMax`-deduped by `spawned_at` (same dedup panel-76's
   `child_anchor` already applies, since `agent_invocations` is a
   `ReplacingMergeTree` with no `FINAL` used anywhere in this codebase's
   queries). Carries `subagent_type` → display name via
   `splitByChar('_', subagent_type)[1]` (same as panel-76).

2. **`spawn_events`** — from `agent_events` where `calculated_type =
   'agent_spawn'`, carrying `(session_id, timestamp, agent_invocation_id)`.
   Unlike panel-76's `agent_spawn_events` (which filters to
   `agent_invocation_id = ''`, i.e. only direct orchestrator spawns), this
   keeps **every** spawn row regardless of invocation id — a spawn event's own
   `agent_invocation_id` is the *spawning* fork's own `agent_id` (blank =
   spawned by main). This is the key trick that makes one ASOF pass resolve a
   parent at **any** depth, not just depth 1.

3. **`fork_parent`** — `fork_dedup ASOF LEFT JOIN spawn_events ON
   se.session_id = fork_dedup.session_id AND fork_dedup.spawned_at >=
   se.timestamp`, giving each fork a `parent_agent_id = se.agent_invocation_id`
   (NULL/`''` → parent is `main`). Same heuristic panel-76 already documents
   and accepts ("nearest preceding spawn", no real parent link exists).

4. **`fork_stats`** — per `(session_id, agent_id)`: `min/max(timestamp)` from
   `agent_events` for that `agent_invocation_id` (duration bounds — take
   `dateDiff('second', ai.spawned_at, max(ts))`), `sum(input_tokens +
   output_tokens - cache adj.)`/`sum(cost)`/`groupUniqArray(model)` from
   `agent_usage` filtered to that `agent_invocation_id` — same arithmetic as
   panel-76's `usage_by_call`/`stats_tokens`, just scoped per fork instead of
   per session.

5. **`fork_prompt`** — from `agent_messages`, `row_number() OVER (PARTITION BY
   session_id, agent_invocation_id ORDER BY timestamp) = 1`, giving
   `prompt_text` verbatim (no truncation) — identical to panel-76's
   `is_agent_task`/`rn_in_invocation` pattern, just selecting the text itself
   instead of only flagging the row.

6. **Depth/order resolution (bounded 8-level chain)** — self-join
   `fork_parent` to itself up to 7 times (`n1` = parent, `n2` = grandparent,
   … `n7`) to get, per fork: `depth` (count of non-null ancestors) and a
   **sort path** built by padding every level slot below the node's own depth
   with its **own** `spawned_at` (standard adjacency-list-to-ordered-list
   trick: a parent's path sorts immediately before all its children because
   children's timestamps are always ≥ the parent's, and the padding keeps
   array shapes uniform for `arraySort`). Also carry, per ancestor level, that
   ancestor's own `is_last_sibling` flag (via `row_number()`/`count(*) OVER
   (PARTITION BY parent_agent_id ORDER BY spawned_at)` on `fork_parent`) so the
   render step can draw the correct `│ `/`  ` continuation glyph at each
   indentation column, not just the node's own `├─`/`└─`.

7. **Final per-session assembly** — same `UNION ALL` of tagged
   `(sort_key, ts, line)` tuples → `arrayStringConcat(arrayMap(x -> x.N,
   arraySort(x -> x.1, groupArray(...))), '\n')` pattern panel-76 already uses,
   producing one `tree` text column per session:
   - one header block (StartedAt/Duration/Cost/Tokens/Model(s)/Agents/Git)
   - one root line: `<b>main</b>`
   - one line per fork: computed glyph prefix (from step 6) + `<b>{agent
     display name}</b>` + ` [{model(s)}] · {tokens} tok · {duration} ·
     ${cost}`, followed immediately by a second, indented line rendering
     `fork_prompt.prompt_text` in a dimmed span, **with line breaks preserved**
     (render inside the same `<pre>` panel-76 already uses — no manual `<br>`
     needed, just HTML-escape `&`/`<`/`>` first).

Escaping order (per `dynamictext-panel-builder.md` convention): escape
`&`/`<`/`>` in raw text first, then wrap with `<b>`/`<span>`.

## Handlebars template

Identical shape to panel-76 (`options.editor.format: "html"`, `renderMode:
"allRows"`, `defaultContent: ""`):

```
{{#each data}}
<pre style="white-space:pre-wrap; margin:0 0 1.2em 0;">{{{this.tree}}}</pre>
{{/each}}
```

All tree-drawing logic (glyphs, indentation, bold/dim spans) lives in the SQL
`line` values, not in the template — same division of labor as panel-76.

## Verification

1. `dynamictext-panel-builder` iterates the SQL above with `mcp__dev__query`
   against a real session_id known to have nested forks (fork-of-a-fork, not
   just flat depth-1 subagents) — check `agent_invocations`/`agent_events` for
   a session with `calculated_type='agent_spawn'` rows carrying a non-blank
   `agent_invocation_id`, confirming a genuine depth-2+ case exists to test
   against.
2. Confirm the query returns exactly one row per session with a correct
   `tree` column (indentation nests correctly, `main` prompts don't leak in,
   token/cost/duration numbers match what panel-76's own header shows for the
   same session).
3. Splice the panel + new sub-tab into `agents_overview.json` using the
   brace-matching procedure (never full-file reserialize), `json.load()` the
   whole file to confirm it's still valid JSON.
4. Poll `curl -s http://localhost:3000/api/dashboards/uid/agents-overview` for
   the new panel/sub-tab title after Grafana's 30s provisioner reload, same
   check panel-76 changes use.
5. Delegate to `dashboard-panels-builder` to sync `query_performance.json`
   for panel-99's new `log_comment`.
