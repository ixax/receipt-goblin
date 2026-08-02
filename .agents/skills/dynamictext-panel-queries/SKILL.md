---
name: dynamictext-panel-queries
description: >
  Query/data/mechanical knowledge for Dynamic Text panels (`marcusolsson-dynamictext-panel`) in services/grafana/dashboards/agents_overview.json.
  Current examples: panel-76 ("Trace"), companion panel-77, and panel-99 ("Fork tree").
  TRIGGER - read only when the current dashboards-expert task is actually to write or fix a Dynamic Text panel's query, `rawSql`, or SQL-side logic.
  SKIP for styling-only work (use `dynamictext-panel-design-system` instead) or any non-Dynamic-Text panel.
  <version>1.1.1</version>
---

Query/data reference for `dashboards-expert`, read on demand.
See description for the trigger condition.
Dynamic Text panels render a per-session call tree (prompts, tool calls, agent spawns, replies) from `agent_events`/`agent_usage`/`agent_messages`/`agent_invocations`/`session_git_branch`.
`panel-76` ("Trace: $session_id", "Sessions & Debugging" -> "Trace" sub-tab) is the primary example.
Read its live element first (see "Safe JSON-editing procedure" below).
This doc explains why it's built this way, not a copy of the SQL to paste blindly.

## Panel identification and the panel-77 exception

`panel-77` ("Tool calls at $trace_ts", plain `table` type, not Dynamic Text) sits below `panel-76` in the same "Trace" sub-tab.
See "Companion detail table and clickable timestamps" below for the wiring.
It's a named exception to `dashboards-expert`'s type-based scope, not evidence the rule is id-based.
It's grouped here because its query and `$trace_ts` handling are inseparable from `panel-76`'s click-through logic, not because of its id.

## Companion detail table and clickable timestamps

Every timestamp on an "important" node (see Display conventions below) is a clickable link.
It sets a hidden dashboard variable, `$trace_ts`, which `panel-77` reads to show every tool call (name + full arguments, via the magnifier/inspect cell override) from that exact `agent_events` row.
One model call can invoke more than one tool in parallel; the tree above only surfaces the first.

- `$trace_ts` variable: `TextVariable` in `spec.variables`, `hide: "hideVariable"`, `current: {"text": "", "value": ""}`.
  Starts empty, so the table renders nothing until a timestamp is clicked.
- Link is a plain `href`, not `onclick`.
  Grafana's HTML sanitizer strips `onclick` unless `disableSanitizeHtml` is set in `grafana.ini` (off by default, a dashboard-wide setting, not to be changed for one link).
- Href must be prefixed with the literal dashboard path `/d/agents-overview/agents-overview`, not a bare `?...`.
  Grafana's app shell renders with `<base href="/">`, so a bare `href="?var-..."` resolves against the site root, not the current dashboard path.
- Query params: `var-session_id=<encodeURLComponent(session_id)>` and `var-trace_ts=<encodeURLComponent(toString(ts))>`.
  `trace_ts` carries the full `DateTime64` value via `toString(ts)`, not the displayed `HH:MM:SS`.
  Two rows in the same session can share a displayed hour:minute:second, so matching on the short string would be ambiguous.
- Also two static tab-state params: `dtab=Sessions-%26-Debugging` and `Sessions-%26-Debugging-dtab=Trace`.
  These keep the dashboard on the Trace tab after the click instead of resetting to the default tab.
  Values must match the actual tab titles exactly: spaces become hyphens, `&` becomes `%26`.
  Update them if "Sessions & Debugging" or "Trace" are ever renamed.
- The link does not preserve time range or active tab; both reset to the dashboard's saved defaults on click.
  Accepted tradeoff: this panel's own query never filters by time range, so losing it doesn't break anything shown.
- `panel-77`'s own query matches on `toString(timestamp) = '$trace_ts'`.
  Same function on both ends keeps the formats aligned.
- `panel-77` query logic: a regular (non-Agent) tool call click shows just that row's `tool_calls`.
  An Agent spawn click shows every tool call from all descendants (direct children, grandchildren, and beyond), matched by timestamp window - all events between the spawn point and the next orchestrator-level `Agent` spawn.
  This timestamp-based matching, not `agent_invocation_id` equality, handles the ingestion race where that field is blank on some rows.
  There is no hard nesting-depth limit.
  The `'$trace_ts' != ''` guard keeps the table empty before any click.
- `Arguments` column uses `custom.inspect: true` + `custom.cellOptions: {"type": "json-view"}`.
  Same override already used elsewhere in this dashboard for `raw_payload` (e.g. the "Raw" sub-tab's Full Trace/Call Stack panels).
  Makes the magnifier/eye icon appear for viewing long arguments in full.
- Failure rows (`status='failure'`) have no `tool_calls` in `raw_payload` since the LLM request itself failed.
  They're surfaced separately via `failed_tool_error` (or `failed_tool_name` if set).
- `panel-77`'s table is `ORDER BY Ts` in the SQL.
  `Ts` is a hidden column (via the `Organize` transformation, `excludeByName: {"Ts": true}`) carrying each row's `agent_events.timestamp`.
  Keeps late-stage calls (e.g. `AskUserQuestion`) in execution order instead of arbitrary order.

## Plugin config that must not drift

- `vizConfig.group`: `"marcusolsson-dynamictext-panel"`.
- `vizConfig.spec.options.editor.format`: `"html"`, not `"markdown"` or `"auto"`.
  In markdown mode the plugin's `markdown-it` runs with `html:false` and escapes raw `<span>`/`<pre>`/`<b>` tags.
  Only `"html"` mode passes them through untouched.
  The SQL is responsible for 100% of the HTML output, including escaping `&`/`<`/`>` in every dynamic string.
- `vizConfig.spec.options.renderMode`: `"allRows"`.
  The Handlebars template runs once with the full result set in `data`, not once per row.
- `vizConfig.spec.options.defaultContent`: `""`.
  "No session selected" then renders as literally nothing, not the plugin's default "no results" message.
- `content` template is intentionally tiny; all the real logic lives in SQL, not Handlebars:
  ```
  {{#each data}}
  <pre style="white-space:pre-wrap; margin:0 0 1.2em 0;">{{{this.tree}}}</pre>
  {{/each}}
  ```
  Triple-stash (`{{{ }}}`) is required so Handlebars doesn't re-escape the HTML the SQL already built.

## SQL shape: one row per session, a single "tree" text column

The query returns one row per selected session, or zero rows if none selected.
Return one row per session, not per event.
Handlebars in `allRows` mode can't easily group/indent per-event; a loop of tiny per-event partials produces output with no visible columns.

Pattern:

1. Build several "row" sub-selects (header line, session-stats block, prompt/comment lines, tool-call/reply/error lines), each tagged `(session_id, sort_ts, tie, ts, line)`.
   `tie` is a small int fixing intra-timestamp order: 0=header, 1=stats block, 2=prompt marker, 3=event line.
   `ts` is the row's own real timestamp.
   `sort_ts` is the position it actually sorts at (see "Concurrent subagent ordering" below - not always equal to `ts`).
2. `UNION ALL` them together.
3. Aggregate per session: `groupArray` the `(sort_ts, tie, ts, line)` tuples, `arraySort` on `(sort_ts, tie, ts)`, then `arrayStringConcat` the sorted `line` values on `'\n'` into the single `tree` output column.
   `arraySort` on that tuple, not `groupArray`'s incidental order, is what keeps the header/stats block pinned above the timeline regardless of execution order.
   Read panel-76's own live `rawSql` for the exact syntax - source of truth, not copied here.

### Concurrent subagent ordering: `sort_ts` vs `ts`

A background subagent (an `Agent` tool_use the model didn't wait on) keeps running while the orchestrator continues its own work.
Sorting every row by its own real timestamp (`ts`) interleaves the subagent's steps with whatever the orchestrator did while it ran, in whichever order the clock happened to land - the tree looks jumbled.
Fix: every subagent's rows (both prompt markers and event lines) share one `sort_ts`, the orchestrator's nearest-preceding `Agent` tool_use row.
The whole block then sorts as one contiguous unit right after its spawn point, ordered internally by real `ts`.
Orchestrator rows (`agent_invocation_id = ''`) are unaffected: their `sort_ts` always equals their own `ts`.

Two extra CTEs sit before `session_header`: `agent_spawn_events` (every orchestrator-level `Agent` tool_use row, `agent_invocation_id = ''`) and `child_anchor` (each subagent's `agent_id` ASOF-joined backward to the nearest-preceding row in `agent_spawn_events`, deduped to one row per `(session_id, agent_id)`).
Read panel-76's own `rawSql` for the exact CTE syntax - both already exist there.
Same nearest-before heuristic as `spawn_info` (no real parent link exists), run in the opposite ASOF direction: `spawn_info` goes from an orchestrator row forward to the next spawn, `child_anchor` goes from a spawn backward to the orchestrator row that triggered it.
Wherever a prompt/event row is built: join `child_anchor` on `(session_id, agent_invocation_id)`, then `sort_ts` is the joined anchor timestamp when `agent_invocation_id != ''`, else the row's own `ts`.
Do not skip the dedup in `child_anchor`.
`agent_invocations` can hold more than one row per `agent_id` (nothing runs `FINAL` against its `ReplacingMergeTree` here).
Joining the raw ASOF result directly into the final SELECT would silently multiply every one of that subagent's event rows by however many duplicate `agent_invocations` rows exist.

This only groups one level deep, matching this panel's single-nesting-level limitation.
A grandchild agent (spawned by another sub-agent) anchors to the same top-level spawn point as its parent, since `agent_spawn_events` only looks at orchestrator-level (`agent_invocation_id = ''`) `Agent` rows.

## Hard-won ClickHouse gotchas (do not reintroduce these bugs)

Read the `clickhouse-sql` skill (`.claude/skills/clickhouse-sql/SKILL.md`) first.
It covers general ClickHouse lexer/regex/type surprises, not specific to this panel.
One entry there, the SQL lexer folding `\b` into a literal backspace byte inside a single-quoted string literal before any regex function runs (`\b` must be written `\\b` to reach RE2 as a word-boundary anchor), was discovered in this exact panel's own debugging.
It's documented there, not duplicated here, since it applies to any ClickHouse regex, not just this panel's SQL.
The entries below are specific to this panel's own tree-rendering logic.

- Byte vs character functions: `substring()`, `rightPad()`/`leftPad()` operate on bytes, not UTF-8 characters.
  Text that can contain Cyrillic, emoji, or box-drawing characters (`●`/`├─`/`▸`, in prompt text, replies, tool args) must use `substringUTF8()`/`rightPadUTF8()` instead.
  Truncating/padding mid-character otherwise produces garbled, hex-dump-looking output.
  `leftPad()` (plain, byte-based) is fine only for pure-ASCII numeric fields (ms/token columns).
- `formatDateTime` minute format: on this ClickHouse version, `%M` means the full month name, not minutes.
  Use `%i` for minutes: `formatDateTime(ts, '%H:%i:%S')`.
  Getting this wrong silently produces times like `14:July:16` instead of `14:37:16`.
- `rightPadUTF8` fixed-width columns: pre-truncate before padding.
  `rightPadUTF8(content, N, ' ')` silently truncates at byte boundaries if content runs longer than N chars, which breaks the later `replaceOne(padded, plain_substring, span_wrapped)` substring match and leaves arguments unstyled.
  Pre-truncate arguments to a safe cap (currently 90 chars, uniform across every `tool_render` argument-preference branch - see next bullet) before padding, so content can never overflow the pad width and `replaceOne()` always finds its target intact.
  This allows fixed-width alignment to the shared `${trace_width_budget}` total-line budget (120 top-level / 117 nested - see "Pad width must be a total-line budget" below).
  The budget is per-row, not per nesting level: deeply nested rows still get the same total width, with `- 3` carved out for the indent.
- Per-field truncation caps are unified to 90 chars.
  All of `tool_render`'s argument-preference branches (`file_path`, `command`, `sql`, `url`, `query`, `description`, `summary`, and the raw-JSON fallback) truncate to 90 chars via `substringUTF8(..., 1, 90)` before HTML-escaping and padding.
  Unifying the cap keeps row types aligned regardless of field.
  If retuning, change all branches together, not just the one named.
  Not part of this unified cap: the Agent-spawn/failure branch's own `description` field (own 50-char cap) and `AskUserQuestion`'s per-question text (120-char cap, own unbounded nested-line rendering path).
- JSON serialization must not double-escape the `rawSql` field.
  When editing the dashboard JSON, always load via `json.load()` and write back via `json.dump()` on the modified in-memory object.
  Never do string-level replacements on the raw file text, and never pass the SQL through a JSON encoder/decoder outside the main dump.
  A tooling bug that re-serializes the string value can introduce stray backslashes before quotes (e.g. `style=\"opacity:.6\"` instead of `style="opacity:.6"`), accumulating across edits until it breaks quote-parity in ClickHouse's string-literal lexer.
  If spotted: load the JSON properly, `.replace('\\"', '"')` on the parsed string value (not the raw file bytes), and write back via `json.dump()`.
  This is distinct from the brace-matching splice procedure below, which deliberately avoids a full `json.load`/`json.dump` round-trip for the everyday edit path.
  Only invoke a full load/dump cycle to fix already-corrupted escaping, never as the normal edit method.
- `agent_invocations` isn't in the `mcp__dev__query` table whitelist (only `agent_events`/`agent_usage`/`agent_messages`/`session_git_branch` are, per `_ALLOWED_TABLES` in `services/mcp-dev/src/server.py`).
  The check only requires one referenced table to be in that whitelist, so a test query that joins `agent_invocations` alongside `agent_events` passes fine.
  This restriction doesn't apply to the deployed panel at all (Grafana talks to ClickHouse directly), only to your own ad-hoc testing.
- `mcp__dev__query`'s validator false-positives: it rejects any literal `;` anywhere in the query text (even inside a string value like `'&amp;'`, which ends in `;`), and rejects the bare word `SYSTEM` case-insensitively as a whole word anywhere (even inside `'<system-reminder>'` or `'[SYSTEM NOTIFICATION'`, since a hyphen counts as a word boundary).
  Both only matter for testing through this tool.
  Work around them in your test copy by building the string from parts, e.g. `concat('&','amp',char(59))` instead of `'&amp;'`, and `concat('[SY','STEM NOTIFICATION')` instead of the literal substring.
  Deploy the real, unobfuscated literal into the actual panel SQL - the validator doesn't run there.
  Do not generalize this into a documented "how to bypass the read-only guard" reference; this entry exists only to explain a narrow test-string-construction case, not a sanctioned bypass technique.
- A stray unpaired `` ` `` or `*` in a fork's raw prompt text can corrupt the rendered HTML for the entire tree, not just that one row.
  `panel-99`'s `content` template wraps the whole `tree` string in one `<pre>...</pre>` block, but the plugin (`marcusolsson-dynamictext-panel`) still runs that content through its bundled `markdown-it`/`marked` libraries when "Wrap automatically in paragraphs" is on (see `Skill(dynamictext-panel-design-system)`).
  `<pre>` isn't treated as an inert raw-HTML block the way strict CommonMark would.
  A single unmatched `*` (e.g. real prompt text like `` `--t-col-*` ``, a wildcard reference, not markdown emphasis) pairs with the next stray `*` anywhere later in the entire concatenated document, wrapping everything between them in `<em>`, including unrelated later rows' timestamp columns.
  Fix, applied in `fork_render`'s `prompt_line` construction and the root user-prompt (`main_prompt`, tie=2) branch: after the existing `'\*\*([^*\n]+?)\*\*' -> '<b>\1</b>'` bold-conversion step, add one more `replaceRegexpAll(<result>, '\*', '&#42;')` pass that neutralizes any remaining single `*` to the HTML entity before the backtick/code-span regexes run.
  This doesn't change what's visually shown (the entity still renders as `*`) but removes the literal character so no later markdown pass can pair it into unintended emphasis.
  Verify with the same `session_id` a real bug report mentions.
  Reproduce via the browser DOM (`document.querySelectorAll('span.t-row')`, inspect for stray `<em>`/`<p>` elements), not just by eyeballing the SQL, since this bug class only manifests in the plugin's own client-side re-processing, not in the query output itself.
- `agent_invocations` `ReplacingMergeTree` ties can silently orphan a fork under a blank-named parent.
  A batch backfill/re-ingest can rewrite many rows with the exact same `spawned_at` timestamp, and some of those rewritten rows have `parent_agent_id`/`subagent_type` reset to `''` while an older, correct version of the same `(session_id, agent_id)` row still exists with the real value.
  `fork_dedup`'s plain `argMax(ai.parent_agent_id, ai.spawned_at)` (and `subagent_type`) picks non-deterministically between the two on a tie.
  Picking the blank one causes `fork_parent_raw`'s ASOF fallback to reassign that fork to whatever spawn event happens to precede it - visually an empty-named `<details>` that turns out, when expanded, to wrap a real, correctly-named child fork.
  Fix: give `argMax` a tuple comparator that prefers the non-blank value on a tie: `argMax(ai.subagent_type, (ai.spawned_at, ai.subagent_type != '')) AS subagent_type`, same shape for `parent_agent_id`.
  This is a display-layer mitigation for a real ingestion data-quality issue, not a fix to the ingestion pipeline itself.
  If orphaned/blank-named forks keep appearing after this, the underlying `ReplacingMergeTree` write pattern needs its own investigation.

## Data-model facts specific to this schema

- `agent_events.turn_id` is always hardcoded to `0` at ingest, never actually computed (see `_event_row`/`_usage_row`/`_message_row` in `services/_common/src/ingest_parsing.py`).
  Never use it for ordering; use `timestamp` instead.
- `agent_events.agent_name`/`agent_version` are blank on a spawned subagent's own rows whenever ingestion raced ahead of the orchestrator's `Agent` tool_use/tool_result (best-effort lookup, see `_agent_name_and_version_for_invocation`'s docstring).
  Don't trust `agent_events.agent_name` alone for "which agents ran this session" - union it with `agent_invocations.subagent_type` (strip the `_vX.Y.Z` suffix via `splitByChar('_', subagent_type)[1]`), filtering both sources for non-empty values before `arrayStringConcat`.
- `agent_invocations` has no column linking a spawn back to the specific parent tool_use call that triggered it.
  Matching an `Agent` tool_use row to its `agent_invocations` row is only possible via an ASOF JOIN on `session_id` + nearest `spawned_at >= timestamp`, a heuristic that breaks down if multiple agents are spawned in the same message/turn.
  Document this limitation in the panel description; don't present it as exact.
- Tool call arguments live at `JSONExtractString(raw_payload, 'response','choices',1,'message','tool_calls',1,'function','arguments')`, returned as a JSON-encoded string, not a parsed object.
  To pull a specific key (`file_path`, `command`, `url`, `query`, `task_id`) cleanly, with real newlines/quotes instead of literal `\n`/`\"`, call `JSONExtractString` a second time on that string: `JSONExtractString(<args_json_string>, 'file_path')`.
  Preference order used so far: `file_path` (Read/Write/Edit) -> `command` (Bash) -> `url` (WebFetch) -> `query` (WebSearch/web_search) -> `task_id` (TaskStop, shown as `task_id: <id>`) -> raw JSON substring as a last resort.
  Normalize tool name display too (`web_search` -> `WebSearch`), since the stored value isn't always the display-friendly one.
- `agent_messages.prompt_text` (`_last_user_text` in `services/_common/src/ingest_parsing.py`) is the last human-role turn verbatim, but that does not mean it's literally what a human typed.
  Claude Code prepends/injects boilerplate under the same `role: user` message:
  - `<system-reminder>...</system-reminder>` prefixed before real text - strip via `replaceRegexpOne(text, '(?s)^<system-reminder>.*?</system-reminder>\s*', '')`.
  - `<command-name>/x</command-name> ... <command-args>...</command-args>` for slash commands - `extract(text, '<command-args>(?s)(.*?)</command-args>')` gives the real typed args; reconstruct as `/command args`.
  - `[SYSTEM NOTIFICATION - NOT USER INPUT]` - a stop-hook background check, not something the user said.
  - `[SUGGESTION MODE: ...]` - an internal autosuggest prompt.
  - `<transcript>{...}` - a `/goal` judge call passing the whole conversation as JSON.
  - `<session>...</session>` - the conversation-title-generation call.
  - `[Request interrupted by user]` prefix - real user text follows, just strip the tag.

  None of this is stripped at ingest by design (see that function's own docstring).
  The panel has to classify and label these itself (mark them `○ [background]`/`[suggestion-mode prompt]`/`[goal-check judge call]`/`[title-gen call]` instead of `●`, a real user turn) rather than presenting harness noise as if the user typed it.
  This is a best-effort prefix-match list, not exhaustive - say so in the panel description.
- Two distinct failure signals; do not conflate them.
  `status = 'failure'` means this call's own LLM request failed (extract a reason via `JSONExtractString(JSONExtractString(raw_payload, 'error_information', 'error_message'), 'error', 'type')`, e.g. `rate_limit_error`).
  `failed_tool_name`/`failed_tool_error` non-empty on an otherwise-successful row means this call is reacting to a different tool call that failed one step earlier (show as an indented note above the row, not as this row's own status).
- The very first row(s) of many sessions are an invisible pre-conversation artifact (a silently-retried rate-limited call, a warm-up ping) with empty `prompt_text` and no trace in the actual CLI transcript.
  Compute `min(ts) WHERE is_real` (the first genuine, non-harness prompt) per session and drop everything before it, or a confusing orphan `FAIL` row shows up that the user never saw.

## Display/rendering conventions (logic, not styling)

Pure styling facts (markers, colors/opacities, `**bold**`/`` `code` `` conversion, slash-command highlighting) live in `Skill(dynamictext-panel-design-system)` instead.
This section covers the data/logic side of what gets shown, not how it looks.

- Timestamps show only on "important" nodes: user prompts/comments, agent spawns, replies, and failed calls.
  Plain mid-chain tool calls get blank space of the same width instead, to cut visual noise.
- Prompt/reply text is capped at 1500 chars, relying on `white-space:pre-wrap` to wrap long text across lines in the viewer.
  Tool-call argument previews use the shorter per-field caps above, plus a short fixed gap (not a padded column) before the stats.
- Stats/labels (`Duration:`, `Cost:`, `Tokens:`, `Model(s):`, `Prompts:`, `Tool calls:`, `Agents:`, `Skills:`, `Git:`) are one per line, not packed two-per-line.
  Packing them risked truncating long agent/skill lists when they shared a padded column.
  The token stat itself is the bare number (`62.5k`, no trailing `tok`).
- Prompt and reply text supports literal newlines via `\n` -> `<br>` conversion, applied after markdown `**bold**`/`` `code` `` conversion.
- `WebFetch` output can come back embedded in a `role: user` message as a plain `Web page content: ---` dump, not always cleanly marked as a pure `tool_result`, and can be enormous.
  Unlike the general 1500-char cap on prompt/reply text, anything starting with `Web page content` (check both the prompt-classification pipeline's `cleaned0` and a reply row's raw `response_text`) is hard-cut to 100 chars plus a literal `...`.
  It's marked with the `●` reply marker via `is_webpage` - `prompt_final` has to pass `is_webpage` through to the final SELECT for this, not only inside the `multiIf` that builds `display`.
- WebFetch nesting: `Web page content:...` response rows render one level deeper than the WebFetch tool-call row that produced them, using an extra 3-space indent to show it's a child output of that specific call.
  Detection is via `startsWith(response_text, 'Web page content')`.
- Agent spawn rows show the spawned agent's name followed by the spawn's own task/prompt description text (capped at 120 chars + `...`), extracted from the Agent row's own `prompt_text` field (system-reminder prefix stripped).
- Suggestion-mode prompts render as a single line showing the actual prompt text with the `○` marker, not a separate label line followed by the prompt text nested below it.
- Failure error lines (both `status='failure'` LLM failures and `failed_tool_name` non-empty tool failures) are indented one level deeper than their parent row, to show they're notes/side-effects rather than primary content.
- Always filter empty strings out of any array before `arrayStringConcat(arr, ', ')`, e.g. `arrayStringConcat(arrayFilter(x -> x != '', groupUniqArray(name)), ', ')`.
  A stray `''` element renders as a trailing `", "` with nothing after it.

### Array-valued tool arguments

When a tool call's argument JSON contains an array field (`AskUserQuestion`'s `questions`, for example), keep the tool-call row itself as one line (tool name + stats: duration/tokens/cost at the end, same as any other tool call).
Render each array element as its own separate nested line underneath, with no per-line stats.
Array elements are joined with newlines (`arrayStringConcat(..., '\n')`) and displayed as a single multi-line block under the tool call.
Currently implemented for `AskUserQuestion` (questions array); the same pattern applies to any future tool with array-valued arguments.

## Column alignment with overflow safety: fixed-width safe truncation

Fixed-width column alignment pads tool-call content out to the shared `${trace_width_budget}` total (120 top-level / 117 nested), so stats columns line up vertically.
Tool-call arguments are pre-truncated to a uniform 90-char cap (see "Per-field truncation caps are unified to 90 chars" above) before being padded.
This prevents an earlier failure mode: `rightPadUTF8` on unbounded content silently truncates at a byte boundary, breaking the later `replaceOne()` substring match and leaving arguments unstyled.
Because content is already shortened first, the match now always succeeds.
Padding itself is applied as literal spaces via `repeat()` appended to the already-built HTML, not via `rightPadUTF8` on the HTML string directly.
`rightPadUTF8` measuring against escaped (entity-inflated) content was the other half of that same bug.
Total width budget is `${trace_width_budget}` (see next section) for the padded content line, leaving room for the right-hand stats (duration/tokens/cost) to appear in a consistent column across all nested indentation levels.

## Pad width must be a total-line budget, not a flat per-branch constant

The `agent_invocation_id != ''` nesting indent (`'   '`, 3 chars, prepended before `├─`/`└─` on a subagent's own rows - see "Concurrent subagent ordering" above) is extra width added on top of whatever comes after it, not carved out of some already-fixed content width.
Any branch that pads its content to a target column must compute its pad target as `(shared total-line budget) - (indent width)`, not use one flat constant for all rows regardless of depth.
Otherwise nested rows end up wider overall than top-level rows and the stats columns drift right with depth.
The regular tool-call branch (`tool_render`/tie=3) pads to `(${trace_width_budget} - 3)` for nested rows and `${trace_width_budget}` for top-level - the shared budget defaults to 120, indent is 3, so nested rows pad to 117.

`${trace_width_budget}` is a hidden Grafana dashboard variable, not a hardcoded SQL literal or a CTE constant.
It's a `TextVariable` in `spec.variables` (same `hide: "hideVariable"` pattern as `trace_ts`), with `current: {"text": "120", "value": "120"}`.
It's referenced unquoted (bare number, no `:singlequote` modifier) throughout panel-76's `rawSql`, including inside arithmetic like `(${trace_width_budget} - 3)` - Grafana's client-side template substitution resolves this to a plain number before the query ever reaches ClickHouse.
A Grafana variable was chosen over a CTE-level SQL constant specifically because it's tunable from the dashboard UI/URL without editing `rawSql` at all.
If you need to retune this width, edit the `trace_width_budget` variable's `current.text`/`current.value` in `spec.variables`, not any literal inside `rawSql`.
There should be no bare `120`/`117`/`90`/`87` width literal left in the SQL for this purpose; if you find one, it's a regression, not an intentional exception.

The Agent-spawn/tool-failure branch (`if(sr.status = 'failure', ..., 'Agent spawned: ...')`, the second branch handling the same tie=3 row set) now uses the same `${trace_width_budget}` arithmetic as the tool-call branch.
Its `description` field itself stays capped at 50 chars, not unified to the 90-char tool-arg cap, since it isn't one of the `tool_render` argument-preference branches (see the per-field-cap note above).

The general rule when adding or auditing any row-rendering branch that appends stats after content:

1. Give the content field an actual truncation cap (currently 90 chars is the standard for tool-arg-like fields) so its length is a true bound, not unbounded.
2. Compute pad target as `${trace_width_budget} - (indent width, 0 or 3 via if(agent_invocation_id != '', 3, 0))`.
3. Measure against raw, unescaped, already-truncated content length via `lengthUTF8`, not the HTML-escaped/tagged length.
4. Only pad when content already fits under the target; skip padding entirely on overflow, never truncate after the fact - same pattern as the tool-call branch's `repeat(' ', greatest(0, target - length))`.

The reply/reasoning row (`sr.tool_name = ''`, marked `└─ ●`) is deliberately exempt from all of this - it has no padding/width-budget logic at all, by design, and its text is capped separately at 1500 chars, not the 90-char tool-arg cap.

## Safe JSON-editing procedure: brace-matching splice

Never `Read` the whole dashboard file into context, and never hand-edit it with the `Edit` tool directly against the raw JSON text.
It's a large v2beta1-schema file; a naive text edit risks corrupting sibling panels.
Instead, do targeted brace-matching text surgery with a small Python script (`Write` it to your scratch area, run via `Bash`):

1. Read the file as plain text, not a `json.load` + `json.dump` round-trip - the file's key order is insertion-order, not sorted, so a full reserialize produces a huge unrelated diff even with `indent=2`.
2. Find the unique anchor `'"panel-76": {'`, then walk forward doing brace-depth counting (tracking whether you're inside a JSON string, so braces inside string values don't confuse the depth count) to find the matching closing `}` for that one element.
3. Build the new panel dict in Python, `json.dumps(panel, indent=2, ensure_ascii=False)`, re-indent it to match the surrounding file's 6-space panel-key indentation, and splice it in as a straight string replacement of the old block.
4. `json.load()` the result to confirm it's still valid JSON before considering the change done.

If you're touching both panel-76 and panel-77, or making more than one edit in the same task, do this whole read-splice-write cycle once per edit, immediately.
Never hold the file's content in memory across multiple edits and write it back once at the end, per `dashboards-expert`'s own atomic-unit rule for editing panel JSON.
A concurrent edit landing on the live file in that window would get silently discarded when a stale in-memory copy is written back.
If a mistake needs correcting mid-task, fix it forward with another scoped edit against the live file.
Never reset the working tree from any git ref (`git checkout`/`restore`/`reset`/`clean`, or the equivalent `git show :path` piped into the file) to "start clean" - stop and report the anomaly to the caller instead.
Only one edit against a given Dynamic Text panel should be in flight at a time.
This splice procedure assumes it's the sole writer at the moment it runs, the same atomic-unit discipline `dashboards-expert` already applies to every other panel type, just enforced here via brace-matching instead of a simple substring replace since the panel's `rawSql` is too large for that.

After writing, Grafana's file-based dashboard provisioner reloads every 30 seconds (`services/grafana/provisioning/dashboards/*.yml`, `updateIntervalSeconds: 30`).
Confirm the change actually landed by polling `curl -s http://localhost:3000/api/dashboards/uid/agents-overview` and grepping for a distinctive fragment of your new SQL/template, looping with a short sleep until it appears, rather than guessing a fixed wait or trusting the file write alone.

## Testing before you deploy

Test new/changed SQL against real data via `mcp__dev__query` before touching the panel JSON.
Pick a real `session_id` from the database (`SELECT session_id, count() FROM agent_events GROUP BY session_id ORDER BY max(timestamp) DESC LIMIT 5`) rather than inventing one.
Remember the validator quirks above when constructing the test query string.
The deployed SQL should use the real, unobfuscated literals - the obfuscation is purely a testing-tool workaround, never carry it into the panel itself.

## Open question: query_performance.json tagging

Whether Dynamic Text panels should actually be profiled the same generic way as every other tagged panel (3 timeseries + 1 table) once tagged is still open.
`panel-76` assembles a full session tree per row, an unusual shape for that template.
This is why `dashboards-expert` excludes Dynamic Text panels from tagging in its own `query_performance.json` sync workflow, on purpose, not as an oversight.
Don't resolve this yourself; flag it to the caller/user the first time it comes up in practice.
