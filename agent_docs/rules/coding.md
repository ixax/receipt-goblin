# Code style & anti-patterns

Full rule content for `AGENTS.md`'s "Code & anti-patterns" pointer.
Read before writing or editing code in this repo.
Skip for a pure analysis/investigation task that touches no code.

## Style

- Stdlib `logging`, never `print()`; bare `LOG_LEVEL` env var (`agent_docs/services/webhook.md`).
- `services/webhook/src/fastjson.py`, never stdlib `json` (`dumps()` returns `bytes`).
- Every `webhook`/`webhook-worker` tunable in `config.py`, never scattered `os.environ`.
- Skills/agents/config stay CLI-agnostic - exception: names the CLI itself defines.
- Comments cover a non-obvious *why*, never this machine's current state.

## Anti-patterns

- No per-service env defaults - `docker-compose.yml` is the only place `CLICKHOUSE_*`/`REDIS_*` defaults live.
- No TTL-based auto-delete on any table - half-year `PARTITION BY` instead.
- No per-service `README.md` under `services/*/` - playbooks live in root README.
- Never loosen `_validate_readonly_sql` in `services/mcp-dev/src/server.py`.
- Never derive cost from a local price table - use LiteLLM's own `response_cost`/`cost_breakdown` (`agent_docs/incidents.md`).
- Never restart/recreate `clickhouse` or edit a dashboard as a side effect of other work.
- Never call `docker compose build up / start / restart logs status` directly - always `make build / up start / restart / logs / status`.
