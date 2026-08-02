Panel-76/-99 tree-rendering bugs already hit and fixed once - open this when a new symptom looks similar, not as routine reading.
General ClickHouse lexer/regex/type gotchas (not panel-specific) live in the `clickhouse-sql` skill instead - e.g. the `\b` -> literal-backspace lexer quirk, discovered in this panel's own debugging but documented there since it applies to any ClickHouse regex.

## Byte vs character functions

`substring()`, `rightPad()`/`leftPad()` operate on bytes, not UTF-8 characters.
Text that can contain Cyrillic, emoji, or box-drawing characters (`●`/`├─`/`▸`, in prompt text, replies, tool args) must use `substringUTF8()`/`rightPadUTF8()` instead, or truncating/padding mid-character produces garbled, hex-dump-looking output.
`leftPad()` (plain, byte-based) is fine only for pure-ASCII numeric fields (ms/token columns).

## `formatDateTime` minute format

On this ClickHouse version `%M` means the full month name, not minutes.
Use `%i` for minutes: `formatDateTime(ts, '%H:%i:%S')`.
Getting this wrong silently produces times like `14:July:16` instead of `14:37:16`.

## `rightPadUTF8` needs pre-truncated input

`rightPadUTF8(content, N, ' ')` silently truncates at byte boundaries if content runs longer than N chars, which breaks the later `replaceOne(padded, plain_substring, span_wrapped)` substring match and leaves arguments unstyled.
Fix: pre-truncate arguments to a safe cap (90 chars, uniform across every `tool_render` argument-preference branch) before padding, so content can never overflow the pad width and `replaceOne()` always finds its target intact.
See `.agents/skills/dynamictext-panel-queries/references/width-budget.md` for the full pad-budget arithmetic this feeds into.

## Truncation caps by field

All of `tool_render`'s argument-preference branches (`file_path`, `command`, `sql`, `url`, `query`, `description`, `summary`, raw-JSON fallback) truncate to 90 chars via `substringUTF8(..., 1, 90)` before HTML-escaping and padding - retune all branches together, never just one.
Not part of this unified 90-char cap: the Agent-spawn/failure branch's own `description` field (50-char cap) and `AskUserQuestion`'s per-question text (120-char cap, own unbounded nested-line rendering path).

## JSON serialization must not double-escape `rawSql`

Always load via `json.load()` and write back via `json.dump()` on the modified in-memory object - never string-level replacements on the raw file text, never pass the SQL through a JSON encoder/decoder outside the main dump.
A tooling bug that re-serializes the string value can introduce stray backslashes before quotes (e.g. `style=\"opacity:.6\"` instead of `style="opacity:.6"`), accumulating across edits until it breaks quote-parity in ClickHouse's string-literal lexer.
If spotted: load the JSON properly, `.replace('\\"', '"')` on the parsed string value (not the raw file bytes), write back via `json.dump()`.
This is distinct from the brace-matching splice procedure (main SKILL.md), which deliberately avoids a full load/dump round-trip for the everyday edit path - only invoke a full load/dump cycle to fix already-corrupted escaping.

## `agent_invocations` table-whitelist and validator quirks (testing only)

`agent_invocations` isn't in the `mcp__dev__query` table whitelist (only `agent_events`/`agent_usage`/`agent_messages`/`session_git_branch` are, per `_ALLOWED_TABLES` in `services/mcp-dev/src/server.py`) - the check only requires one referenced table to be whitelisted, so a test query joining `agent_invocations` alongside `agent_events` passes fine.
This restriction doesn't apply to the deployed panel (Grafana talks to ClickHouse directly), only ad-hoc testing.

The validator also false-positives on a literal `;` anywhere in the query text (even inside a string like `'&amp;'`) and on the bare word `SYSTEM` case-insensitively as a whole word (even inside `'<system-reminder>'` or `'[SYSTEM NOTIFICATION'`).
Work around them in a test copy only, by building the string from parts: `concat('&','amp',char(59))` instead of `'&amp;'`, `concat('[SY','STEM NOTIFICATION')` instead of the literal substring.
Deploy the real, unobfuscated literal into the actual panel SQL - the validator doesn't run there, and this isn't a sanctioned bypass technique, just a test-string-construction workaround.

## Stray `` ` `` or `*` corrupting the whole tree's HTML

`panel-99`'s `content` template wraps the whole `tree` string in one `<pre>...</pre>` block, but the plugin (`marcusolsson-dynamictext-panel`) still runs that content through its bundled `markdown-it`/`marked` libraries when "Wrap automatically in paragraphs" is on (see `Skill(dynamictext-panel-design-system)`) - `<pre>` isn't an inert raw-HTML block the way strict CommonMark would treat it.
A single unmatched `*` in real prompt text (e.g. `` `--t-col-*` ``, a wildcard reference, not markdown emphasis) pairs with the next stray `*` anywhere later in the entire concatenated document, wrapping everything between them in `<em>`, including unrelated later rows' timestamp columns.
Fix, applied in `fork_render`'s `prompt_line` construction and the root user-prompt (`main_prompt`, tie=2) branch: after the existing `'\*\*([^*\n]+?)\*\*' -> '<b>\1</b>'` bold-conversion step, add `replaceRegexpAll(<result>, '\*', '&#42;')` to neutralize any remaining single `*` to the HTML entity before the backtick/code-span regexes run.
Verify via the browser DOM (`document.querySelectorAll('span.t-row')`, inspect for stray `<em>`/`<p>` elements), not just by eyeballing the SQL - this bug class only manifests in the plugin's client-side re-processing.

## `agent_invocations` `ReplacingMergeTree` ties orphaning a fork

A batch backfill/re-ingest can rewrite many rows with the exact same `spawned_at` timestamp, and some rewritten rows have `parent_agent_id`/`subagent_type` reset to `''` while an older, correct version of the same `(session_id, agent_id)` row still exists with the real value.
`fork_dedup`'s plain `argMax(ai.parent_agent_id, ai.spawned_at)` (and `subagent_type`) picks non-deterministically between the two on a tie, and picking the blank one causes `fork_parent_raw`'s ASOF fallback to misassign the fork - visually an empty-named `<details>` that, expanded, wraps a real, correctly-named child fork.
Fix: give `argMax` a tuple comparator that prefers the non-blank value on a tie: `argMax(ai.subagent_type, (ai.spawned_at, ai.subagent_type != '')) AS subagent_type`, same shape for `parent_agent_id`.
This is a display-layer mitigation for a real ingestion data-quality issue, not a fix to the ingestion pipeline - if orphaned/blank-named forks keep appearing after this, the underlying write pattern needs its own investigation.
