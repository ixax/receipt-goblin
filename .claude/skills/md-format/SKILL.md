---
name: md-format
description: >
  Formatting conventions for markdown prose and tables in this repo.
  TRIGGER - read BEFORE writing a new .md file or editing prose/tables in an existing one, anywhere in the project (README, AGENTS.md, SKILL.md bodies, any other .md file).
  Covers: line wrapping, one-sentence-per-line, table pipe alignment.
  SKIP for non-markdown files, and for .md files where only code blocks or frontmatter are touched.
version: 1.0.0
---

# md-format

Two conventions to apply whenever authoring or editing markdown prose or
tables in this repo. Neither applies to code blocks or table cells beyond
alignment - only to prose paragraphs and table pipe formatting.

## Line wrapping

Don't hard-wrap prose at ~80 characters. Write lines at least twice that
long.

Better still: when a paragraph has more than one sentence, put each
sentence on its own line (semantic line breaks) instead of wrapping by
character count. A paragraph becomes one line per sentence, not one
ragged block of short lines.

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
