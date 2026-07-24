-- Migration for stacks whose ClickHouse volume already existed before
-- session_git_branch.issue_id was introduced.
--
-- Why: branch names often embed a ticket key (e.g. "VIEW-12345" out of
-- "VIEW-12345-my-super-branch" or "my-super-branch-VIEW-12345"), which
-- nothing parsed out before - see _issue_id_from_branch in
-- clickhouse_ingest.py for the extraction regex (case-insensitive,
-- normalized to uppercase). Unlike agent_events/agent_usage/agent_messages,
-- session_git_branch isn't backed by event_sources, so there's no
-- `make reparse-all` path to recompute this from raw payloads - the ticket
-- key only ever existed in git_branch itself, which is already sitting in
-- this table. Backfill is therefore done directly in SQL below using
-- ClickHouse's RE2-based extract(), mirroring the Python regex.
--
-- Run manually, in order:
--   1. Apply this file:
--      docker exec -i receipt-goblin-clickhouse clickhouse-client \
--        --database "$CLICKHOUSE_DATABASE" --multiquery < services/clickhouse/migrations/004_session_git_branch_issue_id.sql
--   2. Deploy the updated webhook image (new issue_id ingestion logic in
--      ingest_git_branch) so new rows get issue_id populated on insert.
--   3. `OPTIMIZE TABLE session_git_branch FINAL` - forces the dedup merge
--      immediately so dashboard queries (which don't use FINAL) see the
--      backfilled issue_id right away instead of waiting for a background
--      merge.
--
-- Safe to re-run: ADD COLUMN IF NOT EXISTS is a no-op on a second run, and
-- the backfill UPDATE only ever touches rows still at the issue_id default
-- ('') - a session already backfilled (or freshly ingested with a real
-- issue_id, including one that's legitimately empty because its branch has
-- no ticket key) is never touched again... except the legitimately-empty
-- case is indistinguishable from "not yet backfilled" (both are ''), so a
-- second run just recomputes the same '' result for those rows - a no-op
-- in effect, not just in principle.

ALTER TABLE session_git_branch ADD COLUMN IF NOT EXISTS issue_id String DEFAULT '';

-- Trailing boundary is a non-capturing [^a-z0-9]|$ alternative, not \b:
-- RE2 has no lookahead/lookbehind support at all (rejects `(?!`), and a
-- plain \b wouldn't help anyway - it treats digits and underscores as the
-- same word class, so a branch like "VIEW-100500_my-branch" would never
-- match a trailing \b right after the number. extract() takes the first
-- capturing group, so the trailing alternative staying non-capturing
-- keeps group 1 pointed at the ticket key.
ALTER TABLE session_git_branch
    UPDATE issue_id = upper(extract(git_branch, '(?i)\\b([a-z][a-z0-9]{1,9}-\\d+)(?:[^a-z0-9]|$)'))
    WHERE issue_id = '' AND git_branch != '';
