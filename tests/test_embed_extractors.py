"""Offline tests for the StreamFlix-compatible embed host registry/flows."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.requests import Request

from mediaflow_proxy.extractors.base import HttpResponse
from mediaflow_proxy.extractors.embed_hosts import (
    AUTO_HOST_ORDER,
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


def test_auto_host_order_uses_all_three_supported_hosts():
    assert AUTO_HOST_ORDER == ("VidCore", "VSEmbed", "VidLink")
    assert len(AUTO_HOST_ORDER) == len(set(AUTO_HOST_ORDER))
    assert set(AUTO_HOST_ORDER) <= set(EMBED_HOSTS)


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
    assert "VidLink" in AUTO_HOST_ORDER


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


# ---------------------------------------------------------------------------
# Gateway auto selection / failover
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_auto_uses_first_healthy_host(monkeypatch):
    from mediaflow_proxy.routes import embed as embed_routes

    embed_routes._negative_cache.clear()
    embed_routes._resolved_cache.clear()
    monkeypatch.setattr(embed_routes, "AUTO_HOST_ORDER", ["VidCore", "VSEmbed"])
    probed: list[str] = []

    async def fake_probe(request, config, proxy_headers, media_type, media_id, season, episode):
        probed.append(config.canonical)
        if config.canonical == "VidCore":
            await asyncio.sleep(0.01)
            return config, {"stream_url": "stream", "audit": {}}, None
        await asyncio.sleep(0.5)
        return config, None, f"{config.canonical}: slow"

    monkeypatch.setattr(embed_routes, "_probe_gateway", fake_probe)
    payload = await embed_routes._resolve_gateway(
        _embed_request(), "tv", "tv-1399", "1", "1"
    )

    assert payload["stream_url"] == "stream"
    assert probed[0] == "VidCore"


@pytest.mark.asyncio
async def test_gateway_failure_raises_without_rendering_a_player(monkeypatch):
    from mediaflow_proxy.routes import embed as embed_routes

    embed_routes._negative_cache.clear()
    monkeypatch.setattr(embed_routes, "AUTO_HOST_ORDER", ["VidCore"])

    async def fake_probe(request, config, proxy_headers, media_type, media_id, season, episode):
        return config, None, f"{config.canonical}: episode unavailable"

    monkeypatch.setattr(embed_routes, "_probe_gateway", fake_probe)

    with pytest.raises(embed_routes.EmbedUnavailable, match="episode unavailable"):
        await embed_routes._resolve_gateway(
            _embed_request(), "tv", "94997", "1", "1"
        )


@pytest.mark.asyncio
async def test_gateway_retry_clears_negative_cache(monkeypatch):
    from mediaflow_proxy.routes import embed as embed_routes

    embed_routes._negative_cache.clear()
    monkeypatch.setattr(embed_routes, "AUTO_HOST_ORDER", ["VidCore"])
    attempts = 0

    async def fake_probe(request, config, proxy_headers, media_type, media_id, season, episode):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return config, None, f"{config.canonical}: down"
        return config, {"stream_url": "ok", "audit": {}}, None

    monkeypatch.setattr(embed_routes, "_probe_gateway", fake_probe)
    with pytest.raises(embed_routes.EmbedUnavailable):
        await embed_routes._resolve_gateway(_embed_request(), "tv", "94997", "1", "1")

    payload = await embed_routes._resolve_gateway(
        _embed_request(query="retry=1"), "tv", "94997", "1", "1"
    )
    assert payload["stream_url"] == "ok"
    assert attempts == 2


@pytest.mark.asyncio
async def test_gateway_auto_falls_back_to_vidlink(monkeypatch):
    from mediaflow_proxy.routes import embed as embed_routes

    embed_routes._negative_cache.clear()
    embed_routes._resolved_cache.clear()
    monkeypatch.setattr(
        embed_routes, "AUTO_HOST_ORDER", ["VidCore", "VSEmbed", "VidLink"]
    )
    probed: list[str] = []

    async def fake_probe(request, config, proxy_headers, media_type, media_id, season, episode):
        probed.append(config.canonical)
        if config.canonical == "VidLink":
            return config, {"host": config.canonical, "stream_url": "vidlink-stream", "audit": {}}, None
        return config, None, f"{config.canonical}: unavailable"

    monkeypatch.setattr(embed_routes, "_probe_gateway", fake_probe)
    payload = await embed_routes._resolve_gateway(
        _embed_request(), "tv", "1399", "1", "1"
    )

    assert probed == ["VidCore", "VSEmbed", "VidLink"]
    assert payload["host"] == "VidLink"
    assert payload["stream_url"] == "vidlink-stream"


@pytest.mark.asyncio
async def test_gateway_explicit_host_probes_only_that_host(monkeypatch):
    from mediaflow_proxy.routes import embed as embed_routes

    embed_routes._negative_cache.clear()
    embed_routes._resolved_cache.clear()
    probed: list[str] = []

    async def fake_probe(request, config, proxy_headers, media_type, media_id, season, episode):
        probed.append(config.canonical)
        return config, {"host": config.canonical, "stream_url": "vidlink-stream", "audit": {}}, None

    monkeypatch.setattr(embed_routes, "_probe_gateway", fake_probe)
    payload = await embed_routes._resolve_gateway(
        _embed_request(), "movie", "786892", None, None, host="vidlink"
    )

    assert probed == ["VidLink"]
    assert payload["host"] == "VidLink"
