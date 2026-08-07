# `migrate`

`services/migrate/`, `CMD python -m src.migrate`, the `clickhouse-migrate` compose service.
One of five independent services split from a former single `webhook` service, each with its own Dockerfile/image/`CMD`, sharing code from `services/_common/src/` (`agent_docs/services/common.md`).

## Per-file breakdown

- `migrate.py` (`services/migrate/src/`) - ClickHouse migration runner for the `clickhouse-migrate` service, applies `services/clickhouse/migrations/*.sql` in order.
  Never touches users/roles/grants (see `agent_docs/services/clickhouse.md`'s `init` section for why that's a separate, `make init`-only concern).
- `reset_data.py` (`services/migrate/src/`) - explicit allowlisted `TRUNCATE` path used by `make reset-tracking-data CONFIRM=RESET-TRACKING-DATA`.
  It removes usage/session/trace/raw rows while preserving schema migrations, users, Dictionaries, Grafana data, and LiteLLM keys.

`services/migrate/tests/` covers the reset confirmation guard and table allowlist.
Run `make migrate` to apply migrations standalone after adding a new migration file.
Migrations are also applied automatically at the end of `make init` on a fresh setup.
