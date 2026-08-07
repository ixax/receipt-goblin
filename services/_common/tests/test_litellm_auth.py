import io

from common import litellm_auth


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def test_virtual_key_info_unsuccess_rejects_empty_info_object(monkeypatch):
    monkeypatch.setattr(
        litellm_auth.urllib.request,
        "urlopen",
        lambda request, timeout: _Response(b'{"info": {}}'),
    )

    assert litellm_auth.virtual_key_info("key", "http://litellm", "master") is None


def test_virtual_key_info_unsuccess_rejects_invalid_expiry(monkeypatch):
    monkeypatch.setattr(
        litellm_auth.urllib.request,
        "urlopen",
        lambda request, timeout: _Response(b'{"info": {"key_name": "key", "expires": "invalid"}}'),
    )

    assert litellm_auth.virtual_key_info("key", "http://litellm", "master") is None


def test_team_alias_success_caches_the_resolved_name(monkeypatch):
    litellm_auth._TEAM_ALIAS_CACHE.clear()
    calls = []

    def urlopen(request, timeout):
        calls.append(request.full_url)
        return _Response(b'{"team_info": {"team_alias": "win-hub"}}')

    monkeypatch.setattr(litellm_auth.urllib.request, "urlopen", urlopen)

    assert litellm_auth.team_alias("team-1", "http://litellm", "master") == "win-hub"
    assert litellm_auth.team_alias("team-1", "http://litellm", "master") == "win-hub"
    assert len(calls) == 1


def test_team_alias_unsuccess_does_not_cache_a_failed_lookup(monkeypatch):
    litellm_auth._TEAM_ALIAS_CACHE.clear()
    calls = []

    def urlopen(request, timeout):
        calls.append(request.full_url)
        raise litellm_auth.urllib.error.URLError("litellm down")

    monkeypatch.setattr(litellm_auth.urllib.request, "urlopen", urlopen)

    assert litellm_auth.team_alias("team-1", "http://litellm", "master") == ""
    assert litellm_auth.team_alias("team-1", "http://litellm", "master") == ""
    assert len(calls) == 2


def test_team_alias_unsuccess_returns_empty_without_a_team_id(monkeypatch):
    def urlopen(request, timeout):
        raise AssertionError("must not call litellm without a team id")

    monkeypatch.setattr(litellm_auth.urllib.request, "urlopen", urlopen)

    assert litellm_auth.team_alias("", "http://litellm", "master") == ""
