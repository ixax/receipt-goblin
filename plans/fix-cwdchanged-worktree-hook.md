# Fix: `CwdChanged` hook invisible/broken on `EnterWorktree`

## Context

`TODO.md` item 2 flags that `hooks/report_git_branch.py`, registered for both
`SessionStart` and `CwdChanged` in `.claude/settings.json`, is supposed to
report a session's git branch/repo to ClickHouse's `session_git_branch` table
whenever the working directory changes (e.g. via `EnterWorktree`).
In
practice, all 17 sessions captured since the hook went live
(~2026-07-25 19:20) show `git_branch='main'` — none show a worktree branch —
even though a manually piped payload with an explicit worktree `cwd` proved
the endpoint/ingest/table pipeline itself works correctly.
So the bug is
specifically in what `CwdChanged` sends (or whether/how it's invoked) on
`EnterWorktree`, not in the server-side code.
The hook currently has two
silent failure paths that could each explain this with zero visible signal:
a `cwd = payload.get("cwd") or os.getcwd()` fallback that would resolve to
the wrong directory if `CwdChanged`'s payload omits `cwd`, and a bare
`if not session_id or not git_branch: return` early-out that logs nothing at
all.
This is a diagnose-then-fix task: the exact code fix can't be written
until we've observed what `CwdChanged` actually sends, so the plan front-loads
instrumentation and a real `EnterWorktree` test before prescribing the fix.

## Step 1 — Add gated diagnostic logging to `hooks/report_git_branch.py`

Add a small, env-var-gated debug helper near the top of the file, called right
after `git_repo = _current_repo(cwd)` (i.e. after all four values — payload,
cwd, git_branch, git_repo — are known, but *before* the
`if not session_id or not git_branch: return` early-out, so the early-out path
is captured too):

```python
DEBUG_LOG = os.environ.get("REPORT_GIT_BRANCH_DEBUG_LOG")

def _debug_log(payload, cwd, session_id, git_branch, git_repo):
    if not DEBUG_LOG:
        return
    try:
        with open(DEBUG_LOG, "a") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "raw_stdin_payload": payload,
                "resolved_cwd": cwd,
                "session_id": session_id,
                "git_branch": git_branch,
                "git_repo": git_repo,
            }) + "\n")
    except Exception:
        pass
```

- Gate: `REPORT_GIT_BRANCH_DEBUG_LOG=<path>` env var. Unset in normal operation
  → zero behavior change, zero I/O. Matches the repo's existing style of
  small `os.environ.get(...)` reads at module scope.
- Path: no existing debug-file pattern to reuse in this repo. Use a fixed
  out-of-repo path, e.g. `/tmp/report_git_branch_debug.jsonl`, so it survives
  worktree removal and is never git-tracked.
  Append-only JSONL, one line per
  hook invocation — easy to `tail -f` live during the Step 2 test.
- Set `REPORT_GIT_BRANCH_DEBUG_LOG` for the diagnostic session (either in
  `.claude/settings.json`'s top-level `env` block, adding one if it doesn't
  exist, or exported in the shell before starting the session).
- Temporary in spirit: after root cause is confirmed in Step 2/3, either
  remove this block entirely or explicitly decide to keep it as a permanent,
  off-by-default debugging escape hatch — call out that decision in the
  commit message rather than leaving it silently in place.

## Step 2 — Trigger a real `EnterWorktree` and inspect the captured payload

Requires a live tool invocation — run after this plan is approved and
implementation begins, not derivable from static analysis:

1. With `REPORT_GIT_BRANCH_DEBUG_LOG` set, invoke `EnterWorktree` for real in
   a live Claude Code session.
2. Inspect the debug log for the new line from the `CwdChanged` event:
   - **No new line at all** → `CwdChanged` isn't being emitted by
     `EnterWorktree`, or the hook process isn't spawned.
     Worth confirming via
     any other cwd transition Claude Code exposes, to rule out a
     registration/timeout issue rather than an `EnterWorktree`-specific one.
   - **A new line appears** → read `raw_stdin_payload`, `resolved_cwd`,
     `session_id`, `git_branch`, `git_repo` directly — this is the ground
     truth for what `CwdChanged` actually sent and what the hook derived.
3. Keep the log line(s) from this run as evidence for Step 3.

## Step 3 — Root-cause branches (pick the one Step 2's evidence supports)

Do not implement a fix until the captured payload confirms which of these (or
a third, unanticipated cause) is actually happening.

**Branch A — payload has missing/empty `cwd`.**
Symptom: `raw_stdin_payload` has no `cwd` key (or empty), `resolved_cwd` ==
the hook subprocess's own launch cwd (likely the main checkout).
Fix shape: stop silently falling back to `os.getcwd()` for `CwdChanged`
specifically — that fallback only makes sense for `SessionStart`.
If
`CwdChanged` carries the info under a different key, fix the key name; if it
genuinely carries no cwd info at all, this may be a Claude Code CLI-level
limitation rather than something fixable in `hooks/report_git_branch.py`, in
which case the outcome is "known limitation" rather than a code change.

**Branch B — missing `session_id` or git commands failing silently.**
Symptom: `cwd`/`resolved_cwd` looks like the correct worktree path, but
`session_id` is empty, or `git_branch`/`git_repo` came back empty despite a
plausible cwd (worktree `.git` file/gitdir linkage not resolved yet, or a
genuine timing race right after `EnterWorktree` creates the worktree).
Fix shape depends on which call fails: a timing issue suggests a short retry;
a `session_id` propagation issue suggests checking the actual key name/shape
in the captured payload rather than assuming it's simply absent.

**Also worth confirming regardless of branch:** even once `git_branch` is
fixed, `git_repo` stays collapsed to the main repo's origin-derived name
across worktrees of the same repo (they share the same origin remote).
Decide during the fix whether to also capture something worktree-specific
(e.g. `git rev-parse --show-toplevel`) so worktree sessions are
distinguishable from each other and from the main checkout.

## Step 4 — Permanent stderr logging on the early-out (independent of root cause)

Regardless of which branch applies, add stderr logging to the
`if not session_id or not git_branch: return` early-out, matching the
existing POST-failure convention:

```python
if not session_id or not git_branch:
    print(f"[report_git_branch] skipping: session_id={session_id!r} git_branch={git_branch!r} cwd={cwd!r}", file=sys.stderr)
    return
```

This is a permanent (non-gated) change — it only fires on the already-rare
early-out path, so it's not noise, and it makes this whole class of bug
non-invisible going forward regardless of today's specific root cause.

## Step 5 — End-to-end verification of the eventual fix

1. Trigger `EnterWorktree` again after the fix lands.
2. Query ClickHouse for the session's row using `argMax`/`FINAL`, since
   `session_git_branch` is a `ReplacingMergeTree(captured_at)` and a raw
   `SELECT` may show a stale pre-merge value:
   ```sql
   SELECT session_id,
          argMax(git_branch, captured_at) AS git_branch,
          argMax(git_repo, captured_at)   AS git_repo,
          argMax(issue_id, captured_at)   AS issue_id
   FROM session_git_branch
   WHERE session_id = '<the worktree session's id>'
   GROUP BY session_id
   ```
3. Confirm `git_branch` matches the worktree's actual branch (not `main`),
   and reassess whether the `git_repo`/worktree-identity addition from Step 3
   is also needed.
4. Remove or keep the Step 1 debug logging per the fix-time decision; if kept,
   note that explicitly in the commit message.

## Step 6 — Follow-up (flagged, not required now)

`AGENTS.md`'s description of `hooks/report_git_branch.py` currently describes
`CwdChanged` as if it reliably reports on cwd change, with no documented
caveat about worktree behavior.
Once root cause is confirmed, it likely needs
a one-line correction (either describing the limitation or confirming it's
fixed) — content depends entirely on Steps 2-3's findings, so this is a
flagged follow-up rather than specified here.

### Critical files
- `hooks/report_git_branch.py` — the hook itself, Steps 1 and 4's edits.
- `.claude/settings.json` — hook registration; may need an `env` block for
  the Step 1 debug var.
- `services/_common/src/ingest_db.py` — `ingest_git_branch`, confirmed
  working already, referenced for context only.
- `AGENTS.md` — Step 6 follow-up.
- `TODO.md` — item 2, to be resolved/updated once fixed.
