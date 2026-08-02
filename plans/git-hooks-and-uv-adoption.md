# Git hooks management: `.githooks/` + `core.hooksPath`, and uv/ruff adoption

## Context

The repo has no versioned *git* hooks yet (`.git/hooks/` only contains the stock `.sample` files).
There's a separate, unrelated hooks system already in place - `.claude/settings.json` wires Claude Code lifecycle hooks (`hooks/report_git_branch.py`, `hooks/guard_destructive.py`, `hooks/harness_audit/*`) - but those only fire inside Claude Code sessions, not for every `git checkout`/`pull`/`switch` a human runs from a terminal.

The ask is to add real git hooks, starting with: after a checkout or branch switch, check whether `uv` is installed and suggest installing it if not.
Hooks should be bash, optionally shelling out to Python via `uv`.
The second requirement is that hooks stay in sync and "hot" immediately after checkout/pull/branch-switch - no stale or missing hook problem.

## Approach: `core.hooksPath`, not a framework

Confirmed with the user: use git's native `core.hooksPath` config pointing at a tracked `.githooks/` directory, not lefthook/pre-commit/husky.
No new dependency, and it directly solves the "stay in sync" requirement almost for free - because hook *content* lives in the tracked directory itself.
The moment `git checkout`/`pull` updates `.githooks/post-checkout` (or adds a brand-new hook file), that's what git will invoke next time, with no separate copy/link/reinstall step.
This is the key advantage over `.git/hooks/`-copying approaches (which do need a re-sync step on every change).

The one gap `core.hooksPath` can't close on its own: a fresh clone starts with `core.hooksPath` unset (it's local, untracked `.git/config` state) - git cannot bootstrap this for itself.
Confirmed with the user: fold the one-time `git config core.hooksPath .githooks` into the existing `make init` first-run target, as a new standalone-callable `make git-hooks-install` target that `init` also invokes.
Anyone re-cloning already runs `make init` per README "Getting started", so this needs no new step to remember, and it's re-runnable by hand if `core.hooksPath` ever gets cleared or exec bits get lost on a non-POSIX filesystem.

## Files (Part 1: git hooks)

**`.githooks/lib/check-uv.sh`** - shared function, sourced by hook entry points.
Pure POSIX `command -v uv` check + one-line suggestion (`curl -LsSf https://astral.sh/uv/install.sh | sh`).
Deliberately has zero dependency on `uv`/`python3` itself - see Challenge 1 below.
Never exits non-zero.

**`.githooks/post-checkout`** - entry point, `chmod +x`.
Signature is `post-checkout <prev-HEAD> <new-HEAD> <branch-flag>`; guard on `branch-flag == 1` so it only fires on real branch checkouts/switches, not every internal file-level checkout (stash, IDE operations, etc. also trigger `post-checkout`).
Sources and calls `check-uv.sh`.
Always `exit 0`.

**`.githooks/post-merge`** - entry point, `chmod +x`, covers the "after a pull" case (`git pull` = fetch + merge, which fires `post-merge`, not `post-checkout`).
Same `check-uv.sh` call.
Always `exit 0`.

**`scripts/install-git-hooks.sh`** - `git config core.hooksPath .githooks` + `chmod +x .githooks/* .githooks/lib/*` (belt-and-suspenders for the tracked executable bit) + a one-line confirmation echo.
Idempotent, safe to re-run.

**`Makefile`** - new `git-hooks-install` target running the script above; `init` target gains it as a step/prerequisite so first-run clone setup covers it automatically, matching the pattern of the other one-time `make init` provisioning already there (`scripts/resolve_image_version.py` neighbors this style).

**`README.md`** - one line in "Getting started" noting hooks are installed by `make init` (or standalone via `make git-hooks-install`), and one row in the commands table.

## Challenges surfaced (per the user's request to propose/challenge)

1. **The uv-check hook can't depend on uv.** The stated idea was "write hooks in bash, optionally shell to python3 via `uv`" - but the very first hook's job is to *detect whether uv exists*, so that detection logic must stay pure bash/POSIX, never `uv run ...`.
   The python3-via-`uv` pattern is fine for *future* hooks that need real logic (JSON parsing, etc.), but only after they've confirmed `uv` is present (or they fall back to plain `python3` / skip gracefully) - this is documented once in `check-uv.sh`'s header comment so later hook authors reuse the pattern instead of reinventing it.
2. **Hooks must never block or fail the git operation.** A missing optional tool like `uv` is a suggestion, not an error - every entry point exits 0 unconditionally, even if the notice-printing itself fails.
3. **Don't fire on every internal checkout.** `post-checkout` also fires for single-file checkouts (e.g. `git stash pop`, some IDE actions), not just branch switches - the `branch-flag == 1` guard keeps the notice from becoming noise.
4. **No extra "sync" mechanism is needed going forward.** Because `core.hooksPath` reads hook files directly from the tracked directory at invocation time, adding/editing a hook later is just a normal commit - no reinstall step, except the one edge case where a non-POSIX filesystem or `core.fileMode=false` drops the tracked executable bit, which `make git-hooks-install`'s `chmod +x` step fixes on rerun.

## Part 2: adopt `uv` for the repo's own Python tooling (root-level)

### Context

Today there's no pinned interpreter for the repo's own dev tooling: the README has developers hand-build `.venv` (`.venv/bin/pip install -r requirements-dev.txt`), and `make test` runs `.venv/bin/python -m pytest ...` against whatever landed in that venv.

The system `python3` on this machine is macOS's stock **3.9.6**, while `services/webhook/Dockerfile` pins `python:3.12-slim` - so a hand-built `.venv` can silently run tests against a materially older interpreter than production.

This is additive root-level tooling, not a per-service migration: each service's own `requirements.txt` (used by its `Dockerfile`) is untouched.

Interim step already applied ahead of this plan: `requirements-dev.txt` was relocated from `services/webhook/` to the repo root (still plain pip-based, `Makefile`/`README.md`/`webhook-test-runner.md` updated to match) since it already bootstraps the shared venv for every service's tests, not just webhook's.
Part 2 below still fully supersedes it with `pyproject.toml`/`uv` - the relocation only fixes where it lives in the meantime.

### Files

**`.python-version`** (repo root) - pins `3.12` (matching `services/webhook/Dockerfile`'s `python:3.12-slim`).
`uv` reads this automatically and downloads/uses that exact interpreter for `uv run`/`uv sync`, regardless of whatever `python3` resolves to on `PATH`.

**`pyproject.toml`** (repo root) - minimal, dev-tooling only: `requires-python = ">=3.12"`, and a dependency list matching root `requirements-dev.txt` (which already just layers `pytest` over `services/webhook/requirements.txt`) plus `ruff`.
`uv run`/`uv sync` build and maintain `.venv` from this automatically - no more hand-run `pip install` step.

```toml
[project]
name = "receipt-goblin-dev"
requires-python = ">=3.12"
dependencies = [
    # Mirrors services/webhook/requirements.txt - keep in sync by hand,
    # same duplication the old requirements-dev.txt already had.
    "fastapi==0.115.0",
    "uvicorn[standard]==0.30.6",
    "clickhouse-connect==0.8.3",
    "redis==5.0.8",
    "PyYAML==6.0.2",
    "prometheus-fastapi-instrumentator==7.0.0",
    "orjson==3.10.7",
    "pytest==8.4.2",
    "ruff==0.16.1",
]

[tool.ruff]
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I"]
ignore = ["E501"]
```

**Delete root `requirements-dev.txt`** - fully superseded by the root `pyproject.toml` above.
Confirmed via repo-wide grep it's referenced only in `Makefile` (a comment), `README.md` (setup step + Make-targets table), and `webhook-test-runner.md`'s fallback-install line - all three get updated in the same change, not left dangling.

**`Makefile`** - `test:` target's 5 pytest invocations (webhook, worker, reparse, loadtest, `_common`) switch from `.venv/bin/python -m pytest -c pytest.ini <dir>` to `uv run pytest -c pytest.ini <dir>`; `test-harness-audit` stays on plain `python3` (stdlib-only `unittest`, no deps, no version sensitivity worth pinning).
New `lint:` target, same `check-env` dependency pattern as `test`:

```makefile
lint: check-env
	uv run ruff check .
```

**`README.md`** - replace the `.venv/bin/pip install -r requirements-dev.txt` step with "run any `make` target that needs it (or `uv sync`) - `uv` builds `.venv` automatically from the pinned `.python-version`/`pyproject.toml`."
Add a short "Linting" mention (near "Running tests") documenting `make lint`, that it's mandatory alongside tests after code changes, and that it's always run via `runner-linter`, never inline.
Update the "Make targets" reference table: `test` row's wording drops the `requirements-dev.txt` mention, add a `lint` row.

### How this feeds back into Part 1

Once this lands, any *future* git hook needing real logic (not just the bash uv-checker) can call `uv run <script>.py`, running against this same pinned interpreter/deps - a first-class pattern instead of a one-off fallback.
It does not change the Part 1 conclusion that the uv-*detection* check itself must stay pure bash: it can't presuppose `uv` while checking whether `uv` is installed.

### Propagate `.python-version` into every Dockerfile automatically

Checked all 8 Python-based service Dockerfiles (`webhook`, `worker`, `reparse`, `migrate`, `loadtest`, `loadtest-fixtures`, `mcp-dev`, `mcp-stats`) - every one pins an identical `FROM python:3.12-slim` literal, one copy per file, with no single source of truth tying that value to `.python-version`.

An earlier pass of this plan proposed leaving each `FROM` line a hardcoded literal and relying on a manual `grep -rn '^FROM python:' services/*/Dockerfile` step to keep them in sync on every bump.
Revised direction: bumping the version should be a single-place edit that reaches every image on its own, so a bump can be test-driven ("change it, `make build`, see what breaks") instead of depending on someone remembering to grep-and-fix 8 files.

Mechanism, reusing this repo's existing single-source-of-truth pattern (`VERSIONS.yml` -> `scripts/resolve_image_version.py` -> `.image-tags.mk` -> exported `_TAG` vars -> docker-compose `image: ...:${TAG}`):

- Each Dockerfile gains a top-of-file `ARG PYTHON_VERSION=3.12` line (Docker requires `ARG` declared before `FROM` for it to be usable inside `FROM`), then `FROM python:${PYTHON_VERSION}-slim` replaces the hardcoded literal.
  The `=3.12` default keeps a bare `docker build` runnable without compose or an explicit `--build-arg` - see `services/webhook/Dockerfile`'s own top comment documenting exactly that standalone invocation.
- `Makefile` adds `PYTHON_VERSION := $(shell cat .python-version)` and exports it, next to the existing `.image-tags.mk` include - so it's in-environment for every `docker compose build`/`up --build` invocation.
- `docker-compose.yml` - each of the 8 services' `build:` block gains `args: - PYTHON_VERSION=${PYTHON_VERSION}`, so compose passes the Makefile-exported value through as the build-arg Docker substitutes into `FROM`.
- Result: bumping the Python version is a one-line edit to `.python-version`, followed by `make build` (or `make up`) - every image rebuilds against the new version in one shot, and whichever service's build (or the `make test`/`make lint` run after) fails tells you exactly what broke, instead of a silently-drifted per-file literal.

Document this mechanism once (see `AGENTS.md` change below) so it's a known convention, not tribal knowledge.

### `AGENTS.md` - new "Python version policy" note

`AGENTS.md` currently has no section stating which Python version is correct or where it's pinned (only `VERSIONS.yml`'s image-tag convention for non-Python services is documented, near the "Image tags" line).
Add a short paragraph, near that "Image tags" line, stating:

- The repo targets one Python version, pinned in root `.python-version`.
- Every Python-based `Dockerfile`'s `FROM python:${PYTHON_VERSION}-slim` reads that value automatically via a `PYTHON_VERSION` build-arg, propagated from `.python-version` through the `Makefile` and `docker-compose.yml`'s `build.args` - bump `.python-version`, then `make build`, and every image rebuilds against the new version in one shot.
- Local/dev scripts and tests run via `uv run` (or `uv sync`), which reads `.python-version` automatically - this is what keeps Claude Code, Codex CLI, and any human contributor invoking the same interpreter, rather than each falling back to whatever `python3` happens to resolve to on their machine (e.g. this repo's own dev machine has a stock macOS `python3` at 3.9.6, well behind the pinned 3.12).

### Explicit scope limit

Don't route the existing stdlib-only scripts (`scripts/resolve_image_version.py`, `hooks/report_git_branch.py`, `hooks/guard_destructive.py`, etc.) through `uv run` - they have zero third-party dependencies, so there's no correctness benefit, only ~tens-of-ms added startup overhead per invocation for no gain.
Reserve `uv run` for things that actually consume the `pyproject.toml` environment (pytest, ruff, future hook scripts with real deps).

## Part 3: ruff integration specifics

### Why now, and why folded into this plan

A follow-up ask was to integrate `ruff` as a mandatory check alongside `make test`, mirroring the existing `webhook-test-runner` subagent pattern (Makefile target + a dedicated proactive subagent that keeps raw tool output out of the main conversation).
Part 2 above already put `ruff` in `pyproject.toml` and sketched a `lint:` target - this section fills in the concrete decisions made when actually sizing and scoping that work, so it doesn't drift from what Part 2 assumed.

### Sizing the existing codebase (read-only `uvx ruff check .` runs)

With zero config, ruff 0.16.1's default rule set (much broader than just `E`/`F` - includes bugbear, pyupgrade, simplify, security, etc.) flags 137 violations repo-wide.
Narrowing to a conservative `select = ["E", "F", "I"]` (errors, pyflakes, import-sort) instead surfaces 559 - dominated by 538 `E501` (line-too-long at the default 88-char width), because this codebase's own comment style is deliberately long-form prose.
Excluding `E501` (a style preference this repo doesn't share, not a correctness issue) leaves 21 violations: 18 auto-fixable `I001` (unsorted imports), 2 `E402`, 1 `F841`.
`ruff format --check .` would reformat 62/138 files - too large an unrelated diff to fold in here, so formatting enforcement is explicitly out of scope for this pass.

### Scope decisions (confirmed with user)

- **Rule set**: `select = ["E", "F", "I"]`, `ignore = ["E501"]` in `[tool.ruff.lint]` (already reflected in Part 2's `pyproject.toml` above).
  Deliberately conservative and explicit so a future `ruff` upgrade can't silently widen what's enforced.
- **Ruff scope**: repo-wide (`ruff check .`), not limited to the 5 services `make test` covers.
  It's a static check with no runtime dependency, so it also covers `hooks/`, `scripts/`, and the services `make test` skips (`mcp-dev`, `mcp-stats`, `migrate`, `loadtest-fixtures`).
- **Formatting** (`ruff format`): not enforced in this pass - flagged as a possible future follow-up, not done here.
- **Agent shape**: two sibling subagents, not one combined one.
  Rename the existing `.claude/agents/webhook-test-runner.md` agent to `runner-test` (new filename to match), add a new sibling agent named `runner-linter`.
  Both dispatched proactively after code edits - main agent calls both, not one wrapping the other.
- **Git hooks (Part 1) are out of scope for this pass** - deferred to a separate future `/plan`, unrelated to ruff.

### One-time cleanup (before the gate goes live)

`uv run ruff check --fix .` to auto-fix the 18 `I001` hits, then hand-fix the 2 `E402` and 1 `F841` findings, so `make lint` starts clean rather than mandatory-from-day-one against pre-existing debt.

### Rename `.claude/agents/webhook-test-runner.md` to the `runner-test` agent

Rename the file and its frontmatter `name:` to `runner-test`.
Update its pytest-invocation line and dependency-install fallback to the `uv run` equivalent (no more manual `pip install -r requirements-dev.txt` - `uv run` handles missing deps itself).
Bump `<version>`.

### New sibling agent: `runner-linter`

Same shape as `runner-test` (`tools: Bash, Read, Skill`, `model: claude-haiku-4-5`):

- Frontmatter description: "MUST BE USED PROACTIVELY, without waiting to be asked, any time `make lint` (ruff) needs to run - after every edit to a `.py` file anywhere in the repo, and whenever the user asks to run/verify lint.
  Never run `make lint`/`ruff check` directly in the main conversation - always delegate here instead, so raw ruff output never fills the main conversation's context.
  Runs on a cheap model."
- Body: run `make lint` (`uv run ruff check .`) from repo root.
  Report contract, mirroring `runner-test`'s token-minimizing shape: if it exits clean, reply with nothing more than the shortest possible confirmation (no "everything looks good!" padding - literally just enough for the caller to know it ran and passed).
  If there are violations, report each as ruff's own terse `path:line:col: CODE message` line - not the extended diff/context box ruff sometimes prints.
  Don't suggest fixes unless asked; don't explain own steps.

### `AGENTS.md` updates (harness-expert owns this file)

- `## Commands`: rename the `webhook-test-runner` mention to `runner-test` in the `make test` line.
  Add a `` `make lint` - repo-wide ruff check; always via `runner-linter`, never inline. `` line.
- `## Agent & skill routing` -> Proactive list: rename `webhook-test-runner` -> `runner-test`; add `runner-linter` - lint runs.

### Stale-reference sweep

Dispatch `stale-ref-sweeper` with old name `webhook-test-runner` / new name `runner-test` across the repo.
Expected live hits to fix: `dev-ops.md` (one convention-precedent mention), `agent_docs/services/{webhook,reparse,common,worker,loadtest}.md` (5 files).
Expected to leave alone (historical, not live): `plans/side-channel-redis-and-describe-fix.md`, `plans/common-module-cleanup-refactor.md`.

### Regenerate `agent_docs/harness-index.md`

Run `make harness-index` after the rename + new agent land, since it's derived from agent frontmatter (`scripts/sync_harness.py`), never hand-edited.

## Verification

**Part 1 (git hooks):**

- Fresh-clone simulation: clone the repo to a scratch dir, run `make init`, confirm `git config --get core.hooksPath` prints `.githooks` and `.githooks/post-checkout`/`post-merge` are executable.
- `git checkout -b tmp-test && git checkout -` - confirm the uv notice prints (temporarily `PATH`-hide `uv` to test the "missing" branch, then restore) and that file-level checkouts (e.g. `git checkout -- <file>`) produce no output.
- `git pull` (or a local `git merge`) on a branch with new commits - confirm `post-merge` fires the same check.
- Edit `.githooks/post-checkout` on a branch, switch away and back - confirm the *new* content runs immediately with no reinstall step, demonstrating the "stays in sync" property.

**Part 2 (uv adoption):**

- `rm -rf .venv && uv run pytest -c pytest.ini services/webhook/tests` from a clean checkout - confirm `uv` builds `.venv` from `.python-version`/`pyproject.toml` and tests pass, with no manual `pip install` step.
- `uv run python3 --version` - confirm it reports 3.12, not the system 3.9.6.
- `make test` - confirm the Makefile target now goes through `uv run` and still passes.
- On a scratch branch, bump `.python-version` to a different patch/minor (e.g. `3.12.7`), run `make build`, then `docker run --rm <any built image> python3 --version` for a couple of the 8 services - confirm they report the bumped version, proving the `ARG PYTHON_VERSION` propagation actually reaches the built images and isn't just a doc convention. Revert the bump afterward.

**Part 3 (ruff):**

- `make lint` - confirm it exits clean (0 violations) after the one-time `--fix` + manual cleanup.
- Dispatch `runner-test` and `runner-linter` once each manually - confirm their replies match the terse report contract (no raw pytest/ruff dumps reaching the main conversation).
- `make harness-index` - review the regenerated diff, confirm `runner-test`/`runner-linter` both appear correctly and `webhook-test-runner` is gone.
- `grep -rn "webhook-test-runner" .` repo-wide - only the two historical plan files should remain.
