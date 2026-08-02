# Consolidate ClickHouse table/column documentation around schema.sql

## Context

The user wants one authoritative place describing every ClickHouse table and column.
Knowledge about them is currently scattered across many skills/agents in a way that's inconvenient to maintain.
They also want a single owner for schema.sql, migrations, and descriptions.
Investigation (see Findings) showed `schema.sql` is already a strong, hand-maintained source of truth with ~90% prose-comment coverage.
The real problem is not missing documentation but four other places that have grown their own duplicate/derived copies of the same facts, which can silently drift from schema.sql and from each other.
Goal: keep the content in one place (schema.sql, colocated with the DDL it describes - the lowest-drift position possible), and make one existing skill the explicit accountable owner of keeping it accurate, instead of creating a new document or a second, overlapping skill.

An earlier draft of this plan proposed a new `clickhouse-schema` skill alongside `clickhouse-migration`.
The user pointed out this would create two skills with near-identical triggers - `clickhouse-migration`'s TRIGGER already fires on "changing `agent_events`/`agent_usage`/`agent_messages`/`ingest_raw` schema in any other way," not just migration files.
In practice, whoever touches schema.sql already reaches for `clickhouse-migration` out of habit, so the second skill would get skipped and become dead weight.
Fix: fold everything into `clickhouse-migration` - one skill, one trigger, one accountable owner.

## Findings

- `services/clickhouse/schema.sql` (535 lines, 12 tables): no SQL-native `COMMENT` clauses, but ~318 lines of `--` prose comments.
  Every table has a rationale header, most non-trivial columns have inline provenance/semantics comments.
  Hand-maintained, no generation script.
  Already treated as "source of truth" by `.agents/skills/clickhouse-sql/SKILL.md` and `.claude/agents/sql-expert.md`.
- `.agents/skills/clickhouse-migration/SKILL.md` already instructs "update schema.sql to match the new end state" when a migration lands, and its TRIGGER already covers any schema change to the four core tables, not only migration-file edits.
  Today it only talks about DDL, not comment quality, and has no cross-reference to the other places that also need updating.
- Four places carry duplicated/derived table-column knowledge:
  - `agent_docs/services/clickhouse.md` ("## Tables" section) - one-line-per-table restatement.
  - `.claude/agents/clickhouse-analyst.md` (lines 22-36) - condensed table cheat-sheet, kept there for a real reason: it runs on a cheap model (haiku) and shouldn't re-read a 535-line file on every call.
  - `.agents/skills/dynamictext-panel-queries/SKILL.md` ("Data-model facts specific to this schema", ~lines 173-203) - the deepest duplicate, and the only place holding some facts not in schema.sql at all (e.g. the catalog of harness-injected wrapper strings that show up inside `agent_messages.prompt_text`).
  - `services/grafana/dashboards/agents_overview.json` panel `description` fields - restate general column semantics (e.g. `status='failure'`) alongside panel-specific explanation.
- `agent_events.turn_id` "always 0, don't order by it" is independently stated in 3 of these 5 locations - a concrete instance of the drift risk.
- No agent currently owns schema.sql edits.
  DDL changes happen in the main conversation via Bash, per the migration workflow - `mcp-dev` is read-only by design, so no agent (including `sql-expert`) can apply a schema change.
  `sql-expert` proposes changes and documents gotchas but never applies DDL.
  `clickhouse-analyst` is a pure cheap consumer, not a documentation owner - its cheat-sheet is a derived copy like the others.

## Recommended approach

1. **Keep `schema.sql`'s prose `--` comments as the single content source.**
   Do not add SQL-native `COMMENT ON COLUMN` - nothing today queries `system.columns`, every consumer reads the file text directly (cheaper, no extra round trip), and prose already carries richer rationale than a one-line `COMMENT` could hold.
   Revisit only if a future tool needs schema self-description via query.

2. **Make `clickhouse-migration` the single accountable skill-level owner of schema.sql**, by extending its existing checklist rather than adding a parallel skill:
   - Split the existing "update schema.sql to match the new end state" bullet into (a) DDL and (b) comments - a changed/added column needs a `--` comment explaining purpose + provenance, not just a restated name.
   - Add an explicit known-derived-copies checklist - the four locations above - with the rule: whenever a fact in `schema.sql` changes, check this list and fix any now-stale restatement in the same pass.
   - No new skill file, no new trigger surface - `clickhouse-migration`'s TRIGGER already fires on every schema.sql-affecting change.

3. **Port unique facts out of `dynamictext-panel-queries`** into `schema.sql` as real column comments.
   The `agent_messages.prompt_text` wrapper-string catalog is real column semantics that belongs on the column, not buried in a query-writing skill.
   Shrink that skill's "Data-model facts" section to a pointer back to `schema.sql` plus only the parts that are genuinely panel/query-specific (the `JSONExtractString` extraction technique).

4. **Add a one-line sync note + pointer to `clickhouse-migration`** in the two lighter derived copies (`clickhouse-analyst.md`'s table reference, `agent_docs/services/clickhouse.md`'s Tables section): "kept in sync with `schema.sql`'s comments - that file wins on any conflict."
   This costs nothing in tokens but kills the "which one is right" ambiguity the user flagged.

5. **Ownership**, stated explicitly in `clickhouse-migration` and reflected in `AGENTS.md`'s existing skill entry:
   - Content (`schema.sql` DDL + comments): edited together, in the main conversation, following the `clickhouse-migration` checklist.
     This doesn't change - no agent gets DDL access.
   - Whoever writes a migration is responsible for the DDL, the comment quality, and the known-derived-copies checklist in the same commit - this directly answers "whoever writes migrations should know schema.sql well."
   - `sql-expert` keeps its existing advisory role (reviews schema fit, proposes changes, documents query gotchas).
     Extend it to also flag a stale `schema.sql` comment or a stale derived copy when its own work touches one, but it still never edits schema.sql itself.
   - `clickhouse-analyst` is not a documentation owner; no change to its role beyond the one-line sync note above.
   - New duplicate content appearing in a Skill/Subagent file (someone writing a fresh explanation of a table/column instead of pointing at schema.sql) is caught by the existing `harness-expert`/`harness-guardian` duplicate-content review - no new mechanism needed there either.

## Files to touch

- `.agents/skills/clickhouse-migration/SKILL.md` - split the schema.sql bullet into DDL/comments, add the known-derived-copies checklist.
- `.agents/skills/dynamictext-panel-queries/SKILL.md` - trim "Data-model facts" section, port content out.
- `services/clickhouse/schema.sql` - add the ported comments (`agent_messages.prompt_text` wrapper catalog, confirm `turn_id` rationale is fully stated).
- `.claude/agents/clickhouse-analyst.md` - one-line sync note.
- `agent_docs/services/clickhouse.md` - one-line sync note.
- `.claude/agents/sql-expert.md` - add the "flag stale comments/copies" line to its existing advisory responsibilities.
- Route the actual skill/agent-doc edits through `harness-expert` (per `AGENTS.md`'s existing proactive routing: it owns every Subagent/Skill frontmatter/body edit) rather than editing them inline in the main conversation.

## Verification

- This is a documentation-only change - no runtime/functional testing applies.
- After edits: grep for `turn_id` (and the other previously-triplicated facts) across the five locations to confirm they're now pointers, not restatements.
- Have `harness-expert` do its normal audit pass on the edited skill/agent files (frontmatter, TRIGGER wording, budget) since it already owns that.
- Confirm `clickhouse-migration`'s version marker is bumped and `agent_docs/harness-index.md` reflects the updated description (`make harness-index`).
