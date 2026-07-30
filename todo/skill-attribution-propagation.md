# Skill attribution propagation fix

## Context

Requested review: how `agent_name`/`skill_name`/`command_name` are detected for
Claude Code and Codex CLI traffic, and whether the resulting data actually lets
us filter "pieces of work" done via a subagent, a skill, or a slash command in
the Agents Overview Grafana dashboard (`services/grafana/dashboards/agents_overview.json`).

Findings (full detail from research, condensed here):

- **Command detection is chain-wide and reliable.** `_active_command_name_and_version`
  (`services/webhook/src/clickhouse_ingest.py:213-245`) walks backward through
  the LiteLLM payload's `messages`, skipping over Claude Code's automatic
  tool-result-only continuation turns, until it finds the real human message
  that carried the `<command-name>` tag. Every row in that chain — not just the
  first — inherits the same `command_name`.
- **Subagent detection is reliable but Claude-Code-only.** Claude Code sends a
  genuine per-request header (`x-claude-code-agent-id`) on every call the
  spawned subagent itself makes, joined against the `agent_invocations` table.
  There's a known, already-mitigated race (subagent's first call can outrun the
  orchestrator's own ingest; `services/webhook/reparse.py` / `make reparse-all`
  fixes this after the fact).
- **Skill detection is the real gap.** `_skill_name_and_version`
  (`clickhouse_ingest.py:572-586`) only checks whether *this specific call's own
  response* invoked the `Skill` tool. Unlike a subagent, a skill has no
  distinguishing per-request header — its body is read inline into the same
  ongoing conversation — so any tool call or LLM answer that happens *after* a
  skill fires, within the same turn, currently lands with `skill_name=''`,
  indistinguishable in ClickHouse from ordinary top-level work. This directly
  undermines "can we filter work done via a skill" — today, only the single
  triggering row is attributable.
- **Codex CLI has zero agent/skill/command attribution**, by design — those are
  Claude Code-only CLI concepts (confirmed in `agent_docs/architecture.md:22-26`
  and inline docstrings throughout `clickhouse_ingest.py`). Not a bug to fix.

This plan fixes the skill-attribution gap by mirroring the command-detection
pattern: propagate `skill_name`/`skill_version` backward through a turn's
continuation chain, exactly like `command_name` already does.

## Implementation

**File:** `services/webhook/src/clickhouse_ingest.py`

1. Replace `_skill_name_and_version(payload)` with
   `_active_skill_name_and_version(payload, messages)`. Update the one call
   site, `_derive_context` (line 355-356), to pass `messages` through.

2. Algorithm — priority 1 unchanged (this call's own response invoked `Skill`
   → return immediately, existing logic via `_response_tool_calls`). Priority 2
   is new: walk backward through `messages`, mirroring
   `_active_command_name_and_version`'s continuation-skip check, but scanning
   **assistant**-role messages for a `Skill` `tool_use` block instead of
   user-role messages for a tag:
   - Assistant message with no `Skill` tool_use → keep walking back.
   - Assistant message with a `Skill` tool_use → return that skill (most
     recent one wins, since we're walking backward — handles sequential
     multi-skill turns for free).
   - User message that's a tool-result-only continuation → keep walking back.
   - User message that's a genuine fresh turn (not a continuation) → stop,
     return `("", "")` — the skill context ended at the last real user input.
   - Version resolved via the existing `_version_marker_for_name` helper,
     unchanged.

3. **Do not touch `_classify_event`** (`clickhouse_ingest.py:768-817`).
   `calculated_type`/`calculated_payload` stay strictly per-row, based only on
   this call's own tool invocation. A downstream row keeps whatever
   `calculated_type` it would already get (`tool_call`, `llm_answer`, etc.),
   now simply also carrying a populated `skill_name` — exactly how
   `command_name` and `calculated_type` already coexist independently today.

4. **Judgment call, document inline:** if a skill fires and the orchestrator
   later spawns a subagent, the orchestrator's own subsequent calls keep
   propagating the earlier skill_name (nothing removes the Skill block from
   its own conversation history). The subagent's own calls use a separate
   `messages` array scoped to its isolated context and naturally get `("", "")`
   unless the subagent invoked its own skill — no special-casing needed.

5. Update the docstring on the renamed function to describe propagation
   through the continuation chain (mirroring the language on
   `_active_command_name_and_version`). No change needed to the top-of-file
   module docstring or `AGENTS.md`/README — those describe payload sourcing,
   not per-field propagation semantics.

6. No ClickHouse schema/migration change — `skill_name`/`skill_version`
   columns already exist and are already populated, just more sparsely.

**File:** `services/webhook/tests/test_clickhouse_ingest.py`

Rename the existing `_skill_name_and_version` test section/tests to
`_active_skill_name_and_version`, and add:
- `test_active_skill_name_and_version_success_propagates_through_tool_result_continuation`
- `test_active_skill_name_and_version_success_most_recent_skill_wins`
- `test_active_skill_name_and_version_unsuccess_stops_at_fresh_user_turn`
- `test_active_skill_name_and_version_unsuccess_empty_messages_no_index_error`

Delegate the test run to the `webhook-test-runner` subagent rather than
running pytest directly (repo convention).

## Backfill

After merging, run `make reparse-all` (per README) to re-run
`_derive_context` — and hence the new propagation logic — against
`ingest_raw.raw_payload_full`, retroactively populating `skill_name` on
historical rows that currently sit blank downstream of a skill invocation.

## Live E2E verification

Beyond unit tests, confirm the fix end-to-end with a real skill invocation
routed through LiteLLM:
1. Ensure the Claude CLI environment is routed through the proxy
   (`ANTHROPIC_BASE_URL=http://localhost:4000`,
   `ANTHROPIC_CUSTOM_HEADERS="x-litellm-api-key: Bearer <virtual key>"` — see
   `make setup-client` / README "Connect Claude Code" section). Do not use a
   personal/production key blindly — check `.env`/existing virtual key setup
   first.
2. Spawn a Claude CLI session (or use this session, if already routed) that
   triggers a real skill, followed by at least one more tool call in the same
   turn (so there's a row to check for *propagated*, not just triggering,
   attribution).
3. Confirm via `docker compose logs -f webhook-worker` (or a ClickHouse query
   through `mcp__dev__query`) that the downstream row(s) in `agent_events`/
   `agent_usage` carry the propagated `skill_name`, not just the triggering
   row.

## Documentation

Add a short section (a few sentences, not a full report) to
`agent_docs/services/observability.md` or `agent_docs/architecture.md` (whichever
already covers agent/skill/command attribution — check on execution) noting:
- Codex CLI has no agent/skill/command attribution by design (not a gap to
  fix).
- The `agent_invocation_id` join has a known race, mitigated by
  `make reparse-all`.
- There is currently no dashboard panel surfacing "untagged"/unattributed
  work as its own visible category (panel-48 partially does this for its own
  narrow purpose only).

## Verification

- New/renamed unit tests pass (via `webhook-test-runner`).
- Live E2E check above shows propagated `skill_name` on a downstream row.
- Spot-check `agents_overview.json`'s `$skill_name` filter (Subagents tab /
  Reliability & Performance tab panel-48) picks up the previously-invisible
  downstream rows for a test skill invocation.
