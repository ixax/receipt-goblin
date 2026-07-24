# Loaded via config.yaml's litellm_settings.callbacks (docker-entrypoint.sh
# copies this file next to the merged config so the reference resolves).
#
# Langfuse only groups calls into a "session" via metadata["session_id"] on
# the request, which nothing sets by default. This copies the
# x-claude-code-session-id header into metadata["session_id"]/trace_user_id
# so Langfuse sessions match the grouping ClickHouse already has.
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
# litellm's clean_headers() strips any Authorization header it doesn't
# recognize as provider OAuth via is_anthropic_oauth_key(), which only
# matches Anthropic's sk-ant-oat* prefix (no ChatGPT equivalent exists
# upstream: BerriAI/litellm#23777, #24500). Without this, a Codex caller's
# token gets stripped and falls back to the container's single logged-in
# account.
#
# ChatGPT tokens have no fixed prefix, so decode the JWT and read the
# "https://api.openai.com/auth" claim - same claim litellm's own
# Authenticator._extract_account_id() uses.
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


# clean_headers() imports is_anthropic_oauth_key locally on every call (not
# bound at module load), so patching the module attribute here is enough.
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
