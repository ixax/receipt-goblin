"""Normalized client attribution shared by every ingest adapter."""

from dataclasses import dataclass

PRODUCTS = frozenset({"claude", "codex"})
SURFACES = frozenset({"cli", "desktop", "remote_control"})

PRODUCT_HEADER = "x-rg-client-product"
SURFACE_HEADER = "x-rg-client-surface"
ORIGINATOR_HEADER = "x-rg-client-originator"

_CODEX_ORIGINATORS = {
    "Codex CLI": "cli",
    "Codex Desktop": "desktop",
}


@dataclass(frozen=True)
class ClientAttribution:
    product: str
    surface: str
    ingest_path: str


def _from_user_agent(user_agent: str) -> tuple[str, str]:
    value = user_agent.strip().lower()
    if value.startswith("claude-cli/"):
        return "claude", "cli"
    if value.startswith("claude-desktop/"):
        return "claude", "desktop"
    if value.startswith("claude-remote-control/"):
        return "claude", "remote_control"
    if value.startswith(("codex-tui/", "codex-cli/")):
        return "codex", "cli"
    if value.startswith("codex-desktop/"):
        return "codex", "desktop"
    if value.startswith("codex_cli_rs/"):
        return "codex", "unknown"
    return "unknown", "unknown"


def from_litellm_payload(payload: dict) -> ClientAttribution:
    metadata = payload.get("metadata") or {}
    raw_headers = metadata.get("requester_custom_headers") or {}
    headers = {str(key).lower(): value for key, value in raw_headers.items()}
    product = headers.get(PRODUCT_HEADER)
    surface = headers.get(SURFACE_HEADER)
    if product in PRODUCTS and surface in SURFACES:
        return ClientAttribution(product, surface, "litellm_proxy")

    originator = str(headers.get(ORIGINATOR_HEADER) or "").strip()
    if originator in _CODEX_ORIGINATORS:
        return ClientAttribution("codex", _CODEX_ORIGINATORS[originator], "litellm_proxy")

    product, surface = _from_user_agent(metadata.get("user_agent") or "")
    return ClientAttribution(product, surface, "litellm_proxy")


def from_claude_envelope(envelope: dict) -> ClientAttribution:
    source = envelope.get("source")
    if source == "claude_desktop":
        return ClientAttribution("claude", "desktop", "claude_transcript")
    if source == "claude_remote_control":
        return ClientAttribution("claude", "remote_control", "claude_transcript")
    return ClientAttribution("unknown", "unknown", "claude_transcript")
