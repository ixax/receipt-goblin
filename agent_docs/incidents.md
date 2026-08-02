# Incident history

Narrative context for the one-line rules in `AGENTS.md`'s "Rules to not violate" and "Git: ask before destructive actions" sections.
The rule itself stays in `AGENTS.md`; this file holds the "why" for whoever wants it.
New incidents get appended here by whichever agent hits one, as terse symptom/cause/fix records - never narrative.

## `litellm` restart vs. recreate

Symptom: `litellm` ran for a while with zero Langfuse traces produced after `LANGFUSE_PUBLIC_KEY`/`SECRET_KEY` were added to its `environment:` in `docker-compose.yml`.
Cause: `litellm` was `restart`ed instead of recreated - `restart` reuses the container's existing environment snapshot and never picks up new/changed `environment:` entries, only `up -d` (recreate) does.
Fix: recreate (`up -d`) after any `environment:` change; see `agent_docs/rules/litellm-ops.md`.

## `grafana.db` wipe

A background `dev-ops` subagent, mid an unrelated dashboard-rename rebuild, ran `docker run --rm -v receipt-goblin_grafana-data:/data alpine rm /data/grafana.db` unprompted.
No data loss occurred (the command apparently didn't take effect), but nothing had stopped an agent from reaching for a full DB wipe as a troubleshooting shortcut.

## `model_pricing` cost overcounting

Cause: a manually-maintained `model_pricing` table with an `ASOF JOIN` derivation computed `agent_usage.cost`/`input_cost`/`output_cost`, pricing every input token at full rate and ignoring the cache-read/cache-write discount LiteLLM's own `response_cost`/`cost_breakdown` already applies correctly.
Symptom: overcounted cost by several times whenever prompt caching was in play.
Fix: table removed - use LiteLLM's own `response_cost`/`cost_breakdown` (`agent_docs/rules/coding.md`).

## Static IP race

Cause: before the `docker-compose.yml` network's `ipam.ip_range` exclusion existed, `litellm` and what is now `mcp-dev` could grab `172.28.0.11`/`.12` before `webhook-1`/`webhook-2` claimed their static addresses - Docker's automatic allocator drew from the same range the static IPs needed.
Fix: exclude `172.28.1.x` (the static-IP range) from `ipam.ip_range` (`172.28.0.0/24`), so the allocator can never hand one of those addresses to another container first.

## `/goal` judge calls never hit prompt cache

Symptom: whenever `/goal` is active, Claude Code periodically fires a separate `judge_call` (checks whether the hook's stop condition is met, same `claude-sonnet-5` model as the main session, classified via `_classify_event()`/`_JUDGE_CALL_PREFIX` in `services/_common/src/ingest_parsing.py`) that never hits prompt cache.
Data (last 30 days, `agent_usage`/`agent_events`): 22 `judge_call` rows across 5 sessions, `cache_read_tokens = 0` on every one, `cache_creation_tokens` averaging ~127K (essentially equal to `input_tokens`), $7.07 total cost for those 22 calls.
Non-judge calls in the same sessions behave normally: 1795/1838 hit the cache, ~2.7K average `cache_creation_tokens`, ~117K average `cache_read_tokens`.
Cause: each judge call re-serializes and re-writes the entire current context to cache from scratch rather than reading the already-warm cache the main session just wrote.
Prompt caching is an exact-prefix match, and the judge call's prompt evidently isn't byte-identical to the main session's up to some breakpoint, so every token past the first divergence is treated as new (`cache_creation_tokens` climbs in lockstep with session length, 40K -> 190K across one session's calls).
This is Claude Code's own `/goal` mechanism, not a bug in our ingestion/classification code - no fix was made here.
Mitigation: keep `/goal` sessions short, or avoid `/goal` for long-running sessions, since cost scales with session context size at each judge check (one session had 19 judge calls within about 3 minutes).

## Plan Mode forks a new `session_id` (unrelated to any data-loss gap)

Symptom investigated: the Trace panel (panel-76) showed less activity for a session than the actual chat transcript.
Finding: entering Plan Mode (the `EnterPlanMode`/`ExitPlanMode` tools, `command_name = 'plan'` in `agent_messages`) triggers a genuine new `SessionStart` in Claude Code, producing a brand-new `session_id` (`session_id == trace_id == litellm_call_id`, no `x-claude-code-session-id` header at all) with no data-level link back to whatever conversation the user thinks of as "the same session" - not even a shared `trace_id` to join on.
Correction: this was initially misattributed as the root cause of a specific ~13min gap in session `0cd1980e-...`; checking the forked session's actual prompt content disproved that - it was a coincidentally-nearby, unrelated `/plan` conversation.
The real cause of that gap is the "LiteLLM `GenericAPILogger` drops a whole batch on one failed flush" entry below - never declare a root cause from timing/shape correlation alone, check the actual payload content first.
Fix: the `session_id` dashboard variable (`services/grafana/dashboards/agents_overview.json`, `spec.variables`) excludes plan-mode-fork sessions (`agent_messages` sessions where `sum(command_name != 'plan') = 0`) and zero-real-prompt sessions (no row with non-empty `prompt_text`) from its dropdown - both cluttered the session picker with fragments no one would deliberately pick.

## LiteLLM `GenericAPILogger` drops a whole batch on one failed flush

Symptom: two independently-confirmed data gaps - a ~13min `agent_usage` gap in session `0cd1980e-...`, and missing `response_text` in session `ed2a5cc5-...`.
Cause (found by reading `litellm`'s running code, not by inference from ClickHouse alone): `GenericAPILogger` (the `generic_api` callback behind `metrics_webhook`, shipping `StandardLoggingPayload` to `webhook`'s `/api/v1/metrics` - the real ingestion path, not a side metrics channel) defaulted to `max_retries: 0`, and `services/litellm/config.yaml` didn't override it.
`async_send_batch()`'s `finally: self.log_queue.clear()` ran unconditionally, win or lose, so one failed flush (a `load-balancer` 504, a timeout) silently discarded the entire accumulated batch (up to `batch_size` events), not just the one request.
`docker logs` on `litellm` confirmed a burst of 504s against `http://load-balancer:8000/api/v1/metrics` during the `0cd1980e` gap window, and repeated `chatgpt_responses_output_recovery_handler` timeouts during the `ed2a5cc5` window - both are flush-time failures this same zero-retry/clear-regardless pattern would silently eat.
Fix: `services/litellm/config.yaml`'s `callback_settings.metrics_webhook` sets `max_retries: 3`/`retry_delay: 1.0` (backoff 1s/2s/4s, only for `litellm.Timeout`/`httpx.TransportError`/5xx).
`services/litellm/custom_callbacks.py` monkeypatches `GenericAPILogger.async_send_batch` (`_patched_async_send_batch`) to clear the queue only on success, keeping unsent items queued on failure, capped at `_MAX_RETAINED_LOG_QUEUE = 2000` so a sustained outage grows the retry buffer instead of unboundedly (still sheds oldest events past that cap).
Residual risk: an outage longer than the retry backoff and longer than the time to refill the capped buffer still loses data - narrowed, not closed.
Not yet done: root-causing why `load-balancer`/`webhook` 504'd for several minutes straight in the `0cd1980e` window (overloaded `webhook`, backlogged `redis`, or a container restart) - the retry/no-clear fixes reduce blast radius, they don't explain this specific occurrence.

## `display_text` only stripped the first of multiple leading `<system-reminder>` blocks

Symptom: `display_text` (precomputed at ingest by `_prompt_kind_and_display` in `services/_common/src/ingest_parsing.py`) still had a raw `<system-reminder>...</system-reminder>` block in some rows, instead of being fully cleaned.
Cause, confirmed against a genuine multi-reminder row (`litellm_call_id = 'a8199216-8d1e-44b2-91a2-f95be2bf1cd9'`, checked via `ingest_raw.raw_payload_full`): Claude Code can inject more than one `<system-reminder>` block as separate leading content blocks of the same user turn (e.g. a skills-listing reminder, then a memory/claudeMd reminder, then the real task text - three blocks in that example).
`_SYSTEM_REMINDER_STRIP_RE` only matched one leading block (`^<system-reminder>.*?</system-reminder>\s*`, applied once), so the second reminder survived into `display_text` untouched.
Fix: regex changed to `^(?:\s*<system-reminder>.*?</system-reminder>)+\s*` (repeats, strips every consecutive leading block in one match).
Added `test_prompt_kind_and_display_success_strips_two_leading_system_reminders` in `services/_common/tests/test_ingest_parsing.py`; full suite (116 tests) passes.
Panel-76 ("Trace")'s own `tool_result_preview` CTE had the identical one-block-only bug in its SQL-side regex, fixed alongside.
Not yet done: `make reparse-all` to fix already-ingested `display_text` values - the fix only applies going forward until that's run.

## Dynamic Text panel ownership hardcoded by id instead of `type`

Cause: `dynamictext-panel-builder`'s scope was written as a hardcoded id/title list ("panel-76 ('Trace'), panel-77") instead of deriving ownership from the panel's own `type` field (`marcusolsson-dynamictext-panel`).
Drift confirmed against `services/grafana/dashboards/agents_overview.json`: panel 99 ("Fork tree", added later) is `type: marcusolsson-dynamictext-panel` but wasn't in the hardcoded list, so `dashboard-panels-builder` edited it while `dynamictext-panel-builder` refused it as out of scope.
Panel 77 is `type: table`, not Dynamic Text, yet was listed as owned - correct outcome (its `$trace_ts` coupling to panel-76), but wrong as a general rule.
Fix: both agents' scope rules checked `type` (via `dashboard-parser`/a direct read) rather than memorizing ids, with panel-77's coupling called out as a documented exception.
Both agents have since merged into `dashboards-expert` (`.claude/agents/dashboards-expert.md`), which now owns this rule directly.
Rule: an agent's routing scope must key off a stable, machine-checkable field (`type`) when one exists, never off an id/title that changes as panels are added/renamed.

## Git checkout/restore clobbering uncommitted work

Three past incidents (a dashboard reformat "fixed" via `git checkout -- <file>`, a misdiagnosed-corruption checkout, and a bulk edit that used `git show :path` to self-"restore") each silently discarded concurrent uncommitted work, recovered only by luck each time.
Fix: `hooks/guard_destructive.py` now enforces this (`git checkout --`/`git restore` force a confirmation prompt), not prose alone.
See `agent_docs/git-safety.md` for the `git show :path` variant and the full rule this incident led to.
