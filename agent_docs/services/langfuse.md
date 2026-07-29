# Langfuse profile (`langfuse-minio`, `langfuse-redis`, + compose-only services)

Standard Langfuse stack (LLM trace/session viewer), opt-in `langfuse` compose profile, fed by `litellm`'s native Langfuse callback.
No custom logic worth documenting beyond Langfuse's own docs - managed via `make langfuse-up`/`-down`/`-logs`, or `dev-ops` for anything beyond that.
`langfuse-clickhouse` is Langfuse's own separate ClickHouse instance, unrelated to this stack's main `clickhouse` service/schema (`agent_docs/services/clickhouse.md`).
