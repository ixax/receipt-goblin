# Replay-based regression testing

## Context

`ingest_raw` already stores the full, untouched `StandardLoggingPayload` per call (messages included), `ZSTD(19)`-compressed - the only source of truth for "the full payload behind row X," read today only by `services/reparse/src/reparse.py`.
That makes production traffic usable as a regression dataset without building a synthetic one from scratch, the same idea behind Langfuse's datasets/evals feature, but for free as a side effect of data already captured.

## Design

### v1 (achievable now): replay the same messages against a different model

1. Select a historical `litellm_call_id` (or a sample across a session) from `ingest_raw`.
2. Extract the original `messages` array from the stored payload, unchanged.
3. Re-issue the same messages through the LiteLLM proxy against a different target model.
4. Diff the stored `response_text` (from `agent_messages`) against the new response.

This answers a narrower but immediately useful question: "would a different model answer this the same way," useful for model-migration decisions.
New script under `services/reparse/` (sibling to `reparse.py`, e.g. `replay.py`), read-only against `ingest_raw`.
Results go to a new lightweight table (e.g. `replay_runs`) or a scratch location for diffing - not a live-ingestion path, this never touches `agent_events`/`agent_usage`.

### v2 (open design question, not v1): replay against a different agent/skill version

The hard part: a skill/agent's system prompt is injected fresh at call time by the harness, not stored verbatim as a static string the replay can just resend.
Replaying "the same call under the new skill version" isn't just resending stored messages - it requires reconstructing what the harness would inject today versus what it injected historically.
Since prompt versioning here is git-based (skill/agent `.md` files, `<version>` markers - see the earlier discussion comparing this to Langfuse's prompt-management UI), the reconstruction likely means diffing the relevant `.claude/skills/*.md`/`.claude/agents/*.md` content between the two committed versions rather than anything storable/replayable from `ingest_raw` alone.
This needs its own design pass before scoping - flagged here as a known follow-on, not committed to.

### Safety

Replaying real historical prompts means real spend on the LiteLLM proxy.
Needs an explicit opt-in and a sampling cap per invocation - same cost-consciousness as `quality-signal-loop.md` Tier C's judge-call budget concern, for the same underlying reason (uncapped automated LLM calls against real traffic).

## Rollout

v1 first: same-messages-different-model replay, cheap to build, immediately useful for model-migration decisions (e.g. would `claude-opus-5` answer this differently than `claude-sonnet-5`).
v2: blocked on a separate design session for how to reconstruct an equivalent call under a different skill/agent version - not started until that design question is resolved.
