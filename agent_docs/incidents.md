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

Investigated `todo/judge_call.md`: whenever `/goal` is active, Claude Code periodically fires a separate `judge_call` (an LLM call that checks whether the hook's stop condition is met, same `claude-sonnet-5` model as the main session, classified via `_classify_event()`/`_JUDGE_CALL_PREFIX` in `services/_common/src/ingest_parsing.py`).
Querying `agent_usage`/`agent_events` over the last 30 days (22 `judge_call` rows across 5 sessions) confirmed this is systemic, not a one-session fluke: `cache_read_tokens = 0` on every single `judge_call`, `cache_creation_tokens` averaging ~127K tokens (essentially equal to `input_tokens`), total cost $7.07 for those 22 calls alone.
Non-judge calls in the exact same sessions behave normally: 1795/1838 hit the cache, average `cache_creation_tokens` is only ~2.7K (incremental growth) with ~117K average `cache_read_tokens`.
Within a single session, `judge_call` `cache_creation_tokens` climbs in lockstep with session length (40K -> 190K across one session's calls) - each judge call re-serializes and re-writes the *entire* current context to cache from scratch rather than reading the already-warm cache the main session just wrote, because (per Anthropic's cache-diagnostics docs) prompt caching is an exact-prefix match and the judge call's prompt evidently isn't byte-identical to the main session's up to some breakpoint, so every token past the first divergence is treated as new.
This is Claude Code's own `/goal` mechanism, not a bug in our ingestion/classification code - no fix was made here.
Practical mitigation available to a user: keep `/goal` sessions short, or avoid `/goal` for long-running sessions, since cost scales with how large the session context has grown by the time each judge check fires (judge checks appear to fire quite frequently - one session had 19 judge calls within about 3 minutes).

## Plan Mode forks a new `session_id` (unrelated to any data-loss gap)

Investigated a user report that the Trace panel (panel-76) showed less activity for a session than the actual chat transcript.
While looking for the cause, found that entering Plan Mode (the `EnterPlanMode`/`ExitPlanMode` tools, `command_name = 'plan'` in `agent_messages`) triggers a genuine new `SessionStart` in Claude Code, producing a brand-new `session_id` (with `session_id == trace_id == litellm_call_id`, i.e. no `x-claude-code-session-id` header at all) with no data-level link back to whatever conversation the user thinks of as "the same session" - not even a shared `trace_id` to join on.
**Correction**: this was initially (wrongly) reported as the root cause of a specific ~13min gap in session `0cd1980e-...` - checking the forked session's actual prompt content later disproved that; it was a coincidentally-nearby, completely unrelated `/plan` conversation.
The real cause of that gap is documented separately below ("LiteLLM `GenericAPILogger` drops a whole batch on one failed flush").
The Plan Mode session-forking behavior itself is still real and still worth guarding against in the UI: the `session_id` dashboard variable (`services/grafana/dashboards/agents_overview.json`, `spec.variables`) excludes these plan-mode forks from its dropdown (`agent_messages` sessions where `sum(command_name != 'plan') = 0`), plus sessions with zero real prompts (no `agent_messages` row with non-empty `prompt_text`) - both were cluttering the session picker with fragments no one would deliberately pick.
Lesson: don't declare a root cause from timing/shape correlation alone - check the actual payload content before writing it down (this entry originally didn't, and was wrong).

## LiteLLM `GenericAPILogger` drops a whole batch on one failed flush

Two real, independently-confirmed data gaps (a ~13min `agent_usage` gap in session `0cd1980e-...`, and missing `response_text` in session `ed2a5cc5-...`) both traced to the same upstream mechanism, found by reading `litellm`'s actual running code (`docker exec receipt-goblin-litellm cat .../litellm/integrations/generic_api/generic_api_callback.py`), not by inference from ClickHouse alone.
`GenericAPILogger` (the `generic_api` callback type behind `metrics_webhook`, which ships `StandardLoggingPayload` to `webhook`'s `/api/v1/metrics` - confirmed as the real ingestion path, not a side metrics channel) defaults to `max_retries: 0`, and `services/litellm/config.yaml` didn't override it.
Worse: `async_send_batch()`'s `finally: self.log_queue.clear()` runs unconditionally, win or lose - so one failed flush (a `load-balancer` 504, a timeout) doesn't just lose the one request, it silently discards the *entire* accumulated batch (up to `batch_size` events).
`docker logs` on `litellm` confirmed a burst of 504s against `http://load-balancer:8000/api/v1/metrics` during the `0cd1980e` gap window, and repeated `chatgpt_responses_output_recovery_handler` timeouts during the `ed2a5cc5` window - both are flush-time failures this same zero-retry/clear-regardless pattern would silently eat.
Fix: `services/litellm/config.yaml`'s `callback_settings.metrics_webhook` now sets `max_retries: 3`/`retry_delay: 1.0` (exponential backoff 1s/2s/4s, only for `litellm.Timeout`/`httpx.TransportError`/5xx).
`services/litellm/custom_callbacks.py` also monkeypatches `GenericAPILogger.async_send_batch` (`_patched_async_send_batch`) to only clear the queue on success - on failure it keeps unsent items queued for the next flush, capped at `_MAX_RETAINED_LOG_QUEUE = 2000` so a sustained outage grows the retry buffer instead of unboundedly, but still eventually sheds oldest events past that cap.
Neither fix makes loss impossible - an outage longer than the retry backoff *and* longer than however long it takes to refill the capped buffer will still lose data - it narrows the window rather than closing it.
Separate, not yet done: root-causing *why* `load-balancer`/`webhook` returned 504s for several minutes straight in the `0cd1980e` window (was `webhook` overloaded, `redis` backlogged, or the container itself restarting) - the retry/no-clear fixes above reduce the blast radius of that class of outage, they don't explain this particular occurrence.

## `display_text` only stripped the first of multiple leading `<system-reminder>` blocks

While building the "Fork tree" panel (panel-99), found `display_text` (precomputed at ingest by `_prompt_kind_and_display` in `services/_common/src/ingest_parsing.py`) still had a raw `<system-reminder>...</system-reminder>` block in it for some rows, instead of being fully cleaned.
First checked a row where `display_text` looked completely unrelated to that row's own `prompt_text` (`agent_invocation_id = 'a4940dbd71e06ab00'`) and wrongly concluded it was a join/dedup mismatch between `agent_events` and `agent_messages` - disproven on closer inspection: that row's raw `prompt_text` legitimately had two content blocks (a system-reminder, then the real task text) concatenated together, and `display_text` was correctly the cleaned (second) part all along - not a mismatch at all.
The real bug, confirmed against a genuine multi-reminder row (`litellm_call_id = 'a8199216-8d1e-44b2-91a2-f95be2bf1cd9'`, checked via `ingest_raw.raw_payload_full`): Claude Code can inject **more than one** `<system-reminder>` block as separate leading content blocks of the same user turn (e.g. a skills-listing reminder, then a memory/claudeMd reminder, then the real task text) - three blocks in that example.
`_SYSTEM_REMINDER_STRIP_RE` only matched one leading block (`^<system-reminder>.*?</system-reminder>\s*`, applied once), so the second reminder survived into `display_text` untouched.
Fix: changed the regex to `^(?:\s*<system-reminder>.*?</system-reminder>)+\s*` (repeats, strips every consecutive leading block in one match).
Added `test_prompt_kind_and_display_success_strips_two_leading_system_reminders` in `services/_common/tests/test_ingest_parsing.py`; full suite (116 tests) passes.
Panel-76 ("Trace")'s own `tool_result_preview` CTE has the identical one-block-only bug in its SQL-side regex - fixed alongside (see `dynamictext-panel-builder`'s work).
Not yet done: reparsing historical rows (`make reparse-all`) to fix already-ingested `display_text` values - the fix only applies going forward until that's run.
Lesson (repeats the one two entries up): the first read of the data looked like a mismatch and wasn't - always trace back to the actual raw payload before concluding two fields disagree.

## Git checkout/restore clobbering uncommitted work

Three past incidents (a dashboard reformat "fixed" via `git checkout -- <file>`, a misdiagnosed-corruption checkout, and a bulk edit that used `git show :path` to self-"restore") each silently discarded concurrent uncommitted work, recovered only by luck each time.
`hooks/guard_destructive.py` now enforces this (`git checkout --`/`git restore` force a confirmation prompt), not prose alone.
See `agent_docs/git-safety.md` for the `git show :path` variant and the full rule this incident led to.
