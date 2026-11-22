"""Offline tests for the StreamFlix-compatible embed host registry/flows."""

from __future__ import annotations

import json

import pytest

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


def test_auto_host_order_prefers_vidcore_and_includes_every_host():
    assert AUTO_HOST_ORDER[0] == "VidCore"
    assert AUTO_HOST_ORDER[1] == "VSEmbed"
    assert set(AUTO_HOST_ORDER) == set(EMBED_HOSTS)
    assert len(AUTO_HOST_ORDER) == len(set(AUTO_HOST_ORDER))


def test_player_html_emits_server_track_controls_and_error_messages():
    from mediaflow_proxy.routes.embed import _player_html, PLAYER_LANGUAGE_PRIORITY

    sources = [
        {"id": "auto", "label": "Auto", "url": "https://mfp.example/proxy/master.m3u8"},
        {"id": "1080p", "label": "1080p", "url": "https://mfp.example/proxy/1080.m3u8"},
    ]
    subtitles = [
        {
            "id": "sub-id-0",
            "label": "Indonesian",
            "language": "id",
            "format": "vtt",
            "url": "https://mfp.example/proxy/hls/segment.vtt?d=id",
        },
        {
            "id": "sub-en-1",
            "label": "English",
            "language": "en",
            "format": "vtt",
            "url": "https://mfp.example/proxy/hls/segment.vtt?d=en",
        },
    ]
    player = _player_html(
        "https://mfp.example/proxy/playlist.m3u8",
        "550",
        True,
        {"id": "vidcore", "name": "VidCore"},
        [
            {
                "id": "auto",
                "name": "Auto",
                "url": "https://mfp.example/embed/auto/movie/550",
                "active": False,
            }
        ],
        sources=sources,
        subtitles=subtitles,
        capabilities={"quality_from_api": True, "subtitles_from_api": True},
    )
    assert "OPLAY_EMBED_SERVER" in player
    assert "OPLAY_EMBED_ERROR" in player
    assert "OPLAY_EMBED_READY" in player
    assert "OPLAY_EMBED_TRACKS" in player
    assert "OPLAY_EMBED_QUALITY" in player
    assert "OPLAY_EMBED_SUBTITLE" in player
    assert '"id": "vidcore"' in player
    for control in ("server", "quality", "subtitle", "audio", "speed"):
        assert f'id="{control}"' in player
    assert '"language": "id"' in player
    assert '"language": "en"' in player
    assert "api_password" not in player
    assert PLAYER_LANGUAGE_PRIORITY[0] == "id"
    assert PLAYER_LANGUAGE_PRIORITY[5] == "en"


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
