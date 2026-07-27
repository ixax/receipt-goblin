# Retire `.capture` in favor of ClickHouse-sourced `.loadtest_fixtures`

## Context

`make loadtest` currently replays traffic from `.capture/` — JSON files written
one-per-event by `webhook` (`server.py`, gated by `CAPTURE_ENABLED`/`CAPTURE_DIR`)
whenever that debug flag is on. This is fragile: it only has data if someone
remembered to flip the flag beforehand, it grows unbounded (600MB+ observed),
and it mixes debug-aid and load-test-corpus concerns in one mechanism.

The goal is to retire `.capture` entirely and replace it with an on-demand
extraction tool: pull real, already-ingested traffic straight out of
ClickHouse (`agent_events` + `ingest_raw`) into JSON fixture files, sized by a
`VOLUME` parameter (`small`/`medium`/`large`), always the freshest successful
events, via a `make` target — so a fresh clone (or anyone who never enabled
`CAPTURE_ENABLED`) can still run a load test immediately. `dev-ops` and
`loadtest-runner` both need to know this tool exists; `loadtest-runner`
specifically must check freshness and ask the user whether to (re)generate.

User decisions already made:
- Implement as a **docker-compose one-shot service** (like `webhook-reparse`),
  not a host-side script — reuses `get_client()`/`config.py`/the `ingest`
  ClickHouse role.
- Fixtures live in a **dedicated named Docker volume**, mounted directly into
  both the extraction service (rw) and the `loadtest` service (ro) — not a
  host bind mount, to avoid host/container filesystem translation overhead.
- Volume → row-count mapping: `small=2000`, `medium=20000`, `large=100000`
  successful `agent_events` rows, freshest first.
- Staleness policy: `loadtest-runner` treats fixtures as stale if the
  manifest's age exceeds a TTL **or** its recorded `volume` doesn't match what
  the run needs. The TTL lives in `services/webhook/config.yml` (not
  hardcoded in the agent).

## Avoiding an unindexed scan on `ingest_raw`

`ingest_raw` has no `status` column and no time-ordered sort key (`ORDER BY
(litellm_call_id)` only) — schema.sql says outright it's "never [read] by time
range scan". `agent_events` does have both: `status` and a
`ORDER BY (timestamp, session_id, litellm_call_id)` with `timestamp` leading.
So selection happens in two phases:

1. **Row selection** — `SELECT litellm_call_id, session_id, timestamp FROM
   agent_events WHERE status = 'success' AND litellm_call_id != '' ORDER BY
   timestamp DESC LIMIT <volume count>` — cheap, uses the leading sort key.
2. **Payload fetch** — take the `litellm_call_id`s from step 1, chunk them
   (new `fixtures_chunk_size` in `config.yml`, e.g. 500), and for each chunk:
   `SELECT litellm_call_id, raw_payload_full FROM ingest_raw WHERE
   litellm_call_id IN {ids:Array(String)}` — an `IN` list on `ingest_raw`'s
   own `ORDER BY` column still lets ClickHouse prune granules via the primary
   key, instead of a full-table scan.

This mirrors `reparse.py`'s existing keyset-pagination precedent for reading
`ingest_raw` in bounded chunks rather than one unbounded query.

## New module: `services/webhook/src/build_fixtures.py`

New CLI module, same shape as `reparse.py`:

- `VOLUME_EVENT_COUNTS = {"small": 2000, "medium": 20000, "large": 100000}`
  (also add `fixtures_chunk_size: 500` and `fixtures_ttl_hours: 168` to
  `services/webhook/config.yml`, loaded the same way `REPARSE_CHUNK_SIZE`
  already is in `config.py`).
- `build_fixtures(volume: str) -> dict`: runs the two-phase query above,
  writes one file per event under `FIXTURES_DIR/<sanitized session_id>/
  <event timestamp %Y%m%dT%H%M%S%f>-<sha1(litellm_call_id)[:8]>.json`
  containing `raw_payload_full`'s bytes **verbatim** (it's already the
  original JSON string — no reserialization needed, unlike the old capture
  write path). Session-id sanitization: port the same
  `_UNSAFE_SESSION_ID_CHARS` regex approach `server.py` used (that file's copy
  is being deleted, so this becomes `build_fixtures.py`'s own small local
  helper — session_id is still client-supplied data, so this is a real path-
  traversal concern, not defensive dead code).
  Writes `FIXTURES_DIR/manifest.json`: `{"volume", "event_count",
  "session_count", "generated_at" (UTC ISO8601), "newest_event_timestamp",
  "oldest_event_timestamp"}`.
- Progress reporting: total is known upfront (row count from phase 1) —
  a single redrawing line gated on `sys.stdout.isatty()` (`\r` carriage
  return), falling back to one `logger.info` line per chunk otherwise. Same
  "no new dependency, isatty-gated" principle as
  `scripts/wait_for_stack_healthy.py`'s `_LiveTable`, just a single counter
  instead of a multi-row table (there's only one thing to track here).
- `--status` flag (or `python -m src.build_fixtures --status`): reads and
  prints `FIXTURES_DIR/manifest.json` verbatim (or reports none exists),
  doesn't touch ClickHouse — this is what `loadtest-runner` calls to check
  freshness without regenerating.
- `main()`: `VOLUME` env var (default `medium`), same env-var-with-CLI-
  override pattern as `reparse.py`'s `SESSION_ID`.

## Removing `.capture` entirely

- `services/webhook/src/config.py`: delete `CAPTURE_DIR`/`CAPTURE_ENABLED`;
  add `FIXTURES_DIR = Path(os.environ.get("FIXTURES_DIR",
  "/app/loadtest_fixtures"))`.
- `services/webhook/src/server.py`: delete `_write_capture_file`,
  `_safe_session_dir_name`, `_UNSAFE_SESSION_ID_CHARS`, the `CAPTURE_DIR.mkdir`
  startup line, and the `CAPTURE_ENABLED`-gated call in `receive_metrics()`;
  drop the now-unused imports.
- `services/webhook/src/loadtest.py`: import `FIXTURES_DIR` instead of
  `CAPTURE_DIR`; delete `_is_success_capture`/`_STATUS_PREFIX_BYTES`/
  `_SUCCESS_STATUS_RE` (fixtures are pre-filtered to `status=success` at
  extraction time now); simplify `load_session_corpus` to drop the per-file
  status-peek filter (keep `_filename_epoch`-based gap computation — same
  filename convention is preserved); rename `--capture-dir`/
  `CAPTURE_DIR_OVERRIDE` → `--fixtures-dir`/`FIXTURES_DIR_OVERRIDE`; update
  the module docstring's "replays real captured traffic from CAPTURE_DIR"
  framing.
- `services/webhook/tests/test_loadtest.py`: remove the `_is_success_capture`
  test block; adjust `load_session_corpus` tests for the simplified
  (no-filter) behavior. Run via `webhook-test-runner` after editing, not
  `pytest` directly.
- `docker-compose.yml`:
  - Remove `x-webhook-capture-enabled` anchor and its `CAPTURE_ENABLED` use
    in the webhook service block; remove the `./.capture:/app/captures`
    (rw) bind mount from `webhook-1`/`webhook-2`.
  - Add a new named volume `loadtest-fixtures-data:` under the top-level
    `volumes:` block.
  - New `webhook-fixtures` service (`profiles: [tools]`, same image as
    `webhook-reparse`, `APP_ROLE: fixtures`, ClickHouse creds =
    `*clickhouse-ingest-user`/`*clickhouse-ingest-password` — needs the new
    grant below), `VOLUME: ${VOLUME:-medium}`, mounts
    `loadtest-fixtures-data:/app/loadtest_fixtures` (rw).
  - `loadtest` service: replace `./.capture:/app/captures:ro` with
    `loadtest-fixtures-data:/app/loadtest_fixtures:ro`; update its comments
    that currently say "Replays real captured traffic from .capture/".
- `services/webhook/docker-entrypoint.sh`: add a `fixtures)  exec python -m
  src.build_fixtures ;;` case, update the usage-error message's role list.
- `.gitignore`: remove the `.capture/` line (nothing is written to the host
  filesystem anymore — fixtures live in a Docker volume).
- `.env.example`: remove the `CAPTURE_ENABLED`/captures paragraph under
  "Webhook (optional)".
- `services/init/config.yml`: add `"GRANT SELECT ON {database}.agent_events"`
  to the `ingest` role's grants (needed for phase-1 of the extraction query).
  **Note for verification**: this only takes effect after re-running `make
  init` (idempotent) against an existing ClickHouse instance — call this out
  explicitly when reporting the change done.

## Makefile (delegate to `dev-ops` — sole owner of Makefile edits)

Two new targets:
```
loadtest-fixtures: check-env
	docker compose $(COMPOSE_FILES) run --rm -e VOLUME=$(or $(VOLUME),medium) webhook-fixtures

loadtest-fixtures-status: check-env
	docker compose $(COMPOSE_FILES) run --rm webhook-fixtures python -m src.build_fixtures --status
```
Add both to `.PHONY`. `dev-ops` must also update README's "Make targets"
reference table in the same change (its own standing rule) and add
`loadtest-fixtures`/`loadtest-fixtures-status` to its own refusal list
(mirroring the existing `make loadtest` refusal — this is `loadtest-runner`'s
call to make, not `dev-ops`'s, matching the user's stated intent that
`loadtest-runner` itself decides and prompts about regeneration).

## `.claude/agents/*.md` (delegate to `harness-expert` — sole owner of these)

- **`loadtest-runner.md`**: add a fixture-freshness check as an early step
  (before/alongside Phase 1's litellm question) — run `make
  loadtest-fixtures-status`, parse the manifest (`generated_at`, `volume`,
  `event_count`), compare age against `fixtures_ttl_hours` from
  `services/webhook/config.yml` and whether `volume` matches what this run
  needs. If missing entirely: block and ask (`NEED USER INPUT:`) whether to
  generate now — a load test cannot run with zero fixtures. If stale/volume-
  mismatched: ask whether to regenerate (`make loadtest-fixtures
  VOLUME=<x>`) or proceed anyway with a plain warning. Replace every
  `.capture/`-referencing sentence in the file (module intro, Phase-4 OOM
  callout) with `.loadtest_fixtures`/the new terminology.
- **`dev-ops.md`**: add `make loadtest-fixtures`/`loadtest-fixtures-status`
  to the existing "not your job, point to `loadtest-runner`" refusal
  paragraph and frontmatter description, right next to `make loadtest`.

## `AGENTS.md` (delegate to `harness-expert` — owns this file too)

Update the "Running the load test (`make loadtest`)" section to mention the
fixture-preparation step is now part of `loadtest-runner`'s own workflow
(freshness check → optional regenerate → then proceed), and that traffic no
longer comes from a manually-enabled capture flag.

## `README.md` (edit directly — only the Make-targets *table* is `dev-ops`'s)

- Replace "### Inspecting captured traffic" with a "### Preparing load-test
  fixtures" section: what `make loadtest-fixtures VOLUME=small|medium|large`
  does, the two-phase query rationale (briefly), that fixtures live in a
  Docker volume (not host-inspectable via `ls`), and `make
  loadtest-fixtures-status` for checking the manifest.
- Update "## Load testing" section's opening ("replays real captured traffic
  from `.capture/`") to describe the new source.
- Remove `CAPTURE_ENABLED`/`CAPTURE_DIR` rows from the configuration
  reference table; add `VOLUME` (and note `FIXTURES_DIR`/`FIXTURES_TTL` are
  in-code/config.yml, not `.env`-configurable, same framing as the old
  `CAPTURE_DIR` row had).

## Final sweep

- Run `stale-ref-sweeper` after all edits above to catch any remaining
  `.capture`/`CAPTURE_ENABLED`/`CAPTURE_DIR` reference across the repo
  (comments, docstrings, other docs) missed by this list.
- Run `webhook-test-runner` to confirm `services/webhook/tests` passes after
  `server.py`/`loadtest.py`/`test_loadtest.py` edits.

## Verification

1. `webhook-test-runner` → `make test` green.
2. `dev-ops`: `make init` re-run confirms the new `ingest` grant applies
   (`GRANT SELECT ON <db>.agent_events`), then `make up` rebuilds
   webhook/loadtest images cleanly, `make status` green.
3. Manual: `make loadtest-fixtures VOLUME=small` against a stack with real
   `agent_events` data — confirm progress output, then `make
   loadtest-fixtures-status` prints a sane manifest (nonzero `event_count`,
   recent `generated_at`).
4. `make loadtest VOLUME=... START_USERS=... END_USERS=...` (small/short
   run) — confirm `loadtest.py` loads the corpus from the new volume and
   replays without error.
5. Confirm `docker compose config` shows no remaining reference to
   `./.capture` anywhere.
