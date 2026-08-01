---
name: dynamictext-panel-design-system
description: >
  Reference for the `t-`-prefixed CSS design system (column widths, opacities, colors, indent/depth
  math) used by Dynamic Text panels in services/grafana/dashboards/agents_overview.json.
  Live in panel 99 ("Fork tree")'s `<style>` block; panel 76 ("Trace") hasn't adopted it yet.
  Owns the reusable pure-CSS asset at assets/style.css.
  Also documents panel 76's current pre-migration inline styling: markers, colors/opacities,
  markdown-to-HTML conversion, slash-command highlighting.
  TRIGGER - read only when the current dashboards-expert task is to add/update a Dynamic Text
  panel's CSS, or rewrite/regenerate row markup using `t-` classes or inline styling.
  SKIP for a pure data/SQL change, or any markup change that doesn't touch styling.
  <version>1.1.0</version>
---

# dynamictext-panel-design-system

Design-system reference for `dashboards-expert`, read on demand - see this skill's own
description for the exact trigger.

Describes the `t-`-prefixed variables/classes shipped in `assets/style.css`, currently live in
panel 99 ("Fork tree")'s `<style>` block.
Panel 76 ("Trace") has not adopted this system yet and still uses per-call-site inline
`style="..."` attributes.
Check a panel's actual `rawSql` for an existing `<style>` block before assuming either panel's
current state - don't infer from this doc alone.

This doc supersedes `plans/fork-tree-css-design-system.md`, the original design proposal - values
below reflect what actually shipped, not the draft.

## Variables (`:root`)

- `--t-indent: 1.2em` - one nesting level's margin/padding step, also the code-block left pad.
- `--t-op-duration: .5`, `--t-op-tokens: .6`, `--t-op-cost: .7`, `--t-op-model: .6`,
  `--t-op-muted: .6` - opacities, kept as `opacity` (not fixed grays) so they stay theme-adaptive.
- `--t-color-code: #8ab8ff` - single- and triple-backtick code, identical color for both.
- `--t-color-arrow: #e0af02` - the user-prompt `❯` marker.
- `--t-color-border: rgba(255, 255, 255, .25)` - tree-wrapper left border.
- `--t-col-timestamp: 60px`, `--t-col-wide-title: 470px`, `--t-col-title: 230px`,
  `--t-col-model: 230px`, `--t-col-duration: 70px`, `--t-col-tokens: 70px`, `--t-col-cost: 70px` -
  column widths, px, first-draft measurements (not yet corrected against a real rendered browser
  width).
- `--t-depth-step: calc(var(--t-indent) * 2 + 1px)` - derived, not independently chosen.
  Each ancestor `.t-tree-wrapper` contributes `margin-left` + `padding-left` (2x `--t-indent`) plus
  its 1px `border-left`, so the depth-based width shrink must include that same `+ 1px` per level
  or the columns drift right with depth.
  Never hardcode a shrink amount separately from this variable.

## Classes

- `.t-user-prompt::before` - injects the `❯ ` marker (glyph `\2771`) in `--t-color-arrow`, with
  `margin-right: 0.3em`.
  Applied to the initial user-prompt `<span>` instead of hand-writing the arrow inline.
- `.t-col-wide-title` - depth-aware container spanning name + model combined; width shrinks by
  `--t-depth * --t-depth-step`.
  `--t-depth` is set inline per row (e.g. `style="--t-depth: 2"`, defaulting to 0 when absent).
- `.t-col-title` - half of `.t-col-wide-title`'s width, bold.
  Carries a `title="HH:MM:SS"` tooltip attribute showing that row's own timestamp on hover - added
  after the visible timestamp column shipped, since the name column has room for it and it's a
  convenient secondary lookup.
- `.t-col-model` - the other half of `.t-col-wide-title`'s width, dimmed via `--t-op-model`.
- `.t-col-duration`, `.t-col-tokens`, `.t-col-cost` - fixed-width, right-aligned, each with its own
  opacity variable.
  Cost renders via `round(cost, 2)` (2 decimal places, not 4 - an earlier draft assumed 4).
- `.t-col-timestamp` - fixed-width (60px / 8ch, e.g. `11:28:47`), dimmed via `--t-op-muted`.
  A real shipped column (not just internal duration math like the original design draft assumed) -
  rendered with an inline `position:relative; left:calc(-1 * var(--t-depth, 0) * var(--t-depth-step))`
  offset per row, pulling it back left so it stays aligned to the true left edge regardless of how
  many ancestor `.t-tree-wrapper` divs have pushed the row itself rightward.
- `.t-code` / `.t-code-block` - both use `--t-color-code`, bold.
  `.t-code-block` additionally sets `display: block; white-space: pre; padding-left: var(--t-indent)`
  for triple-backtick fenced blocks; `.t-code` (single backtick, inline) gets neither.
- `.t-tree-wrapper` - one nesting level's indent: `margin-left` + `padding-left` both `var(--t-indent)`,
  plus a 1px left border in `--t-color-border`.
  Deliberately no `color:` override - inherits whatever text color surrounds it.
  Its inline `style="--t-depth: N"` (set to `depth + 1` for a fork's children) is what
  `.t-col-wide-title`/`.t-col-title`/`.t-col-model` read to compute their own width shrink.
- `.t-muted` - generic dimmed text (e.g. the "no agents ran" empty-state message), via `--t-op-muted`.

## Inserting or updating the `<style>` block

- `assets/style.css` is pure CSS only - it has no `<style>`/`</style>` wrapper tags.
  Wrapping those tags around the file's contents is the SQL layer's job (e.g.
  `concat('<style>', <css content>, '</style>')`) when building a panel's `rawSql`.
  Never store the tags as part of this reusable asset.
- Read `assets/style.css` and splice its full contents in, wrapped in `<style>...</style>`, as the
  panel's style element - typically as the tie=0 sentinel row's own line in the query's `UNION ALL`
  result set, the same slot panel 99 uses - only when the current task is actually to add or update
  a panel's CSS.
- For any other task (a data/SQL change, a non-styling markup change), leave whatever `<style>`
  content is already in the panel untouched, whether it's present, absent, or stale relative to
  this asset file.
  Don't read the asset file or substitute its content just because a `<style>` block happens to be
  present or missing.
- Remember the target is a JSON string value inside `rawSql`: a literal newline in the CSS becomes
  the two characters `\` `n` in the raw file text, same escaping rule as any other SQL edit to this
  file (see the brace-matching splice procedure in `Skill(dynamictext-panel-queries)`).

## Current inline styling reference (not yet migrated onto `t-` classes)

Panel 76 hasn't adopted the `t-` system above and still uses per-call-site inline `style="..."`
attributes and hardcoded values.
These are style facts, not query/data logic, so they live here even though they aren't yet
expressed as reusable classes.
Check a panel's actual `rawSql` for its current state before assuming either panel matches this doc.

### Markers

- `❯` real user prompt/comment.
- `●` model reply text and echoed tool output like WebFetch's "Web page content".
- `○` harness-injected pseudo-prompt (system-reminder, suggestion-mode, judge/title-gen calls - see
  `Skill(dynamictext-panel-queries)` for the classification logic).
- `├─`/`└─` tool-call tree branches (`└─` specifically marks a reply/leaf).
- `▸` agent spawn (legacy arrow - kept in the code awaiting later cleanup, no longer paired with a
  visible label since agent spawn rows now show the name in bold directly).
- `🚨` for any error/failure (not `⚠`).

### Colors and opacity values

- `opacity:.6` - the general dimmed/grey treatment: tool-call argument text, the bare token-count
  stat, and failure/error note lines all share this same value.
- `#8ab8ff` - code color (single- and triple-backtick spans) and slash-command highlighting, on both
  panel 76 (inline) and panel 99 (via `--t-color-code`).
  Not `#3b9eff` - an earlier drafted-but-never-implemented value from before a color-consistency bug
  was fixed; don't reintroduce it.
- Stats/labels (`Duration:`, `Cost:`, `Tokens:`, `Model(s):`, `Prompts:`, `Tool calls:`, `Agents:`,
  `Skills:`, `Git:`) render bold via `<b>...</b>`.

### `**bold**`/`` `code` `` markdown-to-HTML conversion

The panel runs in HTML mode, not markdown mode (see plugin config in `Skill(dynamictext-panel-queries)`),
so literal `**`/`` ` `` in prompt/reply text would otherwise show as literal asterisks/backticks, not
render.
Convert via regex, in this order, after escaping `&`/`<`/`>` and before any further wrapping:

1. `replaceRegexpAll(text, '\*\*([^*\n]+?)\*\*', '<b>\1</b>')`
2. `replaceRegexpAll(..., '`([^`\n]+?)`', '<code>\1</code>')`

### Slash-command highlighting regex

A leading slash-command anywhere in real user prompt text gets colored blue+bold (terminal-style):

```
replaceRegexpAll(text, '(^|\s)(/[a-zA-Z][\w-]*)', '\1<span style="color:#8ab8ff;font-weight:bold">\2</span>')
```

**RE2 (ClickHouse's regex engine) has no lookahead/lookbehind support whatsoever** - `(?=...)` fails
outright with `DB::Exception: ... invalid perl operator: (?=` (confirmed live against ClickHouse
24.8).
Do not attempt any `(?=...)`/`(?!...)`/`(?<=...)`/`(?<!...)` pattern in ClickHouse regex functions -
none of the four forms are supported, this isn't specific to this one pattern (also documented as a
general gotcha in the `clickhouse-sql` skill).

The false positive this rule exists to prevent: a path fragment like `/usr/local/bin` or
`/Users/ixax/foo` highlighting only its first segment (`/usr`, `/Users`), since `[\w-]*` greedily
eats word chars and stops at the next `/`, with nothing requiring the match to be followed by
whitespace/tag/end.

**Lookahead-free fix**: run the same unmodified highlighting regex first (so real standalone
commands - own-line, mid-sentence, whole-string - all still highlight correctly), then run a second
`replaceRegexpAll` pass over its output that strips the span back off wherever it's immediately
followed by another `/` (the tell-tale sign the "command" was actually a path's first segment):

```
replaceRegexpAll(<highlighted>, '<span style="color:#8ab8ff;font-weight:bold">(/[a-zA-Z][\w-]*)</span>(/)', '\1\2')
```

This needed no lookahead, no lookbehind, and no change to the original highlighting regex or its
replacement template - it's purely an additive wrapping call around the existing one.
Verified empirically via `mcp__dev__query` against a real path (not highlighted), `/plan` alone on
its own line (highlighted), `/goal do the thing` mid-sentence (highlighted), `/status` as the entire
string (highlighted), and multiple commands on one line (`/plan then /goal now` - both highlighted
independently, chaining unaffected since the fix-up pass never consumes/alters any separator
whitespace).

### Escaping order

Escape `&`/`<`/`>` on a piece of dynamic text *first*, then apply your own `<b>`/`<span>`/`<code>`
wrapping on top - never the other way round, or your own tags get escaped into visible `&lt;b&gt;`
text.
This also means literal user text like `<command-name>/goal</command-name>` (which appears verbatim
in real prompts) must never be trusted as real markup - it has to go through the same escaping as
everything else.
