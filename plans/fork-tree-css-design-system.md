# CSS design system for the Fork tree / Trace panels ("t-" prefix)

This is a **design-only** deliverable: a proposed `<style>` block plus the `t-`-prefixed
variables/classes that would replace today's scattered inline `style="..."` attributes.
Nothing gets implemented in this pass - review the system below, then a follow-up pass
wires it into panel 99 (and later panel 76) via `dynamictext-panel-builder`.

## Context

Today, panels 76 ("Trace") and 99 ("Fork tree") build every visual property inline,
repeated at each call site: `style="opacity:.6"`, `style="color:#8ab8ff;font-weight:bold"`,
`style="margin-left:1.2em;border-left:1px solid rgba(255,255,255,.25);padding-left:1.2em"`,
and so on.
We already confirmed today that a `<style>` block placed once in the panel's own output
survives Grafana's sanitizer and its rules apply live (verified visually: an injected rule
rendered red/bold/underlined exactly as declared).
That makes a real variables-and-classes system viable for the first time.

Both panels render inside an ambient `<pre>` (confirmed via existing code comments in
panel 76: "panel renders inside a `<pre>`") - literal whitespace is already preserved
panel-wide, which is why today's space-padding column alignment works at all.
Neither panel currently contains a literal `<pre>` tag of our own, so there's nothing
extra to strip there.

## Current values this design is grounded in

Pulled directly from the live `rawSql` of panels 76/99 (not guessed):

| Element                        | Current style                                              |
| ------------------------------ | ------------------------------------------------------------ |
| name / title                   | `<b>...</b>`, no color override (inherits default/white)   |
| model                          | `opacity:.6`                                                |
| duration                       | `opacity:.5`, `leftPad(..., 7, ' ')`                        |
| tokens                         | `opacity:.6`, `leftPad(..., 9, ' ')`                        |
| cost                           | `opacity:.7`, **no leftPad today** (natural width, `'   $X.XXXX'` when present) |
| code (single `` ` `` and triple ` ``` `) | `color:#8ab8ff;font-weight:bold`, identical for both today |
| user-prompt arrow (panel 76's real one) | `<span style="color:#e0af02">❯</span> ` (note the trailing literal space) |
| tree-wrapper (nested `<div>` per fork) | `margin-left:1.2em;border-left:1px solid rgba(255,255,255,.25);padding-left:1.2em` |
| timestamp (panel 76's own, `%H:%i:%S`) | 8 characters, e.g. `11:28:47`, dotted-underline link       |

`fork_width.total_w` (the name+model combined budget) is `floor(${trace_width_budget}/2) - depth*3`.
With the dashboard's own default `trace_width_budget = 120`, that's **60 characters at depth 0**,
shrinking by **3 characters per nesting level** - split evenly into a 30/30 name/model pair.

**Open question - character-to-pixel conversion.**
You asked for pixel widths rounded to tens, but these are currently *character counts* in an
ambient monospace `<pre>`, not measured pixel widths.
I don't have the actual rendered font-size, so the px values below assume a common monospace
metric of ~7.8px per character (i.e. ~0.6em at a 13px base) and round to the nearest 10.
Treat every px value below as a **first draft** - correct it once someone measures the real
rendered column width in a browser, and the calc() mechanism below makes that a one-line fix.

## Proposed variables and classes

```css
<style>
:root {
  /* ---- indentation ---- */
  --t-indent: 1.2em;              /* one nesting level's margin/padding step, also used as the code-block left pad */

  /* ---- opacities (kept as opacity, not fixed grays, so it stays theme-adaptive - matches today's convention) ---- */
  --t-op-duration: .5;
  --t-op-tokens: .6;
  --t-op-cost: .7;
  --t-op-model: .6;
  --t-op-muted: .6;                /* "Агенты не были запущены", hidden/secondary text */

  /* ---- reusable colors ---- */
  --t-color-code: #8ab8ff;         /* single- and triple-backtick code, identical */
  --t-color-arrow: #e0af02;        /* user-prompt marker, panel-76's real ❯ */
  --t-color-border: rgba(255, 255, 255, .25); /* tree-wrapper left border */

  /* ---- column widths (px, rounded to tens - see "open question" above) ---- */
  --t-col-timestamp: 60px;         /* 8ch, e.g. "11:28:47" */
  --t-col-wide-title: 470px;       /* 60ch base: name + model combined, depth 0 */
  --t-col-title: 230px;            /* half of --t-col-wide-title */
  --t-col-model: 230px;            /* the other half of --t-col-wide-title */
  --t-col-duration: 50px;          /* 7ch */
  --t-col-tokens: 70px;            /* 9ch */
  --t-col-cost: auto;              /* NOT fixed-width today - flagging rather than inventing a number; see note below */

  /* --t-depth-step is DERIVED, not an independent number - see "depth-aware alignment" below for why it must equal margin+padding per level, not a separately-chosen char-width. */
  --t-depth-step: calc(var(--t-indent) * 2);
}

/* ================= 1. user_prompt - arrow via ::before ================= */
.t-user-prompt::before {
  content: "❯ ";                  /* glyph + the literal trailing space panel-76 already uses */
  color: var(--t-color-arrow);
}

/* ================= 2/3/7. wide-title / title / model columns ================= */
/* .t-col-wide-title is the depth-aware container; --t-depth is set inline per row,
   e.g. style="--t-depth: 2", defaulting to 0 when absent (top-level fork). */
.t-col-wide-title {
  display: inline-block;
  width: calc(var(--t-col-wide-title) - (var(--t-depth, 0) * var(--t-depth-step)));
}
.t-col-title {
  display: inline-block;
  width: calc(var(--t-col-wide-title) / 2 - (var(--t-depth, 0) * var(--t-depth-step) / 2));
  font-weight: bold;
}
.t-col-model {
  display: inline-block;
  width: calc(var(--t-col-wide-title) / 2 - (var(--t-depth, 0) * var(--t-depth-step) / 2));
  opacity: var(--t-op-model);
}

/* ================= 4/5/6. cost / tokens / duration - all right-aligned ================= */
.t-col-cost {
  display: inline-block;
  width: var(--t-col-cost);
  text-align: right;
  opacity: var(--t-op-cost);
}
.t-col-tokens {
  display: inline-block;
  width: var(--t-col-tokens);
  text-align: right;
  opacity: var(--t-op-tokens);
}
.t-col-duration {
  display: inline-block;
  width: var(--t-col-duration);
  text-align: right;
  opacity: var(--t-op-duration);
}

/* ================= NEW: timestamp column (leftmost, before the block starts) ================= */
.t-col-timestamp {
  display: inline-block;
  width: var(--t-col-timestamp);
  opacity: var(--t-op-muted);
}

/* ================= 8. code - inline vs. block, same color ================= */
.t-code {
  color: var(--t-color-code);
  font-weight: bold;
}
.t-code-block {
  color: var(--t-color-code);
  font-weight: bold;
  display: block;
  white-space: pre;               /* explicit, self-contained - doesn't rely on the ambient <pre> */
  padding-left: var(--t-indent);  /* triple-backtick blocks get one indent-unit of left padding; single backtick does not */
}

/* ================= 9. tree-wrapper - indent + border, no forced text color ================= */
.t-tree-wrapper {
  margin-left: var(--t-indent);
  padding-left: var(--t-indent);
  border-left: 1px solid var(--t-color-border);
  /* intentionally no `color:` here - inherits whatever the surrounding text already is */
}

/* ================= muted / empty-state text ================= */
.t-muted {
  opacity: var(--t-op-muted);
}
</style>
```

## How the depth-aware alignment mechanism works (goal 2)

**The bug in the first draft**: shrinking `.t-col-wide-title`'s width by some independently-chosen
amount (I'd picked "3ch worth", ~20px) does NOT compensate for the wrapper's margin - it just
makes that one column narrower.
A row's absolute horizontal position is pushed right by however much margin+padding its N
ancestor `.t-tree-wrapper` divs contributed; shrinking a column inside that row only closes
the gap if the shrink amount is *exactly* the same number as that push.
Two independently-chosen variables (an indent value and a "how much to shrink" value) will
only cancel by coincidence, and any future edit to either one silently breaks alignment again.

**The fix**: derive the shrink amount from the same variable that drives the indent, so they
can't drift apart by construction.

- A fork's own header row (`.t-col-wide-title` etc., inside `<summary>`) is NOT inside its
  *own* `.t-tree-wrapper` - that div comes after `</summary>`, wrapping the fork's body and
  children.
  It IS inside every *ancestor* fork's `.t-tree-wrapper`, one per level up.
- Each `.t-tree-wrapper` contributes `margin-left: var(--t-indent)` **and**
  `padding-left: var(--t-indent)` (both present in today's real markup) - that's
  `2 * var(--t-indent)` of horizontal push per ancestor level, not `1 * var(--t-indent)`.
- So `--t-depth-step` is defined as `calc(var(--t-indent) * 2)`, not a separate number -
  see the variables block above.
  At depth N, `.t-col-wide-title`'s width shrinks by `N * --t-depth-step`, which by
  construction exactly equals the horizontal push from N ancestor wrappers.
  Change `--t-indent` later and the compensation updates itself automatically.
- Everything after `.t-col-wide-title` in a row (`--t-col-duration`, `--t-col-tokens`,
  `--t-col-cost`) has a fixed, depth-independent width, so once the wide-title column's
  right edge lands at the same absolute X regardless of depth, so does everything after it.

**One known residual**: `.t-tree-wrapper` also has `border-left: 1px solid ...`, which the
box model counts as 1 more pixel of horizontal space per ancestor level, not captured by
`--t-depth-step`.
At the maximum 7 levels of nesting this repo supports, that's up to ~7px of drift on the
deepest rows - visually negligible, but real.
Two ways to remove it entirely if it matters: make the border a `box-shadow: -1px 0 0 var(--t-color-border)`
(shadows don't participate in layout) instead of a real `border-left`, or fold `1px` into
`--t-depth-step`'s formula (`calc(var(--t-indent) * 2 + 1px)`).
Flagging both options rather than picking one silently.

## Example markup shape (illustrative only, not the real query)

```html
<span class="t-col-timestamp">11:28:47</span>
<span class="t-tree-wrapper" style="--t-depth: 1">
  <span class="t-col-title">code-locator</span><span class="t-col-model">claude-haiku-4-5</span><span class="t-col-duration">3m 0s</span><span class="t-col-tokens">10.2k</span><span class="t-col-cost">$0.3579</span>
</span>
```

and for the initial user prompt:

```html
<span class="t-user-prompt">Сформируй предложение по фиксу ошибки...</span>
```

## Things to confirm before implementation

1. **Cost column width** - there's no current fixed width to preserve (unlike duration/tokens).
   Either leave it `auto` (as drafted) or pick a width now; I didn't want to invent a number
   and call it "as today" when it isn't.
2. **Px assumption** - the `--t-col-*` px values assume ~7.8px/character; worth a quick
   browser measurement before implementation to correct the base numbers (the `calc()`
   mechanism itself doesn't change either way).
3. **Timestamp column data source** - panel 99 doesn't currently compute a per-fork start
   timestamp column for display (only uses `start_ts` internally for duration math); adding
   the visible column needs a small SQL addition (`formatDateTime(fc.start_ts, '%H:%i:%S')`),
   not just CSS.

## Next step (not part of this pass)

Once this system is approved, implementation is a `dynamictext-panel-builder` task: add the
`<style>` block to panel 99's tie=0 sentinel row (the same slot proven safe today), then
replace every inline `style="..."` call site in panel 99 with the matching `t-` class
(and set `--t-depth` per row from the existing `depth` column).
Panel 76 stays out of scope for now unless you want the same system applied there too.
