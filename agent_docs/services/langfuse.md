# Langfuse profile (`langfuse-minio`, `langfuse-redis`, + compose-only services)

Standard Langfuse stack (LLM trace/session viewer), opt-in `langfuse` compose profile, fed by `litellm`'s native Langfuse callback.
No custom logic beyond Langfuse's own docs.
Managed via `make langfuse-up`/`-down`/`-logs`, or `dev-ops` for anything beyond that.
The 6 services are defined in `docker-compose.langfuse.yml`, loaded automatically by those `make langfuse-*` targets alongside the core `docker-compose.yml` - not in the core file itself.
`langfuse-clickhouse` is Langfuse's own separate ClickHouse instance, unrelated to this stack's main `clickhouse` service/schema (`agent_docs/services/clickhouse.md`).
