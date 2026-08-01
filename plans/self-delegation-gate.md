# Enforce delegation of read-only research (Bash/Grep/Glob) to subagents

## Context

Right now delegation to `script-ops`, `code-locator`, and `Explore` for read-only research (git log/diff/show, `ls`, `find`, `grep`, native Grep/Glob) is purely advisory.
The only mechanism is one bullet in `AGENTS.md` ("Check for an owning agent before inline Bash/Read/Grep") plus prose in the agents' own `description` fields.
There is no mechanical enforcement.
Worse, `.claude/settings.local.json` pre-allows many of these commands (`git status*`, `git diff*`, `git log*`, `ls*`, `find *`, `grep *`) with zero friction, so nothing stops the main agent from just running them inline, as happened this session.

The repo already has a proven pattern for turning an advisory rule into a mechanical one: the md-format `PreToolUse` gate (`hooks/harness_audit/md_format_skill_gate.py` + `hooks/harness_audit/md_format_bash_gate.py`).
It hard-`deny`s a tool call until a precondition is met, checked via the session transcript, including subagent transcript files after an earlier false-positive where subagent reads weren't visible to the gate.
We reuse that same mechanism here, adapted to "always delegate" rather than "delegate once, then proceed."

Goal: make the main agent automatically dispatch `script-ops` / `code-locator` for open-ended read-only research instead of running Bash/Grep/Glob itself, and work from their compressed summaries - via a hook, not just stronger prose.

## Design

New `PreToolUse` hook: `hooks/harness_audit/self_delegation_gate.py`, modeled directly on `md_format_skill_gate.py`'s structure (stdin JSON payload, `hookSpecificOutput.permissionDecision` response).

**Scope guard (critical):** the gate must only fire for tool calls made by the *main* agent, never by a subagent - script-ops itself needs Bash, code-locator/Explore need Grep/Glob.
Distinguish this the same way `md_format_skill_gate.py` already does when scanning for prior skill reads: subagent tool calls land in a separate transcript file under `<session>/subagents/agent-*.jsonl`, while the main agent's calls land in the top-level transcript.
The hook receives `transcript_path` in its input payload - skip (allow) entirely if that path is a subagent transcript.

**Grep / Glob matcher:** deny unconditionally when invoked from the main transcript.
Message points at `code-locator` for a small/known search, or `Explore`/`script-ops` for broader discovery, matching `code-locator.md`'s own line: "Do not run Grep or Glob directly in the main conversation."

**Bash matcher:** deny only when the command matches an investigation-shaped denylist, not all Bash.
Denylist (regex, mirroring `script-ops.md`'s stated scope of read-only git log/diff/show/blame + ls/find/grep/read-only docker):

- `git (log|diff|show|blame)\b`
- `\bfind\b`, `\bgrep\b`, `\brg\b`
- `\bls\b.*-R` (recursive listing only - plain `ls` stays allowed)
- `\bcat\b` with 2+ file args (multi-file dump - a single `cat file` stays allowed)
- read-only `docker logs`/`docker ps`/`docker inspect` with broad output

Exempt (always allow) even though they'd otherwise match, because they're mandated safety checks the main agent must run itself per its own operating instructions, not open-ended research:

- bare `git status` / `git status --short`
- `git branch --show-current`
- single-file `cat`, plain `ls` (no `-R`)

If the command doesn't match the denylist at all (builds, tests, `make`, `docker compose up`, `pytest`, etc.), allow.
This gate is scoped to research/investigation only, not general Bash use.

Denial message (Bash case) points at `script-ops`: "read-only repo investigation belongs to script-ops - hand it a goal, not a command."

## Files to change

1. **New:** `hooks/harness_audit/self_delegation_gate.py` - the hook itself, following `md_format_skill_gate.py`'s stdin/stdout contract and the transcript-path subagent-exemption pattern from the same file.
2. **`.claude/settings.json`** - register the new script:
   - add a `PreToolUse` entry with `"matcher": "Grep|Glob"`
   - append the script to the existing `"matcher": "Bash"` hooks array (alongside `guard_destructive.py` and `md_format_bash_gate.py`)
3. **`AGENTS.md`** (~line 132) - update the existing bullet to note this is now hook-enforced, not just a convention, so the text doesn't drift from what the hook actually does (avoids the "duplicated rule" anti-pattern harness-expert already flags).
4. **`.claude/agents/harness-expert.md`** - add a short subsection documenting the new gate, mirroring how it already documents the md-format gate, since harness-expert owns hook/rule documentation and will need to reason about this gate later.
   Bump its version marker per its own convention.

Note: `hooks/` is outside harness-expert's write scope (`.claude/`, `AGENTS.md`, `agent_docs/*.md` only), so the hook script itself gets written directly (or via `script-ops` for the mechanical write), while harness-expert handles the `.claude/settings.json` wiring, the `AGENTS.md` bullet update, and its own doc subsection.

## Verification

- Directly call `Grep`/`Glob` in the main conversation after the change - expect a `deny` with the redirect message.
- Run `find`, `grep -r`, `git log`, `git diff` directly in Bash - expect `deny`.
- Run bare `git status` directly - expect `allow` (safety exemption preserved).
- Dispatch a `script-ops` agent that internally runs `grep`/`find`/`git log` - expect no denial, confirming the subagent-transcript exemption works, the same mechanism already validated by `md_format_skill_gate.py`.
- Re-run a realistic research task (e.g. "find where X is defined") end-to-end and confirm the main agent now spawns `code-locator`/`script-ops` instead of reaching for Bash/Grep/Glob itself.
