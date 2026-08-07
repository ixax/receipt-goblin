---
name: usage-coach
description: >
  Prints LLM spend/token statistics for this stack into chat with charts - trends, waste diagnosis, and concrete changes to the agent flow, without opening Grafana.
  Also checks whether the statistics pipeline itself broke, syncs to the current `agents_overview.json` panels, and advises what to add to the dashboard, the metric pack, and the data collected.
  Grades its own earlier advice against MEMO.md baselines and revises its thresholds from that evidence.
  Never triggers proactively - invoked by typing /usage-coach, or by its scheduled run, optionally with a scope arg (`7d`, `30d`, `session`, `agent:<name>`, `skill:<name>`, `since-last`, `health`).
  v1.1.0
---

## Preflight

Reads go through `mcp__dev__query` only - the sanctioned read path (`agent_docs/rules/clickhouse-access.md`).
Read `Skill(clickhouse-sql)` before running or editing any query here.

If `mcp__dev__query` is unavailable or refuses to connect, `mcp-dev` is not up.
Say so and hand the user this command instead of falling back to any direct ClickHouse access:

> make start SERVICE=mcp-dev

While it is down, the only available data is `mcp__stats__me` (this session plus a 30-day total).
Report that reduced view, label it explicitly as degraded, and skip every section that needs the full metric pack.

## Scope

Default scope is the whole stack over 30 days, with a 7-day-vs-previous-7-day trend.
Arg overrides:

- `7d` / `30d` / `90d` - change the window on every query.
- `session` - restrict to the current session; read `CLAUDE_CODE_SESSION_ID` via Bash first, then filter `session_id`.
- `agent:<name>` / `skill:<name>` - restrict to that operation, and always include its per-version table (Q12).
- `since-last` - window starts at the previous run's timestamp in MEMO.md.
- `health` - run only the health block and report it, skipping spend analysis entirely.

## Order of a full run

1. Health block - `references/health.md`, HQ1-HQ11.
2. Dashboard fingerprint - `references/dashboard-sync.md`.
3. Metric pack - `references/queries.md`, Q1-Q11 in one batched call.
4. Memo - baselines, open recommendations, revised thresholds.
5. Diagnosis, grading, report, memo update.

Health runs first because a broken pipeline invalidates the spend numbers that follow.
Never report a spend finding that rests on a metric a health check just invalidated - say which finding is unavailable and why.

## Metric pack

`references/queries.md` holds Q1-Q14 and what each one is for.
Substitute the window, then send Q1-Q11 as one batched `mcp__dev__query` list - not one call per query.

Q12-Q14 are on demand: Q12 for `agent:`/`skill:` scope or any version-change verdict, Q13 when more than one client writes to this stack, Q14 for a latency complaint.

Never paste a query's raw rows into the chat.
Every number in the report is a number the analysis actually used.

## Diagnose

`references/playbook.md` maps each signal to its threshold, what it means, and the concrete change to make.
A threshold revised in `MEMO.md` overrides the playbook default - the playbook is the starting point, the memo is what this stack has learned.

Apply it in this order:

1. Trend first - compare each headline metric to the previous run's baseline, not to zero.
2. Then waste - failures, failed-tool reactions, truncation, interrupted work.
3. Then structure - model mix, delegation lane, context growth, cache economics.

Rank findings by money at stake over the window, not by how alarming they look.
Drop any finding worth less than 2% of window spend - it is noise, and reporting it trains the user to ignore the report.

A metric that moved without an obvious cause is a question, not a finding.
Ask it, do not invent the cause.

## Visuals

`references/visuals.md` covers what to draw, when a picture beats a table, and how to capture a real Grafana panel.
Default is charts drawn from this run's own query results, at most three, each followed by what it shows and what the anomaly costs.

## Grade the previous advice

For every open recommendation in MEMO.md, pull its target metric for the window since it was given and assign one verdict:

- `worked` - metric moved in the intended direction beyond noise.
- `no effect` - metric flat.
- `regressed` - metric moved the wrong way.
- `not applied` - the change was never made (no version bump, no config change, user said no).

`not applied` is not a failed recommendation - it stays open, unless the user rejected it, in which case it closes and its reason is recorded so it is never proposed again.

## Report

Answer in the user's language.
Keep it to this shape, in this order, and nothing else:

> **Health** - only when a check fired: what broke, which numbers it invalidates, who owns the fix.
> **Spend** - window total, vs previous window, per-day trend direction, with the headline chart.
> **Where it goes** - top 5 operations by cost, each with cost per call.
> **Waste** - each waste finding with its cost, largest first.
> **Verdicts** - one line per previous recommendation with its grade.
> **Do next** - at most 3 actions, each with the metric it should move and by roughly how much.
> **Setup gaps** - at most 2 dashboard/metric/collection proposals, and at most 1 change to this skill.

Cap "Do next" at 3 items even when more were found - the rest wait for the next run.
Each action names the exact file or setting to change, never a vague direction.
Drop "Setup gaps" entirely on a run that found nothing worth proposing.

## Update the memo

Append one run entry to `MEMO.md` after reporting, then prune it to the last 12 entries.
The entry records the window, the headline metrics as the next run's baseline, health checks that fired, the dashboard fingerprint, the verdicts assigned, and the recommendations left open.

Revise a threshold only on evidence, and record the evidence next to it.
Two runs in a row where a signal fired and the finding turned out worthless means the threshold is too tight - loosen it and note why.
A waste pattern that cost real money while every threshold stayed quiet means a missing signal - add it to the memo's own signal list, and only promote it into `references/playbook.md` once it has fired usefully twice.

Never rewrite `SKILL.md` or `references/playbook.md` silently as part of a run.
Propose the edit with its evidence, let the user approve it, and bump the `v` in the frontmatter when it lands.

## Scheduled runs

A scheduled run is the same workflow with two differences: scope defaults to `since-last`, and the report is written to a file under `.claude/data/usage-coach/` as well as sent, so it survives the notification.
Stay silent on a scheduled run that found no fired health check, no finding above the 2% floor, and no verdict change - a weekly "nothing to report" is what trains the user to stop reading.

The user owns the schedule.
Propose one, never create or change it unasked.

## Guardrails

`agent_messages` holds raw prompt/response text - never read it for statistics, and never quote it into a report.
Cost comes from `agent_usage.cost` summed directly, never derived from a price table (`agent_docs/incidents.md`).
This skill writes only `MEMO.md` and its own files under `.claude/data/usage-coach/`.
Dashboard edits go to `dashboards-expert`, schema changes to `Skill(clickhouse-migration)`, service state to `dev-ops` - this skill proposes, they execute, the user approves.
