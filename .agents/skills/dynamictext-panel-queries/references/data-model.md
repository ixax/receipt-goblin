Data-model facts specific to this panel's schema - a lookup reference, open when a query needs one of these fields/functions.

- `agent_events.turn_id` is always hardcoded to `0` at ingest, never actually computed (see `_event_row`/`_usage_row`/`_message_row` in `services/_common/src/ingest_parsing.py`).
  Never use it for ordering; use `timestamp` instead.
- `agent_events.agent_name`/`agent_version` are blank on a spawned subagent's own rows whenever ingestion raced ahead of the orchestrator's `Agent` tool_use/tool_result (best-effort lookup, see `_agent_name_and_version_for_invocation`'s docstring).
  Don't trust `agent_events.agent_name` alone for "which agents ran this session" - union it with `agent_invocations.subagent_type` (strip the `_vX.Y.Z` suffix via `splitByChar('_', subagent_type)[1]`), filtering both sources for non-empty values before `arrayStringConcat`.
- `agent_invocations` has no column linking a spawn back to the specific parent tool_use call that triggered it.
  Matching an `Agent` tool_use row to its `agent_invocations` row is only possible via an ASOF JOIN on `session_id` + nearest `spawned_at >= timestamp`, a heuristic that breaks down if multiple agents are spawned in the same message/turn.
  Document this limitation in the panel description; don't present it as exact.
- Tool call arguments live at `JSONExtractString(raw_payload, 'response','choices',1,'message','tool_calls',1,'function','arguments')`, returned as a JSON-encoded string, not a parsed object.
  To pull a specific key (`file_path`, `command`, `url`, `query`, `task_id`) cleanly, with real newlines/quotes instead of literal `\n`/`\"`, call `JSONExtractString` a second time on that string: `JSONExtractString(<args_json_string>, 'file_path')`.
  Preference order used so far: `file_path` (Read/Write/Edit) -> `command` (Bash) -> `url` (WebFetch) -> `query` (WebSearch/web_search) -> `task_id` (TaskStop, shown as `task_id: <id>`) -> raw JSON substring as a last resort.
  Normalize tool name display too (`web_search` -> `WebSearch`), since the stored value isn't always the display-friendly one.
- `agent_messages.prompt_text` (`_last_user_text` in `services/_common/src/ingest_parsing.py`) is the last human-role turn verbatim, but that doesn't mean it's literally what a human typed.
  Claude Code prepends/injects boilerplate under the same `role: user` message:
  - `<system-reminder>...</system-reminder>` prefixed before real text - strip via `replaceRegexpOne(text, '(?s)^<system-reminder>.*?</system-reminder>\s*', '')`.
  - `<command-name>/x</command-name> ... <command-args>...</command-args>` for slash commands - `extract(text, '<command-args>(?s)(.*?)</command-args>')` gives the real typed args; reconstruct as `/command args`.
  - `[SYSTEM NOTIFICATION - NOT USER INPUT]` - a stop-hook background check, not something the user said.
  - `[SUGGESTION MODE: ...]` - an internal autosuggest prompt.
  - `<transcript>{...}` - a `/goal` judge call passing the whole conversation as JSON.
  - `<session>...</session>` - the conversation-title-generation call.
  - `[Request interrupted by user]` prefix - real user text follows, just strip the tag.

  None of this is stripped at ingest by design (see that function's own docstring).
  The panel classifies and labels these itself (`○ [background]`/`[suggestion-mode prompt]`/`[goal-check judge call]`/`[title-gen call]` instead of `●`, a real user turn) rather than presenting harness noise as if the user typed it.
  This is a best-effort prefix-match list, not exhaustive - say so in the panel description.
- Two distinct failure signals; do not conflate them.
  `status = 'failure'` means this call's own LLM request failed (extract a reason via `JSONExtractString(JSONExtractString(raw_payload, 'error_information', 'error_message'), 'error', 'type')`, e.g. `rate_limit_error`).
  `failed_tool_name`/`failed_tool_error` non-empty on an otherwise-successful row means this call is reacting to a different tool call that failed one step earlier (show as an indented note above the row, not as this row's own status).
- The very first row(s) of many sessions are an invisible pre-conversation artifact (a silently-retried rate-limited call, a warm-up ping) with empty `prompt_text` and no trace in the actual CLI transcript.
  Compute `min(ts) WHERE is_real` (the first genuine, non-harness prompt) per session and drop everything before it, or a confusing orphan `FAIL` row shows up that the user never saw.
