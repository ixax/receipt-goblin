---
name: md-format
description: >
  Formatting conventions for markdown prose/tables, and for multi-sentence comments/docstrings in Python and YAML.
  TRIGGER - read before EVERY Edit/Write touching .md prose or tables (agent_docs/*.md, README.md, AGENTS.md, SKILL.md, agent/command bodies, any .md), and before writing a multi-sentence `#` comment or docstring in .py/.yml/.yaml.
  Also covers Grafana dashboard-JSON prose: panel `description` field values, and `--`-prefixed SQL comment lines inside `rawSql` strings, under services/grafana/dashboards/*.json and dashboards-health/*.json.
  Covers: line wrapping, one-sentence-per-line, short sentences, enumeration-vs-list, quoting example text, heading hierarchy, table alignment, top-level doc structure.
  SKIP for a single-sentence code comment, and .md edits touching only code blocks/frontmatter.
  v1.10.0
---

Apply to prose paragraphs, list formatting, and table pipe formatting only.
Never reformat code blocks, inline code, frontmatter, or table cell content beyond alignment.

## Read-once gate

The PreToolUse hook (`hooks/harness_audit/md_format_skill_gate.py`) requires reading this skill once per session, before the first qualifying write.
It qualifies Edit/Write and Bash heredoc/redirect/tee writes to these extensions - not Makefile comments or script-assembled content.
The gate covers the read, not compliance: apply these rules by hand to every later qualifying write in the session, including a re-write of already-drafted content to a new path (e.g. copying a plan into `plans/<name>.md`).

## Line wrapping

Never wrap a line by character count - not at ~80 chars, not at ~160.
A line runs however long its sentence needs.
When a paragraph has more than one sentence, put each sentence on its own line (semantic line breaks).

## Short sentences

One idea per sentence.
Clause count is the measure, not word count: if "X, and Y" or "X - Y" splits into "X. Y." without losing meaning, split it.
Keep a sentence whole when it expresses one idea, however many words that takes - don't shred into fragments.

Before (three ideas joined into one sentence):

> Diagnosis: the original AGENTS.md is roughly 5-6k tokens against a budget of <=2k - by this doc's own tagging it splits out to about 25% universal, 50% deep-dive, 20% task-specific, 5% duplicate, and the optimized version comes out to 1.6-1.9k tokens with everything cut kept in two deep-docs.

After (same facts, one idea per line):

> The original AGENTS.md ran ~5-6k tokens against a <=2k budget.
> By this doc's own tagging, roughly 25% was universal, 50% deep-dive, 20% task-specific, 5% duplicate.
> The optimized version is ~1.6-1.9k tokens.
> Everything cut moved into two deep-docs, not lost.

## JSON-embedded prose

The one-sentence-per-line rule applies inside Grafana dashboard-JSON prose too: panel `description` field values, and `--`-prefixed SQL comment lines inside `rawSql` strings.
Inside a JSON string value, a line break is the two literal characters `\n`, not an actual newline - split sentences onto separate `\n`-joined lines, never a real newline that would break the JSON.

## Enumeration vs. inline list

A sentence whose core content is 3+ independent parallel items becomes a bulleted list, one item per line.
Incidental commas inside one flowing idea ("the file is large, so X happens") stay inline - that's one idea, not a list.
The signal: items are independent, parallel, and each carries its own parenthetical detail.
An enumeration inside an existing bullet nests one level deeper under it, not promoted to top level.

Before (one line, 8 parallel items):

> Single nginx service sits in front of every service that used to publish its own host port directly: `webhook` (via `webhook-1`/`webhook-2`), `litellm`, `grafana`, `mcp-dev` (dev-only), `mcp-stats`, `clickhouse` (both its HTTP interface and native protocol), `prometheus` (opt-in `observability` profile), and `langfuse-web` (opt-in `langfuse` profile).

After (lead-in kept as prose, items broken out):

> Single nginx service sits in front of every service that used to publish its own host port directly:
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

Use `> ` (CommonMark blockquote) for any quoted or illustrative text block: example sentences, before/after pairs, quoted output.
Never plain-space indentation: 1-3 spaces render as an ordinary paragraph, 4+ triggers an indented code block - neither renders as a quote.

## Heading hierarchy

Every file has exactly one H1 (`#`), the document title.
Exception: a Subagent/Skill/Command body already carries its identity via frontmatter `name:` (or the command filename) and starts directly at H2.
Sections at H2, subsections at H3; nest deeper only when a subsection itself needs subsections.
Never skip a level and never add a second H1.

## Top-level docs

README.md and other top-level docs: `agent_docs/rules/docs.md` (keep short, `<details>` for secondary content).

## Plain style

- Active voice.
- One topic per paragraph.
- One word for one idea - don't alternate synonyms for the same thing.

## Table alignment

Pad every cell so the `|` separators line up vertically across all rows.
Compute each column's width from its longest cell (header included); pad every other cell in that column to match.

## Applying all

1. Draft the content.
2. Reflow multi-sentence paragraphs into one sentence per line.
3. Break 3+-item independent-parallel enumerations into bulleted lists.
4. Recompute and pad table column widths.
5. Leave code blocks, inline code, and frontmatter untouched.
