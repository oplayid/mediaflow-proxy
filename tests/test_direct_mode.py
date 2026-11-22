"""Offline tests for the resolve-once direct player mode."""

from __future__ import annotations

import time

import pytest
from starlette.requests import Request

from mediaflow_proxy.extractors.embed_hosts import EMBED_HOSTS


def _request(path: str = "/embed/auto/tv/94997/1/1", query: str = "autoplay=true") -> Request:
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
    # Signing needs a Starlette application scope for url_for(); this suite is
    # about cache behaviour, so keep the signed URL deterministic.
    monkeypatch.setattr(
        embed_routes,
        "_sign_asset_url",
        lambda *_args, **_kwargs: "https://mfp.example/proxy/hls/manifest.m3u8",
    )
    yield
    embed_routes._negative_cache.clear()
    embed_routes._resolved_cache.clear()


@pytest.mark.asyncio
async def test_direct_mode_extracts_once_then_reuses_cache(monkeypatch):
    from mediaflow_proxy.routes import embed as embed_routes

    calls: list[str] = []

    async def fake_extract(request, config, proxy_headers, media_type, media_id, season, episode):
        calls.append(config.canonical)
        return _result()

    monkeypatch.setattr(embed_routes, "_extract", fake_extract)
    request = _request()

    first = await embed_routes._serve(request, "vidcore", "tv", "94997", "1", "1")
    second = await embed_routes._serve(request, "vidcore", "tv", "94997", "1", "1")

    assert calls == ["VidCore"]
    assert first.status_code == 200 == second.status_code
    body = second.body.decode()
    assert "mfp.example/proxy/hls/manifest.m3u8" in body
    assert "vidcore.org" not in body
    assert "Indonesian" in body


@pytest.mark.asyncio
async def test_retry_forces_a_fresh_resolution(monkeypatch):
    from mediaflow_proxy.routes import embed as embed_routes

    calls: list[str] = []

    async def fake_extract(request, config, proxy_headers, media_type, media_id, season, episode):
        calls.append(config.canonical)
        return _result()

    monkeypatch.setattr(embed_routes, "_extract", fake_extract)

    await embed_routes._serve(_request(), "vidcore", "tv", "94997", "1", "1")
    await embed_routes._serve(
        _request(query="autoplay=true&retry=1"), "vidcore", "tv", "94997", "1", "1"
    )

    assert calls == ["VidCore", "VidCore"]


@pytest.mark.asyncio
async def test_season_and_episode_have_separate_cache_keys(monkeypatch):
    from mediaflow_proxy.routes import embed as embed_routes

    calls: list[str] = []

    async def fake_extract(request, config, proxy_headers, media_type, media_id, season, episode):
        calls.append(f"{season}/{episode}")
        return _result()

    monkeypatch.setattr(embed_routes, "_extract", fake_extract)

    await embed_routes._serve(_request(), "vidcore", "tv", "94997", "1", "1")
    await embed_routes._serve(_request(), "vidcore", "tv", "94997", "1", "2")
    await embed_routes._serve(_request(), "vidcore", "tv", "94997", "1", "1")

    assert calls == ["1/1", "1/2"]


def test_direct_metadata_uses_long_token_expiration(monkeypatch):
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


def test_direct_routes_are_registered_before_generic_embed_routes():
    from mediaflow_proxy.main import app

    paths = [getattr(route, "path", "") for route in app.routes]
    direct = "/embed/direct/{slug}/tv/{media_id}/{season}/{episode}"
    generic = "/embed/{slug}/tv/{media_id}/{season}/{episode}"
    assert direct in paths
    assert paths.index(direct) < paths.index(generic)
    assert "/embed/cache" in paths
    assert EMBED_HOSTS["VidCore"].slug == "vidcore"
