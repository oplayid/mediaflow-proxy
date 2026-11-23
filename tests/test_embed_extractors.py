"""Offline tests for the StreamFlix-compatible embed host registry/flows."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.requests import Request

from mediaflow_proxy.extractors.base import ExtractorError, HttpResponse
from mediaflow_proxy.extractors.embed_hosts import (
    HOST_ORDER,
    EMBED_HOSTS,
    VIDSPARK_API_BASE,
    anime_variant_candidates,
    build_embed_url,
    canonicalize_host,
    is_public_http_url,
    parse_media_path,
)
from mediaflow_proxy.extractors import generic_embed
from mediaflow_proxy.extractors.factory import ExtractorFactory
from mediaflow_proxy.extractors.generic_embed import GenericEmbedExtractor
from mediaflow_proxy.schemas import ExtractorURLParams


def _response(url: str, *, text: str = "") -> HttpResponse:
    return HttpResponse(
        status=200,
        headers={"content-type": "text/plain"},
        text=text,
        content=text.encode("utf-8"),
        url=url,
    )


def _mock_requests(extractor: GenericEmbedExtractor, responses: dict[str, HttpResponse]):
    async def fake_request(url: str, **kwargs) -> HttpResponse:
        if url not in responses:
            raise AssertionError(f"Unexpected request: {url}")
        return responses[url]

    extractor._make_request = fake_request  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Registry / canonicalization
# ---------------------------------------------------------------------------

def test_vidspark_api_base_includes_v1():
    assert VIDSPARK_API_BASE == "https://vidspark.to/api/vidora/v1"


def test_host_order_is_the_single_first_success_order():
    from mediaflow_proxy.player.simulator import host_configs

    assert HOST_ORDER == ("VidCore", "VSEmbed", "VidSrcTo", "VidLink")
    assert len(HOST_ORDER) == len(set(HOST_ORDER))
    assert set(HOST_ORDER) <= set(EMBED_HOSTS)
    assert [config.canonical for config in host_configs()] == list(HOST_ORDER)


def test_gateway_does_not_serve_a_player_or_provider_registry():
    from mediaflow_proxy.main import app
    from mediaflow_proxy.routes import embed as embed_routes

    assert not hasattr(embed_routes, "_player_html")
    paths = [getattr(route, "path", "") for route in app.routes]
    assert not any(path.startswith("/embed/") for path in paths)
    assert all(path.startswith("/gateway/") for path in paths if "resolve" in path)


def test_all_historical_embed_hosts_are_registered():
    expected = {
        "VSEmbed",
        "VidCore",
        "VidKing",
        "AutoEmbed",
        "TwoEmbed",
        "PrimeWire",
        "MultiEmbed",
        "VidEasy",
        "SmashyStream",
        "PStream",
        "VidSrcCC",
        "VidSrcTo",
        "VidSrcRip",
        "VidSrcSu",
        "VidSrcVip",
        "EmbedSu",
        "Frembed",
        "MoviesAPI",
        "VidLink",
    }
    assert expected <= set(EMBED_HOSTS)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("VSEmbed", "VSEmbed"),
        ("vsembed", "VSEmbed"),
        ("VSEMBED", "VSEmbed"),
        ("stream.oplay.id", "VSEmbed"),
        ("STREAM.OPLAY.ID", "VSEmbed"),
        ("vidcore", "VidCore"),
        ("vidspark.to", "VidCore"),
        ("VidLink", "VidLink"),
        ("vidlink", "VidLink"),
        ("vidlink.pro", "VidLink"),
        ("www.vidlink.pro", "VidLink"),
        ("2embed", "TwoEmbed"),
    ],
)
def test_canonicalize_host(value: str, expected: str):
    assert canonicalize_host(value) == expected


def test_schema_normalizes_host():
    params = ExtractorURLParams(host="vidcore", d="https://vidcore.org/embed/movie/550")
    assert params.host == "VidCore"


def test_build_embed_url_tv():
    config = EMBED_HOSTS["VidCore"]
    assert (
        build_embed_url(config, "tv", "1399", 1, 2)
        == "https://vidcore.org/embed/tv/1399/1/2?autoplay=true&lang=id&theme=FCC217"
    )


def test_ssrf_guard_rejects_private_addresses():
    assert is_public_http_url("https://vidcore.org/embed/movie/550")
    assert not is_public_http_url("http://127.0.0.1/embed/movie/550")
    assert not is_public_http_url("ftp://vidcore.org/embed/movie/550")


@pytest.mark.parametrize("host", ["VidCore", "vidcore", "VIDCORE", "vidspark.to"])
def test_factory_resolves_embed_host_case_insensitively(host: str):
    extractor = ExtractorFactory.get_extractor(host, {})
    assert isinstance(extractor, GenericEmbedExtractor)
    assert extractor.config.canonical == "VidCore"


# ---------------------------------------------------------------------------
# Upstream flows
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_vidspark_flow():
    config = EMBED_HOSTS["VidCore"]
    extractor = GenericEmbedExtractor({}, config)
    api_url = f"{VIDSPARK_API_BASE}/movie/550"
    destination = "https://paretocdn.site/hls2/x/master.m3u8?t=1"
    hd_url = "https://paretocdn.site/hls2/x/1080.m3u8?t=1"
    en_url = "https://paretocdn.site/subtitles/en.vtt"
    id_url = "https://paretocdn.site/subtitles/id.vtt"
    embed_url = "https://vidcore.org/embed/movie/550?autoplay=true&lang=id&theme=FCC217"
    _mock_requests(
        extractor,
        {
            api_url: _response(
                api_url,
                text=json.dumps(
                    {
                        "result": True,
                        "sources": [
                            {"url": destination, "quality": "auto"},
                            {"url": hd_url, "quality": "1080p"},
                        ],
                        "subtitles": [
                            {"url": en_url, "label": "English"},
                            {"url": id_url, "label": "Indonesian"},
                        ],
                    }
                ),
            )
        },
    )

    result = await extractor.extract(embed_url)

    assert result["destination_url"] == destination
    assert result["mediaflow_endpoint"] == "hls_manifest_proxy"
    assert result["request_headers"]["Referer"] == "https://vidspark.to/"
    assert [source["url"] for source in result["sources"]] == [destination, hd_url]
    assert result["sources"][1]["label"] == "1080p"
    assert result["subtitles"] == []
    assert [sub["language"] for sub in result["provider_subtitles"]] == ["id", "en"]
    assert [sub["url"] for sub in result["provider_subtitles"]] == [id_url, en_url]
    assert all(sub["origin"] == "provider" for sub in result["provider_subtitles"])
    assert result["capabilities"]["quality_from_api"] is True
    assert result["capabilities"]["subtitles_from_api"] is True


@pytest.mark.asyncio
async def test_vidspark_rejects_unsafe_subtitle_urls():
    config = EMBED_HOSTS["VidCore"]
    extractor = GenericEmbedExtractor({}, config)
    api_url = f"{VIDSPARK_API_BASE}/movie/550"
    destination = "https://paretocdn.site/hls2/x/master.m3u8?t=1"
    embed_url = "https://vidcore.org/embed/movie/550"
    _mock_requests(
        extractor,
        {
            api_url: _response(
                api_url,
                text=json.dumps(
                    {
                        "result": True,
                        "sources": [{"url": destination}],
                        "subtitles": [
                            {"url": "http://127.0.0.1/id.vtt", "label": "Indonesian"},
                            {"url": "https://paretocdn.site/id.vtt", "label": "Indonesian"},
                        ],
                    }
                ),
            )
        },
    )

    result = await extractor.extract(embed_url)

    assert [sub["url"] for sub in result["provider_subtitles"]] == [
        "https://paretocdn.site/id.vtt"
    ]


@pytest.mark.asyncio
async def test_vsembed_chain():
    config = EMBED_HOSTS["VSEmbed"]
    extractor = GenericEmbedExtractor({}, config)
    embed_url = "https://stream.oplay.id/embed/movie/550"
    gate_url = "https://stream.oplay.id/vs_src.php?type=movie&id=550"
    nested_url = "https://vsplayer.example/watch"
    player_url = "https://vsplayer.example/player.html"
    api_url = "https://vsapi.example/api.php?id=550&api_token=tok"
    destination = "https://cdn.example/master.m3u8"

    _mock_requests(
        extractor,
        {
            gate_url: _response(gate_url, text=f'{{"src":"{nested_url}"}}'),
            nested_url: _response(
                nested_url,
                text=f'<script>window.CFG={{"playerUrl":"{player_url}"}};</script>',
            ),
            player_url: _response(
                player_url,
                text=(
                    '<script>window.CONFIG={"api":"https://vsapi.example/api.php?id=550",'
                    '"apiToken":"tok","cast_urls":[]};</script>'
                ),
            ),
            api_url: _response(
                api_url,
                text=f'{{"data":{{"stream_urls":["{destination}"],"cast_urls":[]}}}}',
            ),
        },
    )

    result = await extractor.extract(embed_url)

    assert result["destination_url"] == destination
    assert result["request_headers"]["Referer"] == player_url
    assert result["expiration"] == 120


@pytest.mark.asyncio
async def test_vidsrc_to_follows_wrapper_and_source_gate():
    config = EMBED_HOSTS["VidSrcTo"]
    extractor = GenericEmbedExtractor({}, config)
    embed_url = "https://vidsrc.to/embed/movie/385687"
    inner_url = "https://vidsrc.sh/embed/movie/385687"
    gate_url = "https://vidsrc.sh/vs_src.php?type=movie&id=385687"
    nested_url = "https://vsplayer.example/watch"
    player_url = "https://vsplayer.example/player.html"
    api_url = "https://vsapi.example/api.php?id=385687&api_token=tok"
    destination = "https://cdn.example/master.m3u8"

    _mock_requests(
        extractor,
        {
            embed_url: _response(
                embed_url,
                text=f'<iframe src="{inner_url}" allowfullscreen></iframe>',
            ),
            inner_url: _response(
                inner_url,
                text='<iframe data-api="/vs_src.php?type=movie&amp;id=385687"></iframe>',
            ),
            gate_url: _response(gate_url, text=f'{{"src":"{nested_url}"}}'),
            nested_url: _response(
                nested_url,
                text=f'<script>window.CFG={{"playerUrl":"{player_url}"}};</script>',
            ),
            player_url: _response(
                player_url,
                text=(
                    '<script>window.CONFIG={"api":"https://vsapi.example/api.php?id=385687",'
                    '"apiToken":"tok","cast_urls":[]};</script>'
                ),
            ),
            api_url: _response(
                api_url,
                text=f'{{"data":{{"stream_urls":["{destination}"],"cast_urls":[]}}}}',
            ),
        },
    )

    result = await extractor.extract(embed_url)

    assert result["destination_url"] == destination
    assert result["request_headers"]["Referer"] == player_url


@pytest.mark.asyncio
async def test_vsembed_accepts_aliased_config_without_semicolon():
    config = EMBED_HOSTS["VSEmbed"]
    extractor = GenericEmbedExtractor({}, config)
    embed_url = "https://stream.oplay.id/embed/movie/550"
    gate_url = "https://stream.oplay.id/vs_src.php?type=movie&id=550"
    nested_url = "https://vsplayer.example/watch"
    player_url = "https://vsplayer.example/player.html"
    api_url = "https://vsapi.example/api.php?id=550&api_token=tok"
    destination = "https://cdn.example/master.m3u8"

    _mock_requests(
        extractor,
        {
            gate_url: _response(gate_url, text=f'{{"src":"{nested_url}"}}'),
            nested_url: _response(
                nested_url,
                # No trailing semicolon, nested object before playerUrl.
                text='<script>window.CFG={"meta":{"a":1},"playerUrl":"' + player_url + '"}</script>',
            ),
            player_url: _response(
                player_url,
                text=(
                    '<script>window.CONFIG={"apiUrl":"https://vsapi.example/api.php?id=550",'
                    '"token":"tok","extra":{"nested":true}}</script>'
                ),
            ),
            api_url: _response(
                api_url,
                text=f'{{"data":{{"stream_urls":["{destination}"],"cast_urls":[]}}}}',
            ),
        },
    )

    result = await extractor.extract(embed_url)

    assert result["destination_url"] == destination


@pytest.mark.asyncio
async def test_vsembed_tv_uses_stream_base_and_preserves_stream_urls_flag():
    config = EMBED_HOSTS["VSEmbed"]
    extractor = GenericEmbedExtractor({}, config)
    embed_url = "https://stream.oplay.id/embed/tv/293610/1/1"
    gate_url = "https://stream.oplay.id/vs_src.php?type=tv&id=293610&season=1&episode=1"
    nested_url = "https://vsplayer.example/watch"
    player_url = "https://vsplayer.example/player.html"
    api_url = (
        "https://vsapi.example/api.php?type=tv&tmdb=293610&stream_urls="
        "&season=1&episode=1&api_token=tok"
    )
    destination = "https://cdn.example/master.m3u8"
    subtitle_url = "https://cdn.example/id.vtt"

    _mock_requests(
        extractor,
        {
            gate_url: _response(gate_url, text=f'{{"src":"{nested_url}"}}'),
            nested_url: _response(
                nested_url,
                text=f'<script>window.CFG={{"playerUrl":"{player_url}"}};</script>',
            ),
            player_url: _response(
                player_url,
                text=(
                    '<script>window.CONFIG={"streamBase":'
                    '"https://vsapi.example/api.php?type=tv&tmdb=293610",'
                    '"apiToken":"tok"};</script>'
                ),
            ),
            api_url: _response(
                api_url,
                text=json.dumps(
                    {
                        "data": {
                            "stream_urls": [destination],
                            "default_subs": [
                                {"url": subtitle_url, "label": "Indonesian"}
                            ],
                        }
                    }
                ),
            ),
        },
    )

    result = await extractor.extract(embed_url)

    assert result["destination_url"] == destination
    assert [sub["url"] for sub in result["subtitles"]] == [subtitle_url]
    assert result["subtitles"][0]["language"] == "id"
    assert result.get("sandbox_used") is not True
    gate_query = parse_qs(urlparse(gate_url).query)
    api_query = parse_qs(urlparse(api_url).query)
    assert gate_query["season"] == ["1"]
    assert gate_query["episode"] == ["1"]
    assert api_query["season"] == ["1"]
    assert api_query["episode"] == ["1"]
    assert "s" not in api_query and "e" not in api_query


@pytest.mark.asyncio
async def test_vsembed_refreshes_missing_api_token():
    """Gated player pages get a second fetch, like the upstream browser does."""
    config = EMBED_HOSTS["VSEmbed"]
    extractor = GenericEmbedExtractor({}, config)
    embed_url = "https://stream.oplay.id/embed/tv/293610/1/4"
    gate_url = "https://stream.oplay.id/vs_src.php?type=tv&id=293610&season=1&episode=4"
    nested_url = "https://vsplayer.example/watch"
    player_url = "https://vsplayer.example/player.html"
    stream_base = "https://vsapi.example/api.php?type=tv&tmdb=293610"
    api_url = (
        f"{stream_base}&stream_urls=&season=1&episode=4&api_token=tok"
    )
    destination = "https://cdn.example/master.m3u8"

    responses = {
        gate_url: _response(gate_url, text=f'{{"src":"{nested_url}"}}'),
        nested_url: _response(
            nested_url,
            text=f'<script>window.CFG={{"playerUrl":"{player_url}"}};</script>',
        ),
        api_url: _response(
            api_url,
            text=json.dumps({"data": {"stream_urls": [destination], "cast_urls": []}}),
        ),
    }
    player_calls = {"count": 0}
    gated_config = {
        "streamBase": stream_base,
        "apiTokenRefresh": True,
        "castGate": True,
    }
    refreshed_config = {
        "streamBase": stream_base,
        "apiToken": "tok",
        "apiTokenRefresh": True,
    }

    async def fake_request(url: str, **kwargs):
        if url == player_url:
            player_calls["count"] += 1
            payload = gated_config if player_calls["count"] == 1 else refreshed_config
            return _response(player_url, text=f"<script>window.CONFIG={json.dumps(payload)};</script>")
        if url not in responses:
            raise AssertionError(f"Unexpected request: {url}")
        return responses[url]

    extractor._make_request = fake_request  # type: ignore[method-assign]

    result = await extractor.extract(embed_url)

    assert player_calls["count"] == 2
    assert result["destination_url"] == destination


def _gated_vsembed_extractor():
    """Player page with a Turnstile gate and no token until a browser solves it."""
    config = EMBED_HOSTS["VSEmbed"]
    extractor = GenericEmbedExtractor({}, config)
    embed_url = "https://stream.oplay.id/embed/tv/293610/1/4"
    gate_url = "https://stream.oplay.id/vs_src.php?type=tv&id=293610&season=1&episode=4"
    nested_url = "https://vsplayer.example/watch"
    player_url = "https://vsplayer.example/player.html?episode=4"
    stream_base = "https://vsapi.example/api.php?type=tv&tmdb=293610"
    api_url = f"{stream_base}&stream_urls=&season=1&episode=4&api_token=tok"
    destination = "https://cdn.example/master.m3u8"
    gated_config = {
        "streamBase": stream_base,
        "apiToken": "",
        "apiTokenRefresh": True,
        "turnstile": True,
        "turnstileSiteKey": "site-key",
    }
    responses = {
        gate_url: _response(gate_url, text=f'{{"src":"{nested_url}"}}'),
        nested_url: _response(
            nested_url,
            text=f'<script>window.CFG={{"playerUrl":"{player_url}"}};</script>',
        ),
        api_url: _response(
            api_url,
            text=json.dumps({"data": {"stream_urls": [destination], "cast_urls": []}}),
        ),
    }

    async def fake_request(url: str, **kwargs):
        if url == player_url:
            return _response(
                player_url,
                text=f"<script>window.CONFIG={json.dumps(gated_config)};</script>",
            )
        if url not in responses:
            raise AssertionError(f"Unexpected request: {url}")
        return responses[url]

    extractor._make_request = fake_request  # type: ignore[method-assign]
    return extractor, embed_url, player_url, nested_url, destination


@pytest.mark.asyncio
async def test_vsembed_gated_uses_headless_fallback_when_enabled(monkeypatch):
    from mediaflow_proxy.configs import settings

    extractor, embed_url, player_url, nested_url, destination = _gated_vsembed_extractor()
    monkeypatch.setattr(settings, "enable_headless_fallback", True)
    calls = []

    async def fake_resolve(url, *, referer, timeout, cache_key):
        calls.append((url, referer, cache_key))
        return {
            "streamBase": "https://vsapi.example/api.php?type=tv&tmdb=293610",
            "apiToken": "tok",
        }

    monkeypatch.setattr(generic_embed, "resolve_gated_config", fake_resolve)

    result = await extractor.extract(embed_url)

    assert result["destination_url"] == destination
    assert len(calls) == 1
    assert calls[0][0] == player_url
    assert calls[0][1] == nested_url
    assert "293610:1:4" in calls[0][2]


@pytest.mark.asyncio
async def test_vsembed_gated_fails_cleanly_when_fallback_disabled(monkeypatch):
    from mediaflow_proxy.configs import settings

    extractor, embed_url, *_ = _gated_vsembed_extractor()
    monkeypatch.setattr(settings, "enable_headless_fallback", False)
    calls = []

    async def fake_resolve(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(generic_embed, "resolve_gated_config", fake_resolve)

    with pytest.raises(ExtractorError, match="playback gated"):
        await extractor.extract(embed_url)
    assert calls == []


@pytest.mark.asyncio
async def test_vsembed_gated_fails_cleanly_when_fallback_has_no_token(monkeypatch):
    from mediaflow_proxy.configs import settings

    extractor, embed_url, *_ = _gated_vsembed_extractor()
    monkeypatch.setattr(settings, "enable_headless_fallback", True)

    async def fake_resolve(*args, **kwargs):
        return None

    monkeypatch.setattr(generic_embed, "resolve_gated_config", fake_resolve)

    with pytest.raises(ExtractorError, match="playback gated"):
        await extractor.extract(embed_url)


@pytest.mark.asyncio
async def test_headless_helper_is_noop_when_disabled(monkeypatch):
    from mediaflow_proxy.configs import settings
    from mediaflow_proxy.utils import headless_browser

    monkeypatch.setattr(settings, "enable_headless_fallback", False)
    assert (
        await headless_browser.resolve_gated_config(
            "https://vsplayer.example/player.html", referer="https://vsplayer.example/"
        )
        is None
    )


@pytest.mark.asyncio
async def test_vsembed_cast_playlist_falls_back_to_browser_sandbox(monkeypatch):
    config = EMBED_HOSTS["VSEmbed"]
    extractor = GenericEmbedExtractor({}, config)
    embed_url = "https://stream.oplay.id/embed/movie/550"
    gate_url = "https://stream.oplay.id/vs_src.php?type=movie&id=550"
    nested_url = "https://vsplayer.example/watch"
    player_url = "https://vsplayer.example/player.html"
    api_url = "https://vsapi.example/api.php?id=550&api_token=tok"
    cast_url = "https://pool.example/plc/abc"
    playlist_url = "https://pool.example/pl/abc"
    token_url = "https://pool.example/generate.php?api_token=tok"
    tokenized_url = f"{playlist_url}?token=stream-token"

    _mock_requests(
        extractor,
        {
            token_url: _response(token_url, text="stream-token"),
            gate_url: _response(gate_url, text=f'{{"src":"{nested_url}"}}'),
            nested_url: _response(
                nested_url,
                text=f'<script>window.CFG={{"playerUrl":"{player_url}"}};</script>',
            ),
            player_url: _response(
                player_url,
                text=(
                    '<script>window.CONFIG={"api":"https://vsapi.example/api.php?id=550",'
                    '"apiToken":"tok"};</script>'
                ),
            ),
            api_url: _response(
                api_url,
                text=json.dumps(
                    {
                        "data": {
                            "stream_urls": "encrypted",
                            "cast_urls": [cast_url],
                        }
                    }
                ),
            ),
            tokenized_url: _response(tokenized_url, text="<html>challenge</html>"),
        },
    )

    async def fake_browser_fetch(url: str, referer: str):
        assert url == tokenized_url
        return "#EXTM3U\n#EXT-X-VERSION:3\n"

    monkeypatch.setattr(extractor, "_browser_fetch_playlist", fake_browser_fetch)

    result = await extractor.extract(embed_url)

    assert result["destination_url"] == tokenized_url
    assert result["sandbox_used"] is True


@pytest.mark.asyncio
async def test_vsembed_cast_playlist_plays_with_generate_php_token(monkeypatch):
    config = EMBED_HOSTS["VSEmbed"]
    extractor = GenericEmbedExtractor({}, config)
    embed_url = "https://stream.oplay.id/embed/movie/550"
    gate_url = "https://stream.oplay.id/vs_src.php?type=movie&id=550"
    nested_url = "https://vsplayer.example/watch"
    player_url = "https://vsplayer.example/player.html"
    api_url = "https://vsapi.example/api.php?id=550&api_token=tok"
    token_url = "https://pool.example/generate.php?api_token=tok"
    tokenized_url = "https://pool.example/pl/abc?token=stream-token"

    _mock_requests(
        extractor,
        {
            gate_url: _response(gate_url, text=f'{{"src":"{nested_url}"}}'),
            nested_url: _response(
                nested_url,
                text=f'<script>window.CFG={{"playerUrl":"{player_url}"}};</script>',
            ),
            player_url: _response(
                player_url,
                text=(
                    '<script>window.CONFIG={"api":"https://vsapi.example/api.php?id=550",'
                    '"apiToken":"tok"};</script>'
                ),
            ),
            api_url: _response(
                api_url,
                text=json.dumps(
                    {
                        "data": {
                            "stream_urls": "encrypted",
                            "cast_urls": ["https://pool.example/plc/abc"],
                        }
                    }
                ),
            ),
            token_url: _response(token_url, text='{"token":"stream-token"}'),
            tokenized_url: _response(tokenized_url, text="#EXTM3U\n#EXT-X-VERSION:3\n"),
        },
    )

    async def no_browser_fetch(*_args):
        raise AssertionError("browser sandbox should not be used for a playable playlist")

    monkeypatch.setattr(extractor, "_browser_fetch_playlist", no_browser_fetch)

    result = await extractor.extract(embed_url)

    assert result["destination_url"] == tokenized_url
    assert result.get("sandbox_used") is not True


@pytest.mark.asyncio
async def test_generic_iframe_flow():
    config = EMBED_HOSTS["VidKing"]
    extractor = GenericEmbedExtractor({}, config)
    embed_url = "https://www.vidking.net/embed/movie/550?color=e50914&autoPlay=true"
    frame_url = "https://frame.example/player"
    destination = "https://cdn.example/video.m3u8"

    _mock_requests(
        extractor,
        {
            embed_url: _response(
                embed_url,
                text=f'<iframe src="{frame_url}" allowfullscreen></iframe>',
            ),
            frame_url: _response(
                frame_url,
                text=f'<script>var player = {{ file: "{destination}" }};</script>',
            ),
        },
    )

    result = await extractor.extract(embed_url)

    assert result["destination_url"] == destination
    assert result["mediaflow_endpoint"] == "hls_manifest_proxy"


@pytest.mark.asyncio
async def test_host_allowlist_is_enforced():
    config = EMBED_HOSTS["VidCore"]
    extractor = GenericEmbedExtractor({}, config)

    with pytest.raises(Exception, match="not allowed"):
        await extractor.extract("https://evil.example/embed/movie/550")


# ---------------------------------------------------------------------------
# VidLink host: URL templates, anime sub/dub fallback, signed playback API
# ---------------------------------------------------------------------------


def test_vidlink_is_a_resolvable_auto_host():
    config = EMBED_HOSTS["VidLink"]
    assert config.slug == "vidlink"
    assert config.flow == "vidlink_api"
    assert config.endpoint == "proxy_stream_endpoint"
    assert "VidLink" in HOST_ORDER


def test_vidlink_embed_url_templates():
    config = EMBED_HOSTS["VidLink"]
    assert build_embed_url(config, "movie", "786892") == "https://vidlink.pro/movie/786892"
    assert (
        build_embed_url(config, "tv", "94997", 1, 1)
        == "https://vidlink.pro/tv/94997/1/1"
    )
    assert (
        build_embed_url(config, "anime", "40748", episode=1, variant="sub")
        == "https://vidlink.pro/anime/40748/1/sub"
    )
    assert (
        build_embed_url(config, "anime", "40748", episode=1, variant="dub")
        == "https://vidlink.pro/anime/40748/1/dub"
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://vidlink.pro/movie/786892",
            ("movie", "786892", None, None, None),
        ),
        (
            "https://vidlink.pro/tv/94997/1/1",
            ("tv", "94997", "1", "1", None),
        ),
        (
            "https://vidlink.pro/anime/40748/1/sub",
            ("anime", "40748", None, "1", "sub"),
        ),
        (
            "https://vidlink.pro/anime/40748/1/dub",
            ("anime", "40748", None, "1", "dub"),
        ),
        (
            "https://vidsrc.sh/embed/movie/385687",
            ("movie", "385687", None, None, None),
        ),
        (
            "https://vidsrc.sh/embed/tv/tt0944947/1-2",
            ("tv", "tt0944947", "1", "2", None),
        ),
    ],
)
def test_vidlink_parse_media_path(url: str, expected):
    assert parse_media_path(url) == expected


def test_anime_variant_candidates_falls_back():
    assert anime_variant_candidates("sub") == ("sub", "dub")
    assert anime_variant_candidates("dub") == ("dub", "sub")
    assert anime_variant_candidates(None) == ("sub", "dub")
    assert anime_variant_candidates("invalid") == ("sub", "dub")


def _vidlink_api_response(url: str) -> HttpResponse:
    payload = {
        "sourceId": "mbVault",
        "stream": {
            "id": "primary",
            "qualities": {
                "1080": {
                    "type": "mp4",
                    "url": "https://cdn.example/video-1080.mp4?sign=abc",
                    "headers": {"referer": "https://filmboom.top/"},
                },
                "720": {
                    "type": "mp4",
                    "url": "https://cdn.example/video-720.mp4?sign=def",
                },
            },
            "captions": [
                {
                    "url": "https://cdn.example/id.srt",
                    "language": "Indonesian",
                },
                {
                    "url": "https://cdn.example/en.srt",
                    "language": "English",
                },
            ],
        },
    }
    return _response(url, text=json.dumps(payload))


@pytest.mark.asyncio
async def test_vidlink_dash_uses_mpd_conversion_endpoint():
    config = EMBED_HOSTS["VidLink"]
    extractor = GenericEmbedExtractor({}, config)
    token = "C" * 64
    embed_url = f"https://vidlink.pro/movie/533535?adv_token={token}"
    api_url = f"https://vidlink.pro/api/b/movie/{token}?multiLang=0"
    mpd = "https://cdn.example/dash/1080_h265/index_web.mpd"
    payload = {
        "sourceId": "mbVault",
        "stream": {
            "id": "primary",
            "qualities": {"1080": {"type": "dash", "url": mpd}},
            "captions": [],
        },
    }

    _mock_requests(extractor, {api_url: _response(api_url, text=json.dumps(payload))})
    result = await extractor.extract(embed_url)

    assert result["destination_url"] == mpd
    assert result["mediaflow_endpoint"] == "mpd_manifest_proxy"


@pytest.mark.asyncio
async def test_vidlink_movie_playback_api():
    config = EMBED_HOSTS["VidLink"]
    extractor = GenericEmbedExtractor({}, config)
    token = "A" * 64
    embed_url = f"https://vidlink.pro/movie/786892?adv_token={token}"
    api_url = f"https://vidlink.pro/api/b/movie/{token}?multiLang=0"

    _mock_requests(extractor, {api_url: _vidlink_api_response(api_url)})
    result = await extractor.extract(embed_url)

    assert result["destination_url"] == "https://cdn.example/video-1080.mp4?sign=abc"
    assert result["mediaflow_endpoint"] == "proxy_stream_endpoint"
    assert len(result["sources"]) == 2  # both API qualities; destination matches the first
    assert [item["language"] for item in result["subtitles"]] == ["id", "en"]


@pytest.mark.asyncio
async def test_vidlink_uses_webkit_environment_and_keeps_source_headers():
    config = EMBED_HOSTS["VidLink"]
    extractor = GenericEmbedExtractor({}, config)
    token = "A" * 64
    embed_url = f"https://vidlink.pro/movie/786892?adv_token={token}"
    api_url = f"https://vidlink.pro/api/b/movie/{token}?multiLang=0"
    calls: list[dict] = []

    async def fake_request(url: str, **kwargs) -> HttpResponse:
        assert url == api_url
        calls.append(kwargs)
        return _vidlink_api_response(url)

    extractor._make_request = fake_request  # type: ignore[method-assign]
    result = await extractor.extract(embed_url)

    assert len(calls) == 1
    assert calls[0]["headers"]["X-Playback-Environment"] == "webkit"
    assert calls[0]["headers"]["Origin"] == "https://vidlink.pro"
    assert result["sources"][0]["request_headers"]["referer"] == "https://filmboom.top/"


@pytest.mark.asyncio
async def test_vidlink_falls_back_to_dash_hevc_when_webkit_has_no_stream():
    config = EMBED_HOSTS["VidLink"]
    extractor = GenericEmbedExtractor({}, config)
    token = "A" * 64
    embed_url = f"https://vidlink.pro/movie/786892?adv_token={token}"
    environments: list[str] = []

    async def fake_request(url: str, **kwargs) -> HttpResponse:
        environment = kwargs["headers"]["X-Playback-Environment"]
        environments.append(environment)
        if environment == "webkit":
            return _response(url, text=json.dumps({"stream": None}))
        return _vidlink_api_response(url)

    extractor._make_request = fake_request  # type: ignore[method-assign]
    result = await extractor.extract(embed_url)

    assert environments == ["webkit", "dash-hevc"]
    assert result["destination_url"] == "https://cdn.example/video-1080.mp4?sign=abc"


@pytest.mark.asyncio
async def test_vidlink_playlist_headers_reach_signed_source():
    from mediaflow_proxy.routes.embed import _headers_for_destination

    config = EMBED_HOSTS["VidLink"]
    extractor = GenericEmbedExtractor({}, config)
    token = "A" * 64
    embed_url = f"https://vidlink.pro/movie/786892?adv_token={token}"
    api_url = f"https://vidlink.pro/api/b/movie/{token}?multiLang=0"
    cookie = (
        "CloudFront-Policy=eyJpolicy; CloudFront-Signature=signed; "
        "CloudFront-Key-Pair-Id=K1EXAMPLE"
    )
    payload = {
        "stream": {
            "id": "primary",
            "url": "https://cdn.example/dash/index.mpd",
            "playlistHeaders": {
                "Cookie": cookie,
                "Referer": "https://vidlink.pro/",
            },
            "captions": [],
        }
    }

    _mock_requests(extractor, {api_url: _response(api_url, text=json.dumps(payload))})
    result = await extractor.extract(embed_url)

    assert result["destination_url"] == "https://cdn.example/dash/index.mpd"
    assert result["sources"][0]["request_headers"]["Cookie"] == cookie
    signed_headers = _headers_for_destination(result, result["destination_url"])
    assert signed_headers is not None
    assert signed_headers["Cookie"] == cookie


@pytest.mark.asyncio
async def test_vidlink_tv_playback_api_uses_season_episode():
    config = EMBED_HOSTS["VidLink"]
    extractor = GenericEmbedExtractor({}, config)
    token = "B" * 64
    embed_url = f"https://vidlink.pro/tv/94997/2/5?adv_token={token}"
    api_url = f"https://vidlink.pro/api/b/tv/{token}/2/5?multiLang=0"

    _mock_requests(extractor, {api_url: _vidlink_api_response(api_url)})
    result = await extractor.extract(embed_url)

    assert result["destination_url"] == "https://cdn.example/video-1080.mp4?sign=abc"


@pytest.mark.asyncio
async def test_vidlink_generates_adv_token_when_missing(monkeypatch):
    config = EMBED_HOSTS["VidLink"]
    extractor = GenericEmbedExtractor({}, config)
    token = "A" * 64
    api_url = f"https://vidlink.pro/api/b/movie/{token}?multiLang=0"
    monkeypatch.setattr(generic_embed, "generate_adv_token", lambda media_id: token)
    _mock_requests(extractor, {api_url: _vidlink_api_response(api_url)})

    result = await extractor.extract("https://vidlink.pro/movie/786892")

    assert result["destination_url"] == "https://cdn.example/video-1080.mp4?sign=abc"
    assert [sub["origin"] for sub in result["subtitles"]] == ["host", "host"]


@pytest.mark.asyncio
async def test_vidlink_anime_falls_back_from_sub_to_dub():
    config = EMBED_HOSTS["VidLink"]
    extractor = GenericEmbedExtractor({}, config)
    sub_url = "https://vidlink.pro/anime/40748/1/sub"
    dub_url = "https://vidlink.pro/anime/40748/1/dub"
    destination = "https://cdn.example/anime-dub.m3u8"

    _mock_requests(
        extractor,
        {
            sub_url: _response(sub_url, text="<html><body>no stream</body></html>"),
            dub_url: _response(
                dub_url,
                text=f'<script>var player = {{ file: "{destination}" }};</script>',
            ),
        },
    )
    result = await extractor.extract(sub_url)

    assert result["destination_url"] == destination
    assert result["mediaflow_endpoint"] == "proxy_stream_endpoint"


# ---------------------------------------------------------------------------
# StreamFlix TV/episode requests and bounded auto failover
# ---------------------------------------------------------------------------


def _embed_request(path: str = "/gateway/resolve/tv/1399/1/1", query: str = "") -> Request:
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


# ---------------------------------------------------------------------------
# Fetch-time ad filtering
# ---------------------------------------------------------------------------


def test_sanitize_html_removes_ad_scripts_iframes_and_redirects():
    from mediaflow_proxy.gateway.ad_filter import AdFilterReport, sanitize_html

    html = (
        '<script src="https://doubleclick.net/banner.js"></script>'
        "<script>adsbygoogle.push({});</script>"
        '<script>var player = { file: "https://cdn.example/a.m3u8" };</script>'
        '<iframe src="https://popunder.network/pop.html"></iframe>'
        '<meta http-equiv="refresh" content="0;url=https://adfly.com/x">'
        '<a href="https://cdn.example/ok">ok</a>'
    )
    report = AdFilterReport()
    clean = sanitize_html(html, "https://provider.example/", report)

    assert "doubleclick.net" not in clean
    assert "adsbygoogle" not in clean
    assert "popunder.network" not in clean
    assert "adfly.com" not in clean
    assert "https://cdn.example/a.m3u8" in clean
    assert report.sanitized_scripts == 2
    assert report.sanitized_iframes == 1
    assert report.sanitized_redirects == 1


def test_sanitize_html_preserves_config_scripts_with_ad_words_in_payload():
    from mediaflow_proxy.gateway.ad_filter import AdFilterReport, sanitize_html

    config_script = (
        '<script>window.CFG={"AdTag":"keep","popundersPerIP":"0",'
        '"playerUrl":"https://cdn.example/player"};</script>'
    )
    loader_script = "<script>aclib.runPop({zoneId:'9257962'});</script>"
    html = config_script + loader_script
    report = AdFilterReport()

    clean = sanitize_html(html, "https://provider.example/", report)

    assert config_script in clean
    assert "aclib.runPop" not in clean
    assert report.sanitized_scripts == 1


def test_filter_candidate_url_rejects_ad_and_non_http_urls():
    from mediaflow_proxy.gateway.ad_filter import AdFilterReport, filter_candidate_url

    report = AdFilterReport()
    assert (
        filter_candidate_url("/subs/id.vtt", "https://cdn.example/player", report)
        == "https://cdn.example/subs/id.vtt"
    )
    assert filter_candidate_url("https://doubleclick.net/x", report=report) is None
    assert filter_candidate_url("https://cdn.example/ad/banner.js", report=report) is None
    assert filter_candidate_url("ftp://cdn.example/x", report=report) is None
    assert "doubleclick.net" in report.blocked_domains
    assert report.blocked_urls >= 3


def test_ad_stream_result_is_rejected_and_audit_survives_repeated_filtering():
    from mediaflow_proxy.extractors.base import ExtractorError
    from mediaflow_proxy.gateway.ad_filter import filter_extractor_result

    with pytest.raises(ExtractorError, match="ad-filter"):
        filter_extractor_result({"destination_url": "https://adfly.com/stream.m3u8"})

    result = {
        "destination_url": "https://cdn.example/master.m3u8",
        "sources": [
            {"url": "https://cdn.example/master.m3u8"},
            {"url": "https://doubleclick.net/ad.m3u8"},
        ],
        "subtitles": [{"url": "https://cdn.example/id.vtt"}],
    }
    first = filter_extractor_result(result)
    second = filter_extractor_result(first)
    assert first["sources"] == second["sources"]
    assert first["ad_filter"] == second["ad_filter"]
    assert first["ad_filter"]["blocked_urls"] == 1


@pytest.mark.asyncio
async def test_vidspark_ad_stream_is_rejected():
    config = EMBED_HOSTS["VidCore"]
    extractor = GenericEmbedExtractor({}, config)
    api_url = f"{VIDSPARK_API_BASE}/movie/550"
    embed_url = "https://vidcore.org/embed/movie/550"
    _mock_requests(
        extractor,
        {
            api_url: _response(
                api_url,
                text=json.dumps(
                    {
                        "result": True,
                        "sources": [{"url": "https://adfly.com/stream.m3u8"}],
                    }
                ),
            )
        },
    )

    with pytest.raises(Exception, match="ad-filter"):
        await extractor.extract(embed_url)


@pytest.mark.asyncio
async def test_vidspark_content_gap_is_reported_as_content_unavailable():
    config = EMBED_HOSTS["VidCore"]
    extractor = GenericEmbedExtractor({}, config)
    api_url = f"{VIDSPARK_API_BASE}/tv/293610/1/1"
    embed_url = "https://vidcore.org/embed/tv/293610/1/1?autoplay=true&lang=id&theme=FCC217"
    _mock_requests(
        extractor,
        {
            api_url: _response(
                api_url,
                text=json.dumps({"result": False, "message": "Episode not found"}),
            )
        },
    )

    with pytest.raises(Exception, match="content_unavailable"):
        await extractor.extract(embed_url)


@pytest.mark.asyncio
async def test_generic_flow_skips_ad_script_and_uses_clean_stream():
    config = EMBED_HOSTS["VidKing"]
    extractor = GenericEmbedExtractor({}, config)
    embed_url = "https://www.vidking.net/embed/movie/550"
    destination = "https://cdn.example/video.m3u8"
    _mock_requests(
        extractor,
        {
            embed_url: _response(
                embed_url,
                text=(
                    '<script>adsbygoogle.push("https://doubleclick.net/x");'
                    'var player = { file: "https://ads.example.com/ad.m3u8" };</script>'
                    f'<script>var player = {{ file: "{destination}" }};</script>'
                ),
            )
        },
    )

    result = await extractor.extract(embed_url)

    assert result["destination_url"] == destination
    assert result["ad_filter"]["sanitized_scripts"] == 1
