# Deploying this stack unattended

How an agent stands the stack up on a host from a clean clone, with no prompts and no `/ui` clickthrough.
Human-facing version, with the manual step-by-step equivalents: README "Getting started".

## The one command

```bash
make bootstrap
```

`preflight` → `init-auto` → `up` → `status` → `litellm-provision`.
Nothing in that chain reads stdin, so it works with no TTY.
Every step is idempotent - re-running on a provisioned host reuses the existing `.env` credentials and an existing valid virtual key instead of reissuing either.

Optional flags:

- `APPLY_CLIENT=1` - also write `~/.claude/settings.json` + `~/.codex/config.toml`. **Ask the user first** - it writes outside the repo (backed up to `<file>.bak-receipt-goblin`, but still their config).
- `SHELL_RC=~/.zshrc` - also write the shell exports Codex CLI's hooks read. Same rule: ask first. On Windows there is no rc file to write; the exports have to become user env vars instead, so `setup_client.py` prints a `setx` hint rather than guessing.

## When a step fails

| Symptom                                                     | What it means                                                                                       |
| ----------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| `preflight` FAILED on `docker daemon`                        | Daemon isn't running. macOS: `colima start`. Windows: start Docker Desktop. Not something to work around. |
| `preflight` FAILED on `docker compose` / `docker buildx`     | Plugin missing - README "Prerequisites". Symptoms are misleading otherwise (`unknown shorthand flag: 'f'`). |
| `preflight` FAILED on `host ports`                           | Something *other than* this stack holds a port. Free it, or override that port var in `.env`. Ports held by `receipt-goblin-*` containers are reported as fine. |
| `preflight` warns on `compose project`                        | A stack is already running, started from a *different* clone of this repo. Compose keys projects by name, so `make up` here would recreate those containers against this directory's files. Stop and ask the user which clone should own the stack. |
| `init-auto` timed out waiting on clickhouse                  | Usually a low-memory Docker VM. `preflight` warns about this before it happens.                      |
| `status` exits 1                                             | It prints the failing service's last 80 log lines. Read those, don't re-run blindly.                 |
| `litellm-provision` 401s                                     | `.env`'s `LITELLM_MASTER_KEY` has drifted from the running container's. Compare `docker exec receipt-goblin-litellm printenv LITELLM_MASTER_KEY` against `.env`; the container is the truth for a stack that's already up. |

## What is deliberately not automated

- **Provider credentials.** `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` come from `.env` and are passed through to the `litellm` container. Never invent, guess, or fetch one. Absent, the stack still comes up fully - only calls to those models 401, so ingest/dashboards are testable without any provider key.
- **Docker itself.** `preflight` diagnoses, it never installs a daemon or resizes a VM.
- **Langfuse / observability.** Opt-in profiles, started explicitly (`make langfuse-up`, `make observability-up`). `bootstrap` never starts them.
- **Deleting a LiteLLM key.** If a key alias is taken and its plaintext isn't in `.env`, `litellm-provision` issues `<alias>-2` instead of deleting - another machine may still be using the original.
- **Destructive recovery.** A wedged install is not fixed by wiping volumes. `AGENTS.md` "Boundaries & safety" applies: DB/volume wipe or `TRUNCATE` requires asking first.

## Staying up afterwards (Windows)

`bootstrap` stands the stack up once.
Keeping it up is a separate concern, and it is not a `make` target - `scripts/ensure-stack.ps1` owns it end to end.

```powershell
pwsh -File scripts\ensure-stack.ps1 -Install
```

Registers a logon Scheduled Task (`receipt-goblin-ensure-stack`) that polls for the Docker daemon, runs `make start` under Git Bash, and waits for LiteLLM's liveliness endpoint.
Without arguments the script just runs that ensure logic; `-Probe` is the fast, never-blocking variant a client hook calls; `-Uninstall` drops the task without touching containers.
Log: `%LOCALAPPDATA%\receipt-goblin\ensure-stack.log`.

Docker Desktop's `AutoStart` plus `restart: always` already handle a reboot; what they cannot handle is `make down`, which removes the containers and leaves the restart policy nothing to restart.

Two constraints are load-bearing, and both fail silently when "simplified":

- **`make start`, not `docker compose` directly.** The compose file interpolates image tags only the `Makefile` produces (`resolve_image_version.py` -> `.image-tags.mk`) and errors out without them. Git Bash specifically: `make` invoked from PowerShell has no POSIX tools on `PATH` and loses `$(shell cat .python-version)`.
- **HTTP health check, not a TCP connect.** `load-balancer` (nginx) owns the published port, so a TCP connect to it succeeds while `litellm` is stopped and every call is 502ing.

PowerShell is the outer layer only because the Scheduled Task has to wait for the Docker daemon before that `make` can work at all, and `pwsh` is the one interpreter guaranteed present at logon.

Two things store an absolute path to the clone: the task, and the optional Claude Code `SessionStart` hook in the user's *global* `~/.claude/settings.json` (README "Keep it up without running `make start`").
Re-run `-Install` after any move - it re-registers the task and warns when the hook points at a different clone.
That warning matters for the same reason `preflight`'s "compose project" one does: same directory name means same compose project, so an autostart aimed at the wrong clone recreates containers against that clone's files.

Deliberately not done: a periodic watchdog, which would fight a deliberate `make down`, and per-client shell wrappers, which miss the Claude Code desktop app.
Nothing equivalent exists on macOS yet.

## Pieces, if a step has to be run on its own

| Piece                                     | Entry point                             |
| ----------------------------------------- | --------------------------------------- |
| Host readiness (read-only)                | `scripts/preflight.py`                  |
| `.env` + ClickHouse roles + migrations    | `services/init/init_clickhouse_users.py --non-interactive` |
| LiteLLM team/user/virtual key             | `scripts/provision_litellm.py`          |
| Client config render/write                | `scripts/setup_client.py [--write]`     |

All four are stdlib-only and run on the host interpreter the `Makefile` resolves into `$(PYTHON)` (`python3`, else `python`, else `py -3` - a stock Windows box has a `python3.exe` alias stub that exits non-zero, so the detection executes each candidate rather than just locating it).
