# `loadtest`

`services/loadtest/`, `CMD python -m src.loadtest`, `make loadtest`'s replay role - not a standing compose service.
One of five independent services split from a former single `webhook` service, each with its own Dockerfile/image/`CMD`, sharing code from `services/_common/src/` (`agent_docs/services/common.md`).
Distinct from `services/loadtest-fixtures/` (`agent_docs/services/loadtest-fixtures.md`): that service extracts fixtures FROM ClickHouse; this one replays them AS traffic.

## Per-file breakdown

- `loadtest.py` (`services/loadtest/src/`) - CLI load generator, replays real captured traffic against `webhook`'s own `POST /api/v1/metrics`.
  `FIXTURES_DIR` is read directly here, not part of `common`'s shared `config/` package.
  Always run via `loadtest-runner`, never inline.

`services/loadtest/tests/` has its own pytest suite.
Run with `make test-services` (a separate pytest invocation per service directory - see the `Makefile`), always via `test-runner`, never inline.
