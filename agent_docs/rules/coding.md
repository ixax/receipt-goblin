Full rule content for `AGENTS.md`'s "Code & anti-patterns" pointer.
Read before writing or editing code in this repo.
Skip for a pure analysis/investigation task that touches no code.
Python-specific style/anti-pattern rules live in `agent_docs/rules/python.md` instead.

## Style

- Skills/agents/config stay CLI-agnostic - exception: names the CLI itself defines.
- Comments cover a non-obvious *why*, never this machine's current state.

## Anti-patterns

- No TTL-based auto-delete on any table - half-year `PARTITION BY` instead.
- No per-service `README.md` under `services/*/` - playbooks live in root README.
- Never loosen `_validate_readonly_sql` in `services/mcp-dev/src/server.py`.
- Never restart/recreate `clickhouse` or edit a dashboard as a side effect of other work.
- Never call `docker compose build up / start / restart logs status` directly - always `make build / up start / restart / logs / status`.
