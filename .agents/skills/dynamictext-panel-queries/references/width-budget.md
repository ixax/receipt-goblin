Pixel/char pad-budget arithmetic for panel-76's fixed-width row alignment - open when adding/retuning a row-rendering branch's padding, not for routine reading.

## Why padding needs a shared budget, not a flat constant

Fixed-width column alignment pads tool-call content out to the shared `${trace_width_budget}` total (120 top-level / 117 nested) so the right-hand stats (duration/tokens/cost) line up vertically across all nesting levels.
Tool-call arguments are pre-truncated to the uniform 90-char cap (see `.agents/skills/dynamictext-panel-queries/references/gotchas.md`) before padding - this is what prevents `rightPadUTF8` silently truncating at a byte boundary and breaking the later `replaceOne()` match.
Padding itself is applied as literal spaces via `repeat()` appended to the already-built HTML, not via `rightPadUTF8` on the HTML string directly - `rightPadUTF8` measuring against escaped (entity-inflated) content was the other half of the original bug.

The `agent_invocation_id != ''` nesting indent (`'   '`, 3 chars, prepended before `├─`/`└─` on a subagent's own rows) is extra width added on top of whatever comes after it, not carved out of an already-fixed content width.
Any branch padding to a target column must compute `(shared total-line budget) - (indent width)`, not use one flat constant regardless of depth, or nested rows end up wider overall than top-level rows and the stats columns drift right with depth.
The regular tool-call branch (`tool_render`/tie=3) pads to `(${trace_width_budget} - 3)` for nested rows and `${trace_width_budget}` for top-level - budget defaults to 120, indent is 3, so nested rows pad to 117.

## `${trace_width_budget}` variable

A hidden Grafana dashboard variable, not a hardcoded SQL literal or CTE constant: a `TextVariable` in `spec.variables` (same `hide: "hideVariable"` pattern as `trace_ts`), `current: {"text": "120", "value": "120"}`.
Referenced unquoted (bare number, no `:singlequote` modifier) throughout panel-76's `rawSql`, including inside arithmetic like `(${trace_width_budget} - 3)` - Grafana's client-side template substitution resolves it to a plain number before the query reaches ClickHouse.
Chosen over a CTE-level SQL constant specifically because it's tunable from the dashboard UI/URL without editing `rawSql`.
To retune, edit the `trace_width_budget` variable's `current.text`/`current.value` in `spec.variables`, not any literal inside `rawSql`.
No bare `120`/`117`/`90`/`87` width literal should remain in the SQL for this purpose - if you find one, it's a regression, not an intentional exception.

The Agent-spawn/tool-failure branch (`if(sr.status = 'failure', ..., 'Agent spawned: ...')`, the second branch handling the same tie=3 row set) uses the same `${trace_width_budget}` arithmetic as the tool-call branch.
Its own `description` field stays capped at 50 chars, not unified to the 90-char tool-arg cap, since it isn't one of the `tool_render` argument-preference branches.

## Adding or auditing a padded row-rendering branch

1. Give the content field an actual truncation cap (90 chars is the standard for tool-arg-like fields) so its length is a true bound, not unbounded.
2. Compute pad target as `${trace_width_budget} - (indent width, 0 or 3 via if(agent_invocation_id != '', 3, 0))`.
3. Measure against raw, unescaped, already-truncated content length via `lengthUTF8`, not the HTML-escaped/tagged length.
4. Only pad when content already fits under the target; skip padding entirely on overflow, never truncate after the fact - same pattern as `repeat(' ', greatest(0, target - length))`.

The reply/reasoning row (`sr.tool_name = ''`, marked `└─ ●`) is deliberately exempt from all of this - no padding/width-budget logic at all, by design, and its text is capped separately at 1500 chars, not the 90-char tool-arg cap.
