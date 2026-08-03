---
name: min
description: >
  Curated context compaction - classifies session state into active work worth keeping vs. resolved noise worth discarding, then compacts accordingly.
  CLI-agnostic (Claude Code or Codex); use when context is large but the session should continue, not restart.
  Never triggers proactively - invoked explicitly by typing /min.
  v1.1.1
---

## 1. Classify the session before compacting

Scan the conversation and sort everything into two buckets.
Do not skip this step or hand /compact a vague instruction - the whole point of /min over bare /compact is this explicit split.

KEEP:
- Open tasks and unfinished work items, with their current status
- Open bugs: symptom, where it was last traced to, what's been ruled out
- Decisions made this session that aren't yet written to AGENTS.md/specs (e.g. "we chose approach X because Y") - these would be lost forever if dropped, since they don't exist anywhere else
- Facts about the codebase discovered this session that contradict or extend what AGENTS.md/skills already say
- Any explicit user instruction given this session that changes default behavior for the rest of it

DROP:
- Resolved side-quests and dead-end explorations (approaches tried and abandoned, once the reason for abandoning is captured in one line)
- Verbose tool output already synthesized into a conclusion (full file dumps, long grep/test output, full stack traces already summarized)
- Errors that were hit and fixed, once the fix is known - keep only "X failed because Y, fixed by Z" as one line, not the raw traceback
- One-off questions that were fully answered and have no bearing on what's left to do

## 2. Print the snapshot

Print the classified snapshot directly in the response as a blockquote, not to a file - so it's visually unambiguous what the user would be copying:

> Session state - <ISO timestamp>
> Open tasks: ...
> Open bugs: ...
> Decisions not yet persisted to AGENTS.md/specs: ...

## 3. Hand off compaction

Build the instruction string from that same classification.
No tool call can trigger `/compact` from inside a turn on either CLI - it's a command the user types, not something invocable via a tool.
Do NOT attempt to compact yourself.
Print the ready-to-run line as its own blockquote, a single clean copy target:

> /compact Keep only: (1) open tasks and bugs with current status, (2) decisions made this session not yet in AGENTS.md/specs, (3) codebase facts discovered this session that update or contradict existing docs. Drop: resolved side-quests, raw tool/test output already summarized, fixed errors (one-line cause+fix only), fully-answered one-off questions.

## 4. Report back

Stop after printing both blockquotes - the user copies the `/compact` line and runs it themselves.
No file is written; nothing to report beyond the snapshot already shown.
