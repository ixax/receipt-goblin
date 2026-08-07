# Diagnosis playbook

Starting thresholds for `/usage-coach`.
A threshold revised in `MEMO.md` wins over the number here - this file only changes when the user approves a promotion, with a version bump on `SKILL.md`.

Every finding needs three parts before it goes in the report: the number, what it costs over the window, and the exact file or setting to change.
A finding without a named change is an observation - drop it.

## Signals

| # | Signal | Source | Fires when | Means | Change to make |
|---|--------|--------|------------|-------|----------------|
| 1 | Low cache reuse | Q2 | `cache_read_share` < 0.60 | The cached prefix is being rebuilt instead of reused | Apply `Skill(harness-guardian)` cache hygiene: no harness edits mid-session, stable MCP/tool set per session, one model family per session |
| 2 | Cache churn | Q2 | `cache_write` > 0.5 x `cache_read` | Prefixes are written and abandoned - sessions too short for the write to pay back, or the prefix keeps changing | Batch related work into one session; check whether an always-loaded file changed mid-session |
| 3 | Cache reuse falling | Q2 | `cache_read_share` down > 10 points vs previous half-window | Something destabilised the prefix in this window | Diff `AGENTS.md`, `.claude/rules/`, `.mcp.json` and the agent set against the previous window |
| 4 | Frontier model on mechanical work | Q3 + Q4 | An op with > 50 calls, `avg_out` < 400, running on the most expensive model | Cheap-model work billed at frontier rates | Add `model: claude-haiku-4-5` to that agent's frontmatter, the way `clickhouse-analyst` already does |
| 5 | Expensive per call | Q4 | `cost_per_call` > 3x the median op, and `avg_ctx` above the run's average | The op is being handed too much context | Narrow what it reads, or move its bulk reading into a subagent that returns only the answer |
| 6 | Chatty op | Q4 | High `calls`, low `cost_per_call`, total in the top 5 | Cost by repetition, not by size | Batch its calls, or cache its result inside the workflow that drives it |
| 7 | Output-heavy op | Q4 | `avg_out` > 3x the run's average | The op writes long output; output tokens are the most expensive tier | Tighten its "report only the answer" wording, cap what it returns |
| 8 | Tool friction | Q7 | Any tool over 3% of window spend in failed-tool reactions | Every failure buys a retry turn | Fix the cause in the harness - a rule, an allowlist entry, a wrapper - not in the prompt |
| 9 | Failure rate | Q8 | `failure` share > 2% of calls | Calls are erroring out and being retried | Read the top failing op's recent errors before recommending anything |
| 10 | Abandoned work | Q9 | `interrupted` > 5% of window spend | Work is paid for and thrown away mid-flight | Shorter task scoping, plan before long runs, stop launching work that gets cancelled |
| 11 | Truncation | Q10 | `max_tokens` > 1% of calls | Answers cut off and asked again - paid twice | Split the task, or raise the output cap for that op |
| 12 | Context bloat | Q11 | `main` lane `avg_ctx` up > 20% over 4 weeks | Sessions carry more context than they used to | Delegate reading to `script-ops`/`code-locator`, `/clear` sooner, stop pasting file dumps into the main thread |
| 13 | Under-delegation | Q11 | `main` lane > 70% of spend while subagent lane is flat | Work that belongs in a cheap subagent runs in the expensive main thread | Route the repeated work to a subagent; the dispatch list lives in `AGENTS.md` |
| 14 | Marathon sessions | Q5 + Q6 | `p90` > 4x `p50` | A few sessions carry the bill | Split those sessions; check `peak_ctx` on the worst one for where it ran away |
| 15 | Version regression | Q12 | `cost_per_call` up > 25% on a newer version of the same agent or skill | The last edit to that agent or skill made it more expensive | Diff the two versions, revert or trim whatever grew |

## Ranking

Rank by money at stake over the window.
Below 2% of window spend a finding does not get reported, whatever its signal says.
Two findings with the same cause collapse into one - report the cause.

## Grading a recommendation

Every recommendation is stored with the metric it should move and the direction.
At the next run, compare that metric over the window since the recommendation was given.

Movement under 10% is noise - grade it `no effect`, not `worked`.
Grade `not applied` when the evidence for the change is missing: no new agent or skill version in Q12, no config change, no new session pattern.
`not applied` twice in a row means the recommendation is too hard to act on - rewrite it smaller or close it.

## Revising a threshold

A threshold moves on evidence, never on a hunch, and the evidence is written next to it in `MEMO.md`.

- Fired twice, both times worthless: loosen it one step, record both run dates.
- Real money lost while every threshold stayed quiet: add a signal to the memo's own list, with the query it needs.
- A memo signal that fired usefully twice is a candidate for promotion into this table - propose it to the user, land it with a version bump.
