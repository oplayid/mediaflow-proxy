"""Offline tests for the output-only JSON gateway."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import time

import pytest
from starlette.requests import Request

from mediaflow_proxy.extractors.embed_hosts import EMBED_HOSTS


def _request(path: str = "/gateway/resolve/tv/94997/1/1", query: str = "") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "server": ("mfp.example", 443),
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.encode(),
            "headers": [(b"host", b"mfp.example")],
        }
    )


def _result() -> dict:
    return {
        "destination_url": "https://cdn.example/hls/master.m3u8?e=9999999999",
        "request_headers": {"Referer": "https://provider.example/"},
        "sources": [
            {"id": "auto", "label": "Auto", "url": "https://cdn.example/hls/master.m3u8"}
        ],
        "subtitles": [
            {
                "id": "sub-id-0",
                "label": "Indonesian",
                "language": "id",
                "url": "https://cdn.example/subs/id.vtt",
            }
        ],
        "capabilities": {"subtitles_from_api": True},
        "expiration": 120,
    }


@pytest.fixture(autouse=True)
def _clean_caches(monkeypatch):
    from mediaflow_proxy.routes import embed as embed_routes

    embed_routes._negative_cache.clear()
    embed_routes._resolved_cache.clear()
    monkeypatch.setattr(embed_routes, "AUTO_HOST_ORDER", ["VidCore"])
    # Signing needs a Starlette application scope for url_for(); this suite is
    # about gateway/cache behaviour, so keep the signed URL deterministic.
    monkeypatch.setattr(
        embed_routes,
        "_sign_asset_url",
        lambda *_args, **_kwargs: "https://mfp.example/proxy/hls/token.m3u8",
    )
    yield
    embed_routes._negative_cache.clear()
    embed_routes._resolved_cache.clear()


async def _resolve(request: Request, media_type="tv", media_id="94997", season="1", episode="1"):
    from mediaflow_proxy.routes import embed as embed_routes

    response = await embed_routes._gateway_response(
        request, media_type, media_id, season, episode
    )
    return response, json.loads(response.body)


@pytest.mark.asyncio
async def test_gateway_extracts_once_then_reuses_cache(monkeypatch):
    from mediaflow_proxy.routes import embed as embed_routes

    calls: list[str] = []

    async def fake_extract(request, config, proxy_headers, media_type, media_id, season, episode):
        calls.append(config.canonical)
        return _result()

    monkeypatch.setattr(embed_routes, "_extract", fake_extract)
    request = _request()

    first, first_body = await _resolve(request)
    second, second_body = await _resolve(request)

    assert calls == ["VidCore"]
    assert first.status_code == 200 == second.status_code
    assert first_body == second_body
    assert first_body["stream_url"] == "https://mfp.example/proxy/hls/token.m3u8"
    assert first_body["media"] == {"type": "tv", "id": "94997", "season": "1", "episode": "1"}
    assert first_body["subtitles"][0]["language"] == "id"
    assert "cdn.example" not in second.body.decode()
    assert "vidcore.org" not in second.body.decode()


@pytest.mark.asyncio
async def test_retry_forces_a_fresh_resolution(monkeypatch):
    from mediaflow_proxy.routes import embed as embed_routes

    calls: list[str] = []

    async def fake_extract(request, config, proxy_headers, media_type, media_id, season, episode):
        calls.append(config.canonical)
        return _result()

    monkeypatch.setattr(embed_routes, "_extract", fake_extract)

    await _resolve(_request())
    await _resolve(_request(query="retry=1"))

    assert calls == ["VidCore", "VidCore"]


@pytest.mark.asyncio
async def test_season_and_episode_have_separate_cache_keys(monkeypatch):
    from mediaflow_proxy.routes import embed as embed_routes

    calls: list[str] = []

    async def fake_extract(request, config, proxy_headers, media_type, media_id, season, episode):
        calls.append(f"{season}/{episode}")
        return _result()

    monkeypatch.setattr(embed_routes, "_extract", fake_extract)

    await _resolve(_request(), episode="1")
    await _resolve(_request(), episode="2")
    await _resolve(_request(), episode="1")

    assert calls == ["1/1", "1/2"]


@pytest.mark.asyncio
async def test_gateway_never_exposes_api_password(monkeypatch):
    from mediaflow_proxy.configs import settings
    from mediaflow_proxy.routes import embed as embed_routes

    monkeypatch.setattr(settings, "api_password", "super-secret")
    monkeypatch.setattr(
        embed_routes,
        "_sign_asset_url",
        lambda *_args, **_kwargs: "https://mfp.example/proxy/hls/encrypted-token",
    )

    async def fake_extract(*_args, **_kwargs):
        return _result()

    monkeypatch.setattr(embed_routes, "_extract", fake_extract)
    _, body = await _resolve(_request())

    assert "api_password" not in body
    assert "super-secret" not in str(body)
    assert body["audit"]["hls_filter_enabled"] is True
    assert body["expires_in"] > 0


@pytest.mark.asyncio
async def test_sandbox_usage_is_reported_in_audit(monkeypatch):
    from mediaflow_proxy.routes import embed as embed_routes

    async def fake_extract(*_args, **_kwargs):
        result = _result()
        result["sandbox_used"] = True
        return result

    monkeypatch.setattr(embed_routes, "_extract", fake_extract)
    _, body = await _resolve(_request())

    assert body["audit"]["sandbox_used"] is True


@pytest.mark.asyncio
async def test_server_query_cannot_select_a_provider(monkeypatch):
    from mediaflow_proxy.routes import embed as embed_routes

    chosen: list[str] = []

    async def fake_extract(request, config, *_args, **_kwargs):
        if config.canonical != "VidCore":
            await asyncio.sleep(0.05)
        chosen.append(config.canonical)
        return _result()

    monkeypatch.setattr(embed_routes, "_extract", fake_extract)
    monkeypatch.setattr(embed_routes, "AUTO_HOST_ORDER", ["VidCore", "VSEmbed"])

    await _resolve(_request(query="server=vsembed&mode=relay"))

    assert chosen == ["VidCore"]


def test_gateway_metadata_uses_long_token_expiration(monkeypatch):
    from mediaflow_proxy.routes import embed as embed_routes

    seen: list[int | None] = []

    def fake_sign(request, result, proxy_headers, destination_url, **kwargs):
        seen.append(kwargs.get("expiration"))
        return "https://mfp.example/proxy/hls/manifest.m3u8"

    monkeypatch.setattr(embed_routes, "_sign_asset_url", fake_sign)
    embed_routes._signed_metadata(_request(), _result(), None, expiration=7200)

    assert seen == [7200, 7200]


def test_upstream_expiry_is_respected_and_capped():
    from mediaflow_proxy.routes.embed import RESOLVE_CACHE_TTL, _upstream_ttl

    soon = int(time.time()) + 1000
    ttl = _upstream_ttl({"destination_url": f"https://cdn.example/master.m3u8?e={soon}"})
    assert 0 < ttl <= 1000
    assert _upstream_ttl({"destination_url": "https://cdn.example/master.m3u8"}) == RESOLVE_CACHE_TTL


def test_gateway_routes_are_registered_and_legacy_endpoints_are_gone():
    from mediaflow_proxy.main import app

    paths = [getattr(route, "path", "") for route in app.routes]
    for path in (
        "/gateway/resolve/movie/{media_id}",
        "/gateway/resolve/tv/{media_id}",
        "/gateway/resolve/tv/{media_id}/{season}",
        "/gateway/resolve/tv/{media_id}/{season}/{episode}",
        "/gateway/cache",
    ):
        assert path in paths
    for path in (
        "/embed/relay",
        "/embed/relay/hosts",
        "/embed/servers",
        "/embed/direct/{slug}/tv/{media_id}/{season}/{episode}",
        "/embed/{slug}/tv/{media_id}/{season}/{episode}",
    ):
        assert path not in paths
    assert importlib.util.find_spec("mediaflow_proxy.routes.relay") is None
    assert EMBED_HOSTS["VidCore"].slug == "vidcore"


def test_tv_params_are_normalised_for_streamflix_ids():
    from mediaflow_proxy.routes.embed import _normalise_media

    assert _normalise_media("tv", "tv-1399", "0", "oops") == ("1399", "1", "1")
    assert _normalise_media("tv", "94997", "2", "5") == ("94997", "2", "5")
    assert _normalise_media("movie", "550", None, None) == ("550", None, None)
