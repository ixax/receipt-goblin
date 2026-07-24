---
name: md-format
description: >
  <skill_version>1.1.0</skill_version> Formatting conventions for markdown prose and tables in this repo.
  TRIGGER - read BEFORE writing a new .md file or editing prose/tables in an existing one, anywhere in the project (README, AGENTS.md, SKILL.md bodies, any other .md file).
  Covers: line wrapping, one-sentence-per-line, table pipe alignment.
  SKIP for non-markdown files, and for .md files where only code blocks or frontmatter are touched.
---

# md-format

Two conventions to apply whenever authoring or editing markdown prose or
tables in this repo. Neither applies to code blocks or table cells beyond
alignment - only to prose paragraphs and table pipe formatting.

## Line wrapping

Never wrap a line by character count - not at ~80 chars, not at ~160. A
line runs however long the sentence needs; use the full width.

When a paragraph has more than one sentence, put each sentence on its own
line (semantic line breaks) instead. A paragraph becomes one line per
sentence, not a ragged block of short, character-wrapped lines.

This applies to prose only - not to code blocks, table cells, or
frontmatter.

## Table alignment

Pad every cell so the `|` column separators line up vertically across all
rows, instead of minimal-width `| a | b |` pipes. Compute each column's
width from its longest cell (header included) and pad every other cell in
that column to match before placing the `|`.

## Applying both

Before writing or editing a markdown file:

1. Draft the content.
2. Reflow any multi-sentence prose paragraph into one sentence per line.
3. Recompute and pad table column widths so pipes align.
4. Leave code blocks, inline code, and frontmatter untouched.
