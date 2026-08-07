from common.model_pricing import ModelPricingResolver


def test_model_pricing_resolver_success_caches_live_cost_map():
    calls = []

    def fetch(url, timeout):
        calls.append((url, timeout))
        return {"claude-opus-5": {"input_cost_per_token": 5e-6}}

    resolver = ModelPricingResolver(
        "http://litellm:4000/",
        fetch_cost_map=fetch,
    )

    first = resolver.pricing_for("claude-opus-5")
    second = resolver.pricing_for("claude-opus-5")

    assert first == {"input_cost_per_token": 5e-6}
    assert second == first
    assert calls == [("http://litellm:4000/public/litellm_model_cost_map", 3)]


def test_model_pricing_resolver_unsuccess_unknown_model_returns_none():
    resolver = ModelPricingResolver(
        "http://litellm:4000",
        fetch_cost_map=lambda url, timeout: {},
    )

    assert resolver.pricing_for("missing-model") is None


def test_model_pricing_resolver_unsuccess_backs_off_after_refresh_failure():
    calls = []

    def fetch(url, timeout):
        calls.append((url, timeout))
        raise OSError("offline")

    resolver = ModelPricingResolver(
        "http://litellm:4000",
        fetch_cost_map=fetch,
    )

    assert resolver.pricing_for("claude-opus-5") is None
    assert resolver.pricing_for("claude-opus-5") is None
    assert len(calls) == 1
