# Git hooks management: `.githooks/` + `core.hooksPath`

## Context

The repo has no versioned *git* hooks yet (`.git/hooks/` only contains the
stock `.sample` files).
There's a separate, unrelated hooks system already
in place — `.claude/settings.json` wires Claude Code lifecycle hooks
(`hooks/report_git_branch.py`, `hooks/guard_destructive.py`,
`hooks/harness_audit/*`) — but those only fire inside Claude Code sessions,
not for every `git checkout`/`pull`/`switch` a human runs from a terminal.

The ask is to add real git hooks, starting with: after a checkout or branch
switch, check whether `uv` is installed and suggest installing it if not.
Hooks should be bash, optionally shelling out to Python via `uv`.
The
second requirement is that hooks stay in sync and "hot" immediately after
checkout/pull/branch-switch — no stale or missing hook problem.

## Approach: `core.hooksPath`, not a framework

Confirmed with the user: use git's native `core.hooksPath` config pointing
at a tracked `.githooks/` directory, not lefthook/pre-commit/husky.
No new
dependency, and it directly solves the "stay in sync" requirement almost
for free — because hook *content* lives in the tracked directory itself.
The moment `git checkout`/`pull` updates `.githooks/post-checkout` (or adds
a brand-new hook file), that's what git will invoke next time, with no
separate copy/link/reinstall step.
This is the key advantage over
`.git/hooks/`-copying approaches (which do need a re-sync step on every
change).

The one gap `core.hooksPath` can't close on its own: a fresh clone starts
with `core.hooksPath` unset (it's local, untracked `.git/config` state) —
git cannot bootstrap this for itself.
Confirmed with the user: fold the
one-time `git config core.hooksPath .githooks` into the existing `make
init` first-run target, as a new standalone-callable `make
git-hooks-install` target that `init` also invokes.
Anyone re-cloning
already runs `make init` per README "Getting started", so this needs no
new step to remember, and it's re-runnable by hand if `core.hooksPath`
ever gets cleared or exec bits get lost on a non-POSIX filesystem.

## Files

**`.githooks/lib/check-uv.sh`** — shared function, sourced by hook entry
points.
Pure POSIX `command -v uv` check + one-line suggestion
(`curl -LsSf https://astral.sh/uv/install.sh | sh`).
Deliberately has zero
dependency on `uv`/`python3` itself — see Challenge 1 below.
Never exits
non-zero.

**`.githooks/post-checkout`** — entry point, `chmod +x`. Signature is
`post-checkout <prev-HEAD> <new-HEAD> <branch-flag>`; guard on
`branch-flag == 1` so it only fires on real branch checkouts/switches, not
every internal file-level checkout (stash, IDE operations, etc. also
trigger `post-checkout`).
Sources and calls `check-uv.sh`.
Always `exit 0`.

**`.githooks/post-merge`** — entry point, `chmod +x`, covers the "after a
pull" case (`git pull` = fetch + merge, which fires `post-merge`, not
`post-checkout`).
Same `check-uv.sh` call.
Always `exit 0`.

**`scripts/install-git-hooks.sh`** — `git config core.hooksPath .githooks`
+ `chmod +x .githooks/* .githooks/lib/*` (belt-and-suspenders for the
tracked executable bit) + a one-line confirmation echo.
Idempotent, safe
to re-run.

**`Makefile`** — new `git-hooks-install` target running the script above;
`init` target gains it as a step/prerequisite so first-run clone setup
covers it automatically, matching the pattern of the other one-time
`make init` provisioning already there (`scripts/resolve_image_version.py`
neighbors this style).

**`README.md`** — one line in "Getting started" noting hooks are installed
by `make init` (or standalone via `make git-hooks-install`), and one row
in the commands table.

## Challenges surfaced (per the user's request to propose/challenge)

1. **The uv-check hook can't depend on uv.** The stated idea was "write
   hooks in bash, optionally shell to python3 via `uv`" — but the very
   first hook's job is to *detect whether uv exists*, so that detection
   logic must stay pure bash/POSIX, never `uv run ...`.
   The
   python3-via-`uv` pattern is fine for *future* hooks that need real
   logic (JSON parsing, etc.), but only after they've confirmed `uv` is
   present (or they fall back to plain `python3` / skip gracefully) — this
   is documented once in `check-uv.sh`'s header comment so later hook
   authors reuse the pattern instead of reinventing it.
2. **Hooks must never block or fail the git operation.** A missing
   optional tool like `uv` is a suggestion, not an error — every entry
   point exits 0 unconditionally, even if the notice-printing itself
   fails.
3. **Don't fire on every internal checkout.** `post-checkout` also fires
   for single-file checkouts (e.g. `git stash pop`, some IDE actions), not
   just branch switches — the `branch-flag == 1` guard keeps the notice
   from becoming noise.
4. **No extra "sync" mechanism is needed going forward.** Because
   `core.hooksPath` reads hook files directly from the tracked directory
   at invocation time, adding/editing a hook later is just a normal commit
   — no reinstall step, except the one edge case where a non-POSIX
   filesystem or `core.fileMode=false` drops the tracked executable bit,
   which `make git-hooks-install`'s `chmod +x` step fixes on rerun.

## Part 2: adopt `uv` for the repo's own Python tooling (root-level)

### Context

Today there's no pinned interpreter for the repo's own dev tooling: the
README has developers hand-build `.venv` (`.venv/bin/pip install -r
services/webhook/requirements-dev.txt`), and `make test` runs
`.venv/bin/python -m pytest ...` against whatever landed in that venv.

The system `python3` on this machine is macOS's stock **3.9.6**, while
`services/webhook/Dockerfile` pins `python:3.12-slim` — so a hand-built
`.venv` can silently run tests against a materially older interpreter than
production. `todo/optimize-harness/methodics.md` already documents `uv run
pytest`/`uv run ruff check --fix` as the intended dev commands, so this
closes an existing gap rather than introducing a new direction.

This is additive root-level tooling, not a per-service migration: each
service's own `requirements.txt` (used by its `Dockerfile`) is untouched.

### Files

**`.python-version`** (repo root) — pins `3.12` (matching
`services/webhook/Dockerfile`'s `python:3.12-slim`). `uv` reads this
automatically and downloads/uses that exact interpreter for `uv
run`/`uv sync`, regardless of whatever `python3` resolves to on `PATH`.

**`pyproject.toml`** (repo root) — minimal, dev-tooling only:
`requires-python = ">=3.12"`, and a dependency list matching
`services/webhook/requirements-dev.txt` (which already just layers
`pytest` over the service's own `requirements.txt`) plus `ruff` per
`methodics.md`. `uv run`/`uv sync` build and maintain `.venv` from this
automatically — no more hand-run `pip install` step.

**`Makefile`** — `test:` becomes `uv run pytest -c
services/webhook/pytest.ini services/webhook/tests` (same target,
underlying `.venv` now uv-managed instead of hand-built); `test-harness-audit`
stays on plain `python3` (stdlib-only `unittest`, no deps, no version
sensitivity worth pinning).
Optionally add a `lint:` target
(`uv run ruff check --fix`) since `methodics.md` already names it as the
intended command.

**`README.md`** — replace the `.venv/bin/pip install -r
requirements-dev.txt` step with "run any `make` target that needs it (or
`uv sync`) — `uv` builds `.venv` automatically from the pinned
`.python-version`/`pyproject.toml`."

### How this feeds back into Part 1

Once this lands, any *future* git hook needing real logic (not just the
bash uv-checker) can call `uv run <script>.py`, running against this same
pinned interpreter/deps — a first-class pattern instead of a one-off
fallback.
It does not change the Part 1 conclusion that the uv-*detection*
check itself must stay pure bash: it can't presuppose `uv` while checking
whether `uv` is installed.

### Dockerfile audit — one Python version everywhere

Checked all four Python-based service images
(`services/webhook`, `services/loadtest-fixtures`, `services/mcp-dev`,
`services/mcp-stats`) — every `Dockerfile` already pins `FROM
python:3.12-slim`, so there's no drift to fix today, but there was no
single declared source of truth tying that value to `.python-version`.
Fix that as part of this change:

- `.python-version` (`3.12`, added above) becomes the one source of truth
  for "what Python version this repo targets" — both for `uv`
  (local/dev scripts, tests) and for container images.
- Docker can't read an external file at `FROM` time, so each
  `Dockerfile`'s `FROM python:3.12-slim` stays a hardcoded literal (per
  the user's direction) rather than templated — but it must always match
  `.python-version`'s value.
  Bumping the version means updating
  `.python-version` *and* grepping/fixing every `FROM python:` line in
  the same commit: `grep -rn '^FROM python:' services/*/Dockerfile`.
- Document this pairing once (see AGENTS.md change below) so it's a known
  convention, not tribal knowledge — the next person/agent bumping Python
  knows both places exist and must move together.

### `AGENTS.md` — new "Python version policy" note

`AGENTS.md` currently has no section stating which Python version is
correct or where it's pinned (only `VERSIONS.yml`'s image-tag convention
for non-Python services is documented, near the "Image tags" line).
Add a short paragraph, near that "Image tags" line, stating:

- The repo targets one Python version, pinned in root `.python-version`.
- Every Python-based `Dockerfile`'s `FROM python:X.Y-slim` must match it
  (hardcoded, not templated — verify with `grep -rn '^FROM python:'
  services/*/Dockerfile` after any bump).
- Local/dev scripts and tests run via `uv run` (or `uv sync`), which reads
  `.python-version` automatically — this is what keeps Claude Code,
  Codex CLI, and any human contributor invoking the same interpreter,
  rather than each falling back to whatever `python3` happens to resolve
  to on their machine (e.g. this repo's own dev machine has a stock
  macOS `python3` at 3.9.6, well behind the pinned 3.12).

### Explicit scope limit

Don't route the existing stdlib-only scripts (`scripts/resolve_image_version.py`,
`hooks/report_git_branch.py`, `hooks/guard_destructive.py`, etc.) through
`uv run` — they have zero third-party dependencies, so there's no
correctness benefit, only ~tens-of-ms added startup overhead per
invocation for no gain.
Reserve `uv run` for things that actually consume
the `pyproject.toml` environment (pytest, ruff, future hook scripts with
real deps).

## Verification

**Part 1 (git hooks):**
- Fresh-clone simulation: clone the repo to a scratch dir, run `make
  init`, confirm `git config --get core.hooksPath` prints `.githooks` and
  `.githooks/post-checkout`/`post-merge` are executable.
- `git checkout -b tmp-test && git checkout -` — confirm the uv notice
  prints (temporarily `PATH`-hide `uv` to test the "missing" branch, then
  restore) and that file-level checkouts (e.g. `git checkout -- <file>`)
  produce no output.
- `git pull` (or a local `git merge`) on a branch with new commits —
  confirm `post-merge` fires the same check.
- Edit `.githooks/post-checkout` on a branch, switch away and back —
  confirm the *new* content runs immediately with no reinstall step,
  demonstrating the "stays in sync" property.

**Part 2 (uv adoption):**
- `rm -rf .venv && uv run pytest -c services/webhook/pytest.ini
  services/webhook/tests` from a clean checkout — confirm `uv` builds
  `.venv` from `.python-version`/`pyproject.toml` and tests pass, with no
  manual `pip install` step.
- `uv run python3 --version` — confirm it reports 3.12, not the system
  3.9.6.
- `make test` — confirm the Makefile target now goes through `uv run` and
  still passes.
