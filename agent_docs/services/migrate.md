# `migrate`

`services/migrate/`, `CMD python -m src.migrate`, the `clickhouse-migrate` compose service.
One of five independent services split from a former single `webhook` service, each with its own Dockerfile/image/`CMD`, sharing code from `services/_common/src/` (`agent_docs/services/common.md`).

## Per-file breakdown

- `migrate.py` (`services/migrate/src/`) - ClickHouse migration runner for the `clickhouse-migrate` service, applies `services/clickhouse/migrations/*.sql` in order.
  Never touches users/roles/grants (see `agent_docs/services/clickhouse.md`'s `init` section for why that's a separate, `make init`-only concern).

`services/migrate/` has no pytest suite of its own.
Run `make migrate` to apply, explicit-only.
