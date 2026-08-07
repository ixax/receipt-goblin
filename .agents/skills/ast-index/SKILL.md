---
name: ast-index
description: >
  Query CLI and cache conventions for `agent_docs/ast_index/`, the committed structural digest of every tracked `.py` file (schema documented in `scripts/ast_index.py`'s module docstring).
  TRIGGER - read before running `uv run python3 scripts/ast_index.py query`/`build`, or when deciding whether a STALE cache needs `--file` or `--all`.
  v1.0.3
---

## Cache & schema

`agent_docs/ast_index/cache/manifest.json` (relpath -> `sha256`/`size`/`cache_path`) and `agent_docs/ast_index/manifests/<relpath>.json` (one flat document per file: imports, module constants, classes, methods, functions, signatures, docstrings).
Schema and CLI usage are documented in `scripts/ast_index.py`'s module docstring - read that directly rather than a duplicate copy here, it's the source of truth and would drift if restated.

## Query CLI

Run from the repo root:

```
uv run python3 scripts/ast_index.py query services/_common/src/ingest_parsing.py --view outline
uv run python3 scripts/ast_index.py query services/_common/src/ingest_parsing.py --view signatures
uv run python3 scripts/ast_index.py query services/_common/src/ingest_parsing.py --view full
uv run python3 scripts/ast_index.py query services/_common/src/ingest_parsing.py --symbol EventContext.__init__
uv run python3 scripts/ast_index.py query --grep EventContext
```

Add `--json` to any of these for structured output.
Plain text is the default - line-oriented, cheap to read.

## Staleness & rebuilds

A stale cache entry (on-disk sha256 no longer matches the manifest) still returns its result, but prints `STALE: run build --file <path>` to stderr.
Never silently wrong, never blocks the read.
Self-heal one file: `uv run python3 scripts/ast_index.py build --file <relpath>`.

Run a full `build --all` instead when the whole cache is untrustworthy, not just one entry:

- fresh clone (no `agent_docs/ast_index/` yet)
- missing cache dir
- drift after a non-hook change, e.g. `git pull` bringing in commits the session hook never saw

`build --check` exits 1 if anything is stale, without writing - used by `.githooks/pre-push`.

## Cross-language note

Cache layout, manifest shape, hook wiring, and the query-view CLI are already language-agnostic by construction.
The only Python-specific piece is the extraction core (`ast.parse` -> the schema above).
Porting to JS/PHP later means writing a new extractor that emits the same JSON shape, not redesigning the surrounding layers.
Not built now - no `--lang` flag, no extractor registry.
