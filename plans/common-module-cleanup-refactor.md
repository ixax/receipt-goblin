# Refactoring: unused imports + single-caller functions out of `common`

## Context

`services/_common/src/` is the shared library for the webhook/worker/reparse split (plus mcp-stats/mcp-dev/loadtest-fixtures/migrate, out of scope here).
Some functions living there are, in practice, only ever imported by one specific service.
For example, `ingest_git_branch`/`ingest_plan_proposal`/`ingest_litellm_alert` in `ingest_db.py` are called only from `services/webhook/src/server.py`.
Keeping single-caller code in `common` adds an indirection with no reuse benefit, and makes `_common`'s surface area look bigger/more shared than it actually is.
Separately, `ruff` found a small number of genuinely dead imports left over from earlier refactors.

Ground truth was gathered via `uvx ruff check --select F401` (repo-wide) and by grepping every call site of each `_common` export to count distinct importing services.

## Part A - Remove unused imports (confirmed by ruff, no behavior change)

`services/_common/src/ingest_db.py`:

- `typing.Optional` (line 8) - unused.
- From the `common.ingest_parsing` import block (lines 15-71): `_serialize_row`, `_serialize_row_multi`, `_user_agent`, `build_event` - all unused in this file. These are used elsewhere - e.g. `build_event` is worker.py's own direct import of `ingest_parsing`, so `ingest_db.py` doesn't need it too.

`hooks/harness_audit/tests/test_comment_format.py`:

- `contextlib.redirect_stdout` (line 12) - unused.

Apply with `uvx ruff check --select F401 --fix <paths>`, then eyeball the diff (ruff's suggested fix is already shown in the check output above).

Additionally, found by manual inspection (ruff's F401 doesn't catch this case - a dotted `import a.b` only binds the top-level name `a`, so ruff considers it "used" as long as `a` appears anywhere, even via a sibling `import a.c`):

- `hooks/report_git_branch.py:13` - `import urllib.error`, never referenced (`urllib.request` is the only submodule actually used).
- `hooks/report_plan_proposal.py:12` - same pattern, same fix.

Remove both lines manually; ruff's `--fix` will not touch them.

## Part B - Move single-caller functions out of `common`

Criterion used: a function must satisfy both of these to move.

1. It is imported by exactly one service outside `_common`.
2. It is self-contained - it does not deeply reach into other private (`_`-prefixed) helpers of its own module that are *also* used by a different pipeline.

Condition 2 rules out `build_event`, `ingest_events_batch`, `reparse_event`, and `get_client`, even though each has only one external caller today.
Each is a thin public entry point over roughly 10-15 shared private helpers in `ingest_db.py`/`ingest_parsing.py` that both the worker path and the reparse path depend on.
Moving them would mean either duplicating those private helpers, or having webhook/worker reach into `common`'s private internals across the package boundary - worse than the current state.

### B1. Webhook-only ingest functions move to a new `services/webhook/src/ingest.py`

Move, verbatim, out of `services/_common/src/ingest_db.py`:

- `ingest_git_branch()`, `_insert_git_branch()`, `_GIT_BRANCH_COLUMNS`
- `ingest_plan_proposal()`, `_insert_plan_proposal()`, `_PLAN_PROPOSAL_COLUMNS`
- `ingest_litellm_alert()`, `_insert_litellm_alert()`, `_LITELLM_ALERT_COLUMNS`

And out of `services/_common/src/ingest_parsing.py`:

- `_issue_id_from_branch()` - used only by `ingest_git_branch`, confirmed no other caller besides its own unit test (move the test too, see below).

The new module imports `get_client` and `fastjson as json` from `common` - both stay shared (`get_client` has 5 internal callers in `ingest_db.py` plus `reparse.py`).
`server.py`'s three route handlers switch their import from `common.ingest_db` to `.ingest`.

Test moves: `services/_common/tests/test_ingest_db.py`'s cases for these three functions, and `test_ingest_parsing.py`'s `_issue_id_from_branch` cases, move to a new `services/webhook/tests/test_ingest.py`.

### B2. Split `services/_common/src/queue_client.py` (webhook vs. worker)

Today this module mixes two disjoint halves that never share code, only adjacent config (`common/config/queue.py`, `common/config/redis.py`, which stay in `common` since both services already import from there directly):

- `get_async_redis()`, `enqueue()`, `enqueue_raw()` - used only by `services/webhook/src/server.py`. Move to `services/webhook/src/queue.py`.
- `get_redis()` - used only by `services/worker/src/worker.py`. Move to `services/worker/src/queue.py`.

Delete `services/_common/src/queue_client.py` and `services/_common/tests/test_queue_client.py` (split its cases across the two new test files, mirroring the module split).

Update imports:

- `server.py`: `from common.queue_client import enqueue_raw, get_async_redis` becomes `from .queue import enqueue_raw, get_async_redis`.
- `worker.py`: `from common.queue_client import get_redis` becomes `from .queue import get_redis`.

## Not doing (flagged, but out of scope unless you want it)

- `ingest_db.ingest_standard_logging_payload` and `ingest_db.ingest_webhook_body` - both docstrings admit "zero call sites today" (pre-Redis-queue-split leftovers). Dead code, not a move candidate. Say the word and this can be deleted in the same pass.
- `ingest_parsing.session_and_trace_id` docstring says "server.py reaches into this directly" - stale, no service imports it anymore, it's only used internally now. Cosmetic docstring fix, not a functional issue.
- `fastjson.load`/`fastjson.dump` (file-object variants) have no production caller, but `fastjson.py` is a deliberate full drop-in replacement for stdlib `json`'s API surface, so leaving unused variants there is intentional, not dead code.

## Verification

- `uvx ruff check --select F401 .` returns zero results after Part A.
- `cd services/_common && python -m pytest` - updated/removed tests still pass, nothing left behind referencing the moved functions.
- `cd services/webhook && python -m pytest` - new `test_ingest.py`/`test_queue.py` (or wherever B1/B2 tests land) pass.
- `cd services/worker && python -m pytest` - worker's queue import still resolves.
- `make test` (delegate to the webhook-test-runner agent) for the full suite across webhook/worker/reparse/loadtest/_common, to catch any import path missed above.
