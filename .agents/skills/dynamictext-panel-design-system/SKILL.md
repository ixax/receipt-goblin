---
name: dynamictext-panel-design-system
description: >
  Reference for the `t-`-prefixed CSS design system used by Dynamic Text panels in
  services/grafana/dashboards/agents_overview.json.
  Lives at services/grafana/static/dynamic-text-vN.css (served at /public/receipt-goblin/), no
  mirror copy anywhere.
  Attached to panel 99 ("Fork tree") and panel 76 ("Trace"); panel 76's markup still uses inline
  `style="..."`.
  TRIGGER - read only when the current dashboards-expert task is to add/update a Dynamic Text
  panel's CSS, or rewrite/regenerate row markup using `t-` classes or inline styling.
  SKIP for a pure data/SQL change, or any markup change that doesn't touch styling.
  <version>3.0.0</version>
---

# dynamictext-panel-design-system

Design-system reference for `dashboards-expert`, read on demand - see this skill's own
description for the exact trigger.

Check a panel's actual `rawSql`/`vizConfig.spec.options` for its current state before assuming
either panel matches this doc - don't infer from this doc alone.

## Variables (`:root`)

- `--t-indent: 1.2em` - one nesting level's margin/padding step, also the code-block left pad.
- `--t-op-duration: .5`, `--t-op-tokens: .6`, `--t-op-cost: .7`, `--t-op-model: .6`,
  `--t-op-muted: .6` - opacities, kept as `opacity` (not fixed grays) so they stay theme-adaptive.
- `--t-color-code: #8ab8ff` - single- and triple-backtick code, identical color for both.
- `--t-color-arrow: #e0af02` - the user-prompt `❯` marker.
- `--t-color-border: rgba(255, 255, 255, .25)` - tree-wrapper left border.
- `--t-col-timestamp`, `--t-col-wide-title`, `--t-col-title`, `--t-col-model`, `--t-col-duration`,
  `--t-col-tokens`, `--t-col-cost` - column widths in px.
- `--t-depth-step: calc(var(--t-indent) * 2 + 1px)` - derived, not independently chosen.
  Each ancestor `.t-tree-wrapper` contributes `margin-left` + `padding-left` (2x `--t-indent`) plus
  its 1px `border-left`, so the depth-based width shrink must include that same `+ 1px` per level
  or the columns drift right with depth.
  Never hardcode a shrink amount separately from this variable.

## Classes

- `.t-user-prompt::before` - injects the `❯ ` marker (glyph `\2771`) in `--t-color-arrow`.
- `.t-col-wide-title` - depth-aware container spanning name + model combined; width shrinks by
  `--t-depth * --t-depth-step`.
  `--t-depth` is set inline per row (e.g. `style="--t-depth: 2"`, defaulting to 0 when absent).
- `.t-col-title` - half of `.t-col-wide-title`'s width, bold, `title="HH:MM:SS"` tooltip.
- `.t-col-model` - the other half, dimmed via `--t-op-model`.
- `.t-col-duration`, `.t-col-tokens`, `.t-col-cost` - fixed-width, right-aligned, own opacity var.
- `.t-col-timestamp` - fixed-width, dimmed via `--t-op-muted`.
- `.t-code` / `.t-code-block` - both use `--t-color-code`, bold; `.t-code-block` additionally sets
  `display: block; white-space: pre; padding-left: var(--t-indent)` for fenced blocks.
- `.t-tree-wrapper` - one nesting level's indent (`margin-left` + `padding-left`, both
  `var(--t-indent)`), plus a 1px left border.
  Its inline `style="--t-depth: N"` is what the column classes read to compute their width shrink.
- `.t-muted` - generic dimmed text, via `--t-op-muted`.
- `.t-pre` - the outer content wrapper (`white-space: pre-wrap; margin: 0 0 1.2em 0`).
- `.t-row` - flex row wrapping a fork's timestamp + collapsible body on one line
  (`display: flex; flex-wrap: nowrap; align-items: baseline`).
- `.t-fork-details` - the `<details>` element (`margin-top: 0.6em; flex: 1; min-width: 0`).
- `.t-summary-toggle` - the `<summary>` element; must be `display: flex` to override the browser's
  default `list-item` display, which otherwise pushes the disclosure content onto its own line.
- `.t-fork-summary` - wraps a fork's stat spans (title/model/duration/tokens/cost) so they stay on
  one line inside `<summary>`.
- `.t-slash-command` - blue+bold slash-command highlight, uses `--t-color-code`.

## Usage rule: no inline `style="..."`

All row-markup styling uses these `t-` classes, not per-call-site `style="..."` attributes.
The only allowed exception is `style="--t-depth: N"` (a CSS custom property set per-row from a
query-computed value - there's no way to express that as a static class).

## Connecting the stylesheet to a panel

Panels attach the CSS via `vizConfig.spec.options.externalStyles`, an array of
`{"id": "...", "name": "...", "url": "/public/receipt-goblin/dynamic-text-vN.css"}` - the plugin's
"CSS Styles" panel option (`New Resource` / `Add` in the panel editor).
This requires `vizConfig.spec.options.wrap: false` - with `wrap: true` the plugin's markdown-it
pass corrupts long HTML content and an embedded `<style>` block is the only thing that survives its
sanitizer, which is why this panel doesn't use an embedded `<style>` block at all.

**Any content change to the stylesheet requires a new versioned file**, not an edit to the existing
one: create `dynamic-text-vN+1.css`, update every attached panel's `externalStyles[].url` to match,
delete the old version once unreferenced.
Both Grafana's static-file serving and the plugin's own `<link>` loader cache aggressively enough
that editing the existing file in place will not reliably reach already-open browser tabs, even
after a hard reload.
Confirm the new version is actually served with `curl localhost:3000/public/receipt-goblin/
dynamic-text-vN+1.css`; if it still shows old content, check the bind mount directly with `docker
exec receipt-goblin-grafana cat /usr/share/grafana/public/receipt-goblin/dynamic-text-vN+1.css`.
