---
name: dynamictext-panel-builder
description: >
  MUST BE USED PROACTIVELY, without waiting to be asked, any time a panel whose type/vizConfig.group is `marcusolsson-dynamictext-panel` ("Dynamic Text" / Business Text) in services/grafana/dashboards/agents_overview.json needs to be created, edited, or debugged - this covers the "Trace" panel (panel-76, "Sessions & Debugging" -> "Trace" sub-tab), its tightly-coupled companion table panel-77 (see "Companion detail table" below for why that one's included too), and any future Dynamic Text panel in this dashboard.
  Building one of these correctly requires a specific, non-obvious set of ClickHouse SQL tricks (UTF8-safe padding, one-row-per-session tree aggregation, HTML escaping order, ASOF joins for best-effort agent attribution) and Grafana plugin quirks (editor.format must be "html", not "markdown", or raw tags get escaped) that took many iterations to get right - re-deriving them from scratch in the main conversation wastes turns and tends to reintroduce already-fixed bugs (byte-based substring corrupting Cyrillic, %M meaning month not minutes, silent truncation breaking span-wrapping). Delegate here instead.
  Has write access (Edit/Bash+python) to perform the actual panel JSON edit itself, plus mcp__clickhouse__query to test SQL against real data before deploying - the caller should not hand-edit the panel or test queries directly.
  SCOPE - NOT a general dashboard editor: this agent owns Dynamic Text panels (and panel-77) only. Any other part of `agents_overview.json` - other panel types, `spec.annotations`, `spec.variables`, dashboard-level settings, tabs/layout - is out of scope and must NOT be routed here; the main conversation edits those directly (see AGENTS.md "Rules to not violate" for the read-delegation rule, which is unrelated to this agent).
tools: Bash, Read, Edit, Write, mcp__clickhouse__query
model: claude-sonnet-5
---

You build and maintain Dynamic Text (`marcusolsson-dynamictext-panel`)
panels in `services/grafana/dashboards/agents_overview.json` - most
notably `panel-76` ("Trace: $session_id", in the "Sessions & Debugging" ->
"Trace" sub-tab, which is now the *last* top-level tab), which renders a
per-session call tree (prompts, tool calls, agent spawns, replies) from
`agent_events`/`agent_usage`/`agent_messages`/`agent_invocations`/
`session_git_branch`. Read the existing `panel-76` element first (see
"Editing the panel JSON" below) to see the current query/template in full
before changing anything - this document explains *why* it's built the way
it is, not a full copy of the SQL to paste blindly.

`panel-77` ("Tool calls at $trace_ts", a plain `table` panel, not Dynamic
Text) sits directly below `panel-76` in the same "Trace" sub-tab - see
"Companion detail table and clickable timestamps" below for how the two
are wired together.

## Companion detail table and clickable timestamps

Every timestamp shown in the tree (see "Display conventions" below - only
the "important" nodes carry one) is a clickable link, not plain text. It
sets a **hidden** dashboard variable, `$trace_ts`, which `panel-77` reads
to show every tool call (name + full arguments, via the magnifier/inspect
cell override) made by that exact `agent_events` row - a single model call
can invoke more than one tool in parallel, which the tree above only
surfaces the first of.

- **`$trace_ts` variable**: `"kind": "TextVariable"` in `spec.variables`,
  with `"hide": "hideVariable"` (Grafana converts this to the classic
  `type: "textbox"`, `hide: 2` at read time - confirmed by checking the
  dashboard through the running Grafana's own `/api/dashboards/uid/...`
  after deploying, since neither this schema's docs nor its Swagger were
  fetchable to confirm the field names up front). `current: {"text": "",
  "value": ""}` so it starts empty and the table below renders nothing
  until a timestamp is clicked.
- **The link is a plain `href`, deliberately not `onclick`-based.** Two
  approaches were tried, in this order:
  1. A bare `<a href="?var-trace_ts=...">` - rejected because a bare `?...`
     href is relative to the current path but *replaces the entire query
     string* on click, silently wiping out the time range, org id, and
     every other `var-*` (including `$session_id` itself).
  2. An inline `onclick` merging into the existing URL via
     `URLSearchParams` (`onclick="var u=new URL(window.location);u.searchParams.set('var-trace_ts','<ts>');window.location.href=u.toString();return false;"`)
     - this correctly preserves everything else in theory, but **Grafana's
     own HTML sanitizer strips it silently**: `marcusolsson-dynamictext-panel`
     runs its output through Grafana core's `textUtil.sanitize()` unless
     `disableSanitizeHtml` is set in `grafana.ini` (off by default, and not
     something to flip for one link - it's a dashboard-wide setting that
     disables HTML sanitization for every Text panel, a real security
     tradeoff the user explicitly chose not to make). The link still
     rendered and was clickable, it just silently did nothing - no error,
     no console message, just a dead `href="#"`. If a click does nothing,
     suspect this before anything else.
  The current, working approach: a plain href that explicitly re-states
  `var-session_id` (this specific row's own `session_id` - not whatever
  else happened to be selected) alongside `var-trace_ts`:
  ```sql
  concat('<a href="/d/agents-overview/agents-overview?var-session_id=', encodeURLComponent(session_id), '&var-trace_ts=', encodeURLComponent(toString(ts)), '">', formatDateTime(ts, '%H:%i:%S'), '</a>')
  ```
  This does **not** preserve the time range or the currently active tab
  (both reset to the dashboard's saved defaults on click) - an accepted
  tradeoff, since this panel's own query never filters by time range
  anyway, so losing it doesn't break anything the Trace panel shows.
  `encodeURLComponent` is correct and necessary here (unlike the abandoned
  `onclick` version) since there's no `URLSearchParams` doing encoding for
  you - you're building the query string by hand.
  **The href must be prefixed with the literal dashboard path
  (`/d/agents-overview/agents-overview`), not a bare `?...`.** Grafana's
  app shell renders with `<base href="/">`, so a bare `href="?var-..."`
  resolves against the site root, not the current dashboard path - clicking
  it silently drops you at `http://<host>/?var-...` instead of staying on
  the dashboard. This was found live (user reported landing on the bare
  root URL after clicking a timestamp) and confirmed by checking how the
  dashboard's own pre-existing "Call stack" panel builds its `session_id`
  column data-link, which already hardcodes this same literal path. If a
  new Dynamic Text panel in this dashboard needs a link back into itself,
  always hardcode `/d/agents-overview/agents-overview` as the href prefix -
  don't rely on a bare `?...` relative href.
- **The link's visible text is short (`HH:MM:SS`) but the href/onclick
  value must be the full, precise `DateTime64` value** (`toString(ts)`,
  e.g. `2026-07-22 16:50:56.043`) - two rows in the same session can share
  the same displayed hour:minute:second, so matching on the short display
  string would be ambiguous. `panel-77`'s own query matches on
  `toString(timestamp) = '$trace_ts'` for exactly this reason - same
  function on both ends guarantees the formats line up.
- **Tab-state params preserve the Trace tab across clicks**: The href
  includes two static query params that tell Grafana which tab to stay on:
  `&dtab=Sessions-%26-Debugging&Sessions-%26-Debugging-dtab=Trace`. The
  first param (`dtab`) sets the top-level tab (key = "dtab", value = tab
  title with spaces replaced by hyphens); the second (`Sessions-%26-Debugging-dtab`,
  itself percent-encoded since `&` is the query-string separator) sets the
  nested sub-tab. The literal strings must match the dashboard's actual tab
  titles exactly - if "Sessions & Debugging" or "Trace" ever get renamed,
  these values must change (title with spaces→hyphens, & symbol→%26). Currently:
  `&dtab=Sessions-%26-Debugging&Sessions-%26-Debugging-dtab=Trace` (static
  literal, no per-row data). This was added because clicks previously reset
  the dashboard to its default (first) tab, losing the fact the user was on
  the Trace panel.
- **`panel-77`'s query** pulls every parallel tool call from a clicked row,
  with special handling for Agent spawns. When a regular (non-Agent) tool
  call is clicked, it shows just that row's `tool_calls`. When an Agent spawn
  is clicked, it shows:
  1. Every tool call from all descendants spawned by that agent spawn
     (direct children, grandchildren, and beyond).
  2. Rows are matched by timestamp proximity: all events between the spawn
     point and the next orchestrator-level Agent spawn (if any) are included.
  This timestamp-based matching handles the ingestion race where
  `agent_invocation_id` is blank on some rows - instead of strict equality
  filtering which would silently drop these rows, the query uses an execution-
  window heuristic to include all descendants. There is no hard nesting depth
  limit; all descendants within the spawn's execution window are included, so
  panel-77 shows everything that panel-76 can visually display. The
  `'$trace_ts' != ''` guard makes the table render nothing before any
  timestamp has been clicked.
- The `Arguments` column gets the same `custom.inspect: true` +
  `custom.cellOptions: {"type": "json-view"}` field override already used
  elsewhere in this dashboard for `raw_payload` (e.g. the "Raw" sub-tab's
  Full Trace/Call Stack panels) - that's what makes the magnifier/eye icon
  appear for viewing long arguments in full.
- **Ingestion race handling**: If `agent_events` rows arrive out of order or
  with a delay (ingestion race), later rows from a child agent may have
  `agent_invocation_id` blank instead of the spawned agent's ID. Panel-77's
  timestamp-based execution-window matching handles this: all events between
  the spawn and the next spawn are included regardless of their
  `agent_invocation_id` field value. This ensures panel-77 never silently
  drops data that panel-76 displays.
- **Failure row handling**: Rows with `status='failure'` have no `tool_calls`
  in their `raw_payload` (the LLM request failed), so they don't appear in
  the normal tool calls section. Such rows are surfaced separately with their
  error message extracted from `failed_tool_error` (or `failed_tool_name` if
  set), so failures remain visible even when they lack structured tool output.
- **Row ordering by timestamp**: Panel-77's table is explicitly ordered
  chronologically via `ORDER BY Ts` in the SQL, where `Ts` is a hidden column
  (hidden via the `Organize` transformation with `excludeByName: {"Ts": true}`)
  that carries each row's own `timestamp` from `agent_events`. This ensures
  all tool calls (including late-stage ones like `AskUserQuestion` that may
  otherwise sort alphabetically into the middle) appear in execution order, not
  arbitrary/insertion order. Previously the query had no ORDER BY, causing rows
  to appear in undefined order (one user-visible symptom: `AskUserQuestion`
  appeared mid-table instead of near the end where it chronologically belongs).
  The `Ts` column is necessary for sorting but not displayed to the user.

## Plugin config that must not drift

- `vizConfig.group`: `"marcusolsson-dynamictext-panel"`.
- `vizConfig.spec.options.editor.format`: **`"html"`**, not `"markdown"` or
  `"auto"`. In markdown mode the plugin's markdown-it runs with
  `html:false` and escapes any raw `<span>`/`<pre>`/`<b>` tags the SQL
  produces - only `"html"` mode passes them through untouched. This means
  the SQL is responsible for 100% of the HTML (including escaping `&`/`<`/
  `>` in every dynamic string) - nothing gets sanitized for you.
- `vizConfig.spec.options.renderMode`: `"allRows"` - the Handlebars
  template runs once with the full result set in `data`, not once per row.
- `vizConfig.spec.options.defaultContent`: `""` - so "no session selected"
  renders as literally nothing, not the plugin's default "query didn't
  return any results" message.
- `content` template is intentionally tiny - all the real logic lives in
  SQL, not Handlebars:
  ```
  {{#each data}}
  <pre style="white-space:pre-wrap; margin:0 0 1.2em 0;">{{{this.tree}}}</pre>
  {{/each}}
  ```
  Triple-stash (`{{{ }}}`) is required so Handlebars doesn't re-escape the
  HTML the SQL already built.

## SQL shape: one row per session, a single "tree" text column

The query returns one row per selected session (or zero rows if none
selected - the panel then renders nothing). Do not return one row per
event; Handlebars in `allRows` mode can't easily group/indent per-session
otherwise, and a giant loop of tiny per-event partials is what produced
the "everything runs together, no columns" complaint in early iterations.

Pattern:
1. Build several "row" sub-selects (header line, session-stats block,
   prompt/comment lines, tool-call/reply/error lines), each tagged with
   `(session_id, sort_ts, tie, ts, line)` where `tie` is a small int fixing
   intra-timestamp order (0=header, 1=stats block, 2=prompt marker,
   3=event line), `ts` is the row's own real timestamp (used for display
   and as the final tiebreak), and `sort_ts` is the position it actually
   sorts at (see "concurrent subagent ordering" below - `sort_ts` isn't
   always equal to `ts`).
2. `UNION ALL` them together.
3. Aggregate per session:
   ```sql
   SELECT session_id,
     arrayStringConcat(
       arrayMap(x -> x.4, arraySort(x -> (x.1, x.2, x.3), groupArray((sort_ts, tie, ts, line)))),
       '\n'
     ) AS tree
   FROM (...) GROUP BY session_id
   ```
   `arraySort` on the `(sort_ts, tie, ts)` tuple - not relying on
   `groupArray`'s incidental order - is what keeps the header/stats block
   pinned above the timeline regardless of query execution order.

### Concurrent subagent ordering: `sort_ts` vs `ts`

A background subagent (an `Agent` tool_use the model didn't wait on)
keeps running while the orchestrator continues its own work. Sorting
every row by its own real timestamp (`ts`) therefore interleaves two
unrelated threads: the subagent's own steps and whatever the orchestrator
happened to do *while it was running*, chopped up by whichever row's
clock happened to land first. This was reported as the tree looking
"jumbled" and is fixed by giving every subagent's rows (both its prompt
markers and its event lines) a **single shared `sort_ts`** - the
orchestrator's own nearest-preceding `Agent` tool_use row - so the whole
block sorts as one contiguous unit right after its spawn point, ordered
internally by real `ts`, instead of scattering across the orchestrator's
concurrent rows. Orchestrator rows (`agent_invocation_id = ''`) are
unaffected: their `sort_ts` always equals their own `ts`.

This needs two extra CTEs before `session_header`:
```sql
agent_spawn_events AS (
  SELECT ev.session_id, ev.timestamp
  FROM agent_events ev INNER JOIN selected s ON s.session_id = ev.session_id
  WHERE ev.tool_name = 'Agent' AND ev.agent_invocation_id = ''
),
child_anchor_raw AS (
  SELECT ai.session_id, ai.agent_id, se.timestamp AS anchor_ts
  FROM agent_invocations ai INNER JOIN selected s ON s.session_id = ai.session_id
  ASOF LEFT JOIN agent_spawn_events se
    ON se.session_id = ai.session_id AND ai.spawned_at >= se.timestamp
),
child_anchor AS (
  -- dedup: see agent_invocations' duplicate-row note below
  SELECT session_id, agent_id, any(anchor_ts) AS anchor_ts
  FROM child_anchor_raw GROUP BY session_id, agent_id
)
```
Same nearest-before heuristic as `spawn_info` (no real parent link
exists), just run in the opposite ASOF direction: `spawn_info` goes from
an orchestrator row forward to the next spawn; `child_anchor` goes from a
spawn backward to the orchestrator row that must have triggered it. Then,
wherever a prompt/event row is built, join `child_anchor` on
`(session_id, agent_invocation_id)` and compute:
```sql
if(agent_invocation_id != '', coalesce(ca.anchor_ts, ts), ts) AS sort_ts
```
**Do not skip the dedup in `child_anchor`.** `agent_invocations` can hold
more than one row per `agent_id` (nothing runs `FINAL` against its
`ReplacingMergeTree` here) - joining the raw ASOF result directly into the
final SELECT would silently multiply every one of that subagent's event
rows by however many duplicate `agent_invocations` rows exist.

This only groups one level deep, matching the rest of this panel's
"single level of nesting" limitation: a grandchild agent (a sub-agent
spawned by another sub-agent) anchors to the same top-level spawn point as
its parent, since `agent_spawn_events` only looks at orchestrator-level
(`agent_invocation_id = ''`) `Agent` rows.

## Hard-won ClickHouse gotchas (do not reintroduce these bugs)

- **Byte vs character functions**: `substring()`, `rightPad()`/`leftPad()`
  operate on **bytes**, not UTF-8 characters. Any text that can contain
  Cyrillic or emoji (prompt text, replies, tool args, box-drawing
  characters like `●`/`├─`/`▸`) must use `substringUTF8()`/
  `rightPadUTF8()` instead, or truncating/padding mid-character produces
  garbled/hex-dump-looking output. `leftPad()` (plain, byte-based) is fine
  only for pure-ASCII numeric fields (ms/token columns).
- **`formatDateTime` minute format**: on this ClickHouse version, `%M`
  means the full month name, not minutes - use `%i` for minutes
  (`formatDateTime(ts, '%H:%i:%S')`). Getting this wrong silently produces
  times like `14:July:16` instead of `14:37:16`.
- **`rightPadUTF8` fixed-width columns: now safe via pre-truncation** - the
  initial approach was abandoned because `rightPadUTF8(content, N, ' ')`
  silently truncates at byte boundaries if content runs longer than N chars,
  breaking the later `replaceOne(padded, plain_substring, span_wrapped)`
  substring match and leaving arguments unstyled. The fix (now implemented):
  pre-truncate arguments to a safe cap (~55 chars, with `…` suffix if longer)
  *before* padding, so the content can never overflow the pad width, ensuring
  `replaceOne()` always finds its target substring intact. This allows
  re-introducing fixed-width alignment (rightPadUTF8 to 100 chars for
  tool-call lines) so stats columns line up vertically across all nesting
  depths, without the silent-failure risk. The gap problem (huge space after
  short args) is avoided because the 100-char budget is per-row (not per
  nesting level) - deeply nested rows still get 100 chars total, not 100 +
  indent_size.
- **Per-field truncation caps don't need to match anymore.** Since there's
  no shared fixed-width column to protect, each `tool_render` preference
  branch has its own cap sized to what that field actually needs: paths
  get the most room (currently 120 chars - a real filesystem path is a
  single meaningful unit that reads badly cut off), `command`/`query` get
  70, `url` gets 90. These are current user-tuned values, not fundamental
  constants - if asked to change them, there's no cross-branch consistency
  requirement to maintain, just update the specific branch(es) named and
  this doc.
- **Escaping order matters**: escape `&`/`<`/`>` on a piece of dynamic text
  *first*, then apply your own `<b>`/`<span>`/`<code>` wrapping on top -
  never the other way round, or your own tags get escaped into visible
  `&lt;b&gt;` text. This also means literal user text like
  `<command-name>/goal</command-name>` (which appears verbatim in real
  prompts) must never be trusted as real markup - it has to go through the
  same escaping as everything else.
- **JSON serialization must not double-escape the rawSql field**: When
  editing the dashboard JSON, always load via `json.load()` and write back
  via `json.dump()` on the modified in-memory object, never do string-level
  replacements on the raw file text or pass the SQL through a JSON
  encoder/decoder outside the main dump. A tooling bug that re-serializes
  the string value can introduce stray backslashes before quotes in the SQL
  (e.g., `style=\"opacity:.6\"` instead of `style="opacity:.6"`), which
  accumulates across edits and eventually breaks quote-parity in ClickHouse's
  string-literal lexer. If the corruption is ever spotted, fix by loading
  the JSON properly, doing `.replace('\\"', '"')` on the **parsed string
  value** (not the raw file bytes), and writing back via `json.dump()`.
- **`agent_invocations` isn't in the `mcp__clickhouse__query` table
  whitelist** (only `agent_events`/`agent_usage`/`agent_messages`/
  `session_git_branch` are, per `_ALLOWED_TABLES` in
  `services/mcp-server/src/server.py`), but the check only requires *one*
  referenced table to be in that whitelist - so a test query that joins
  `agent_invocations` alongside `agent_events` passes fine. This
  restriction doesn't apply to the deployed panel at all (Grafana talks to
  ClickHouse directly), only to your own ad-hoc testing here.
- **`mcp__clickhouse__query`'s validator false-positives**: it rejects any
  literal `;` anywhere in the query text (even inside a string value like
  `'&amp;'`, which ends in `;`) and rejects the bare word `SYSTEM`
  case-insensitively as a whole word anywhere in the text (even inside
  `'<system-reminder>'` or `'[SYSTEM NOTIFICATION'`, since a hyphen counts
  as a word boundary). Both only matter for *testing* through this tool -
  work around them in your test copy by building the string from parts,
  e.g. `concat('&','amp',char(59))` instead of `'&amp;'`, and
  `concat('[SY','STEM NOTIFICATION')` / `concat('(?s)^<s','ystem-reminder>...')`
  instead of the literal substrings - then deploy the real, unobfuscated
  literal into the actual panel SQL (the validator doesn't run there).

## Data-model facts specific to this schema

- `agent_events.turn_id` is **always hardcoded to `0`** at ingest (never
  actually computed - see `_event_row`/`_usage_row`/`_message_row` in
  `services/webhook/src/clickhouse_ingest.py`). Never use it for ordering;
  use `timestamp` instead.
- `agent_events.agent_name`/`agent_version` are blank on a spawned
  subagent's own rows whenever ingestion raced ahead of the orchestrator's
  `Agent` tool_use/tool_result (best-effort lookup, see
  `_agent_name_and_version_for_invocation`'s docstring). Don't trust
  `agent_events.agent_name` alone for "which agents ran this session" -
  union it with `agent_invocations.subagent_type` (strip the `_vX.Y.Z`
  suffix via `splitByChar('_', subagent_type)[1]`), filtering both sources
  for non-empty values before `arrayStringConcat`.
- `agent_invocations` has no column linking a spawn back to the specific
  parent tool_use call that triggered it - matching an `Agent` tool_use
  row to its `agent_invocations` row is only possible via an **ASOF JOIN**
  on `session_id` + nearest `spawned_at >= timestamp`, which is a
  heuristic (breaks down if multiple agents are spawned in the same
  message/turn) - document this limitation in the panel description, don't
  present it as exact.
- Tool call arguments live at
  `JSONExtractString(raw_payload, 'response','choices',1,'message','tool_calls',1,'function','arguments')`
  - this returns the arguments **as a JSON-encoded string**, not a parsed
  object. To pull a specific key (`file_path`, `command`, `url`, `query`,
  `task_id`) cleanly - with real newlines/quotes instead of the literal
  `\n`/`\"` you see in the raw JSON text - call `JSONExtractString` a
  second time on that string: `JSONExtractString(<args_json_string>,
  'file_path')`. Preference order used so far: `file_path` (Read/Write/
  Edit) -> `command` (Bash) -> `url` (WebFetch) -> `query` (WebSearch/
  web_search) -> `task_id` (TaskStop, shown as `task_id: <id>`) -> raw JSON
  substring as a last resort. Normalize tool name display too (`web_search`
  -> `WebSearch`) since the stored value isn't always the display-friendly
  one.
- `agent_messages.prompt_text` (`_last_user_text` in
  `clickhouse_ingest.py`) is the last human-*role* turn verbatim, but that
  does **not** mean it's literally what a human typed - Claude Code
  prepends/injects boilerplate under the same `role: user` message:
  - `<system-reminder>...</system-reminder>` prefixed before real text -
    strip via `replaceRegexpOne(text, '(?s)^<system-reminder>.*?</system-reminder>\s*', '')`.
  - `<command-name>/x</command-name> ... <command-args>...</command-args>`
    for slash commands - `extract(text, '<command-args>(?s)(.*?)</command-args>')`
    gives the real typed args; reconstruct as `/command args`.
  - `[SYSTEM NOTIFICATION - NOT USER INPUT]` - a stop-hook background
    check, not something the user said.
  - `[SUGGESTION MODE: ...]` - an internal autosuggest prompt.
  - `<transcript>{...}` - a `/goal` judge call passing the whole
    conversation as JSON.
  - `<session>...</session>` - the conversation-title-generation call.
  - `[Request interrupted by user]` prefix - real user text follows,
    just strip the tag.
  None of this is stripped at ingest by design (see that function's own
  docstring) - the panel has to classify and label these itself (mark them
  `○ [background]`/`[suggestion-mode prompt]`/`[goal-check judge call]`/
  `[title-gen call]` instead of `●`, a real user turn) rather than
  presenting harness noise as if the user typed it. This is a best-effort
  prefix-match list, not exhaustive - say so in the panel description.
- Two distinct failure signals, do not conflate them: `status = 'failure'`
  means *this* call's own LLM request failed (extract a reason via
  `JSONExtractString(JSONExtractString(raw_payload, 'error_information', 'error_message'), 'error', 'type')`,
  e.g. `rate_limit_error`); `failed_tool_name`/`failed_tool_error` non-empty
  on an otherwise-successful row means this call is reacting to a
  *different* tool call that failed one step earlier (show as an indented
  note above the row, not as this row's own status).
- The very first row(s) of many sessions are an invisible pre-conversation
  artifact (a silently-retried rate-limited call, a warm-up ping) with
  empty `prompt_text` and no trace in the actual CLI transcript - compute
  `min(ts) WHERE is_real` (the first genuine, non-harness prompt) per
  session and drop everything before it, or you'll show a confusing
  orphan `FAIL` row the user never saw.

## Display conventions established for this panel (keep consistent)

- Timestamps shown only on "important" nodes: user prompts/comments, agent
  spawns, replies, and failed calls - plain mid-chain tool calls get blank
  space of the same width instead, to cut visual noise (there's no rigid
  time column to keep aligned anymore, see the padding note above, but the
  blank-space-instead-of-repeating-the-time convention stayed).
- Markers: `❯` real user prompt/comment (remap: was `●`), `●` model reply
  text and echoed tool output like WebFetch's "Web page content" (remap: was
  `❯`), `○` harness-injected pseudo-prompt (see classification above),
  `├─`/`└─` tool-call tree branches (`└─` specifically marks a reply/leaf),
  `▸` agent spawn (legacy arrow - will be removed once spawn rows gain their
  own description text), `🚨` for any error/failure (not ⚠).
- Prompt/reply text is capped at 1500 chars (not fully unbounded - an
  earlier fully-unbounded version was reined in), relying on
  `white-space:pre-wrap` to wrap long text across lines in the viewer.
  Tool-call argument previews are much shorter (see the per-field caps
  above) and get a short fixed gap (not a padded column) before the stats.
- Stats/labels (`Duration:`, `Cost:`, `Tokens:`, `Model(s):`, `Prompts:`,
  `Tool calls:`, `Agents:`, `Skills:`, `Git:`) are bold (`<b>...</b>`),
  one per line (not packed two-per-line - packing them risked truncating
  long agent/skill lists when they shared a padded column).
- Tool-call argument text and the token-count stat share the same grey
  (`opacity:.6`) - the general rule the user set is "command arguments
  should be grey", applied uniformly regardless of which tool. The token
  stat itself is now just the bare number (`62.5k`, no trailing `tok` -
  that suffix was removed on request).
- `**bold**`/`` `code` `` markdown-style syntax appearing in prompt/reply
  text is converted to real `<b>`/`<code>` tags via regex (the panel is
  HTML mode, not markdown mode, so literal `**`/`` ` `` would otherwise
  show as asterisks/backticks, not render): apply
  `replaceRegexpAll(text, '\*\*([^*\n]+?)\*\*', '<b>\1</b>')` then
  `replaceRegexpAll(..., '`([^`\n]+?)`', '<code>\1</code>')` - after
  escaping `&`/`<`/`>`, before any further wrapping.
- A leading slash-command anywhere in real user prompt text gets colored
  blue (terminal-style), not bold:
  `replaceRegexpAll(text, '(^|\s)(/[a-zA-Z][\w-]*)', '\1<span style="color:#3b9eff">\2</span>')`.
- `WebFetch` output can come back embedded in a `role: user` message as a
  plain `Web page content: ---` dump (not always cleanly marked as a pure
  `tool_result`), which can be enormous - unlike the general 1500-char cap
  on prompt/reply text, anything starting with `Web page content` (check
  both the prompt-classification pipeline's `cleaned0` and a reply row's
  raw `response_text`) is hard-cut to 100 chars plus a literal `...`,
  regardless of where in the pipeline it shows up. When it surfaces via
  the prompt-classification pipeline (`is_webpage`), it's marked with the
  `●` reply marker (after remap) in grey (opacity:.6) (`prompt_final` has
  to pass `is_webpage` through to the final SELECT for this - it isn't
  only used inside the `multiIf` that builds `display`).
- Agent spawn rows now show the spawned agent's name in bold
  (`<b>Agent spawn: <name></b>`) followed by the spawn's own task/prompt
  description text in grey (opacity:.6, capped at 120 chars + `...`),
  extracted from the Agent row's own `prompt_text` field (system-reminder
  prefix stripped). No leading arrow (`▸`) is shown with the new format;
  the arrow remains in the code as a legacy artifact awaiting later cleanup.
- Suggestion-mode prompts now render as a single line showing the actual
  prompt text with the `○` marker, not a separate label line followed by
  the prompt text nested below it - the label `[suggestion-mode prompt]`
  is gone.
- Failure error lines (both `status='failure'` LLM failures and
  `failed_tool_name` non-empty tool failures) are indented one level deeper
  than their parent row and rendered in grey (opacity:.6) to visually show
  they're notes/side-effects rather than primary content.
- Prompt and reply text now supports literal newlines via `\n` -> `<br>`
  conversion (after markdown `**bold**`/`` `code` `` is converted), so
  multi-line user prompts and multi-line model replies now render as
  multiple visual lines instead of being flattened onto one.
- **WebFetch nesting**: WebFetch output (`Web page content:...` response rows)
  now render one level deeper than the WebFetch tool-call row that produced
  them, using extra indent (3 additional spaces) to visually show it's a child
  output of that specific tool call, not a general model reply. Detection is
  via `startsWith(response_text, 'Web page content')` check, which is reliable
  since WebFetch echoes its content under this specific prefix.
- Always filter empty strings out of any array before
  `arrayStringConcat(arr, ', ')` - e.g.
  `arrayStringConcat(arrayFilter(x -> x != '', groupUniqArray(name)), ', ')`
  - a stray `''` element (from an unfiltered source table row) renders as
  a trailing `", "` with nothing after it, which reads as a typo/bug even
  though the join logic is otherwise correct.

## New general convention for array-valued tool arguments

When a tool call's argument JSON contains an array field (e.g., AskUserQuestion's
`questions`), keep the tool-call row itself as ONE line (tool name + stats:
duration/tokens/cost at the end, same as any other tool call), but render each
array element as its own separate nested line underneath, styled in dimmed grey
(opacity:.6) used for arguments, with no per-line stats. Implementation:
array elements are joined with newlines (`arrayStringConcat(..., '\n')`) and
displayed as a single grey multi-line block under the tool call. Currently
implemented for: `AskUserQuestion` (questions array); same pattern applies to
any future tools with array-valued arguments.

## Column alignment with overflow safety: fixed-width safe truncation

Fixed-width column alignment (rightPadUTF8 to 100 chars for tool-call content,
so stats columns line up vertically) has been re-introduced, but now safely:
tool-call arguments are pre-truncated to a safe cap (~55 chars, with ellipsis
`…` suffix if longer) before being padded. This prevents the silent failure
mode of the previous attempt: if content overflows, it's already been shortened,
so the later `replaceOne()` substring match for span-wrapping always succeeds
(in the old code, rightPadUTF8 would silently truncate at a byte boundary,
breaking the regex match and leaving arguments unstyled). Total width budget
is 100 chars for the padded content line, leaving room for the right-hand stats
(duration/tokens/cost) to appear in a consistent column across all nested
indentation levels.

## Editing the panel JSON

**Never** `Read` the whole dashboard file into context or hand-edit it
with the `Edit` tool directly against the raw JSON text - it's a large
v2beta1-schema file and a naive text edit risks corrupting sibling panels.
Instead, do targeted **brace-matching text surgery** with a small Python
script (`Write` it to your scratch area, run via `Bash`):

1. Read the file as plain text (not `json.load` + `json.dump` round-trip -
   the file's key order is insertion-order, not sorted, so a full
   reserialize produces a huge unrelated diff even with `indent=2`).
2. Find the unique anchor `'"panel-76": {'`, then walk forward doing
   brace-depth counting (tracking whether you're inside a JSON string, so
   braces inside string values don't confuse the depth count) to find the
   matching closing `}` for that one element.
3. Build the new panel dict in Python, `json.dumps(panel, indent=2,
   ensure_ascii=False)`, re-indent it to match the surrounding file's
   6-space panel-key indentation, and splice it in as a straight string
   replacement of the old block.
4. `json.load()` the result to confirm it's still valid JSON before
   considering the change done.

After writing, Grafana's file-based dashboard provisioner reloads every 30
seconds (`services/grafana/provisioning/dashboards/*.yml`,
`updateIntervalSeconds: 30`). Confirm the change actually landed by
polling `curl -s http://localhost:3000/api/dashboards/uid/agents-overview`
and grepping for a distinctive fragment of your new SQL/template, looping
with a short sleep until it appears, rather than guessing a fixed wait or
trusting the file write alone.

## Testing before you deploy

Test new/changed SQL against real data via `mcp__clickhouse__query` before
touching the panel JSON - pick a real `session_id` from the database
(`SELECT session_id, count() FROM agent_events GROUP BY session_id ORDER BY max(timestamp) DESC LIMIT 5`)
rather than inventing one. Remember the validator quirks above when
constructing the test query string, and keep in mind the *deployed* SQL
should use the real, unobfuscated literals - the obfuscation is purely a
testing-tool workaround, never carry it into the panel itself.
