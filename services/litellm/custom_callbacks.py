# Loaded by litellm's proxy via the `custom_callbacks.session_id_handler`
# entry in config.yaml's litellm_settings.callbacks - docker-entrypoint.sh
# copies this file next to the effective merged config so that reference
# resolves (see "same directory as config.yaml" import convention:
# https://docs.litellm.ai/docs/proxy/call_hooks).
#
# Purpose: litellm already captures the "x-claude-code-session-id" header
# services/webhook/src/clickhouse_ingest.py's _session_and_trace_id reads
# from payload["metadata"]["requester_custom_headers"] - but that's only
# used by the metrics_webhook callback today. Langfuse groups traces into a
# "session" via metadata["session_id"] on the *request*, which nothing sets
# by default, so without this every call would land in Langfuse as its own
# disconnected trace instead of being grouped per CLI session. This hook
# copies the same header into metadata["session_id"] (and
# metadata["trace_user_id"]) before the call, so Langfuse ends up with the
# exact same session grouping ClickHouse already has.
import base64
import binascii
import json
from typing import Literal, Optional

import litellm.llms.anthropic.common_utils as _anthropic_common_utils
from litellm.integrations.custom_logger import CustomLogger


class SessionIdHandler(CustomLogger):
    async def async_pre_call_hook(
        self,
        user_api_key_dict,
        cache,
        data: dict,
        call_type: Literal[
            "completion",
            "text_completion",
            "embeddings",
            "image_generation",
            "moderation",
            "audio_transcription",
            "responses",
        ],
    ) -> Optional[dict]:
        metadata = data.setdefault("metadata", {})
        headers = metadata.get("requester_custom_headers") or {}
        session_id = headers.get("x-claude-code-session-id")
        if session_id:
            metadata["session_id"] = session_id
            metadata.setdefault("trace_user_id", getattr(user_api_key_dict, "user_id", None) or session_id)
        return data


session_id_handler = SessionIdHandler()


# --- Codex/ChatGPT subscription passthrough -------------------------------
#
# Mirrors the Anthropic OAuth passthrough this proxy already relies on for
# Claude Code (see README.md "Routing Claude Code through it" and
# BerriAI/litellm#19618): litellm's own `clean_headers()` (proxy internal,
# proxy/litellm_pre_call_utils.py) drops any incoming `Authorization` header
# unless it recognizes it as a provider OAuth token via
# `litellm.llms.anthropic.common_utils.is_anthropic_oauth_key()` - which only
# checks for Anthropic's `sk-ant-oat*` prefix. There is no equivalent
# built-in recognizer for a ChatGPT/Codex subscription token (confirmed
# against litellm's docs and open issues - BerriAI/litellm#23777 and #24500
# are open feature requests for exactly this, unresolved upstream), so
# without this, a Codex caller's own token is silently stripped and the
# built-in `chatgpt` provider falls back to whatever single account is
# logged into this container (see docker-entrypoint.sh's dummy auth.json
# seed).
#
# ChatGPT/OpenAI tokens have no fixed prefix to check the way Anthropic's
# do, so instead this decodes the JWT payload and looks for the
# "https://api.openai.com/auth" claim - the same claim litellm's own
# Authenticator._extract_account_id() (llms/chatgpt/authenticator.py) reads
# to derive the account id, so this isn't guessing at OpenAI's token
# format, it's reusing litellm's own detection logic.
def _chatgpt_account_id(token: str) -> Optional[str]:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    return claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")


_original_is_anthropic_oauth_key = _anthropic_common_utils.is_anthropic_oauth_key


def _is_anthropic_or_chatgpt_oauth_key(value: Optional[str]) -> bool:
    if _original_is_anthropic_oauth_key(value):
        return True
    return bool(value and _chatgpt_account_id(value))


# `clean_headers()` does a function-local `from litellm.llms.anthropic.common_utils
# import is_anthropic_oauth_key` on every call (not a module-load-time bind),
# so patching the module attribute here is enough for it to pick up the
# broadened check - no litellm proxy source needs touching.
_anthropic_common_utils.is_anthropic_oauth_key = _is_anthropic_or_chatgpt_oauth_key


class ChatGPTAuthForwardHandler(CustomLogger):
    """Forwards a Codex caller's own ChatGPT subscription token to the
    `chatgpt` provider per-call, instead of using its single shared
    device-code-logged-in identity. Requires the monkeypatch above to have
    already let the header survive `clean_headers()`."""

    async def async_pre_call_hook(
        self,
        user_api_key_dict,
        cache,
        data: dict,
        call_type: Literal[
            "completion",
            "text_completion",
            "embeddings",
            "image_generation",
            "moderation",
            "audio_transcription",
            "responses",
        ],
    ) -> Optional[dict]:
        headers = (data.get("proxy_server_request") or {}).get("headers") or {}
        auth_header = headers.get("authorization")
        if not auth_header:
            return data
        token = auth_header.removeprefix("Bearer ").strip()
        account_id = _chatgpt_account_id(token)
        if not account_id:
            return data
        data["extra_headers"] = {
            **(data.get("extra_headers") or {}),
            "Authorization": auth_header if auth_header.startswith("Bearer ") else f"Bearer {token}",
            "ChatGPT-Account-Id": account_id,
        }
        return data


chatgpt_auth_forward_handler = ChatGPTAuthForwardHandler()
