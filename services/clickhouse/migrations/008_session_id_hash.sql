-- Adds a numeric surrogate key for session_id on session_git_branch,
-- following the id = cityHash64(value) convention this schema already
-- established for `clients` (see schema.sql) - reusing one convention
-- rather than introducing a second way to do the same thing.
--
-- Why: every one of the dashboard's 84 queries currently re-runs a full
-- GROUP BY/argMax scan of session_git_branch on every panel load (see the
-- LEFT JOIN (SELECT session_id, argMax(...) FROM session_git_branch GROUP
-- BY session_id) pattern in agents_overview.json). session_git_branch_dict
-- (created separately by migrate.py's _create_session_git_branch_dict_once
-- - a plain .sql file has no templating for the CLICKHOUSE_USER/PASSWORD a
-- dictionary's SOURCE(CLICKHOUSE(...)) needs, same reason
-- _grant_ui_access_to_app_user_once is Python too) replaces that JOIN with
-- a single dictGetOrDefault() lookup, refreshed on a LIFETIME instead of
-- recomputed per query. The UInt64 key added here lets that dictionary use
-- the cheaper HASHED() layout instead of COMPLEX_KEY_HASHED() on a String
-- key.
--
-- Unlike `clients` (a separate lookup table needing a Python-side resolve
-- round-trip, since it's an FK stored on a *different* table), this is a
-- MATERIALIZED column computed from session_git_branch's own session_id
-- column - ClickHouse computes it inline on insert, no ingest code change
-- needed. MATERIALIZE COLUMN below backfills existing rows too (ClickHouse
-- would otherwise compute it on the fly for old parts at read time, which
-- is already correct - this just forces it onto disk immediately rather
-- than waiting on that path, matching the FINAL-forcing precedent in
-- migration 004).
--
-- Safe to re-run: ADD COLUMN IF NOT EXISTS is a no-op on a second run;
-- MATERIALIZE COLUMN is idempotent too (recomputes the same values).

ALTER TABLE session_git_branch
    ADD COLUMN IF NOT EXISTS session_id_hash UInt64 MATERIALIZED cityHash64(session_id);

ALTER TABLE session_git_branch
    MATERIALIZE COLUMN session_id_hash SETTINGS mutations_sync = 1;
