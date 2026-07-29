"""Shared LiteLLM virtual-key check - used by every service that gates
requests behind a LiteLLM virtual key (services/webhook, services/mcp-stats).
"""
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone


def virtual_key_is_valid(key: str, base_url: str, master_key: str, timeout: int = 3) -> bool:
    # Checks the caller's key against LiteLLM's own /key/info instead of inventing a signing scheme.
    if not key:
        return False
    req = urllib.request.Request(
        f"{base_url}/key/info?key={key}",
        # LiteLLM's litellm_key_header_name is x-litellm-api-key (see AGENTS.md);
        # plain Authorization: Bearer here is rejected as malformed.
        headers={"x-litellm-api-key": f"Bearer {master_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            info = json.load(resp).get("info") or {}
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return False
    if info.get("blocked"):
        return False
    expires = info.get("expires")
    if expires and datetime.fromisoformat(expires.replace("Z", "+00:00")) < datetime.now(timezone.utc):
        return False
    return True
