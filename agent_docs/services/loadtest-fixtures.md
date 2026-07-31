# `loadtest-fixtures`

Standalone service (own image `receipt-goblin-loadtest-fixtures`, own minimal `src/clickhouse_client.py`, own `src/config.py`/`config.yml`), not a `webhook` `APP_ROLE`.
Extracts real, already-ingested traffic from ClickHouse into JSON fixture files for `make loadtest` to replay.

## `config.yml`

`fixtures_chunk_size` (500) - sizes `build_fixtures.py`'s phase-2 `ingest_raw` payload-fetch chunking, same OOM rationale as `services/reparse/src/reparse.py`'s `reparse_chunk_size`.
`fixtures_ttl_hours` (168) - `loadtest-runner` treats a generated fixture set as stale once its manifest's `generated_at` exceeds this age (or its volume doesn't match the requested run) and asks whether to regenerate.

## Env prefix

Every env var this service reads is prefixed `LOADTEST_FIXTURES_`, deliberately not shared with `services/_common/src/config/`'s bare `CLICKHOUSE_*`/etc names since this is an isolated service, not part of the `webhook` family.
Build with `make loadtest-fixtures [VOLUME=small|medium|large]`.
