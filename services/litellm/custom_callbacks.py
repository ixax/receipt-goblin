# Loaded via config.yaml's litellm_settings.callbacks (docker-entrypoint.sh
# copies this file next to the merged config so the reference resolves).
#
# Langfuse only groups calls into a "session" via metadata["session_id"] on
# the request, which nothing sets by default. This copies the
# x-claude-code-session-id header into metadata["session_id"]/trace_user_id
# so Langfuse sessions match the grouping ClickHouse already has.
import asyncio
import base64
import binascii
import json
from collections import OrderedDict
from typing import Any, Literal, Optional, Tuple

import litellm.llms.anthropic.common_utils as _anthropic_common_utils
from litellm._logging import verbose_proxy_logger
from litellm.integrations.custom_logger import CustomLogger
from litellm.responses.sse_output_recovery import (
    record_output_item_chunk,
    record_output_text_chunk,
)


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


# --- ChatGPT/Codex streamed tool-call output recovery ---------------------
#
# For custom_llm_provider="chatgpt" (Codex CLI, OpenAI Responses API,
# stream=True), BaseResponsesAPIStreamingIterator._process_chunk (litellm's
# own responses/streaming_iterator.py) just overwrites self.completed_response
# with each SSE event, ending on "response.completed" - which the ChatGPT
# Codex backend sends with an empty output[] even when the turn made tool
# calls. The real content only ever appeared in earlier
# "response.output_item.done"/"response.output_text.done" events, which
# litellm never accumulates for the streaming path (a non-streaming path
# already does this via litellm.responses.sse_output_recovery, but nothing
# wires it into the streaming iterator - see BerriAI/litellm#25429, unmerged
# fix in PR #31332 as of writing). So StandardLoggingPayload.response.output
# reaches our webhook empty, and the Trace panel/ClickHouse can't show what
# Codex actually did.
#
# Fix: mirror the same recovery logic here, using the two hooks that
# actually bracket the gap. async_post_call_streaming_iterator_hook sees
# every already-parsed SSE chunk as it passes through to the client (pure
# pass-through - we don't mutate it, litellm's own logging pipeline has
# already captured a reference to the same completed_response by the time
# this fires) and accumulates output items into a dict keyed by
# litellm_call_id. async_logging_hook runs before any success_callback
# dispatch (including metrics_webhook) and receives
# kwargs["standard_logging_object"] as a mutable dict reference sharing the
# same litellm_call_id - if its response.output is still empty, we inject
# the recovered items there.
#
# The two hooks race: async_logging_hook fires off litellm's own success
# path, independent of whether the client has finished consuming the SSE
# stream, while the iterator's `finally` (where recovery is written) only
# runs once that consumption completes. Confirmed against live Codex CLI
# traffic - a turn with a tool call had the iterator finish first
# (recovery landed), a plain-text-reply turn had async_logging_hook fire
# first (recovery arrived too late, silently dropped) - same SSE event
# types present both times, so this is a timing race, not a chunk-type
# mismatch. Each pending call_id therefore gets an asyncio.Event that the
# iterator sets once its recovery (or lack of one) is decided, and
# async_logging_hook waits on it briefly before giving up.
_MAX_PENDING_OUTPUT_RECOVERIES = 500
_RECOVERY_WAIT_TIMEOUT_S = 2.0

_pending_output_items: "OrderedDict[str, list]" = OrderedDict()
_recovery_ready: "OrderedDict[str, asyncio.Event]" = OrderedDict()


def _recovery_event_for(call_id: str) -> asyncio.Event:
    event = _recovery_ready.get(call_id)
    if event is None:
        event = asyncio.Event()
        if len(_recovery_ready) >= _MAX_PENDING_OUTPUT_RECOVERIES:
            _recovery_ready.popitem(last=False)
        _recovery_ready[call_id] = event
    return event


class ChatGPTResponsesOutputRecoveryHandler(CustomLogger):
    async def async_post_call_streaming_iterator_hook(
        self,
        user_api_key_dict,
        response,
        request_data: dict,
    ):
        call_id = request_data.get("litellm_call_id")
        output_items: dict = {}
        text_only_items: dict = {}
        try:
            async for chunk in response:
                if call_id and hasattr(chunk, "model_dump"):
                    parsed_chunk = chunk.model_dump()
                    chunk_type = parsed_chunk.get("type")
                    if chunk_type == "response.output_item.done":
                        record_output_item_chunk(parsed_chunk, output_items)
                    elif chunk_type == "response.output_text.done":
                        record_output_text_chunk(parsed_chunk, output_items, text_only_items)
                yield chunk
        finally:
            if call_id:
                if output_items or text_only_items:
                    merged = {**text_only_items, **output_items}
                    if len(_pending_output_items) >= _MAX_PENDING_OUTPUT_RECOVERIES:
                        _pending_output_items.popitem(last=False)
                    _pending_output_items[call_id] = [merged[i] for i in sorted(merged)]
                _recovery_event_for(call_id).set()

    async def async_logging_hook(self, kwargs: dict, result: Any, call_type: str) -> Tuple[dict, Any]:
        call_id = kwargs.get("litellm_call_id")
        if call_id:
            event = _recovery_event_for(call_id)
            try:
                await asyncio.wait_for(event.wait(), timeout=_RECOVERY_WAIT_TIMEOUT_S)
            except asyncio.TimeoutError:
                verbose_proxy_logger.warning(
                    "chatgpt_responses_output_recovery_handler: timed out waiting for "
                    "streamed output recovery (call_id=%s)", call_id,
                )
            _recovery_ready.pop(call_id, None)
        recovered = _pending_output_items.pop(call_id, None) if call_id else None
        if recovered:
            standard_logging_object = kwargs.get("standard_logging_object")
            response = (standard_logging_object or {}).get("response") if isinstance(standard_logging_object, dict) else None
            if isinstance(response, dict) and not response.get("output"):
                response["output"] = recovered
        return kwargs, result


chatgpt_responses_output_recovery_handler = ChatGPTResponsesOutputRecoveryHandler()
