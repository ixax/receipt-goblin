# `reparse`

`services/reparse/`, `CMD python -m src.reparse`, the `metrics-reparse` compose service.
One of five independent services split from a former single `webhook` service, each with its own Dockerfile/image/`CMD`, sharing code from `services/_common/src/` (`agent_docs/services/common.md`).

## Per-file breakdown

- `reparse.py` (`services/reparse/src/`) - CLI for the `metrics-reparse` service, replays `ingest_raw`'s full stored payloads back through `common.ingest_db.reparse_event()` after a parsing bug fix, without needing the original LiteLLM webhook POST again.
  `REPARSE_CHUNK_SIZE` is read directly here, not part of `common`'s shared `config/` package.

`services/reparse/tests/` has its own pytest suite.
Run with `make test-services` (a separate pytest invocation per service directory - see the `Makefile`), always via `runner-test-services`, never inline.
