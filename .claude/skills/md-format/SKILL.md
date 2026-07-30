---
name: md-format
description: >
  Formatting conventions for markdown prose/tables, and for multi-sentence comments/docstrings in Python and YAML.
  TRIGGER - read before EVERY Edit/Write touching .md prose or tables (agent_docs/*.md, README.md, AGENTS.md, SKILL.md, agent/command bodies, any .md), and before writing a multi-sentence `#` comment or docstring in .py/.yml/.yaml.
  Covers: line wrapping, one-sentence-per-line, keep-sentences-short, enumeration-vs-list, quoting example text, table alignment.
  A PostToolUse hook enforces one-sentence-per-line on .py/.yml/.yaml comments mechanically - read this to know the rule before writing, not just to pass the hook.
  SKIP for a single-sentence code comment, and .md files touching only code blocks/frontmatter.
  <version>1.6.0</version>
---

# md-format

Conventions to apply whenever authoring or editing markdown prose or tables in this repo.
None apply to code blocks or table cells beyond alignment - only to prose paragraphs, list formatting, and table pipe formatting.

## Line wrapping

Never wrap a line by character count - not at ~80 chars, not at ~160.
A line runs however long the sentence needs; use the full width.

When a paragraph has more than one sentence, put each sentence on its own line (semantic line breaks) instead.
A paragraph becomes one line per sentence, not a ragged block of short, character-wrapped lines.

This applies to prose only - not to code blocks, table cells, or frontmatter.

## Keep each sentence short

The one-sentence-per-line rule above splits a paragraph by sentence.
This rule makes each of those sentences short in the first place.

A sentence with two or more independent clauses joined by "-" or ";" should usually become two separate sentences (two lines) instead.
One idea per sentence.
No word-count target - length isn't measured, clause count is: if you can split "X, and Y" or "X - Y" into "X. Y." without losing meaning, split it.

This doesn't mean cramming everything into fragments - keep a sentence whole when it expresses one idea, however many words that takes.
It means not joining two separate, already-complete ideas into one line just to avoid a second line.

Before (a real sentence from this repo's docs, one idea per clause, three
clauses joined into one sentence):

> Diagnosis: the original AGENTS.md is roughly 5-6k tokens against a
> budget of <=2k - by this doc's own tagging it splits out to about 25%
> universal, 50% deep-dive, 20% task-specific, 5% duplicate, and the
> optimized version comes out to 1.6-1.9k tokens with everything cut kept
> in two deep-docs.

After (same facts, one idea per sentence):

> The original AGENTS.md ran ~5-6k tokens against a <=2k budget.
> By this doc's own tagging, roughly 25% was universal, 50% deep-dive,
> 20% task-specific, 5% duplicate.
> The optimized version is ~1.6-1.9k tokens.
> Everything cut moved into two deep-docs, not lost.

## Enumeration vs. inline list

When a sentence's core content is itself an enumeration of 3+ independent parallel items, format it as a bulleted list (one item per line) instead of joining the items with commas/dashes into a single line.
This is different from incidental commas inside one flowing idea ("the file is large, so X happens") - that's one idea, not a list, and stays inline.
The signal is whether the items are independent and parallel, especially when each one carries its own parenthetical detail: that's a sign the line is actually a list wearing sentence clothing.

If the enumeration sits inside an existing bullet (a parent `- label - ...`
item), nest the new list one level deeper under that bullet rather than
promoting it to a new top-level bullet.

Before (one line, 8 independent items each following the same "name +
optional parenthetical" shape):

> Single nginx service sits in front of every service that used to publish
> its own host port directly: `webhook` (via `webhook-1`/`webhook-2`),
> `litellm`, `grafana`, `mcp-dev` (dev-only), `mcp-stats`, `clickhouse`
> (both its HTTP interface and native protocol), `prometheus` (opt-in
> `observability` profile), and `langfuse-web` (opt-in `langfuse` profile).

After (lead-in sentence kept as prose, items broken out):

> Single nginx service sits in front of every service that used to publish
> its own host port directly:
>
> - `webhook` (via `webhook-1`/`webhook-2`)
> - `litellm`
> - `grafana`
> - `mcp-dev` (dev-only)
> - `mcp-stats`
> - `clickhouse` (both its HTTP interface and native protocol)
> - `prometheus` (opt-in `observability` profile)
> - `langfuse-web` (opt-in `langfuse` profile)

## Quoting example text

Use `> ` (CommonMark blockquote) for any quoted or illustrative text block - example sentences, before/after pairs, quoted output.
Never use plain space indentation for this: 1-3 spaces render as an ordinary paragraph (the indentation is silently dropped), and 4+ spaces triggers an indented code block instead (monospace, no wrapping) - neither renders as a quote.
`>` is the only CommonMark construct that reliably renders as a distinct quoted block across renderers.

## Heading hierarchy

Every file has exactly one H1 (`#`), the document title.
Put sections at H2 (`##`), subsections at H3 (`###`).
Nest deeper only when a subsection itself needs subsections.
Never skip a level (no `#` straight to `###`) and never add a second H1.

## ASD-STE100 Simplified Technical English

Apply this when writing instructions for AI agents, `.md` files, or comments.

Key rules:
- Use approved words only. The standard gives a word list.
- Use one word for one idea. Do not use two words for the same thing.
- Write short sentences. Use 20 words or less for instructions.
- Use active voice.
- Write short paragraphs. Keep one topic in each paragraph.

This makes texts clear.
Agents work better.
Non-native readers understand the work.

## Table alignment

Pad every cell so the `|` column separators line up vertically across all rows, instead of minimal-width `| a | b |` pipes.
Compute each column's width from its longest cell (header included) and pad every other cell in that column to match before placing the `|`.

## Applying all

Before writing or editing a markdown file:

1. Draft the content.
2. Reflow any multi-sentence prose paragraph into one sentence per line.
3. Break any 3+-item independent-parallel enumeration out into a bulleted list.
4. Recompute and pad table column widths so pipes align.
5. Leave code blocks, inline code, and frontmatter untouched.
