#!/usr/bin/env python3
"""Renders (and optionally writes) the client-side config that routes Codex
CLI through this stack's LiteLLM proxy and points every client's tracking
hooks at the ingest endpoint.

    make setup-client          # print everything, change nothing (default)
    make setup-client-apply    # write ~/.claude/settings.json + ~/.codex/config.toml

What is deliberately NOT written globally: `ANTHROPIC_BASE_URL` and
`ANTHROPIC_CUSTOM_HEADERS`.
Putting either in `~/.claude/settings.json`'s "env" (or in a shell profile)
applies it to *every* Claude client on the machine, including Claude Desktop
and `claude --remote-control`, which must keep their direct Anthropic
connection.
Those two belong on a per-launch wrapper around the normal CLI only -
`make setup-client` prints them as commented guidance for exactly that
reason, and this script never writes them anywhere.

Stdlib-only (no tomllib write support exists anyway, see below), same
constraint as every other `scripts/*` entry point.

What gets written, and how it stays idempotent:

  ~/.claude/settings.json - real JSON, so it's parsed, its "env" object is
    updated key-by-key, and it's re-serialized. Every other setting (hooks,
    permissions, model) is preserved untouched.

  ~/.codex/config.toml - TOML has no stdlib writer, so this does NOT
    round-trip the file. Instead it maintains one marked block:

        # >>> receipt-goblin (managed by `make setup-client-apply`) >>>
        ...
        # <<< receipt-goblin <<<

    re-written in place on every run, appended if absent. Anything outside
    the markers (hooks, mcp_servers, your own model settings) is left alone.
    `model_provider` is a top-level key, which in TOML must appear before the
    first `[table]` header, so it's handled separately from the block.

Both files are backed up to `<file>.bak-receipt-goblin` before the first
write, and never overwritten wholesale.

Exit codes: 0 = printed/written, 1 = failed.
"""
import json
import pathlib
import re
import shutil
import sys

MANAGED_START = "# >>> receipt-goblin (managed by `make setup-client-apply`) >>>"
MANAGED_END = "# <<< receipt-goblin <<<"

CLAUDE_SETTINGS = pathlib.Path.home() / ".claude" / "settings.json"
CODEX_CONFIG = pathlib.Path.home() / ".codex" / "config.toml"

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"


def _key_from_env_file() -> str:
    """Read LITELLM_VIRTUAL_KEY straight off .env rather than taking it from
    the Makefile. `make` snapshots .env at parse time, so during a single
    `make bootstrap` run the key `litellm-provision` just wrote is invisible
    to the `setup-client-apply` step that follows it - reading here is what
    makes bootstrap work end-to-end in one invocation."""
    if not ENV_PATH.exists():
        return ""
    for line in ENV_PATH.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() == "LITELLM_VIRTUAL_KEY":
            return value.strip()
    return ""


def _parse_args(argv: list[str]) -> dict:
    opts = {
        "anthropic-uri": "http://localhost:4001",
        "openai-uri": "http://localhost:4002",
        "ingest-uri": "http://localhost:8010",
        "key": _key_from_env_file() or "<virtual key>",
        "write": False,
        "shell-rc": "",
    }
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
        if arg == "--write":
            opts["write"] = True
            i += 1
            continue
        if arg.startswith("--") and arg[2:] in opts and i + 1 < len(argv):
            opts[arg[2:]] = argv[i + 1]
            i += 2
            continue
        print(f"unrecognized argument: {arg}", file=sys.stderr)
        sys.exit(1)
    return opts


def shell_exports(o: dict) -> str:
    """Only the globals that are safe for every client on the machine.
    LITELLM_AUTH_HEADER derives from $LITELLM_VIRTUAL_KEY rather than
    repeating the substituted value, so a hand-edited placeholder only ever
    needs replacing in one line."""
    return f"""export LITELLM_VIRTUAL_KEY="{o['key']}"
export LITELLM_AUTH_HEADER="Bearer $LITELLM_VIRTUAL_KEY"
export AGENT_CLI_TRACKING_API_URL="{o['ingest-uri']}\""""


def claude_wrapper_note(o: dict) -> str:
    return f"""# --- Claude smart wrapper (do not put Anthropic proxy vars in global env/settings.json) ---
# Normal Claude CLI: set these only on the child process:
#   ANTHROPIC_BASE_URL="{o['anthropic-uri']}"
#   ANTHROPIC_CUSTOM_HEADERS="x-litellm-api-key: $LITELLM_AUTH_HEADER"
# For `claude --remote-control` and `claude remote-control`: leave both
# proxy vars unset and set CLAUDE_TRANSCRIPT_TRACKING_MODE=direct so
# transcript hooks track the direct Anthropic session."""


def codex_block(o: dict) -> str:
    return f"""[model_providers.litellm]
name = "LiteLLM"
base_url = "{o['openai-uri']}"
wire_api = "responses"
requires_openai_auth = true
env_http_headers = {{ "x-litellm-api-key" = "LITELLM_AUTH_HEADER", \
"x-rg-client-originator" = "CODEX_INTERNAL_ORIGINATOR_OVERRIDE" }}"""
# x-rg-client-originator above: Codex CLI and Codex Desktop share one user-agent, so ingest can only
# tell them apart when the launching client exports CODEX_INTERNAL_ORIGINATOR_OVERRIDE and the header
# carries it to the local proxy (read by common/client_attribution.py; header goes nowhere else).
# Harmless when the env var is unset - Codex simply omits the header.


def claude_env(o: dict) -> dict[str, str]:
    """Tracking identity only - never the Anthropic proxy vars, see the module
    docstring.
    Claude Code doesn't expand $VAR inside an "env" value, so the key is
    substituted literally here rather than referenced."""
    return {
        "AGENT_CLI_TRACKING_API_URL": o["ingest-uri"],
        "LITELLM_VIRTUAL_KEY": o["key"],
    }


def do_print(o: dict) -> None:
    print("# --- ~/.zshrc / ~/.bashrc (safe globals for every client) ---")
    print(shell_exports(o))
    print()
    print(claude_wrapper_note(o))
    print()
    print("# --- ~/.codex/config.toml (merge in, keep any hooks/mcp_servers already there) ---")
    print("# Only covers model routing - the export lines above are still needed")
    print("# in your shell for hooks/report_git_branch.py, see comment above.")
    print('model_provider = "litellm"')
    print()
    print(codex_block(o))
    print()
    print('# --- ~/.claude/settings.json ("env" block - merge in, keep any hooks already there) ---')
    print(json.dumps({"env": claude_env(o)}, indent=2))
    print()
    print("Nothing was written. `make setup-client-apply` writes the last two blocks for you.")


def _backup(path: pathlib.Path) -> None:
    backup = path.with_suffix(path.suffix + ".bak-receipt-goblin")
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)
        print(f"  backed up {path} -> {backup.name}")


def write_claude_settings(o: dict) -> None:
    CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    settings: dict = {}
    if CLAUDE_SETTINGS.exists():
        text = CLAUDE_SETTINGS.read_text().strip()
        if text:
            try:
                settings = json.loads(text)
            except json.JSONDecodeError as e:
                # Refusing rather than overwriting: this file holds the user's
                # hooks/permissions, and a parse failure usually means a
                # hand-edit in progress, not a corrupt file to discard.
                raise RuntimeError(f"{CLAUDE_SETTINGS} is not valid JSON ({e}) - fix it, then re-run") from e
    _backup(CLAUDE_SETTINGS)
    env = settings.get("env")
    if not isinstance(env, dict):
        env = {}
    env.update(claude_env(o))
    settings["env"] = env
    CLAUDE_SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"  wrote {CLAUDE_SETTINGS} (env block; every other setting preserved)")


def write_codex_config(o: dict) -> None:
    CODEX_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    text = CODEX_CONFIG.read_text() if CODEX_CONFIG.exists() else ""
    _backup(CODEX_CONFIG)

    managed = f"{MANAGED_START}\n{codex_block(o)}\n{MANAGED_END}"
    if MANAGED_START in text and MANAGED_END in text:
        text = re.sub(
            re.escape(MANAGED_START) + r".*?" + re.escape(MANAGED_END),
            managed.replace("\\", "\\\\"),
            text,
            flags=re.DOTALL,
        )
    else:
        text = (text.rstrip() + "\n\n" if text.strip() else "") + managed + "\n"

    # `model_provider` is top-level, so in TOML it has to sit above the first
    # [table] header - can't live inside the managed block appended at the end.
    line = 'model_provider = "litellm"'
    if re.search(r"(?m)^\s*model_provider\s*=", text):
        text = re.sub(r"(?m)^\s*model_provider\s*=.*$", line, text, count=1)
    else:
        text = f"{line}\n" + text

    CODEX_CONFIG.write_text(text)
    print(f"  wrote {CODEX_CONFIG} (managed block + model_provider; everything else preserved)")


def write_shell_rc(o: dict, rc_path: pathlib.Path) -> None:
    text = rc_path.read_text() if rc_path.exists() else ""
    _backup(rc_path)
    managed = f"{MANAGED_START}\n{shell_exports(o)}\n{MANAGED_END}"
    if MANAGED_START in text and MANAGED_END in text:
        text = re.sub(
            re.escape(MANAGED_START) + r".*?" + re.escape(MANAGED_END),
            managed.replace("\\", "\\\\"),
            text,
            flags=re.DOTALL,
        )
    else:
        text = (text.rstrip() + "\n\n" if text.strip() else "") + managed + "\n"
    rc_path.write_text(text)
    print(f"  wrote {rc_path} (managed block) - open a new shell to pick it up")


def do_write(o: dict) -> None:
    if o["key"] == "<virtual key>":
        print(
            "error: no LITELLM_VIRTUAL_KEY in .env - run `make litellm-provision` first "
            "(writing the placeholder into your client config would just break every call).",
            file=sys.stderr,
        )
        sys.exit(1)
    print("=== writing client config ===")
    write_claude_settings(o)
    write_codex_config(o)
    if o["shell-rc"]:
        write_shell_rc(o, pathlib.Path(o["shell-rc"]).expanduser())
    else:
        print("\nShell exports were NOT written (no --shell-rc given). Codex CLI's hooks read")
        print("them from the environment, so add them to your shell profile if you use Codex:")
        if sys.platform.startswith("win"):
            print("  Windows: set them as user env vars (e.g. `setx AGENT_CLI_TRACKING_API_URL "
                  f"{o['ingest-uri']}`), then reopen the terminal.")
        else:
            print("  make setup-client-apply SHELL_RC=~/.zshrc")
        print()
        print(shell_exports(o))


def main() -> None:
    opts = _parse_args(sys.argv[1:])
    try:
        if opts["write"]:
            do_write(opts)
        else:
            do_print(opts)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
