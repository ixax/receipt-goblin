# Split observability and langfuse stacks into dedicated compose files

## Context

`docker-compose.yml` currently holds three logical stacks in one 1173-line file: the core stack (always on), the observability stack (Prometheus, Blackbox, redis-exporter, nginx-exporter, cadvisor, node-exporter, Loki, Alloy — gated behind the `observability` compose profile), and the Langfuse stack (langfuse-db/-clickhouse/-minio/-redis/-worker/-web — gated behind the `langfuse` compose profile).
Both stacks are already opt-in via Makefile targets (`make observability-up`, `make langfuse-up`) and already listed as explicit service groups (`OBSERVABILITY_SERVICES`, `LANGFUSE_SERVICES` in the Makefile).
The goal is to make that opt-in boundary a physical file boundary too: `docker-compose.observability.yml` and `docker-compose.langfuse.yml`, loaded only by the targets that need them, leaving the core `docker-compose.yml` smaller and focused on the always-on stack.
Grafana stays in the core file — it has no `profiles:` entry today, depends only on `clickhouse`, and always starts with `make start`.
Moving it would change default behavior, which is out of scope here.

Verified before planning (see anchor/alias grep across the whole file):

- The 8 observability services use no `x-*` anchor aliases internally — fully self-contained blocks, safe to move verbatim.
- The 6 langfuse services use anchors defined in the `x-langfuse-*` block (lines 105–125), but two of those anchors are also aliased from core services: `x-langfuse-port` (`&langfuse-port`, line 108) is used by `load-balancer`, and `x-langfuse-base-url` (`&langfuse-base-url`, line 125) is used by `litellm`.
  Those two anchors must stay in core `docker-compose.yml`; only the anchors used exclusively inside the langfuse block move with it.
- `x-prometheus-port` (line 103) is likewise only aliased by `load-balancer` — stays in core.
- Docker Compose merges top-level `networks:`/`volumes:` sections by key across multiple `-f` files in a single invocation, so the split files just need a `networks: { receipt-goblin: {} }` stub referencing the same network the core file fully defines (driver/ipam).
  No `external: true` is needed, since all files are always passed together in one `docker compose` command.

## Changes

### 1. `docker-compose.observability.yml` (new)

Move these service blocks out of `docker-compose.yml` verbatim, in the same order: `prometheus`, `blackbox`, `redis-exporter`, `nginx-exporter`, `cadvisor`, `node-exporter`, `loki`, `alloy` (currently lines ~731–937, including their comments).
The `alloy` service's `depends_on: loki` stays intact since both move together.

Add:

- A short header comment: what this file is, that it's only loaded via `make observability-*` (which always includes the core file's `-f` flags too), and that it depends on core services (`redis`, `load-balancer`) being up.
- Top-level `volumes:` with `prometheus-data:` and `loki-data:` (moved from core's volumes list).
- Top-level `networks: { receipt-goblin: {} }` stub.

No anchors move here — none are used internally by these 8 services.

### 2. `docker-compose.langfuse.yml` (new)

Move the 6 langfuse service blocks (currently lines ~939–1140) verbatim, plus the anchors used only within them: `x-langfuse-clickhouse-http-port`, `x-langfuse-clickhouse-native-port`, `x-langfuse-clickhouse-user`, `x-langfuse-clickhouse-password`, `x-langfuse-db-password`, `x-langfuse-db-url`, `x-langfuse-redis-password`, `x-langfuse-minio-user`, `x-langfuse-minio-password`, `x-langfuse-salt`, `x-langfuse-encryption-key`, `x-langfuse-nextauth-secret` (lines 109–124, excluding 108/125 — see Context).
The `&langfuse-env` merge anchor (currently inline on `langfuse-worker`'s `environment:`, aliased by `langfuse-web` via `<<: *langfuse-env`) moves with both services since they move together.

Add:

- Header comment analogous to the observability file's, noting `litellm` (core) reads `LANGFUSE_HOST`/`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` as optional env vars.
  Langfuse tracing is soft-opt-in from litellm's side regardless of whether this file is loaded.
- Top-level `volumes:` with `langfuse-db-data`, `langfuse-clickhouse-data`, `langfuse-clickhouse-logs`, `langfuse-minio-data`, `langfuse-redis-data`.
- Top-level `networks: { receipt-goblin: {} }` stub.

### 3. `docker-compose.yml` (core, trimmed)

- Remove the 14 moved service blocks and the corresponding volume entries from the top-level `volumes:` list (keep `clickhouse-data`, `grafana-data`, `litellm-db-data`, `redis-data`, `loadtest-fixtures-data`).
- Remove the moved `x-langfuse-*` anchors (keep `x-prometheus-port`, `x-langfuse-port`, `x-langfuse-base-url` — still aliased by `load-balancer`/`litellm`).
- Update the inline comments near those three retained anchors and near `litellm`'s `LANGFUSE_HOST` line to point at the new file names.
  They currently say "opt-in, see 'Observability'/'Langfuse' in README.md" — extend to mention `docker-compose.observability.yml` / `docker-compose.langfuse.yml`.
- `networks:` top-level section is untouched — it remains the full `driver: bridge` + `ipam` definition that the two new files' stubs merge into.

### 4. `Makefile`

- Add two new compose-file variables next to `COMPOSE_FILES`: `OBSERVABILITY_COMPOSE_FILES := $(COMPOSE_FILES) -f docker-compose.observability.yml` and `LANGFUSE_COMPOSE_FILES := $(COMPOSE_FILES) -f docker-compose.langfuse.yml`.
- `langfuse-up`/`langfuse-down`/`langfuse-logs` (lines 166–177): swap `$(COMPOSE_FILES)` → `$(LANGFUSE_COMPOSE_FILES)`.
- `observability-up`/`observability-down`/`observability-logs`/`observability-status` (lines 183–197): swap `$(COMPOSE_FILES)` → `$(OBSERVABILITY_COMPOSE_FILES)`.
- `--profile langfuse` / `--profile observability` flags stay (per your answer — keep as a belt-and-suspenders gate even though the file itself now also gates it).
- `stop down: check-env langfuse-down observability-down` (line 156) needs no change — it just depends on the two targets above, which now carry the right `-f` flags themselves.
- `backup-grafana`/`restore-grafana`/`backup-all` (lines 374–394) are untouched — Grafana and the `backup` service both stay in core.
- `LANGFUSE_SERVICES`/`OBSERVABILITY_SERVICES` variables (lines 83/88) are unchanged — service-name lists work the same regardless of which file defines them.

### 5. `README.md`

- `## Langfuse` section: mention that the stack now lives in `docker-compose.langfuse.yml`, loaded automatically by `make langfuse-*`.
- `## Observability` section: same for `docker-compose.observability.yml`.
  While touching this section, also fix an existing drift noticed during research — its service table currently omits `nginx-exporter` even though it's one of the 8 services in `OBSERVABILITY_SERVICES`.
- "Start the stack" / "Stop the stack" sections: no behavioral wording changes needed (Grafana still always-on, Langfuse/observability still opt-in) — just double-check no sentence assumes a single compose file.

### 6. `agent_docs/`

- `agent_docs/services/observability.md` and `agent_docs/services/langfuse.md` (currently short stubs): add the new file name each stack lives in.
  Fix `observability.md`'s existing omission of `cadvisor`/`redis-exporter`/`nginx-exporter` from its service list while there.
- `agent_docs/services/load-balancer.md`: light wording update where it says "opt-in `observability` profile" / "opt-in `langfuse` profile" to also name the compose file.

### 7. `.claude/agents/dev-ops.md`

- "Profile-scoped stacks (Langfuse/observability)" section: note each stack now has its own compose file (`docker-compose.observability.yml`, `docker-compose.langfuse.yml`) in addition to the profile flag.
- "Editing the `Makefile` and `docker-compose.yml`" section: extend dev-ops's ownership to explicitly cover the two new files (still sole owner of edits to all compose files, not just the core one).

## Verification

1. `docker compose -f docker-compose.yml -f docker-compose.dev.yml -f docker-compose.observability.yml -f docker-compose.langfuse.yml config` — confirms the merged YAML parses cleanly (anchors resolve within their own file, networks/volumes merge without conflicts) before touching running containers.
2. `make observability-up` → `make observability-status` → confirm all 8 containers healthy and reachable (Prometheus UI via load-balancer, Grafana's Prometheus/Loki datasources still work) → `make observability-down`.
3. `make langfuse-up` → confirm `langfuse-web` reachable at `:3001` via load-balancer, `litellm` still logs traces (LANGFUSE_HOST resolves langfuse-web across the merged network) → `make langfuse-down`.
4. `make stop` (or `make down`) from a state with both stacks up — confirm the courtesy teardown still tears down all three stacks correctly.
5. Grep the repo afterward for any remaining bare `docker-compose.yml` mentions in docs that imply a single file, to catch anything the research passes missed.
