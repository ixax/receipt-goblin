import importlib

import pytest


def _attribution_module():
    try:
        return importlib.import_module("common.client_attribution")
    except ModuleNotFoundError:
        pytest.fail("common.client_attribution is not implemented")


@pytest.mark.parametrize(
    ("product", "surface"),
    [
        ("codex", "cli"),
        ("codex", "desktop"),
        ("claude", "cli"),
    ],
)
def test_litellm_attribution_prefers_valid_explicit_markers(product, surface):
    module = _attribution_module()
    payload = {
        "metadata": {
            "requester_custom_headers": {
                "x-rg-client-product": product,
                "x-rg-client-surface": surface,
            },
            "user_agent": "unknown-client/1.0",
        }
    }

    attribution = module.from_litellm_payload(payload)

    assert attribution.product == product
    assert attribution.surface == surface
    assert attribution.ingest_path == "litellm_proxy"


def test_litellm_attribution_rejects_invalid_markers_and_uses_known_user_agent():
    module = _attribution_module()
    payload = {
        "metadata": {
            "requester_custom_headers": {
                "x-rg-client-product": "attacker-controlled",
                "x-rg-client-surface": "browser",
            },
            "user_agent": "claude-cli/2.1.221 (external, cli)",
        }
    }

    attribution = module.from_litellm_payload(payload)

    assert attribution.product == "claude"
    assert attribution.surface == "cli"
    assert attribution.ingest_path == "litellm_proxy"


@pytest.mark.parametrize(
    ("originator", "surface"),
    [
        ("Codex CLI", "cli"),
        ("Codex Desktop", "desktop"),
    ],
)
def test_litellm_attribution_maps_allowlisted_codex_originator(originator, surface):
    module = _attribution_module()
    payload = {
        "metadata": {
            "requester_custom_headers": {"x-rg-client-originator": originator},
            "user_agent": "codex_cli_rs/0.145.0",
        }
    }

    attribution = module.from_litellm_payload(payload)

    assert attribution.product == "codex"
    assert attribution.surface == surface


def test_litellm_attribution_does_not_guess_codex_surface_from_ambiguous_user_agent():
    module = _attribution_module()
    payload = {"metadata": {"user_agent": "codex_cli_rs/0.145.0"}}

    attribution = module.from_litellm_payload(payload)

    assert attribution.product == "codex"
    assert attribution.surface == "unknown"
    assert attribution.ingest_path == "litellm_proxy"


@pytest.mark.parametrize(
    ("source", "surface"),
    [
        ("claude_desktop", "desktop"),
        ("claude_remote_control", "remote_control"),
    ],
)
def test_claude_transcript_attribution_uses_validated_envelope_source(source, surface):
    module = _attribution_module()

    attribution = module.from_claude_envelope({"source": source})

    assert attribution.product == "claude"
    assert attribution.surface == surface
    assert attribution.ingest_path == "claude_transcript"
