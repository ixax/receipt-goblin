"""Creates the first personal LiteLLM team + virtual key via LiteLLM's own
Admin API. Not a tracked entry point run directly - piped via stdin into
`docker compose exec -T litellm python3 -` by init_litellm_key.py, so it runs
*inside* the litellm container against its own http://localhost:4000, since
litellm publishes no host port (see init_litellm_key.py's docstring).

Reads LITELLM_MASTER_KEY from its own environment (already set inside the
container by docker-compose.yml) and an optional EXISTING_TEAM_ID (passed in
by init_litellm_key.py via `exec -e`) to skip team creation on a resumed run.
Prints a single JSON line to stdout: {"team_id": ..., "key": ...}.
Any HTTP error raises uncaught, so the exec'd python3 exits non-zero and
init_litellm_key.py's own subprocess.run(check=True) surfaces the failure.
"""
import json
import os
import urllib.request

BASE_URL = "http://localhost:4000"
MASTER_KEY = os.environ["LITELLM_MASTER_KEY"]
# LiteLLM's litellm_key_header_name: x-litellm-api-key repoints auth at this
# custom header for every route, including admin ones - plain Authorization
# is rejected as malformed (see services/litellm/config.yaml, README.md).
HEADERS = {
    "x-litellm-api-key": f"Bearer {MASTER_KEY}",
    "Content-Type": "application/json",
}


def _post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE_URL}{path}", method="POST", headers=HEADERS, data=json.dumps(payload).encode(),
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def main() -> None:
    team_id = os.environ.get("EXISTING_TEAM_ID") or _post("/team/new", {"team_alias": "default"})["team_id"]
    key = _post("/key/generate", {"team_id": team_id, "key_alias": "personal"})["key"]
    print(json.dumps({"team_id": team_id, "key": key}))


if __name__ == "__main__":
    main()
