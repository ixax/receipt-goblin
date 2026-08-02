# Root-cause subagent for version-bump regressions

## Context

`clickhouse-analyst`/`sql-expert` already answer open-ended questions about the data, but nothing proactively correlates "this metric moved" with "this is probably why."
Today that correlation is manual: a human notices a spike on a dashboard panel, then has to separately check whether a version bump happened around the same time.
`plans/features/version-regression-alerting.md` will detect the spike automatically; this plan is what turns that detection into a finding instead of a bare threshold breach.

## Design

New subagent, candidate name `regression-analyst`, modeled on `clickhouse-analyst` (cheap model, `mcp__dev__query` read access only - proposes findings, never acts) but with a fixed investigative procedure instead of open-ended Q&A:

1. Given an `agent_name`/`skill_name` (or "any") and a time window, find which metrics moved beyond a threshold - error rate, cost/call, latency, and quality score once `quality-signal-loop.md` ships.
2. For each, find the nearest preceding version bump for that agent/skill, using the same `min(timestamp)` grouped by `(agent_name, agent_version)` derivation `version-regression-alerting.md` uses.
3. Check whether the regression's onset lines up with that version-bump timestamp, or with something else - a traffic spike, a new user cohort, a model deprecation upstream.
4. Produce a natural-language finding: likely cause, confidence, and the underlying query results as evidence, not just an assertion.

### Trigger modes

- Explicit: "investigate why agent X got slower this week", same as any other subagent invocation today.
- Automatic: `version-regression-alerting.md`'s alert firing enqueues a job this subagent picks up, then posts its finding back (`SendMessage` to the main conversation, or a Slack notification once that plan's contact-point decision is made).

## Rollout

Ship explicit-invocation-only first.
Validate the investigative procedure's accuracy by backtesting it against real regressions already documented in `agent_docs/incidents.md`, before wiring it to fire automatically off alerts - an automated root-cause agent that's wrong half the time is worse than no automation at all.
