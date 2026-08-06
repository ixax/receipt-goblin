"""Shared LiteLLM virtual-key check - used by every service that gates
requests behind a LiteLLM virtual key (services/webhook, services/mcp-stats).
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

_TEAM_ALIAS_TTL_S = 300
_TEAM_ALIAS_CACHE: dict[str, tuple[str, float]] = {}


def virtual_key_info(key: str, base_url: str, master_key: str, timeout: int = 3) -> dict | None:
    # Checks the caller's key against LiteLLM's own /key/info instead of inventing a signing scheme.
    if not key:
        return None
    req = urllib.request.Request(
        f"{base_url}/key/info?key={key}",
        # LiteLLM's litellm_key_header_name is x-litellm-api-key (see AGENTS.md);
        # plain Authorization: Bearer here is rejected as malformed.
        headers={"x-litellm-api-key": f"Bearer {master_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    info = payload.get("info")
    if not isinstance(info, dict) or not isinstance(info.get("key_name"), str) or not info["key_name"]:
        return None
    if info.get("blocked"):
        return None
    expires = info.get("expires")
    if expires:
        if not isinstance(expires, str):
            return None
        try:
            expires_at = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        except ValueError:
            return None
        if expires_at.tzinfo is None or expires_at < datetime.now(timezone.utc):
            return None
    return info


def team_alias(team_id: str, base_url: str, master_key: str, timeout: int = 3) -> str:
    """Display name of a LiteLLM Team, or "" when it can't be resolved.

    `/key/info` carries `team_id` but not the alias, and the direct-collector
    path has no LiteLLM payload to read `user_api_key_team_alias` off, so this
    is the only way that path can name a Team.
    Team names change rarely and every usage batch would otherwise re-ask,
    hence the small TTL cache - a rename shows up within _TEAM_ALIAS_TTL_S.
    """
    if not team_id:
        return ""
    now = time.monotonic()
    cached = _TEAM_ALIAS_CACHE.get(team_id)
    if cached and cached[1] > now:
        return cached[0]
    req = urllib.request.Request(
        f"{base_url}/team/info?team_id={urllib.parse.quote(team_id)}",
        headers={"x-litellm-api-key": f"Bearer {master_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return ""
    info = payload.get("team_info") if isinstance(payload, dict) else None
    if not isinstance(info, dict):
        info = payload if isinstance(payload, dict) else {}
    alias = info.get("team_alias")
    alias = alias if isinstance(alias, str) else ""
    # A failed lookup is not cached: it's usually litellm being briefly
    # unavailable, and caching "" would keep the Team nameless for the whole
    # TTL after it recovers.
    if alias:
        _TEAM_ALIAS_CACHE[team_id] = (alias, now + _TEAM_ALIAS_TTL_S)
    return alias


def virtual_key_is_valid(key: str, base_url: str, master_key: str, timeout: int = 3) -> bool:
    return virtual_key_info(key, base_url, master_key, timeout) is not None
