"""Offline tests for the MediaFlow gateway/relay player."""

from __future__ import annotations

import pytest
from fastapi.responses import HTMLResponse
from starlette.requests import Request

from mediaflow_proxy.extractors.embed_hosts import EMBED_HOSTS


def _request(path: str = "/embed/relay/auto/tv/94997/1/1", query: str = "autoplay=true") -> Request:
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


async def _vidcore(*args, **kwargs):
    return EMBED_HOSTS["VidCore"]


@pytest.mark.asyncio
async def test_relay_translates_streamflix_tv_url_to_provider_url(monkeypatch):
    from mediaflow_proxy.routes import relay as relay_routes

    monkeypatch.setattr(relay_routes, "_pick_auto_config", _vidcore)
    response = await relay_routes.relay_response(
        _request(), "auto", "tv", "tv-94997", "1", "1"
    )
    body = response.body.decode()

    assert "https://vidcore.org/embed/tv/94997/1/1" in body
    assert "https://vidcore.org/embed/tv/94997/1/1?autoplay=true" in body
    assert "/embed/relay/vidcore/tv/94997/1/1" in body
    assert '"hostUrl": "https://vidcore.org/embed/tv/94997/1/1?autoplay=true&lang=id&theme=FCC217"' in body
    assert "var target = OPTIONS[index].hostUrl;" in body
    assert "frame.src = target;" in body
    assert "frame.src = OPTIONS[index].url" not in body
    assert "api_password" not in body
    assert "__MFP_" not in body


def test_relay_bridge_exposes_streamflix_contract():
    from mediaflow_proxy.routes.relay import _relay_html

    options = [
        {
            "id": "vidcore",
            "name": "VidCore",
            "url": "https://mfp.example/embed/relay/vidcore/tv/94997/1/1",
            "hostUrl": "https://vidcore.org/embed/tv/94997/1/1",
            "selected": True,
        }
    ]
    body = _relay_html(
        "VidCore — 94997",
        "https://vidcore.org/embed/tv/94997/1/1",
        options,
        "1",
        "1",
        True,
    ).body.decode()

    assert 'src="https://vidcore.org/embed/tv/94997/1/1"' in body
    assert "OPLAY_EMBED_READY" in body
    assert "OPLAY_EMBED_PROGRESS" in body
    assert "OPLAY_EMBED_ERROR" in body
    assert "MEDIA_NOT_FOUND" in body
    assert "TV_SET" in body
    assert 'id="next"' in body
    assert "var target = OPTIONS[index].hostUrl;" in body
    assert "frame.src = target;" in body
    assert "frame.src = OPTIONS[index].url" not in body
    assert "window.location.href = target" in body


def test_relay_preflight_error_offers_next_host():
    from mediaflow_proxy.routes.relay import _relay_html

    options = [
        {
            "id": "vidcore",
            "name": "VidCore",
            "url": "https://mfp.example/embed/relay/vidcore/tv/293610/1/1",
            "hostUrl": "https://vidcore.org/embed/tv/293610/1/1",
            "selected": True,
        }
    ]
    body = _relay_html(
        "VidCore — 293610",
        "https://vidcore.org/embed/tv/293610/1/1",
        options,
        "1",
        "1",
        True,
        "VidCore tidak menyediakan episode ini.",
    ).body.decode()

    assert "VidCore tidak menyediakan episode ini." in body
    assert "INITIAL_ERROR" in body
    assert "post('OPLAY_EMBED_ERROR'" in body
    assert 'id="next"' in body


@pytest.mark.asyncio
async def test_auto_skips_provider_that_reports_missing_episode(monkeypatch):
    from mediaflow_proxy.routes import relay as relay_routes

    async def reachable(config, *_args, **_kwargs):
        return config.canonical in ("VidCore", "VSEmbed", "MultiEmbed")

    async def has_episode(config, *_args, **_kwargs):
        # VidCore's embed page is 200 but VidSpark says "Episode not found";
        # VSEmbed's gate has no usable player either.
        return config.canonical == "MultiEmbed"

    monkeypatch.setattr(relay_routes, "_relay_reachable", reachable)
    monkeypatch.setattr(relay_routes, "_relay_has_episode", has_episode)

    chosen = await relay_routes._pick_auto_config("tv", "293610", "1", "1")
    assert chosen.canonical == "MultiEmbed"


def test_relay_translates_host_specific_url_shapes():
    from mediaflow_proxy.extractors.embed_hosts import build_embed_url

    config = EMBED_HOSTS["TwoEmbed"]
    url = build_embed_url(config, "tv", "94997", "2", "5")
    assert url == "https://www.2embed.cc/embedtv/94997&s=2&e=5"

    config = EMBED_HOSTS["VidCore"]
    url = build_embed_url(config, "tv", "94997", "1", "1")
    assert url.startswith("https://vidcore.org/embed/tv/94997/1/1")


@pytest.mark.asyncio
async def test_native_route_can_force_gateway_mode(monkeypatch):
    from mediaflow_proxy.routes import embed as embed_routes
    from mediaflow_proxy.routes import relay as relay_routes

    async def fake_relay(request, slug, media_type, media_id, season, episode):
        return HTMLResponse(f"relay:{slug}:{media_type}:{media_id}:{season}:{episode}")

    monkeypatch.setattr(relay_routes, "relay_response", fake_relay)
    request = _request("/embed/auto/tv/94997/1/1", "mode=relay&autoplay=true")

    response = await embed_routes._serve(request, "auto", "tv", "94997", "1", "1")
    assert response.body.decode() == "relay:auto:tv:94997:1:1"


@pytest.mark.asyncio
async def test_extraction_failure_falls_back_to_gateway_player(monkeypatch):
    from mediaflow_proxy.routes import embed as embed_routes
    from mediaflow_proxy.routes import relay as relay_routes

    embed_routes._negative_cache.clear()

    async def fail_probe(request, config, proxy_headers, media_type, media_id, season, episode):
        return config, None, f"{config.canonical}: extraction unavailable"

    monkeypatch.setattr(embed_routes, "_probe_config", fail_probe)
    monkeypatch.setattr(relay_routes, "_pick_auto_config", _vidcore)

    request = _request("/embed/auto/tv/94997/1/1", "autoplay=true")
    response = await embed_routes._serve(request, "auto", "tv", "94997", "1", "1")

    assert response.status_code == 200
    assert "https://vidcore.org/embed/tv/94997/1/1" in response.body.decode()
