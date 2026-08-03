# Convert min/me commands to skills; drop command_version tracking

## Final scope (revised mid-execution)

The original plan below called for dropping both `command_name` and `command_version` entirely.
After executing that (schema migration, ingest code, ~94 dashboard filter clauses, three whole tabs), the user reverted it and narrowed the scope: `command_name` stays - it's still populated by harness-builtin/synthetic commands (Plan Mode's `'plan'`, Codex's `'goal'`) and drives real dashboard functionality (the `/goal`/`/plan` usage tabs, the Plan-Mode session-fork filter).
Only `command_version` was dropped, since no command left in this repo (custom or builtin) has a version concept - the two commands that did, `/min` and `/me`, are Skills now.

**What actually shipped:**

1. The old `min`/`me` command files converted to `.agents/skills/min/SKILL.md`/`.agents/skills/me/SKILL.md`, `.claude/commands/` deleted, harness-expert.md/AGENTS.md/README.md updated - unchanged from the original plan below.
2. `services/clickhouse/migrations/014_drop_command_version.sql` + `schema.sql`: drop `command_version` only, from `agent_events`/`agent_usage`/`agent_messages`.
   `command_name` and `idx_command_name` untouched.
3. `services/_common/src/ingest_parsing.py`: `_active_command_name_and_version()` (returned `(name, version)`) renamed to `_active_command_name()` (returns just the name).
   `EventContext.command_version` removed, `.command_name` kept.
4. `services/_common/tests/test_ingest_parsing.py` updated to match; the two version-marker-recovery tests deleted (no longer applicable).
5. `services/mcp-stats/src/server.py` and `services/grafana/dashboards/agents_overview.json` - **not touched** in the final version; neither ever referenced `command_version`.

Reloads still pending user approval (see "Reloads" section below, from the original plan - still the right sequence, just now a smaller footprint: only the migration + `webhook-worker` restart are needed; no Grafana/mcp-stats change to pick up).

---

## Original plan (superseded above, kept for context)

`.claude/commands/` originally had exactly two members: `min.md` and `me.md` - the harness's only "Command" entities.
Converting both into Skills (section 1 below) happened as planned and is unaffected by the scope revision.
Everything from section 2 onward describes the full `command_name`+`command_version` removal that was later partially reverted - superseded by "Final scope" above.

### 1. Convert commands to skills (harness-expert) - unchanged, see "Final scope" item 1.

### 2-7 (superseded)

Originally: drop both `command_name`/`command_version` from ClickHouse schema/migration, ingest code, `mcp-stats`, and the dashboard (including deleting the `Commands`/`/goal`/`/plan` tabs and the `Command adoption` panel).
All of that was reverted via `git checkout` back to `HEAD` once the user narrowed scope to `command_version` only - see "Final scope" above for what actually landed instead.

### 8. Reloads (last step, only after explicit user approval of the diff)

1. Apply migration `014_drop_command_version.sql` to the live ClickHouse container.
2. Restart `webhook-worker` (picks up the new `ingest_parsing.py`).

No Grafana or `mcp-stats` reload needed this time - neither file changed.

## Verification

- Full `make test` pass confirmed via `test-runner` after the revised, smaller change set (webhook/worker/reparse/loadtest/`_common` all green).
- After the migration is applied: re-run a `SELECT` against `agent_events`/`agent_usage`/`agent_messages` via the ClickHouse MCP tool to confirm `command_version` is gone and `command_name` still works.
- After `webhook-worker` restarts: invoke `/me` and `/min` manually to confirm the new skills still work identically to the old commands.
