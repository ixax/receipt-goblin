"""Source adapter registry for queued usage payloads."""

import os

from common.claude_transcript_adapter import build_claude_transcript_event
from common.ingest_parsing import build_event as build_litellm_event
from common.model_pricing import ModelPricingResolver

LITELLM_STANDARD_ADAPTER = "litellm_standard"
CLAUDE_TRANSCRIPT_ADAPTER = "claude_transcript"

_pricing_resolver: ModelPricingResolver | None = None


def _pricing_for(model: str) -> dict | None:
    global _pricing_resolver
    base_url = os.environ.get("LITELLM_BASE_URL")
    if not base_url:
        return None
    if _pricing_resolver is None:
        _pricing_resolver = ModelPricingResolver(base_url)
    return _pricing_resolver.pricing_for(model)


def build_ingest_event(adapter: str, payload: dict) -> dict:
    if adapter == LITELLM_STANDARD_ADAPTER:
        return build_litellm_event(payload)
    if adapter == CLAUDE_TRANSCRIPT_ADAPTER:
        return build_claude_transcript_event(payload, pricing=_pricing_for(payload.get("model", "")))
    raise ValueError(f"unknown ingest adapter: {adapter!r}")
