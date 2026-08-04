---
name: dynamictext-panel-queries
description: >
  Query/data/mechanical knowledge for Dynamic Text panels (`marcusolsson-dynamictext-panel`) in services/grafana/dashboards/agents_overview.json.
  Current examples: panel-76 ("Trace"), companion panel-77, and panel-99 ("Fork tree").
  TRIGGER - read only when the current dashboards-expert task is actually to write or fix a Dynamic Text panel's query, `rawSql`, or SQL-side logic.
  SKIP for styling-only work (use `dynamictext-panel-design-system` instead) or any non-Dynamic-Text panel.
  v1.2.2
---

Query/data reference for `dashboards-expert`, read on demand - see description for the trigger condition.
Dynamic Text panels render a per-session call tree (prompts, tool calls, agent spawns, replies) from `agent_events`/`agent_usage`/`agent_messages`/`agent_invocations`/`session_git_branch`.
`panel-76` ("Trace: $session_id", "Sessions & Debugging" -> "Trace" sub-tab) is the primary example.
Read its live element first (see "Safe JSON-editing procedure" below).
This doc explains why it's built this way, not a copy of the SQL to paste blindly.

## Panel identification and the panel-77 exception

`panel-77` ("Tool calls at $trace_ts", plain `table` type, not Dynamic Text) sits below `panel-76` in the same "Trace" sub-tab - see "Companion detail table" below for the wiring.
It's a named exception to `dashboards-expert`'s type-based scope, not evidence the rule is id-based: it's grouped here because its query and `$trace_ts` handling are inseparable from `panel-76`'s click-through logic, not because of its id.

## Companion detail table and clickable timestamps

Every timestamp on an "important" node (see Display conventions below) is a clickable link.
It sets a hidden dashboard variable, `$trace_ts`, which `panel-77` reads to show every tool call (name + full arguments, via the magnifier/inspect cell override) from that exact `agent_events` row.
One model call can invoke more than one tool in parallel; the tree above only surfaces the first.

- `$trace_ts` variable: `TextVariable` in `spec.variables`, `hide: "hideVariable"`, `current: {"text": "", "value": ""}` - starts empty, so the table renders nothing until a timestamp is clicked.
- Link is a plain `href`, not `onclick` - Grafana's HTML sanitizer strips `onclick` unless `disableSanitizeHtml` is set in `grafana.ini` (off by default, a dashboard-wide setting).
- Href must be prefixed with the literal dashboard path `/d/agents-overview/agents-overview`, not a bare `?...` - Grafana's app shell renders with `<base href="/">`, so a bare `href="?var-..."` resolves against the site root.
- Query params: `var-session_id=<encodeURLComponent(session_id)>` and `var-trace_ts=<encodeURLComponent(toString(ts))>`.
  `trace_ts` carries the full `DateTime64` value via `toString(ts)`, not the displayed `HH:MM:SS` - two rows in the same session can share a displayed hour:minute:second, so matching on the short string would be ambiguous.
- Two static tab-state params keep the click on the Trace tab instead of resetting to default: `dtab=Sessions-%26-Debugging` and `Sessions-%26-Debugging-dtab=Trace`.
  Values must match the actual tab titles exactly (spaces -> hyphens, `&` -> `%26`) - update if "Sessions & Debugging" or "Trace" are ever renamed.
- The link doesn't preserve time range or active tab (both reset to saved defaults) - accepted, since this panel's own query never filters by time range.
- `panel-77`'s own query matches on `toString(timestamp) = '$trace_ts'` - same function on both ends keeps formats aligned.
- `panel-77` query logic: a regular (non-Agent) tool call click shows just that row's `tool_calls`.
  An Agent spawn click shows every tool call from all descendants, matched by timestamp window (all events between the spawn point and the next orchestrator-level `Agent` spawn) - timestamp-based, not `agent_invocation_id` equality, since ingestion sometimes races that field blank.
  No hard nesting-depth limit.
  `'$trace_ts' != ''` guards the table empty before any click.
- `Arguments` column uses `custom.inspect: true` + `custom.cellOptions: {"type": "json-view"}` (same override used elsewhere in this dashboard for `raw_payload`) to make the magnifier/eye icon appear for viewing long arguments in full.
- Failure rows (`status='failure'`) have no `tool_calls` in `raw_payload` since the LLM request itself failed - surfaced separately via `failed_tool_error` (or `failed_tool_name`).
- `panel-77`'s table is `ORDER BY Ts`, a hidden column (via the `Organize` transformation, `excludeByName: {"Ts": true}`) carrying each row's `agent_events.timestamp` - keeps late-stage calls (e.g. `AskUserQuestion`) in execution order instead of arbitrary order.

## Plugin config that must not drift

- `vizConfig.group`: `"marcusolsson-dynamictext-panel"`.
- `vizConfig.spec.options.editor.format`: `"html"`, not `"markdown"` or `"auto"` - in markdown mode the plugin's `markdown-it` runs with `html:false` and escapes raw `<span>`/`<pre>`/`<b>` tags; only `"html"` mode passes them through.
  The SQL is responsible for 100% of the HTML output, including escaping `&`/`<`/`>` in every dynamic string.
- `vizConfig.spec.options.renderMode`: `"allRows"` - the Handlebars template runs once with the full result set in `data`, not once per row.
- `vizConfig.spec.options.defaultContent`: `""` - "No session selected" then renders as literally nothing, not the plugin's default "no results" message.
- `content` template is intentionally tiny; all the real logic lives in SQL, not Handlebars:
  ```
  {{#each data}}
  <pre style="white-space:pre-wrap; margin:0 0 1.2em 0;">{{{this.tree}}}</pre>
  {{/each}}
  ```
  Triple-stash (`{{{ }}}`) is required so Handlebars doesn't re-escape the HTML the SQL already built.

## SQL shape: one row per session, a single "tree" text column

The query returns one row per selected session, or zero rows if none selected - one row per session, not per event, since Handlebars in `allRows` mode can't easily group/indent per-event.

Pattern:

1. Build several "row" sub-selects (header line, session-stats block, prompt/comment lines, tool-call/reply/error lines), each tagged `(session_id, sort_ts, tie, ts, line)`.
   `tie` is a small int fixing intra-timestamp order: 0=header, 1=stats block, 2=prompt marker, 3=event line.
   `ts` is the row's own real timestamp; `sort_ts` is the position it actually sorts at - not always equal to `ts` (see `references/concurrent-ordering.md` for concurrent-subagent handling).
2. `UNION ALL` them together.
3. Aggregate per session: `groupArray` the `(sort_ts, tie, ts, line)` tuples, `arraySort` on `(sort_ts, tie, ts)`, then `arrayStringConcat` the sorted `line` values on `'\n'` into the single `tree` output column.
   `arraySort` on that tuple, not `groupArray`'s incidental order, is what keeps the header/stats block pinned above the timeline regardless of execution order.
   Read panel-76's own live `rawSql` for the exact syntax - source of truth, not copied here.

## Display/rendering conventions (logic, not styling)

Pure styling facts (markers, colors/opacities, `**bold**`/`` `code` `` conversion, slash-command highlighting) live in `Skill(dynamictext-panel-design-system)`.
This covers the data/logic side of what gets shown, not how it looks.

- Timestamps show only on "important" nodes: user prompts/comments, agent spawns, replies, and failed calls.
  Plain mid-chain tool calls get blank space of the same width instead, to cut visual noise.
- Prompt/reply text is capped at 1500 chars, relying on `white-space:pre-wrap` to wrap long text in the viewer.
  Tool-call argument previews use the shorter per-field caps (`references/gotchas.md`), plus a short fixed gap (not a padded column) before the stats.
- Stats/labels (`Duration:`, `Cost:`, `Tokens:`, `Model(s):`, `Prompts:`, `Tool calls:`, `Agents:`, `Skills:`, `Git:`) are one per line, not packed two-per-line - packing risked truncating long agent/skill lists when they shared a padded column.
  The token stat itself is the bare number (`62.5k`, no trailing `tok`).
- Prompt and reply text supports literal newlines via `\n` -> `<br>` conversion, applied after markdown `**bold**`/`` `code` `` conversion.
- `WebFetch` output can come back embedded in a `role: user` message as a plain `Web page content: ---` dump, not always cleanly marked as a `tool_result`, and can be enormous.
  Unlike the general 1500-char cap, anything starting with `Web page content` (check both the prompt-classification pipeline's `cleaned0` and a reply row's raw `response_text`) is hard-cut to 100 chars plus `...`, marked with the `●` reply marker via `is_webpage` (`prompt_final` must pass `is_webpage` through to the final SELECT for this).
- WebFetch nesting: `Web page content:...` response rows render one level deeper than the WebFetch tool-call row that produced them (extra 3-space indent), detected via `startsWith(response_text, 'Web page content')`.
- Agent spawn rows show the spawned agent's name followed by the spawn's own task/prompt description text (120 chars + `...`), extracted from the Agent row's own `prompt_text` (system-reminder prefix stripped).
- Suggestion-mode prompts render as a single line showing the actual prompt text with the `○` marker, not a separate label line above it.
- Failure error lines (both `status='failure'` and non-empty `failed_tool_name`) are indented one level deeper than their parent row, to show they're notes/side-effects.
- Always filter empty strings out of any array before `arrayStringConcat(arr, ', ')`, e.g. `arrayStringConcat(arrayFilter(x -> x != '', groupUniqArray(name)), ', ')` - a stray `''` element renders as a trailing `", "` with nothing after it.

### Array-valued tool arguments

When a tool call's argument JSON contains an array field (`AskUserQuestion`'s `questions`, for example), keep the tool-call row itself as one line (tool name + stats: duration/tokens/cost at the end, same as any other tool call).
Render each array element as its own separate nested line underneath, with no per-line stats, joined with newlines (`arrayStringConcat(..., '\n')`) into a single multi-line block.
Currently implemented for `AskUserQuestion`; the same pattern applies to any future tool with array-valued arguments.

## Safe JSON-editing procedure: brace-matching splice

Never `Read` the whole dashboard file into context, and never hand-edit the raw JSON with `Edit` - a naive text edit against the large v2beta1 file risks corrupting sibling panels.
Do targeted brace-matching text surgery with a small Python script (`Write` to scratch, run via `Bash`):

1. Read the file as plain text, not a `json.load` + `json.dump` round-trip - the file's key order is insertion-order, not sorted, so a full reserialize produces a huge unrelated diff even with `indent=2`.
2. Find the unique anchor `'"panel-76": {'`, then walk forward doing brace-depth counting (tracking whether you're inside a JSON string, so braces inside string values don't confuse the depth count) to find the matching closing `}` for that one element.
3. Build the new panel dict in Python, `json.dumps(panel, indent=2, ensure_ascii=False)`, re-indent it to match the surrounding file's 6-space panel-key indentation, and splice it in as a straight string replacement of the old block.
4. `json.load()` the result to confirm it's still valid JSON before considering the change done.

If touching both panel-76 and panel-77, or making more than one edit in the same task, do this whole read-splice-write cycle once per edit, immediately - never hold the file's content in memory across multiple edits and write it back once at the end (`dashboards-expert`'s atomic-unit rule).
A concurrent edit landing on the live file in that window would get silently discarded when a stale in-memory copy is written back.
If a mistake needs correcting mid-task, fix it forward with another scoped edit against the live file - never reset the working tree from any git ref (`git checkout`/`restore`/`reset`/`clean`, or `git show :path` piped into the file) to "start clean"; stop and report the anomaly instead.
Only one edit against a given Dynamic Text panel should be in flight at a time - this splice procedure assumes it's the sole writer, the same atomic-unit discipline `dashboards-expert` applies to every other panel type, just enforced here via brace-matching since the panel's `rawSql` is too large for a simple substring replace.

After writing, Grafana's file-based dashboard provisioner reloads every 30 seconds (`services/grafana/provisioning/dashboards/*.yml`, `updateIntervalSeconds: 30`).
Confirm the change landed by polling `curl -s http://localhost:3000/api/dashboards/uid/agents-overview` and grepping for a distinctive fragment of your new SQL/template, looping with a short sleep until it appears - don't guess a fixed wait or trust the file write alone.

## Testing before you deploy

Test new/changed SQL against real data via `mcp__dev__query` before touching the panel JSON.
Pick a real `session_id` from the database (`SELECT session_id, count() FROM agent_events GROUP BY session_id ORDER BY max(timestamp) DESC LIMIT 5`) rather than inventing one.
Remember the validator quirks in `references/gotchas.md` when constructing the test query string.
The deployed SQL should use the real, unobfuscated literals - the obfuscation is purely a testing-tool workaround, never carry it into the panel itself.

## Deeper reference material

- `references/gotchas.md` - hard-won ClickHouse/plugin bugs already hit and fixed once (byte-vs-UTF8 functions, `formatDateTime` minute format, truncation caps, JSON double-escaping, `mcp__dev__query` validator quirks, stray-`*` HTML corruption, `ReplacingMergeTree` tie orphaning) - open when a new symptom looks similar.
- `references/width-budget.md` - the `${trace_width_budget}` pad-arithmetic (120 top-level / 117 nested) - open when adding/retuning a padded row-rendering branch.
- `references/concurrent-ordering.md` - `sort_ts` vs `ts` mechanics for interleaving a background subagent's rows - open when touching ordering/CTE logic.
- `references/data-model.md` - schema-specific lookup facts (`turn_id`, `agent_name` blankness, tool-argument extraction, `prompt_text` injected-boilerplate stripping, the two failure signals, pre-conversation artifact rows) - open when a query needs one of these fields.

## query_performance.json tagging

Dynamic Text panels are tagged and profiled the same as every other panel, no exception - `panel-76`/`panel-99` already carry a `log_comment` tag and a full mirror entry in `query_performance.json`, despite `panel-76` assembling a full session tree per row (an unusual shape for that template).
