# Fix blank `agent_version` race in ingest, plus backfill

## Context

The "Agent version-change impact" panel in `agents_overview.json` sometimes shows a blank `current_version` for a real version transition (confirmed live for `loadtest-sql`, `query-perf-runner`, `sql-expert`, whose `.md` files carry a proper version marker matching the panel's `prev_version`, not the blank `current_version`).

Root cause: `services/_common/src/ingest_db.py`'s `_agent_name_and_version_for_invocation` (line 161) resolves a subagent's own `agent_name`/`agent_version` by querying `agent_invocations` for that `agent_id`.
If a subagent's own first call is processed by `webhook-worker` before its own spawn event (the row `write_dimensions` inserts into `agent_invocations`), the lookup returns `("", "")`, and that blank value is written permanently into `agent_usage`/`agent_events` for that row - nothing retries or corrects it later.

Explored two areas before designing the fix:

- `services/worker/src/worker.py`: batches are read via `XREADGROUP`, processed through `ingest_events_batch`, then acknowledged with **one unconditional `XACK` covering the whole batch** (`_flush`, line 98-106) - there is no existing per-event ack/requeue mechanism in this codebase, and building one would mean new partial-ack semantics not used anywhere else.
  `stale_idle_ms` (5 minutes, `queue.yml`) already drives `_claim_stale_entries`'s `XAUTOCLAIM`, but only for entries that were never acked at all (worker crash) - repurposing it for this race would add an unnecessary 5-minute delay to a race that, per `queue.yml`'s own `flush_interval_ms: 2000`, typically spans at most one or two 2-second flush windows.
- `services/reparse/src/reparse.py`: reruns the exact same resolution logic (`reparse_event` -> `_derive_context_with_client` -> the same `_agent_name_and_version_for_invocation`) against `ingest_raw`, safe to rerun any number of times since the target tables are `ReplacingMergeTree` keyed so the new run's `now()` wins.
  No existing flag targets only broken rows - the only selector is `--session-id`/`SESSION_ID`, otherwise it processes all of `ingest_raw`.

Given no existing requeue/partial-ack pattern to build on, and the actual race window is short (one flush cycle, ~2s) rather than the 5-minute crash-recovery case `XAUTOCLAIM` is tuned for, the right-sized fix is: close the race at the point it happens (cheaply, in-process, no queue/ack changes) for going-forward correctness, and use the existing, already-safe `reparse` mechanism to clean up the already-broken historical rows.
Building a new partial-ack/requeue mechanism on the live shared queue was considered and rejected as disproportionate: it touches the production ack path every other event type also relies on, for a race that the cheaper fix below should already close in the common case.

## Fix 1: resolve from the in-memory batch first, then retry, before ever falling back to a ClickHouse round-trip

`_BatchWriter.write_dimensions` (`ingest_db.py`) already builds `invocation_rows` for this exact batch before inserting them.
The most common real-world case (per migration `013_agent_invocations_parent_id.sql`'s own docstring: "several forks spawned close together from the same session") is a spawn event and its child's first call landing in the *same* batch - which today still round-trips to ClickHouse and can lose the race against that batch's own not-yet-committed insert.

Changes, all in `services/_common/src/ingest_db.py`:

1. In `_BatchWriter.write_dimensions`, alongside the existing `invocation_rows` construction, build `self._invocation_batch_map: dict[str, tuple[str, str]]` (`agent_id -> (subagent_type, agent_version)`) from the same data, before it's inserted.
2. In `_BatchWriter._agent_fields`, check `self._invocation_batch_map` first.
   A hit resolves instantly, with no ClickHouse query at all - this deterministically fixes the same-batch case, which is the dominant one.
3. Only on a miss, fall through to today's `_agent_name_and_version_for_invocation` ClickHouse lookup, now with a small bounded retry: if it returns blank and `agent_invocation_id` is non-empty, retry up to 2 more times with `time.sleep(0.3)` between attempts (~0.6-0.9s added latency, only for the events that actually need it, and only once per distinct `agent_invocation_id` per batch since `_agent_fields_cache` already dedupes).
   This covers the near-miss cross-batch case (spawn landed in the immediately preceding batch, just committed).
4. If still blank after retries, keep today's behavior - write blank, don't block the pipeline.
   This residual case should now be rare, and is covered by Fix 2's backfill going forward too (see "Ongoing safety net" below).

## Fix 2: backfill the already-broken historical rows

Run `make reparse-all` (already the supported, chunked, safe-to-rerun operation per `reparse.py`'s own docstring) to fix rows already stuck blank - by now `agent_invocations` has long since caught up for all of them, so the existing resolution logic will succeed this time.
No new targeted-row filter is being built for this - `reparse-all`'s existing per-row try/except and chunking already make a full run safe, and scoping a new CLI flag down to "just the broken rows" is unrequested extra surface for a one-time cleanup.

After it completes, run `OPTIMIZE TABLE agent_events FINAL`, `OPTIMIZE TABLE agent_usage FINAL`, `OPTIMIZE TABLE agent_messages FINAL` (and `agent_invocations` if touched) - dashboard queries don't use `FINAL`, so stale blank-version duplicates would otherwise keep showing up until a background merge happens on its own schedule.
This is a real operational action against the live database - confirm with the user immediately before running `reparse-all` and the `OPTIMIZE ... FINAL` calls, same as any other state-changing action against shared infra.

## Testing

Extend `services/_common/tests/test_ingest_db.py` (existing `_FakeClient` pattern):

- A batch where `invocation_rows` contains the spawning agent's info resolves via the in-memory map, asserted by confirming the fake client's `query` was never called for that lookup.
- A ClickHouse lookup that returns empty on the first call(s) and a real row on a later one resolves correctly through the retry loop.
- A lookup that stays empty through all retries still falls back to writing blank (no pipeline stall, no exception).

Delegate the actual test run to `webhook-test-runner`, per this repo's convention - never run `pytest`/`make test` inline here.

## Verification end-to-end

1. After `webhook-test-runner` confirms the suite passes, `dev-ops` rebuilds/recreates `webhook-worker` (never a plain `restart`, which wouldn't pick up code changes reliably) to pick up the fix.
2. Re-run the diagnostic query already used to confirm the bug (the `version_starts`/`ordered`/`transitions` CTE chain against `agent_usage`) after a few new real agent-version bumps happen, to confirm no new blank `current_version` rows appear.
3. After the Fix 2 backfill and `OPTIMIZE ... FINAL`, re-run the same diagnostic query to confirm `loadtest-sql`, `query-perf-runner`, and `sql-expert` now show their real current version instead of a blank one.
