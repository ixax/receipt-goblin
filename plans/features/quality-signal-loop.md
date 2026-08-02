# Quality signal loop (agent/skill/version quality score)

## Context

Regression detection today (bloat detector, verbosity trend, "Agent version-change impact" panel in `agents_overview.json`) only measures cost/tokens/latency.
None of that tells us whether a response was actually *good*.
There is no score tied to `session_id`/`trace_id`/`agent_name`+`agent_version`/`skill_name`+`skill_version` today.

Prior art already exists for one narrow case: the `/goal` Stop hook issues a "judge call" and `services/_common/src/ingest_parsing.py`'s `_judge_verdict` parses its `{"ok": bool, "reason": string}` response, which the Trace panel displays.
That convention is scoped to a single user-invoked goal check, not a general quality signal across all traffic - but the shape (LLM judge -> structured verdict -> stored alongside the row) is worth reusing.
`agent_docs/incidents.md` documents that `/goal` judge calls never hit prompt cache and rewrite the full context every time - the same cost trap applies to any new sampled-judge mechanism, so cost control is a first-class design constraint, not an afterthought.

## Design: three tiers, ship independently

### Tier A - implicit signal from data already collected (no new LLM calls)

Detect "user pushed back / rephrased / corrected" from the transcript already stored in `agent_messages` (`prompt_text`/`response_text`, ordered `(session_id, litellm_call_id)`).

- Add a parsing function in `services/_common/src/ingest_parsing.py`, alongside the existing `_last_user_text` extractor, that compares consecutive turns within a session and flags a regex hit against a correction/negation wordlist (RU+EN: "не то", "стоп", "отмена", "not that", "wrong", "undo", "revert", etc.).
- Store the result as a new column (e.g. `pushback_flag`) on `agent_events`, or a small dedicated table keyed the same way `agent_messages` is - decide during implementation, following `clickhouse-migration`'s checklist either way.
- Backfill historical rows via `services/reparse/src/reparse.py` against `ingest_raw`, the documented mechanism for re-deriving columns from historical payloads.
- Add a "Pushback rate by agent/skill/version" panel next to the existing version-change-impact panel in `agents_overview.json`.

Caveat: this is a noisy heuristic, not ground truth.
It is the cheapest tier and ships first because it costs no extra model calls and works on data already ingested.

### Tier B - explicit human feedback via the harness

No existing Claude Code lifecycle event carries an explicit "this response was good/bad" signal.
Needs a new user-facing mechanism (candidate: a `/feedback good|bad "why"` slash command) wired the same way `hooks/report_plan_proposal.py` reports to `/api/v1/plan-proposal` today.
Route it through the side-channel Redis stream design (`plans/side-channel-redis-and-describe-fix.md`, `kind="feedback"` on `webhook:side-events`) rather than a new synchronous insert path, since that plan already establishes the pattern for exactly this kind of low-volume hook traffic.
New table: `agent_feedback(session_id, trace_id, litellm_call_id, sentiment Enum('positive','negative'), note String, submitted_at)`.

Open question for the user: what UX should trigger this - a slash command, something else?
This tier cannot start until that's decided.

### Tier C - sampled LLM-as-judge scoring

Generalizes the `/goal` judge_call convention beyond `/goal` itself.

- A scheduled job (cron-triggered subagent, or a script under `services/reparse/`) samples a small percentage of completed sessions and sends the transcript plus a rubric prompt through the existing LiteLLM proxy, under its own virtual key/team so judge spend is tracked separately from the sampled agent's own cost dimensions.
- Parse a `{"score": 1-5, "reason": string}` response, same shape as `_judge_verdict`.
- Store in a new `agent_scores(session_id, trace_id, litellm_call_id, score_type LowCardinality(String), score Float32, rationale String, judge_model String, scored_at DateTime64(3))` table.
- Score whole-session summaries, not every individual call, to bound spend given the known cache-miss cost trap.

Open question for the user: acceptable sampling rate and monthly budget for judge calls - this tier should not start without an explicit number.

## Rollout order

Tier A first: near-zero cost, ships fastest, backfillable against all history immediately.
Tier B second: blocked on the UX decision above.
Tier C last: blocked on an explicit budget decision, and ideally validated against Tier A's heuristic first to see how well they agree.

## Dashboard integration

Add "Quality score by agent/version" (Tier C) and "Pushback rate by agent/version" (Tier A) panels beside the existing "Agent version-change impact" panel, using the same before/after-version-bump comparison pattern, once each tier has real data.
