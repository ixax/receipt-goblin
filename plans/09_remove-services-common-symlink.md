# Remove the `services/common` symlink, resolve `common` via `services/_common/src` directly

## Context

`services/common` is a git-tracked symlink (`services/common -> _common/src`) that exists for exactly one reason: locally-run pytest suites do `from common import X`, and Python needs a directory literally named `common` on `sys.path` to satisfy that.
Docker never uses the symlink.
Every Dockerfile does `COPY services/_common/src/ ./common/`, and `docker-compose.dev.yml` bind-mounts `./services/_common/src:/app/common:ro` — both already name `_common/src` directly and only create the `common/` directory inside the container.

The user wants the symlink gone, with `services/_common` used as the one real, canonical path everywhere.

**Why not just rename `services/_common/src` → `services/_common/common`?**
That was the first approach considered: make the real directory literally named `common`, matching what containers already expect.
It was rejected after a repo-wide search turned up **127 lines across 53 files** that reference the path `services/_common/src/...` in prose (e.g. "see `_agent_invocations_from_messages` in `services/_common/src/ingest_parsing.py`"), including:

- `schema.sql` comments and every migration file
- `README.md`
- `agent_docs/**`
- `plans/**`
- the ast-index cache/manifests (auto-generated)
- embedded `rawSql` comments inside `services/grafana/dashboards/agents_overview.json`

Renaming `src` would turn every one of those into a stale reference for no functional benefit, and several of those files (dashboard JSON, ast-index cache) aren't meant to be hand-edited.
`services/_common/src` stays exactly as-is.

**The actual fix**: replace the "symlink resolves `common`" trick with an equivalent "`sys.path` + a directory named `common`" trick, using `importlib` to register `services/_common/src` directly into `sys.modules["common"]` — no directory named `common` required at all, symlinked or real.
This is a ~6 line, well-documented stdlib recipe (`importlib.util.spec_from_file_location` with `submodule_search_locations`, then `module_from_spec` + `sys.modules[...] = module` + `exec_module`).
`__file__` for every submodule resolves to the exact same real path as today's `.resolve()`-through-symlink behavior, so `services/_common/src/config/queue.py`'s existing two-candidate `queue.yml` lookup (`_here.parent.parent` vs `_here.parent.parent.parent`) needs no logic change — only its comment, which currently explains the symlink, needs rewording.

Net effect: delete the symlink, touch 6 `conftest.py` files plus 1 comment block in `queue.py`.
Nothing else changes — not Dockerfiles, not `docker-compose.dev.yml`, not any of the 127 doc references to `_common/src`, not the Makefile (`test-services` already iterates `_common`, never `common`).

## Files to change

### 1. Delete the symlink

`git rm services/common` (tracked as git mode `120000`, points to `_common/src`).

### 2. Six `conftest.py` files — replace the "add services root to sys.path" trick with the importlib alias

- `services/_common/tests/conftest.py` (lines 16-19)
- `services/webhook/tests/conftest.py` (lines 20-21)
- `services/reparse/tests/conftest.py` (lines 11-18)
- `services/worker/tests/conftest.py` (lines 13-21)
- `services/loadtest/tests/conftest.py` (lines 4-11)
- `services/mcp-dev/tests/conftest.py` (lines 16-25)

Two current shapes, both replaced the same way.

**Shape A** (`_common`, `webhook`) is a plain `sys.path.insert(0, str(<...>.parent.parent.parent))` aimed at the services root so `import common` finds the symlink.
Replace with the importlib alias, pointed straight at `services/_common/src` (depth varies: 2 parents up from `services/_common/tests/conftest.py` itself, 3 parents up from the other services' `tests/conftest.py`).

**Shape B** (`worker`, `reparse`, `loadtest`, `mcp-dev`) builds `_service_dir` (kept, unrelated — it's for `from src import ...`) and `_services_dir` (the one being removed), then rebuilds `sys.path` with a dedup dance.
The alias no longer needs a second `sys.path` entry at all, so this simplifies to a single `sys.path.insert(0, _service_dir)` (no more dedup dance needed) plus the same importlib alias block.

Representative replacement (`services/worker/tests/conftest.py`):

```python
import importlib.util
import sys
from pathlib import Path

...

_service_dir = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _service_dir)

_common_src = Path(__file__).resolve().parent.parent.parent / "_common" / "src"
_common_spec = importlib.util.spec_from_file_location(
    "common", _common_src / "__init__.py", submodule_search_locations=[str(_common_src)]
)
_common_module = importlib.util.module_from_spec(_common_spec)
sys.modules["common"] = _common_module
_common_spec.loader.exec_module(_common_module)
```

For `services/_common/tests/conftest.py`, `_common_src = Path(__file__).resolve().parent.parent / "src"` (it's already inside `_common/`).

Update each file's explanatory comment (e.g. worker's "Add paths in order: this service first, then services root", webhook's "services/ for common module (from services/_common/src)", mcp-dev's "`from common import ...` inside server.py resolves against the services root") to describe the new mechanism instead of the old symlink/services-root one.

### 3. `services/_common/src/config/queue.py` (lines 9-16)

Reword the comment block — it currently says "Locally, `services/common` is a symlink to services/_common/src. `.resolve()` follows it to the real `services/_common/src/config/queue.py`..." and that symlink no longer exists.
Replace with a short explanation that locally, `conftest.py` registers `common` as an alias directly for `services/_common/src` (same real path either way), so the two-candidate search below is unchanged.
**No logic change** — `_here.parent.parent` / `_here.parent.parent.parent` candidates stay as-is; only the comment explaining *why* two candidates exist needs to stop mentioning a symlink.

## Verification

1. `git status` / `git ls-files -s services/common` — confirm the symlink is gone from the working tree and the index.
2. `make test-services` — runs `webhook`, `worker`, `reparse`, `loadtest`, `_common` each as a separate pytest invocation; this is the thing that actually depends on `common` resolving locally, so it must pass end-to-end (catches any `from common import X` in those suites, e.g. `services/_common/tests/test_ingest_parsing.py`, `services/worker/tests/` etc.).
3. `services/mcp-dev/tests` isn't in `test-services` (separate Python version per Makefile comment) — run it explicitly too: `uv run pytest -c pytest.ini services/mcp-dev/tests` (or whatever interpreter the Makefile comment specifies) to confirm its conftest change also works.
4. Docker path is untouched by this change (Dockerfiles/`docker-compose.dev.yml` never referenced the symlink), so no rebuild/re-run is needed there.
   Still worth a sanity `docker compose config`, or spot-checking one Dockerfile's `COPY services/_common/src/ ./common/` line, to confirm nothing assumed the symlink's presence.
