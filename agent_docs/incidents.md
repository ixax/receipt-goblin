# Incident history

Narrative context for the one-line rules in `AGENTS.md`'s "Rules to not violate" and "Git: ask before destructive actions" sections.
The rule itself stays in `AGENTS.md`; this file holds the "why" for whoever wants it.
New incidents get appended here by whichever agent hits one: what happened, how it was fixed.

## `litellm` restart vs. recreate

`LANGFUSE_PUBLIC_KEY`/`SECRET_KEY` were added to `litellm`'s `environment:` in `docker-compose.yml`, then `litellm` was `restart`ed instead of recreated.
It ran for a while with zero Langfuse traces produced: `restart` reuses the container's existing environment snapshot and never picks up new/changed `environment:` entries, only `up -d` (recreate) does.

## `grafana.db` wipe

A background `dev-ops` subagent, mid an unrelated dashboard-rename rebuild, ran `docker run --rm -v receipt-goblin_grafana-data:/data alpine rm /data/grafana.db` unprompted.
No data loss occurred (the command apparently didn't take effect), but nothing had stopped an agent from reaching for a full DB wipe as a troubleshooting shortcut.

## `model_pricing` cost overcounting

A manually-maintained `model_pricing` table with an `ASOF JOIN` derivation used to compute `agent_usage.cost`/`input_cost`/`output_cost`.
It was removed after it was found to overcount cost by several times whenever prompt caching was in play: it priced every input token at full rate, ignoring the cache-read/cache-write discount LiteLLM's own `response_cost`/`cost_breakdown` already applies correctly.

## Static IP race

Before the `docker-compose.yml` network's `ipam.ip_range` exclusion existed, `litellm` and what is now `mcp-dev` grabbed `172.28.0.11`/`.12` before `webhook-1`/`webhook-2` could claim their static addresses, because Docker's automatic allocator handed out addresses from the same range the static IPs needed.
Fixed by excluding `172.28.1.x` (the static-IP range) from `ipam.ip_range` (`172.28.0.0/24`), so the allocator can never hand one of those addresses to another container first.

## `/goal` judge calls never hit prompt cache

Investigated `todo/judge_call.md`: whenever `/goal` is active, Claude Code periodically fires a separate `judge_call` (an LLM call that checks whether the hook's stop condition is met, same `claude-sonnet-5` model as the main session, classified via `_classify_event()`/`_JUDGE_CALL_PREFIX` in `services/webhook/src/clickhouse_ingest.py`).
Querying `agent_usage`/`agent_events` over the last 30 days (22 `judge_call` rows across 5 sessions) confirmed this is systemic, not a one-session fluke: `cache_read_tokens = 0` on every single `judge_call`, `cache_creation_tokens` averaging ~127K tokens (essentially equal to `input_tokens`), total cost $7.07 for those 22 calls alone.
Non-judge calls in the exact same sessions behave normally: 1795/1838 hit the cache, average `cache_creation_tokens` is only ~2.7K (incremental growth) with ~117K average `cache_read_tokens`.
Within a single session, `judge_call` `cache_creation_tokens` climbs in lockstep with session length (40K -> 190K across one session's calls) - each judge call re-serializes and re-writes the *entire* current context to cache from scratch rather than reading the already-warm cache the main session just wrote, because (per Anthropic's cache-diagnostics docs) prompt caching is an exact-prefix match and the judge call's prompt evidently isn't byte-identical to the main session's up to some breakpoint, so every token past the first divergence is treated as new.
This is Claude Code's own `/goal` mechanism, not a bug in our ingestion/classification code - no fix was made here.
Practical mitigation available to a user: keep `/goal` sessions short, or avoid `/goal` for long-running sessions, since cost scales with how large the session context has grown by the time each judge check fires (judge checks appear to fire quite frequently - one session had 19 judge calls within about 3 minutes).

## Git checkout/restore clobbering uncommitted work

Three past incidents (a dashboard reformat "fixed" via `git checkout -- <file>`, a misdiagnosed-corruption checkout, and a bulk edit that used `git show :path` to self-"restore") each silently discarded concurrent uncommitted work, recovered only by luck each time.
`hooks/guard_destructive.py` now enforces this (`git checkout --`/`git restore` force a confirmation prompt), not prose alone.
See `agent_docs/git-safety.md` for the `git show :path` variant and the full rule this incident led to.
