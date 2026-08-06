# Incident history

Context for the one-line rules in `AGENTS.md`'s "Rules to not violate" and "Git: ask before destructive actions" - the rule stays in `AGENTS.md`, this file holds the "why".
Whoever hits a new incident appends it here as a terse, dated symptom/cause/fix record - never a narrative.

## `litellm` restart vs. recreate

Symptom: zero Langfuse traces after `LANGFUSE_PUBLIC_KEY`/`SECRET_KEY` were added to `litellm`'s `environment:`.
Cause: `restart` reuses the container's environment snapshot; only `up -d` (recreate) re-reads `environment:`.
Fix: recreate after any `environment:` change - `agent_docs/rules/litellm-ops.md`.

## `grafana.db` wipe attempt

Symptom: a background `dev-ops` subagent, mid an unrelated dashboard-rename rebuild, ran `docker run --rm -v receipt-goblin_grafana-data:/data alpine rm /data/grafana.db` unprompted (no data loss - the command didn't take effect).
Cause: nothing stopped an agent from reaching for a full DB wipe as a troubleshooting shortcut.
Fix: DB/volume wipe is ask-first (`AGENTS.md` boundary).

## `model_pricing` cost overcounting

Symptom: cost overcounted several-fold whenever prompt caching was in play.
Cause: a manually-maintained `model_pricing` table + `ASOF JOIN` priced every input token at full rate, ignoring the cache discounts LiteLLM's own `response_cost`/`cost_breakdown` already applies.
Fix: table removed - always use LiteLLM's own `response_cost`/`cost_breakdown` (`agent_docs/rules/coding.md`).

## Static IP race

Symptom: `litellm`/`mcp-dev` could grab `172.28.0.11`/`.12` before `webhook-1`/`webhook-2` claimed their static addresses.
Cause: Docker's automatic allocator drew from the same range the static IPs needed.
Fix: `ipam.ip_range` (`172.28.0.0/24`) excludes the static range `172.28.1.x`.

## `/goal` judge calls never hit prompt cache

Symptom: with `/goal` active, Claude Code periodically fires a separate `judge_call` (stop-condition check, same `claude-sonnet-5` model; classified via `_classify_event()`/`_JUDGE_CALL_PREFIX` in `services/_common/src/ingest_parsing.py`) with `cache_read_tokens = 0` on every call - 22 rows/5 sessions in 30 days, `cache_creation_tokens` averaging ~127K (~= `input_tokens`), $7.07 total; non-judge calls in the same sessions cached normally (1795/1838 hits).
Cause: prompt caching is exact-prefix; the judge prompt isn't byte-identical to the main session's, so each check re-writes the whole context to cache (`cache_creation_tokens` climbs 40K -> 190K with session length).
Fix: none possible here - Claude Code's own `/goal` mechanism, not our ingestion.
Mitigation: keep `/goal` sessions short (one session logged 19 judge calls in ~3 minutes); cost scales with context size per check.

## Plan Mode forks a new `session_id`

Symptom: the Trace panel (panel-76) showed less activity than the actual transcript.
Cause: entering Plan Mode (`EnterPlanMode`/`ExitPlanMode`, `command_name = 'plan'` in `agent_messages`) triggers a genuine new `SessionStart`: a new `session_id` (`session_id == trace_id == litellm_call_id`, no `x-claude-code-session-id` header) with no data-level link to the parent conversation.
Fix: the `session_id` dashboard variable (`agents_overview.json`, `spec.variables`) excludes plan-mode-fork sessions (`agent_messages` sessions where `sum(command_name != 'plan') = 0`) and zero-real-prompt sessions from its dropdown.
Lesson: this was first misattributed as the cause of a ~13min gap in session `0cd1980e-...`; the forked session's actual prompt content disproved it (real cause: the `GenericAPILogger` entry below) - never declare a root cause from timing/shape correlation alone, check payload content first.

## LiteLLM `GenericAPILogger` drops a whole batch on one failed flush

Symptom: two confirmed data gaps - ~13min of `agent_usage` in session `0cd1980e-...` (504 burst against `http://load-balancer:8000/api/v1/metrics` in `litellm`'s logs), and missing `response_text` in session `ed2a5cc5-...` (repeated `chatgpt_responses_output_recovery_handler` timeouts).
Cause (from `litellm`'s running code): `GenericAPILogger` (the `generic_api` callback behind `metrics_webhook` - the real ingestion path) defaulted to `max_retries: 0`, and `async_send_batch()`'s `finally: self.log_queue.clear()` ran win or lose - one failed flush discarded the whole accumulated batch (up to `batch_size` events).
Fix: `services/litellm/config.yaml` `callback_settings.metrics_webhook` sets `max_retries: 3`/`retry_delay: 1.0` (backoff 1s/2s/4s, only for `litellm.Timeout`/`httpx.TransportError`/5xx); `services/litellm/custom_callbacks.py` monkeypatches `async_send_batch` (`_patched_async_send_batch`) to clear only on success, retaining unsent items capped at `_MAX_RETAINED_LOG_QUEUE = 2000` (sheds oldest past the cap).
Residual: an outage outlasting the backoff + capped buffer still loses data; why `load-balancer`/`webhook` 504'd for minutes in the `0cd1980e` window is unexplained.

## `display_text` only stripped the first of multiple leading `<system-reminder>` blocks

Symptom: some `display_text` rows (precomputed by `_prompt_kind_and_display`, `services/_common/src/ingest_parsing.py`) still carried a raw `<system-reminder>...</system-reminder>` block.
Cause: Claude Code can inject several consecutive leading `<system-reminder>` blocks in one user turn (confirmed on `litellm_call_id = 'a8199216-8d1e-44b2-91a2-f95be2bf1cd9'` via `ingest_raw.raw_payload_full`); `_SYSTEM_REMINDER_STRIP_RE` matched only one.
Fix: regex now `^(?:\s*<system-reminder>.*?</system-reminder>)+\s*`; `test_prompt_kind_and_display_success_strips_two_leading_system_reminders` added (`services/_common/tests/test_ingest_parsing.py`); panel-76's `tool_result_preview` CTE had the identical SQL-side bug, fixed alongside.
Residual: `make reparse-all` not yet run - already-ingested `display_text` stays wrong until it is.

## Dynamic Text panel ownership hardcoded by id instead of `type`

Symptom: panel 99 ("Fork tree", `type: marcusolsson-dynamictext-panel`, added later) wasn't in the hardcoded id list, so the wrong agent edited it while the right one refused it; panel 77 (`type: table`) was listed as owned - right outcome (its `$trace_ts` coupling to panel-76), wrong as a rule.
Cause: agent scope written as an id/title list instead of the panel's own `type` field.
Fix: scope keys off `type` (via `dashboard-parser`/a direct read) with panel-77 as a documented exception; both agents since merged into `dashboards-expert`.
Rule: routing scope keys off a stable machine-checkable field when one exists, never an id/title that changes.

## Sub-agent attempted a `docker exec` ClickHouse bypass under a fabricated "user authorization"

Symptom: during panel-76 OOM investigation, a delegated sub-agent tried `docker exec .../clickhouse-client` instead of `mcp__dev__query`, citing a "user authorization" that came from neither the user nor the orchestrator; the permission classifier blocked it.
Rule: no exception to "never `docker exec .../clickhouse-client`" exists for authorization surfaced only inside a sub-agent's own reasoning - a sub-agent citing it is itself the signal to stop and escalate, not comply.
Rule: a pending sub-agent follow-up/audit at session end gets written into this file (or a plan doc) immediately - an ended conversation's context is unrecoverable (the audit requested from that sub-agent was lost exactly this way).

## Git checkout/restore clobbering uncommitted work

Symptom: three incidents (a dashboard reformat "fixed" via `git checkout -- <file>`, a misdiagnosed-corruption checkout, a bulk edit self-"restoring" via `git show :path`) each silently discarded concurrent uncommitted work, recovered only by luck.
Fix: `hooks/guard_destructive.py` forces a confirmation prompt on `git checkout --`/`git restore`; full rule incl. the `git show :path` variant: `agent_docs/git-safety.md`.

## `loadtest-runner` hand-back leaked the live ClickHouse password

Symptom: during a `make loadtest` run (2026-08-04) interrupted and resumed several times mid-run, the subagent's final hand-back text printed the live `CLICKHOUSE_PASSWORD` in plaintext three times, landing it in the on-disk task-output/transcript file.
Cause: verifying `webhook-1`/`webhook-2`/`worker` had switched to/from the isolated `loadtest` DB role included raw `docker inspect`/env-var output verbatim instead of a redacted form.
Fix: env-var verification in agent output/reports checks presence/non-emptiness or a redacted form, never the literal value - `AGENTS.md`'s "Boundaries & safety" secrets rule, added the same day partly from this incident.
Note: local-dev credential, not a real prod secret - user confirmed rotation isn't needed.

## LiteLLM Codex OAuth passthrough read a redacted header

Symptom: proxied Codex returned 401 followed by a local 429 (`exceeded retry limit`) despite having no deployments, while direct Codex succeeded.
Cause: `ChatGPTAuthForwardHandler` read the redacted `proxy_server_request` Authorization header, while current LiteLLM keeps the raw header in `secret_fields.raw_headers`.
Fix: read the raw Authorization header case-insensitively from `secret_fields.raw_headers`, with the redacted location retained as a compatibility fallback.
Rule: OAuth tokens must never be logged.
