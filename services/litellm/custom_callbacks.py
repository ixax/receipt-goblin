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
from typing import Literal, Optional

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
