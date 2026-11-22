"""Offline tests for the StreamFlix-compatible embed host registry/flows."""

from __future__ import annotations

import asyncio
import json

import pytest
from starlette.requests import Request

from mediaflow_proxy.extractors.base import HttpResponse
from mediaflow_proxy.extractors.embed_hosts import (
    AUTO_HOST_ORDER,
    EMBED_HOSTS,
    VIDSPARK_API_BASE,
    build_embed_url,
    canonicalize_host,
    is_public_http_url,
)
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


def test_auto_host_order_only_uses_vidcore_and_vsembed():
    assert AUTO_HOST_ORDER == ("VidCore", "VSEmbed")
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
    assert [sub["language"] for sub in result["subtitles"]] == ["id", "en"]
    assert [sub["url"] for sub in result["subtitles"]] == [id_url, en_url]
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

    assert [sub["url"] for sub in result["subtitles"]] == [
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
    gate_url = "https://stream.oplay.id/vs_src.php?type=tv&id=293610"
    nested_url = "https://vsplayer.example/watch"
    player_url = "https://vsplayer.example/player.html"
    api_url = (
        "https://vsapi.example/api.php?type=tv&tmdb=293610&stream_urls=&api_token=tok"
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
                            "cast_urls": [cast_url],
                        }
                    }
                ),
            ),
            playlist_url: _response(playlist_url, text="<html>challenge</html>"),
        },
    )

    async def fake_browser_fetch(url: str, referer: str):
        assert url == playlist_url
        return "#EXTM3U\n#EXT-X-VERSION:3\n"

    monkeypatch.setattr(extractor, "_browser_fetch_playlist", fake_browser_fetch)

    result = await extractor.extract(embed_url)

    assert result["destination_url"] == playlist_url
    assert result["sandbox_used"] is True


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
