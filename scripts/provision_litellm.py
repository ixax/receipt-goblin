#!/usr/bin/env python3
"""Provisions the LiteLLM side of a fresh install without the /ui clickthrough
README "Issue yourself a personal key" otherwise walks through by hand: makes
sure a team exists, makes sure a user exists, issues one personal virtual key,
and writes it back to `.env` as `LITELLM_VIRTUAL_KEY` so `make setup-client`
(and everything reading that var) has a real value instead of a
`<virtual key>` placeholder.

Run via `make litellm-provision` (part of `make bootstrap`), or directly:

    python3 scripts/provision_litellm.py --alias alice --team Personal

`.env` holds exactly one `LITELLM_VIRTUAL_KEY`, but a machine can legitimately
need several identities at once - e.g. two Claude accounts run side by side out
of separate CLI profiles, each carrying its own key so their usage doesn't
collapse into one `user_id`. Pass `--write-env no` for those extra identities:
the key is printed instead of written, the "`.env` key still valid, nothing to
do" shortcut is skipped (it only makes sense for the `.env`-owning identity),
and the caller installs the printed key wherever that profile reads it from.

    python3 scripts/provision_litellm.py \
        --alias alice-laptop --user-id alice --team laptop --write-env no

Stdlib-only (urllib), same constraint as every other `scripts/*` entry point.

Idempotent by design - every step is "find, else create":
  - team:  matched by `team_alias`
  - user:  matched by `user_id`
  - key:   matched by `key_alias`

The one thing that can't be made idempotent is retrieving an *existing* key's
plaintext: LiteLLM stores only the hash, so a key whose plaintext isn't in
`.env` can never be recovered. In that case this issues a fresh key under a
numbered alias (`<alias>-2`, `-3`, ...) rather than deleting the old one -
deleting a key someone else's machine may still be using is not this script's
call to make.

Auth gotcha (cost an hour once, hence this comment): LiteLLM's admin API
rejects a master key passed as `Authorization: Bearer ...` unless it starts
with `sk-`, and `make init` generates one that doesn't. The
`x-litellm-api-key` header has no such check, so that's what's sent first,
with `Authorization` kept only as a fallback for a `sk-`-shaped master key.

Exit codes: 0 = provisioned (or already provisioned), 1 = failed.
"""
import json
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"

DEFAULT_TEAM = "Personal"
DEFAULT_ALIAS = "personal"
DEFAULT_USER_ID = "default_user_id"


def _read_env(path: pathlib.Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def _write_env_var(path: pathlib.Path, key: str, value: str) -> None:
    """Rewrites `key` in place if present (including if it's currently
    commented out, as `.env.example` ships `# LITELLM_VIRTUAL_KEY=`), else
    appends it. Same in-place strategy as init_clickhouse_users.py's
    _write_env - never regenerates the file, so comments survive."""
    lines = path.read_text().splitlines() if path.exists() else []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped in (f"# {key}=", f"#{key}=") or stripped.startswith(f"# {key}="):
            lines[i] = f"{key}={value}"
            path.write_text("\n".join(lines) + "\n")
            return
        if stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.partition("=")[0].strip() == key:
            lines[i] = f"{key}={value}"
            path.write_text("\n".join(lines) + "\n")
            return
    lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n")


class LiteLLM:
    def __init__(self, base_url: str, master_key: str):
        self.base_url = base_url.rstrip("/")
        self.master_key = master_key

    def _request(self, method: str, path: str, body: dict | None = None, timeout: float = 30) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        last_error: Exception | None = None
        # x-litellm-api-key first - see the module docstring's auth gotcha.
        for header in ("x-litellm-api-key", "Authorization"):
            req = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method)
            req.add_header(header, f"Bearer {self.master_key}")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode() or "{}"
                return json.loads(raw)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    last_error = RuntimeError(f"{method} {path}: {e.code} {e.read().decode()[:200]}")
                    continue  # try the other header
                raise RuntimeError(f"{method} {path}: {e.code} {e.read().decode()[:400]}") from e
        raise last_error or RuntimeError(f"{method} {path}: unauthorized with both auth headers")

    def wait_healthy(self, timeout_s: int = 180) -> None:
        print(f"waiting for litellm at {self.base_url} ...")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"{self.base_url}/health/liveliness", timeout=5):
                    print("litellm is up")
                    return
            except Exception:
                time.sleep(3)
        raise RuntimeError(
            f"litellm did not answer {self.base_url}/health/liveliness within {timeout_s}s - "
            "check `make logs SERVICE=litellm`"
        )

    def ensure_team(self, alias: str) -> str:
        teams = self._request("GET", "/team/list")
        # /team/list returns a bare list on current LiteLLM, but has returned
        # {"teams": [...]} historically - accept both rather than pin a shape.
        items = teams if isinstance(teams, list) else teams.get("teams", [])
        for team in items:
            if team.get("team_alias") == alias:
                print(f"team '{alias}' already exists ({team['team_id']})")
                return team["team_id"]
        created = self._request("POST", "/team/new", {"team_alias": alias})
        print(f"created team '{alias}' ({created['team_id']})")
        return created["team_id"]

    def ensure_user(self, user_id: str, team_id: str) -> None:
        try:
            self._request("GET", f"/user/info?user_id={urllib.parse.quote(user_id)}")
            print(f"user '{user_id}' already exists")
            return
        except RuntimeError as exc:
            # Only swallow the lookup miss (LiteLLM answers 4xx/500 depending on version).
            # An auth failure would hit every later call too - surface it here instead of misreporting it as "user not found".
            if " 401 " in str(exc) or " 403 " in str(exc):
                raise
            pass  # not found
        # auto_create_key=false: the key is issued explicitly below with its
        # own alias/team, so letting /user/new mint an extra unnamed one just
        # leaves an orphan key nothing ever uses.
        self._request("POST", "/user/new", {"user_id": user_id, "teams": [team_id], "auto_create_key": False})
        print(f"created user '{user_id}'")

    def key_alias_taken(self, alias: str) -> bool:
        keys = self._request("GET", f"/key/list?key_alias={urllib.parse.quote(alias)}&return_full_object=true")
        items = keys.get("keys", keys if isinstance(keys, list) else [])
        return any((k.get("key_alias") if isinstance(k, dict) else None) == alias for k in items)

    def key_is_valid(self, key: str) -> bool:
        try:
            self._request("GET", f"/key/info?key={urllib.parse.quote(key)}")
            return True
        except RuntimeError:
            return False

    def generate_key(self, alias: str, team_id: str, user_id: str, models: list[str]) -> str:
        body = {"key_alias": alias, "team_id": team_id, "user_id": user_id}
        if models:
            body["models"] = models
        created = self._request("POST", "/key/generate", body)
        return created["key"]


def _parse_args(argv: list[str]) -> dict[str, str]:
    # Same reason as init_clickhouse_users.py: hand-rolled rather than
    # argparse, so this stays callable with whatever the Makefile passes.
    opts = {
        "team": DEFAULT_TEAM,
        "alias": DEFAULT_ALIAS,
        "user-id": DEFAULT_USER_ID,
        "models": "",
        "write-env": "yes",
    }
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
        if arg.startswith("--") and arg[2:] in opts and i + 1 < len(argv):
            opts[arg[2:]] = argv[i + 1]
            i += 2
            continue
        print(f"unrecognized argument: {arg}", file=sys.stderr)
        sys.exit(1)
    return opts


def main() -> None:
    opts = _parse_args(sys.argv[1:])
    env = _read_env(ENV_PATH)

    master_key = env.get("LITELLM_MASTER_KEY", "")
    if not master_key:
        print("error: LITELLM_MASTER_KEY is unset in .env - run `make init` first", file=sys.stderr)
        sys.exit(1)

    base_url = env.get("LITELLM_URI") or f"http://localhost:{env.get('LITELLM_PORT') or 4000}"
    client = LiteLLM(base_url, master_key)
    write_env = opts["write-env"].strip().lower() not in ("no", "false", "0")

    try:
        client.wait_healthy()

        # Only meaningful for the identity `.env` actually owns - an extra
        # identity (--write-env no) is a different key by definition, so a
        # valid LITELLM_VIRTUAL_KEY says nothing about whether it exists yet.
        existing_key = env.get("LITELLM_VIRTUAL_KEY", "") if write_env else ""
        if existing_key and client.key_is_valid(existing_key):
            print("LITELLM_VIRTUAL_KEY in .env is still valid - nothing to do")
            print(f"\n=== done ===\nkey: {existing_key[:12]}... (unchanged)")
            return

        team_id = client.ensure_team(opts["team"])
        client.ensure_user(opts["user-id"], team_id)

        alias = opts["alias"]
        if client.key_alias_taken(alias):
            # See the module docstring: the old key's plaintext is
            # unrecoverable, so take the next free alias instead of deleting.
            suffix = 2
            while client.key_alias_taken(f"{alias}-{suffix}"):
                suffix += 1
            print(f"key alias '{alias}' is taken and its plaintext isn't in .env - issuing '{alias}-{suffix}' instead")
            alias = f"{alias}-{suffix}"

        models = [m.strip() for m in opts["models"].split(",") if m.strip()]
        key = client.generate_key(alias, team_id, opts["user-id"], models)
        if write_env:
            _write_env_var(ENV_PATH, "LITELLM_VIRTUAL_KEY", key)

        print("\n=== done ===")
        print(f"team:  {opts['team']} ({team_id})")
        print(f"user:  {opts['user-id']}")
        if write_env:
            print(f"key:   alias '{alias}', written to .env as LITELLM_VIRTUAL_KEY")
            print("\nNext: `make setup-client` to print the client config, or "
                  "`make setup-client-apply` to write it into ~/.claude/settings.json and ~/.codex/config.toml.")
        else:
            # Printed once and never recoverable afterwards (LiteLLM stores
            # only the hash) - the caller installs it wherever this identity
            # reads LITELLM_VIRTUAL_KEY from.
            print(f"key:   alias '{alias}', NOT written to .env (--write-env no)")
            print(f"\n{key}")
    except RuntimeError as e:
        print(f"\nerror: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
