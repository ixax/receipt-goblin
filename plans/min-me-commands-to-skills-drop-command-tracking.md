# Convert min/me commands to skills; drop all Command/command_name/command_version support

## Context

`.claude/commands/` currently has exactly two members: `min.md` and `me.md`.
These are the harness's only "Command" entities.
The goal is to convert both into Skills, still invoked only by typing `/min`/`/me`, never proactively.
Then delete the Command concept entirely: no more `.claude/commands/`, no more `command_name`/`command_version` anywhere in code, ClickHouse schema, or dashboards.

User decisions already made (via AskUserQuestion):

- ClickHouse: full migration, physically dropping `command_name` and `command_version` (not just "stop populating").
- Scope: both `command_name` and `command_version` go, not just the version half.
  `command_name` also carries synthetic values (Plan Mode's `'plan'`, Codex's `'goal'`), so this is a bigger behavior change than just cleaning up dead command tracking - covered explicitly below.

Reloads/restarts happen only at the very end, after the user reviews the diff and approves.

## Known side effects of dropping `command_name`

Not just cleanup - flagging explicitly:

1. `agents_overview.json`'s `session_id` picker variable relies on `AND e.session_id NOT IN (SELECT session_id FROM agent_messages GROUP BY session_id HAVING sum(command_name != 'plan') = 0)` to hide Plan-Mode session forks (see `agent_docs/incidents.md`) from the session dropdown.
   This clause will be deleted outright, since the column is gone.
   Accepted regression: plan/goal-fork sessions may reappear in the picker.
   No substitute heuristic will be engineered as part of this task.
2. `mcp-stats/src/server.py`'s `_operation_label` currently labels a `/me` "top operations" row as `/<command_name>` when applicable.
   That branch and the `au.command_name` column in its SQL are removed.
   Those rows fall through to the existing `calculated_type`/`llm:<model>` fallback labels instead (e.g. "conversation reply").

Both are accepted as in-scope removals per "убрать всё из кода и структур данных" / "убрать всё из дашбордов", not deferred cleanup.

## 1. Convert commands to skills (harness-expert)

Delegate to the `harness-expert` subagent (owns all `.claude/`/`.agents/skills/` writes) with this exact scope:

- Create new skill directories `min` and `me` under `.agents/skills/` (each holding a `SKILL.md`), from the current `.claude/commands/min.md`/`me.md` bodies, converted to Skill frontmatter shape (`name`, `description`, version marker as `<version>X.Y.Z</version>` on the last line of `description:`, per the harness's own Subagent/Skill convention - see `harness-expert.md`'s "Version marker" section).
- Description must make clear these never trigger proactively, only by explicit `/min`/`/me`, following the same pattern as `harness-guardian`'s "Never triggers proactively - invoked explicitly by name."
- Delete `.claude/commands/min.md`, `.claude/commands/me.md`, and the now-empty `.claude/commands/` directory.
- Update `harness-expert.md` itself: remove the "Command" row from "Entity shapes" (line 42), the Command-specific version-placement clause (line 47-50), and repoint the two exemplar references to `.claude/commands/min.md` (lines 84, 91, used as the "zero bold" / "numbered H2 procedure" pattern examples) to the new `min` skill's `SKILL.md` under `.agents/skills/`.
- Regenerate `agent_docs/harness-index.md` via `make harness-index` (it's generated, never hand-edited).
  This table only covers agents, not skills/commands, so it may end up unaffected - confirm either way.
- Update `README.md`'s "Version marker" section (~line 550-565): delete the **Commands** subsection entirely (its example, `<command_version>` explanation), leaving Subagents/Skills only.

## 2. ClickHouse migration (main conversation, per `clickhouse-migration` skill)

New file `services/clickhouse/migrations/014_drop_command_tracking.sql`, idempotent (`DROP COLUMN IF EXISTS`, guard the index drop), covering:

- `agent_events`: `DROP INDEX IF EXISTS idx_command_name`, `DROP COLUMN IF EXISTS command_name`, `DROP COLUMN IF EXISTS command_version`.
- `agent_usage`: same (has `idx_command_name` too).
- `agent_messages`: `DROP COLUMN IF EXISTS command_name`, `DROP COLUMN IF EXISTS command_version` (no index on this table).

Confirmed via `schema.sql`: none of these three tables have `command_name`/`command_version` in their `ORDER BY` key.
They're plain `DEFAULT ''` columns with skip indexes only, so no `MODIFY ORDER BY`/table-rebuild is needed, just straightforward `DROP COLUMN`/`DROP INDEX`.

Also update `services/clickhouse/schema.sql` to the new end-state: remove the 6 column definitions, 2 index definitions, and the doc comment on `agent_events.command_name`/`.command_version` at lines ~248-260 explaining the old convention.
Migrations are for existing stacks, `schema.sql` is what a fresh stack gets, per the `clickhouse-migration` skill.

Test the migration's own `SELECT`s (post-drop schema sanity) via the ClickHouse MCP tool before considering it done, per skill convention.
Applying `ALTER TABLE ... DROP COLUMN` on the live stack itself only happens as part of the final "reload" step (section 8), not while this section is otherwise being drafted/reviewed.

## 3. Ingest code (`services/_common/src/ingest_parsing.py`)

Remove, in order:

- `_COMMAND_NAME_RE`, `_COMMAND_VERSION_RE` regex constants.
- `_active_command_name_and_version()` function entirely, including its Codex `<codex_internal_context>` synthetic-command handling.
  The `_CODEX_INTERNAL_CONTEXT_RE`/`_OBJECTIVE_TAG_RE` machinery it uses is also used elsewhere for `command_args`/prompt-kind classification in `_prompt_kind_and_display` - keep those two regexes and that logic, only remove the command-name/version resolution itself.
- `command_name`/`command_version` fields from the `EventContext` dataclass, and their assignment in `_derive_context()`.
- `command_name`, `command_version` from `_EVENT_COLUMNS`, `_USAGE_COLUMNS`, `_MESSAGE_COLUMNS` lists, and their corresponding values in `_event_row`, `_usage_row`, `_message_row`'s return lists.
- `_prompt_kind_and_display()`'s `command_name` parameter: it's still needed for the "command" `prompt_kind` display text (`f"/{command_name} ..."`), which is driven by `<command-args>`/`<local-command-stdout>`/`<objective>` presence, not by `_active_command_name_and_version`.
  Since there's no more resolved `command_name` to pass in, change this branch to a fixed label (e.g. `"[command]"`) - confirm exact wording during implementation, matching the surrounding style.
- Update every docstring/comment referencing `command_name`/`command_version`/`_active_command_name_and_version` (the module docstring area, `EventContext`'s docstring, `_agent_invocations_from_messages`'s "exactly like command_name already does" comment, etc.).
  This is exactly `stale-ref-sweeper`'s job - delegate the repo-wide comment/doc sweep to it after the code edit lands.

## 4. Tests (`services/_common/tests/test_ingest_parsing.py`)

- Delete the `_active_command_name_and_version` test block (~lines 158-215): `test_active_command_name_and_version_success_recovers_slash_command`, `_recovers_version_marker`, `_recovers_version_marker_at_end`, `_unsuccess_freeform_prompt_returns_empty`, `_recovers_codex_internal_context`, `_recovers_arbitrary_codex_context_source`.
- Keep `test_prompt_kind_and_display_success_renders_codex_goal_context_as_command`, but update its expected display text to match the new fixed label from section 3.
- Grep the file for any other row-shape assertions (`_EVENT_COLUMNS`, `_USAGE_COLUMNS`, `_MESSAGE_COLUMNS` index-based lookups) that would shift once the two columns are removed.
- `services/_common/tests/captures/success_with_command.json` and the other two capture files referencing `command_version`/`command_name` stay as raw payload fixtures - they simulate LiteLLM's own webhook payload, not this repo's schema.
  Only revisit if a specific test that reads them breaks.
- Run the suite via the `webhook-test-runner` subagent, not inline.

## 5. `mcp-stats/src/server.py`

- Drop `au.command_name` from the `top_result` SQL in `me()`.
- Remove `_operation_label`'s `if row["command_name"]: return f"/{row['command_name']}"` branch.

## 6. Grafana dashboard (`services/grafana/dashboards/agents_overview.json`)

Delegate to `dashboards-expert` (owner of all panel edits in this file) with this exact scope:

- Remove the `command_name` `QueryVariable` from `spec.variables` entirely (the "Command" filter dropdown).
- Strip every panel's `AND (has([${command_name:singlequote}], '__all__') OR has([${command_name:singlequote}], u.command_name))`-shaped clause (~94 occurrences) from `rawSql` - mechanical, same pattern each time.
- Remove the Plan-Mode-fork exclusion clause from the `session_id` variable's query (documented side effect #1 above).
  The `AND e.session_id NOT IN (... command_name != 'plan' ...)` subquery goes away with its comment.
- Update `README.md`'s "Seven template variables in order" line (~line 613) to six, dropping `$command_name`.
- `query-performance-sync` skill applies automatically after any panel edit here - keep `dashboards-health/query_performance.json` in sync per that skill's own workflow.
- `services/grafana/scripts/query_perf.py` line 78 (`"command_name": "__all__"` in its template-variable substitution defaults) also needs the entry removed.

## 7. Docs sweep

- `agent_docs/architecture.md` lines 37, 43: rewrite the two sentences describing `command_name` as a Claude-Code-only concept propagating through continuation chains - it no longer exists.
- `agent_docs/incidents.md` line 42: the Plan-Mode-fork incident entry references the now-removed dashboard filter mechanism.
  Add a one-line note that the filter was removed, don't rewrite the historical incident narrative itself - `incidents.md` is explicitly exempt from the "never a changelog" rule and stays dated/historical, so a short append is correct here.
- Delegate this whole section to `stale-ref-sweeper` alongside its section-3 sweep, since it's the same "old name removed, find every reference" job.

## 8. Reloads (last step, only after explicit user approval of the diff)

In order, only after the user has reviewed the full diff:

1. Apply migration `014_drop_command_tracking.sql` to the live ClickHouse container (`dev-ops`/direct `docker exec ... clickhouse-client`).
2. Restart `webhook-worker` (picks up the new `ingest_parsing.py`) via `dev-ops`.
3. Confirm Grafana picks up the dashboard JSON change (provisioning reload or restart, via `dev-ops`).
4. Restart/rebuild `mcp-stats` if its container needs a rebuild for the `server.py` change, via `dev-ops`.

No reload happens before this point - all prior sections are file edits only.

## Verification

- `webhook-test-runner`: full `make test` pass after sections 3-5 land.
- After section 2's migration is applied (step 8.1): re-run a couple of the migration's own `SELECT`s via the ClickHouse MCP `query` tool to confirm the columns are gone and no query errors.
- After section 6 lands: spot-check `agents_overview.json` loads in Grafana with no broken template variable (the "Command" filter is gone from the variable row) and the session picker still populates.
- After section 8.4: invoke the new `/me` and `/min` skills manually to confirm they still work identically to the old commands.
