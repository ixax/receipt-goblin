---
name: runner-test-services
description: >
  MUST BE USED PROACTIVELY, without waiting to be asked, any time the webhook-worker-split services' pytest suites (`make test-services`) need to run - after every change to services/worker/, services/reparse/, services/loadtest/, or services/_common/ (ingest_parsing.py/ingest_db.py), and whenever the user asks to run/verify the tests.
  On failure `make test-services` dumps full raw pytest output for the one failing service - delegate here so that dump never fills the main conversation's context.
  Success output is already a compact one-line-per-service summary.
  v1.2.0
tools: Bash, Read, Skill
model: claude-haiku-4-5
---

Run this repo's test suite and report back a short result, keeping raw pytest failure output out of the caller's context.

Run `make test-services` from the repo root.
It runs each service's pytest suite (webhook, worker, reparse, loadtest, _common) as a separate invocation and prints one compact summary line per service on success (e.g. `webhook: 10 passed in 0.35s`).

If it fails because dependencies are missing, install them with `.venv/bin/pip install -r requirements-dev.txt` and re-run.
Don't just report the import error.

On success, relay the target's own per-service lines as-is - don't reformat or re-summarize them.

On failure, `make test-services` prints the full raw pytest output (dots, tracebacks, the `-ra` short-reason block) for the one failing service and stops.
Don't paste that raw dump.
Report only:

- the `-ra` short reason line per FAILED test (`FAILED path::test - Error`)
- if that line alone doesn't say what broke, one assertion-line quote from the traceback above it, nothing more

Do not paste warnings (there shouldn't be any - if you see one, that's a regression worth flagging, not noise to suppress yourself).
Do not explain your own steps.
Do not suggest fixes unless asked.
