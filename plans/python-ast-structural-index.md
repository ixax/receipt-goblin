---
date: 2026-08-05
context: ""
---

# Python AST structural index ("ast-index")

## Context

The user asked (in Russian) whether it's feasible to build an auto-generated, self-refreshing AST-tree of the repo so Claude Code could query a cached structural index instead of reading files directly, regenerating only what was touched after an edit.

Investigation found the literal goal ("never read files directly") isn't achievable.
The `Edit` tool needs an exact verbatim match against real file content, which a structural digest can't supply.
Genuine logic/comment understanding also needs real source.
The underlying mechanism is real and already proven in this repo: `hooks/harness_audit/sync_hook.py` is a `PostToolUse` hook that regenerates `agent_docs/harness-index.md` after every relevant edit.
This plan adapts that exact pattern into an incremental, per-file cache instead of a full-rebuild-every-time one.

Scope narrowed through conversation to: all 78 Python files in the repo (~11.8k lines, largest file 1,333 lines), owned by a new agent + skill, with the cache **committed to git** (not the usual gitignored `.claude/data/` scratch convention) and a `.githooks/pre-push` hook doing partial/diff-based regeneration as a commit-time safety net, alongside a Claude-Code-session `PostToolUse` hook for live freshness during active work.
This is explicitly a first step toward porting the same pattern to other repos/languages (JS, PHP) later.
This plan stays Python-only, but keeps the parsing core swappable so that port doesn't require redesigning the surrounding cache/hook/query layers.

Measured directly (stdlib `ast.parse`, no I/O beyond reading source): parsing all 78 files takes **48.6ms total** (0.62ms/file average); the single largest file (1,333 lines) parses in **3.3ms**.
Rebuild speed is a non-issue at this repo's current size.
Incrementality here is about avoiding git-diff noise and building a pattern that scales to larger codebases later, not local latency.

## What this is / isn't

A navigation aid, not a `Read` replacement.
It answers "what's in this file / where is X defined" without a full read, so an agent can decide whether — and where — to `Read`/`Grep` at all.
It never substitutes for `Read` before an `Edit` (verbatim `old_string` needs real source), and it deliberately omits full docstring bodies / implementation logic needed for real code comprehension before a change.

## Cache format & location

`agent_docs/ast_index/` — **tracked in git**, mirroring the existing `agent_docs/harness-index.md` precedent for committed, agent-regenerated artifacts (unlike `.claude/data/`, which is gitignored scratch per `.gitignore:3` and was the wrong home once "must be committed" was confirmed).

```
agent_docs/ast_index/
  cache/
    manifest.json
  manifests/
    services/_common/src/ingest_parsing.py.json
    hooks/harness_audit/sync_hook.py.json
    hooks/ast_index/sync_hook.py.json
    ...
```

`<relpath>.json` (append, not swap, the extension) avoids collisions and needs no path sanitization — just `mkdir -p` the parent dirs.

`manifest.json` — flat map, one entry per tracked file, for fast staleness checks without re-parsing everything:

```json
{
  "schema_version": 1,
  "generated_at": "...",
  "files": {
    "services/_common/src/ingest_parsing.py": {
      "sha256": "...",
      "size": 41022,
      "cache_path": "services/_common/src/ingest_parsing.py.json"
    }
  }
}
```

`sha256` (not mtime) is the staleness signal — content hash is authoritative, mtime is only a cheap first check to skip re-hashing unchanged files.

Per-file JSON — comprehensive-but-flat, the "store everything once" layer every query view projects from:

```json
{
  "path": "services/_common/src/ingest_parsing.py",
  "sha256": "...",
  "line_count": 1333,
  "module_docstring": "...",
  "imports": [{"kind": "from", "module": "dataclasses", "names": ["dataclass"], "line": 10}],
  "module_constants": [{"name": "_AGENT_ID_RE", "line": 15}],
  "classes": [
    {
      "qualname": "EventContext",
      "line_start": 305, "line_end": 318,
      "decorators": ["dataclass"], "bases": [], "docstring": null,
      "methods": [
        {"qualname": "EventContext.__init__", "signature": "(self, session_id: str, trace_id: str)",
         "args": [...], "returns": null, "decorators": [], "docstring": null,
         "is_async": false, "line_start": 306, "line_end": 308}
      ]
    }
  ],
  "functions": [
    {"qualname": "_to_dt", "signature": "(epoch_seconds: Optional[float]) -> datetime",
     "args": [...], "returns": "datetime", "decorators": [], "docstring": null,
     "is_async": false, "line_start": 79, "line_end": 83}
  ]
}
```

Fields chosen so every query view (below) is a pure filter over this one document, with no re-parsing per view.
Classes are walked one level deep (their direct methods), not recursively into nested functions — deliberately flat.

## Scripts

New module, filename `ast_index.py`, living in the `scripts/` directory next to the existing `sync_harness.py`.

Stdlib only (`ast`, `json`, `hashlib`, `argparse`, `pathlib`) — no new dependency, no `.lock` change, matches this repo's convention for `scripts/*` (plain `python3`, no `uv run`).

**`build` subcommand:**

- `--file <relpath>` — single-file incremental regen (what both hooks call).
  Computes sha256; skips the write entirely if content is unchanged from the manifest record (no mtime churn on a no-op edit); otherwise writes the per-file cache entry and updates just that file's manifest entry.
- `--all` — full rebuild: walks the repo excluding `.venv`/`__pycache__`/`node_modules`/`.git`, writes every entry plus a fresh manifest, and prunes cache entries whose source no longer exists (handles renames/deletes, which the incremental path can't).
- `--check` — exits 1 if any tracked file's on-disk sha256 disagrees with the manifest, without writing anything (used by the pre-push hook).
- A file that fails to parse (`SyntaxError`) is recorded with an `"error"` field instead of a cache entry — never aborts a `--all` run over one bad file.

**`query` subcommand** — the "digest depending on what you want to see" layer:

```
python3 ast_index.py query <relpath> --view outline      # qualnames + line ranges only
python3 ast_index.py query <relpath> --view signatures   # + full signature + 1-line docstring
python3 ast_index.py query <relpath> --view full         # entire stored document
python3 ast_index.py query <relpath> --symbol Class.method
python3 ast_index.py query --grep <substring>            # cross-file qualname search
```

Plain-text output by default (line-oriented, cheap for an agent to read), `--json` for programmatic use.
A stale cache entry (sha256 mismatch) still returns its (possibly outdated) result but prints a `STALE: run build --file <path>` warning to stderr — never silently wrong, never blocks the read.

## Hooks (two, different jobs)

**1. Session-time freshness**, filename `sync_hook.py` under a new `hooks/ast_index/` directory (Claude-Code `PostToolUse`, matcher `Edit|Write`), a direct structural mirror of `hooks/harness_audit/sync_hook.py`.
It reads `tool_input.file_path` from stdin JSON, checks it's a tracked `.py` file (not under `.venv`/`__pycache__`/`node_modules`), and runs the `build --file <rel>` subcommand — exit 2 with stderr on failure, silent 0 on success.
Wired in `.claude/settings.json`'s existing `PostToolUse` → `Edit|Write` block, alongside `audit_hook.py`/`sync_hook.py` (harness).
**This edit is `.claude/` scope, so it goes through `harness-expert`, not applied inline.**

Without this hook the cache goes stale the moment an edit happens mid-session, defeating the "query the cache instead of reading" goal, so it stays even though the cache is committed and also checked at push time (below).
This mirrors exactly how the existing `sync_hook.py` already keeps a *committed* file (`agent_docs/harness-index.md`) live-updated during a session — same precedent, not a new pattern.

**2. Commit-time guarantee** — `.githooks/pre-push` + `.githooks/lib/check-ast-index.sh`, matching this repo's established git-hook style exactly.
`pre-commit` calls `"$(dirname "$0")/lib/check-lock.sh" || exit 1` — a standalone executable script communicating via exit code, not a sourced shell library; confirmed convention, previously noted as a preference after `.githooks/pre-commit` had a sourcing bug.
`check-ast-index.sh`:

1. Reads the pushed refs from stdin (git's pre-push protocol).
2. Computes changed `.py` files via `git diff --name-only` between the remote and local sha.
3. Runs the `build --file <path>` subcommand for each changed file.
4. If that leaves anything uncommitted (`git status --porcelain -- agent_docs/ast_index/`), **blocks the push** with a message to review, commit, and push again.

Deliberately does **not** auto-commit.
A pre-push hook runs after commits already exist, so silently amending or creating a surprise commit at push time would violate this repo's git-safety norms and diverge from the existing `check-lock.sh`/`check-uv.sh` fail-and-instruct style (they check and block, they don't auto-fix).
This does perform real generation (not just a check) — it just stops short of committing on the developer's behalf.

## New agent — `python-structure-navigator`

Created via `harness-expert` (owns every `.claude/agents/*.md` file), not written freehand.

- **Tools**: `Bash` (run `ast_index.py` query/build), `Read` (fallback when the digest isn't enough — actual source, or the skill's usage docs), `Grep`/`Glob` (fallback for non-Python files or patterns the flat schema doesn't index, e.g. text inside docstrings), `Skill` (load the `ast-index` skill for query-CLI conventions).
  Deliberately **no `Write`/`Edit`** — all cache writes go through the `build` subcommand, keeping schema/manifest invariants enforced in one place.
  Deliberately **no `Agent`** — it's a leaf, doesn't dispatch further.
- **Model**: same tier as `code-locator` (`claude-haiku-4-5`) — mechanical lookup, not deep reasoning.
- **Description** (trigger conditions, distinguishing it from `code-locator`): structural digest lookups for Python files via the ast-index cache — "what does file X define", "where is Class.method defined" — before falling back to a raw `Read`/`Grep`.
  `code-locator` remains the small-targeted-search generalist across any file type with no index, judging relevance by reading matches; this agent answers structural-shape questions about Python specifically from a precomputed cache.
  MUST BE USED PROACTIVELY when a query reports broad staleness or the cache is entirely absent (runs `build --all` itself).
  SKIP for non-Python files, "why does this do X" semantic questions, and anything needing an edit.

## New skill — `ast-index`

Canonical location: the `ast-index` subdirectory under `.agents/skills/`, filename `SKILL.md` — same canonical-copy convention as every other skill (`.claude/skills` is a directory-level symlink to `.agents/skills`, so the real copy is picked up from there).
Created via `harness-expert`.
Documents: cache layout/schema (points at the `ast_index.py` module's own docstring as source of truth rather than duplicating it), the query CLI with one worked example per view, staleness/rebuild handling (`STALE` self-heal via `build --file`, when `--all` is warranted — fresh clone, missing cache dir, drift after a non-hook change like `git pull`), and a short forward-looking note.
The extraction core (`ast.parse` → schema above) is the only Python-specific piece.
Cache layout, manifest, hook shape, and query-view CLI are already language-agnostic by construction, so porting to JS/PHP later means writing a new extractor emitting the same JSON shape.
This is noted as an observation for later, not built now (no `--lang` flag, no extractor registry).

## Makefile (via `dev-ops`)

New `ast-index` target running `python3` against the `ast_index.py` module's `build --all` form (module lives under `scripts/`, see Scripts section above).
Add to `.PHONY` and the README's make-targets table.
Also wire the new hook's test directory into `test-hooks` (currently a single `unittest discover -s hooks/harness_audit/tests`).
**Not** wired into `make init` — the cache self-heals via the session hook and is checked at push time, so forcing a rebuild into every fresh-clone bootstrap would add an unrequested step for a self-healing cache.

## Harness routing (via `harness-expert`)

`AGENTS.md` (the actual routing-table file — no `CLAUDE.md` exists at repo root): add `python-structure-navigator` to the PROACTIVE list, add `ast-index` to the SKILLS list.
`agent_docs/harness-index.md` needs no manual edit: the existing `sync_hook.py` already fires on any new `.claude/agents/*.md` file, so creating the agent through `harness-expert` triggers that regen automatically.

Not done now, flagged as a future follow-up for `harness-expert`'s judgment: a one-clause addition to `code-locator`'s description noting it should defer Python structural questions to the new agent.

## Explicit scope boundaries

- No multi-language support now — no `--lang` flag or extractor registry, just a single swappable extraction function so a future port doesn't require redesigning the rest.
- No call-graph / cross-reference index — materially different, more expensive whole-program analysis; not part of this ask.
- Does not redesign `code-locator` — only names the future touch point.
- Does not eliminate `Read` before `Edit`, ever.
- Does not track non-`.py` files.
- No auto-commit at pre-push — generates and blocks, never commits on the developer's behalf.

## Verification

1. Run the full rebuild (`build --all`) — confirm 78 entries in `manifest.json`, no parse errors, matches `find . -name '*.py' -not -path '*/.venv/*' | wc -l`.
2. Edit exactly one file via the `Edit` tool; confirm via `manifest.json` that only that file's entry changed (spot-check 3-4 other cache files' content untouched).
3. Force one hook failure deliberately (temporarily rename the `ast_index.py` module, trigger an edit, confirm exit 2 + stderr surfaces), then restore and confirm success is silent.
4. Run all four query views against the largest file (`ingest_parsing.py`, 1,333 lines); confirm each is a strict projection of `--view full`, and `--symbol EventContext.__init__` returns exactly that entry.
5. `query --grep <a known top-level function name>` returns the right file+line, no false positives.
6. Hand-edit a source file outside the hook path (raw filesystem write), confirm `query` emits `STALE`, confirm `build --file` clears it.
7. Run `build --all` then `build --check` — clean exit; hand-modify one file, re-run `--check` — non-zero, names the stale file.
8. Simulate `.githooks/pre-push`: stage a `.py` change, commit, run the hook script manually against fake ref input, confirm it regenerates and blocks (exits non-zero) when the regenerated cache isn't yet committed, and passes cleanly once it is.
9. Dispatch `python-structure-navigator` with a real structural question about `ingest_parsing.py` and confirm it answers from `query` output without a raw full-file `Read`.
10. After `harness-expert` creates the agent file, run `scripts/sync_harness.py --check` and confirm `agent_docs/harness-index.md` picked up the new row with no manual edit.

## Implementation log

Implemented 2026-08-05.
`scripts/ast_index.py`, `hooks/ast_index/sync_hook.py` (+ 8 passing tests), and `.githooks/pre-push` + `.githooks/lib/check-ast-index.sh` are in place.
`build --all` populated `agent_docs/ast-index/` with 80 tracked files and 0 parse errors (78 was this doc's original count; the file total drifted upward since it was written).
The new agent, skill, and Makefile/README/AGENTS.md wiring were delegated to `harness-expert` and `dev-ops` per the ownership boundaries above.

All 10 verification checklist items above passed, after fixing one real bug they surfaced.
`query_view`/`query_symbol` accessed `entry['signature']` directly, which crashed with `KeyError: 'signature'` on any class entry (only functions/methods carry that key) - fixed both call sites to use `entry.get('signature', '')`.
Item 9's literal `--symbol EventContext.__init__` target doesn't exist in the AST: `EventContext` is a bare `@dataclass` with no explicit `__init__`, so that symbol was never going to be found - not a bug, just a mismatch between this doc's example and the actual source.
`python-structure-navigator` answered a real structural question about `services/_common/src/ingest_parsing.py` correctly via the query CLI.
The isolated pre-push simulation confirmed both the block path (unregenerated cache after a source change) and the clean-pass path.
File count is now 83 (was 78 when this doc was written, 80 at initial `build --all`) - ordinary repo growth, not drift to fix.

### Restructuring: agent_docs/ast-index/ -> agent_docs/ast_index/{cache,manifests}

After initial implementation, the user disliked the flat `agent_docs/ast-index/{manifest.json, files/**/*.json}` layout - JSON sitting directly next to a `files/` name gave no sense of a shadow tree mirroring the repo.
Renamed the storage directory to `agent_docs/ast_index/{cache/manifest.json, manifests/**/*.json}`: `cache/` holds the manifest (the index), `manifests/` is the shadow JSON tree, one document per source file.
(An intermediate step briefly used `agent_docs/ast/` for the top-level directory; the user then asked for `ast_index` to match the script/hook naming instead, so that name won out.)
Every reference across the repo was updated to match: `scripts/ast_index.py` (constants, `save_manifest()`, docstring), `hooks/ast_index/sync_hook.py` and its tests, `.githooks/lib/check-ast-index.sh`, this design doc, the `python-structure-navigator` agent, the `ast-index` skill, `AGENTS.md`, the Makefile comment, and the README make-targets table.
The old `agent_docs/ast-index/` directory was deleted (fully untracked in git) and the cache was rebuilt fresh at the final location via `build --all`.

`scripts/ast_index.py` itself was never renamed to `ast.py`: the script does `import ast` (the stdlib module), and running it directly puts its own directory first on `sys.path`, so a file named `ast.py` there would shadow the stdlib module it depends on.
Naming convention confirmed with the user: `.py` files and their containing directories use underscores (`ast_index`), skill directories may use hyphens (`ast-index`) - both were already correct before this rename, only the storage-directory name changed.
