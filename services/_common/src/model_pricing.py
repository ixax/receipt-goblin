"""Fetches LiteLLM's live model cost map for direct-call cost estimates."""

import logging
import time
import urllib.error
import urllib.request
from typing import Callable

from common import fastjson as json

logger = logging.getLogger("common.model_pricing")


def _fetch_cost_map(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = json.loads(response.read())
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise ValueError("LiteLLM cost map is not an object")
    return payload


class ModelPricingResolver:
    def __init__(
        self,
        base_url: str,
        *,
        ttl_seconds: float = 3600,
        failure_retry_seconds: float = 30,
        timeout: float = 3,
        fetch_cost_map: Callable[[str, float], dict] = _fetch_cost_map,
    ):
        self._url = f"{base_url.rstrip('/')}/public/litellm_model_cost_map"
        self._ttl_seconds = ttl_seconds
        self._failure_retry_seconds = failure_retry_seconds
        self._timeout = timeout
        self._fetch_cost_map = fetch_cost_map
        self._cost_map: dict = {}
        self._refresh_after = 0.0

    def pricing_for(self, model: str) -> dict | None:
        now = time.monotonic()
        if now >= self._refresh_after:
            try:
                self._cost_map = self._fetch_cost_map(self._url, self._timeout)
                self._refresh_after = now + self._ttl_seconds
            except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError):
                self._refresh_after = now + self._failure_retry_seconds
                logger.warning("failed to refresh LiteLLM model pricing", exc_info=True)
        pricing = self._cost_map.get(model)
        return pricing if isinstance(pricing, dict) else None
