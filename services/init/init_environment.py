#!/usr/bin/env python3
"""Interactive ENVIRONMENT (development/production) prompt for `make init`.
Run via `make init` (or directly: `python3 services/init/init_environment.py`) - always the first init step, before any ClickHouse/LiteLLM provisioning, since those steps' own `$(COMPOSE_FILES)` depend on this choice.

Stdlib-only - loads services/init/init_common.py (shared .env helpers, also used by init_clickhouse_users.py/init_litellm_key.py) via importlib.util.spec_from_file_location rather than a package import, since this script runs on the host, outside any container.

Unlike the ClickHouse/LiteLLM steps, this always asks - it's not a secret and re-confirming (or switching dev<->prod) is cheap, so there's no "skip if already set" idempotency check.
The current .env value (or "development" if unset) is offered as the default, so a repeat run is just Enter.

The typed value must exactly match the Makefile's `ifeq ($(ENVIRONMENT), production)`, so unlike init_clickhouse_users.py's free-text `_prompt`, this validates against a fixed set of accepted spellings and re-prompts on anything else instead of silently writing a typo into .env.
"""
import importlib.util
import pathlib
import sys

INIT_COMMON_PATH = pathlib.Path(__file__).resolve().parent / "init_common.py"

CHOICES = {
    "1": "development",
    "2": "production",
    "development": "development",
    "dev": "development",
    "production": "production",
    "prod": "production",
}


def _load_init_common():
    spec = importlib.util.spec_from_file_location("init_common", INIT_COMMON_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


init_common = _load_init_common()


def _prompt_environment(default: str) -> str:
    print("Which environment is this?")
    print("  1) development - live-editing workflow: source/config bind mounts, auto-reload (default)")
    print("  2) production  - just want to use it: no bind mounts, closer to a real deploy")
    while True:
        raw = input(f"ENVIRONMENT [{default}]: ").strip().lower()
        if not raw:
            return default
        if raw in CHOICES:
            return CHOICES[raw]
        print(f"  not recognized: {raw!r} - enter 1, 2, development, or production")


def main() -> None:
    print("=== Environment setup (make init) ===")
    existing = init_common.read_existing_env(init_common.ENV_PATH)
    default = existing.get("ENVIRONMENT") or "development"
    chosen = _prompt_environment(default)
    init_common.write_env(init_common.ENV_PATH, {"ENVIRONMENT": chosen})
    print(f"\nENVIRONMENT={chosen} written to {init_common.ENV_PATH.name}.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted - re-run `make init` to resume, it's safe to repeat.", file=sys.stderr)
        sys.exit(130)
