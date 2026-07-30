# `litellm`

Proxy every LiteLLM-routed CLI call goes through - model list, virtual-key auth, and the callbacks feeding both ClickHouse (via `webhook`) and the optional Langfuse stack.

## `config.yaml`

- `model_list` - `claude-sonnet-5`/`claude-haiku-4-5`(+`-20251001` alias)/`claude-opus-4-8`/`claude-fable-5` plus `gpt-5.6-{sol,terra,luna}` (Codex/ChatGPT models, forwarded via the caller's own `codex login` OAuth token - see `custom_callbacks.py` below).
  The `gpt-5.6-*` entries carry manual `model_info.*_cost_per_token` since these model strings aren't in LiteLLM's built-in cost map: `response_cost`/`cost_breakdown` came back $0 for every call until added.
  Rates are OpenAI's public per-token pricing (confirmed 2026-07-25) - a cost proxy, since usage actually rides the caller's flat-billed ChatGPT subscription, not per-token billing.
- `general_settings.alerting` - LiteLLM's native alerting (`llm_exceptions`/`llm_too_slow`/`llm_requests_hanging`/`outage_alerts`/`db_exceptions`/`budget_alerts`/`failed_tracking_spend`), reads its webhook target from a hardcoded `WEBHOOK_URL` env var (no config override), distinct from `WEBHOOK_METRICS_URL` below (used to collide under the same name).
  Deliberately excludes management-event types (key/team/user lifecycle, digests) - not reliability signals.
- `general_settings.litellm_key_header_name: x-litellm-api-key` - personal virtual keys go in this header regardless of route (see README.md "LiteLLM"). Needed even for OAuth-backed routes: without it LiteLLM can't tell "authed to me" from "caller's own Anthropic cred, forward it" and strips `Authorization` before forwarding (litellm#19618).
- `model_group_settings.forward_client_headers_to_llm_api` - forwards the caller's own `Authorization` (OAuth token) to Anthropic for every model above. If a model moves to a real `api_key`, remove it from this list too or its credential could still get forwarded.
- `litellm_settings.callbacks` - registered callbacks:
  - `metrics_webhook` (`generic_api`, ships the full `StandardLoggingPayload` to `webhook`'s `/api/v1/metrics` - **not** the same as a bare `success_callback: ["webhook"]`, which fires the thin alerting webhook above instead)
  - `custom_callbacks.session_id_handler`
  - `custom_callbacks.chatgpt_auth_forward_handler`
  - `custom_callbacks.chatgpt_responses_output_recovery_handler`
  - `prometheus` (exposes `/metrics` on port 4000, scraped by Prometheus - `require_auth_for_metrics_endpoint: false` since this project authenticates via `x-litellm-api-key`, which the built-in `/metrics` auth doesn't understand, and scraping only happens over the internal Docker network anyway)
- `litellm_settings.success_callback`/`failure_callback: [langfuse]` - native LiteLLM integration, reads `LANGFUSE_*` env vars, no `callback_settings` block needed. Ships every call as a trace grouped by `metadata.session_id`.

## `custom_callbacks.py`

- `SessionIdHandler` (`session_id_handler`) - a `CustomLogger` pre-call hook copying the `x-claude-code-session-id` header into `metadata.session_id`/`trace_user_id`, so Langfuse groups traces into sessions the same way ClickHouse does.
  `docker-entrypoint.sh` copies this file next to the merged effective config so LiteLLM's bare `custom_callbacks.` module path resolves.
- `ChatGPTAuthForwardHandler` (`chatgpt_auth_forward_handler`) - forwards a Codex caller's own ChatGPT subscription token to the `chatgpt` provider per-call instead of the container's single shared device-code-logged-in identity.
  Requires a monkeypatch on `litellm.llms.anthropic.common_utils.is_anthropic_oauth_key` (also in this file): `clean_headers()` otherwise strips any `Authorization` header it doesn't recognize as Anthropic OAuth (no ChatGPT equivalent exists upstream: BerriAI/litellm#23777, #24500).
  Decodes the JWT's `https://api.openai.com/auth` claim to detect a ChatGPT token.
- `ChatGPTResponsesOutputRecoveryHandler` (`chatgpt_responses_output_recovery_handler`) - works around litellm's streaming iterator dropping tool-call/text output for `custom_llm_provider="chatgpt"` (Codex CLI, streamed Responses API - BerriAI/litellm#25429, unmerged fix as of writing), which otherwise leaves `StandardLoggingPayload.response.output` empty and breaks the Trace panel/ClickHouse view of what Codex did.
  Accumulates output items via `async_post_call_streaming_iterator_hook` as SSE chunks pass through, then injects them in `async_logging_hook` if `response.output` is still empty.
  The two hooks race (confirmed against live Codex traffic), so each pending `litellm_call_id` gets an `asyncio.Event` the logging hook waits on briefly (`_RECOVERY_WAIT_TIMEOUT_S`) before giving up.
