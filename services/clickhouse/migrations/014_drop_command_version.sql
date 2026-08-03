-- Only custom-authored .claude/commands/*.md files ever carried a version
-- marker (services/_common/src/ingest_parsing.py's now-removed
-- _COMMAND_VERSION_RE). This repo's only two such commands, /min and /me,
-- are Skills now - every command_name value left possible (built-in slash
-- commands, or Codex's synthetic "goal"/"plan" context names) has no
-- version concept, so command_version is permanently dead going forward.
-- command_name itself stays: it's still populated by those builtin/
-- synthetic commands and drives real dashboard functionality (the /goal,
-- /plan usage tabs, the Plan-Mode session-fork filter).
--
-- Not part of any table's ORDER BY key (see schema.sql), so this is a
-- plain column drop, no MODIFY ORDER BY/table rebuild required.
ALTER TABLE agent_events DROP COLUMN IF EXISTS command_version;
ALTER TABLE agent_usage DROP COLUMN IF EXISTS command_version;
ALTER TABLE agent_messages DROP COLUMN IF EXISTS command_version;
