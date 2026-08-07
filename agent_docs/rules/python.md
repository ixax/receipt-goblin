# Python conventions

Full rule content for `AGENTS.md`'s "## Python" pointer.
Read before writing or editing Python code in this repo.
Skip for a pure analysis/investigation task that touches no Python code.

## Interpreter & tooling

- NEVER USE BARE `python`/`python3`/`pip install`.
- Every Python invocation in this repo runs via `uv run` (`uv run python3 ...`, `uv run pytest`, `uv run ruff`, etc.) - even stdlib-only scripts with no third-party deps, since the point is pinning the interpreter to `.python-version`, not just resolving dependencies.
- If `uv` isn't installed: `make install-uv`
- Python version: `.python-version`
- Every Python-based `Dockerfile`'s `FROM python:${PYTHON_VERSION}-slim` reads that value via a `PYTHON_VERSION` build-arg, propagated through `Makefile` and `docker-compose.yml`'s `build.args`
- bump `.python-version` -> run `make build`, every image rebuilds against the new version in one shot

## Dependencies

Service dependency edits are a three-step chain - stopping early leaves the change with no effect anywhere:

1. Edit `services/<svc>/requirements.txt` - direct deps only, keep the `why` comments.
2. Run `make lock` - regenerates `requirements.lock` (full transitive pin).
   Images install from the `.lock`, never the `.txt`, so an unlocked edit reaches nothing.
   Commit both files together - `.githooks/lib/check-lock.sh` fails the commit otherwise.
3. Hand the rebuild to `dev-ops` - a lock change is a baked-in-file change like any other, so it only reaches a running container via its rebuild.
   Never rebuild inline instead.

## Style

- Stdlib `logging`, never `print()`; bare `LOG_LEVEL` env var (`agent_docs/services/common.md`).
- `services/_common/src/fastjson.py`, never stdlib `json` (`dumps()` returns `bytes`).
- Every `webhook`/`webhook-worker` tunable in `services/_common/src/config/`, never scattered `os.environ`.

## Anti-patterns

- No per-service env defaults - `docker-compose.yml` is the only place `CLICKHOUSE_*`/`REDIS_*` defaults live.
- Never derive cost from a local price table - use LiteLLM's own `response_cost`/`cost_breakdown` (`agent_docs/incidents.md`).

## Encoding

- Every `open()`/`Path.read_text()`/`Path.write_text()` call passes an explicit `encoding="utf-8"`.
- A write of a file meant to stay LF-only (e.g. committed JSON/config) also passes `newline=""`.
- Without it, Python falls back to the OS locale encoding: `UnicodeDecodeError` on non-ASCII content on Windows (cp1252), and a silent CRLF corruption of LF-only committed files on write.
- Reference: `services/grafana/scripts/query_perf.py` and its siblings `parse_dashboard.py`, `build_query_perf_dashboard.py`, `extract_panel_tree.py`, `tag_panel_queries.py` in `services/grafana/scripts/`.
