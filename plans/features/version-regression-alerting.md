# Automatic version-bump regression alerting

## Context

`agents_overview.json` already has "Agent version-change impact" and "Skill version-change impact" panels that compare metrics before vs. after an agent/skill adopted its current version.
Nobody is paged when that comparison looks bad - a human has to open the dashboard and notice.
Grafana alerting is already wired up for a different purpose: `services/grafana/provisioning/alerting/rules.yml` has an `llm-alerts` group (e.g. `alert-llm-error-rate-high`) reading straight ClickHouse SQL against the `clickhouse` datasource, on a 60s interval.
Per that file's own comment, no contact point is configured yet - alerts land in Grafana's default receiver only, not delivered anywhere real.

## Design

### Query shape

The existing version-change-impact panels compare arbitrary historical windows.
An alert rule needs a self-contained, schedulable version: derive each agent's (or skill's) most recent version-bump timestamp from `min(timestamp)` grouped by `(agent_name, agent_version)` in `agent_events` - this value is already latent in the table, no new column needed - then compare a window since that bump against an equal-length window immediately before it.

### Rule set (phase 1, no new ingestion needed)

- Error-rate delta per agent/skill since last version bump.
- p95 latency delta per agent/skill since last version bump.
- Cost-per-call delta per agent/skill since last version bump.

Each rule needs a minimum-row-count guard (e.g. `WHERE count() > N`, else `NoData`) so a version bump with only a handful of calls so far doesn't fire on noise.
Evaluation interval should be longer than the existing `llm-alerts` group's 60s - these comparisons are meaningful on an hours-scale window, not real-time, so 15m is a reasonable starting interval, tuned later same as the existing group's thresholds.

### Rule set (phase 2, depends on `plans/features/quality-signal-loop.md`)

- Quality-score delta per agent/skill since last version bump, once Tier A (pushback rate) or Tier C (judge score) of that plan has real data.

### Notification

This has no teeth without a contact point.
Decide the channel (Slack webhook, email, something else) before shipping phase 1 - otherwise these alerts silently join the existing `llm-alerts` group in Grafana's default receiver, same as today's error-rate alert, and nobody sees them proactively.

### Optional: feed the root-cause subagent

Once `plans/features/root-cause-subagent.md` exists, an alert firing could enqueue a job for that subagent instead of (or in addition to) paging a human directly, so the notification arrives with a likely-cause finding attached rather than just a bare threshold breach.

## Rollout

Phase 1: cost/error-rate/latency delta rules, added to `services/grafana/provisioning/alerting/rules.yml` as a new group alongside `llm-alerts`, following that file's existing pattern (SQL model in `A`, threshold expression in a later `refId`).
Blocked on the notification-channel decision above before it's actually useful.
Phase 2: quality-score delta rule, blocked on `quality-signal-loop.md` shipping first.
