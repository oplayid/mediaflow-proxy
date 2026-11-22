from urllib.parse import parse_qs, urlparse

import pytest
from starlette.datastructures import URL

from mediaflow_proxy.utils.m3u8_processor import M3U8Processor


class DummyRequest:
    def __init__(self, query_params: dict):
        self.headers = {}
        self.query_params = query_params
        self.url = URL("http://localhost/test")

    def url_for(self, name: str, **kwargs):
        if name == "hls_manifest_proxy":
            return URL("http://localhost/proxy/hls/manifest.m3u8")
        raise KeyError(name)


@pytest.mark.asyncio
async def test_process_key_line_resolves_relative_key_uri_for_proxy_encoding():
    request = DummyRequest({"api_password": "devpass"})
    processor = M3U8Processor(request)

    line = '#EXT-X-KEY:METHOD=AES-128,URI="/key/premium49/5913111",IV=0x00'
    base_url = "https://ai.the-sunmoon.site/proxy/wind/premium49/mono.css"

    processed = await processor.process_key_line(line, base_url)

    assert "d=https%3A%2F%2Fai.the-sunmoon.site%2Fkey%2Fpremium49%2F5913111" in processed
    assert "d=%2Fkey%2Fpremium49%2F5913111" not in processed


@pytest.mark.asyncio
async def test_proxy_url_keeps_force_playlist_proxy_for_nested_playlists():
    request = DummyRequest({"api_password": "devpass", "force_playlist_proxy": "1"})
    processor = M3U8Processor(request)

    playlist_url = await processor.proxy_url(
        "https://upstream.example/hls/variant.m3u8",
        "https://upstream.example/hls/master.m3u8",
        use_full_url=True,
        is_playlist=True,
    )
    segment_url = await processor.proxy_url(
        "https://upstream.example/hls/segment0.ts",
        "https://upstream.example/hls/variant.m3u8",
        use_full_url=True,
        is_playlist=False,
    )

    assert "force_playlist_proxy=1" in playlist_url
    assert "force_playlist_proxy=1" not in segment_url


@pytest.mark.asyncio
async def test_proxy_url_inherits_upstream_token_for_same_host_children():
    request = DummyRequest({"api_password": "devpass"})
    processor = M3U8Processor(request)
    base_url = "https://pool.example/pl/master?token=playback-token"

    variant_url = await processor.proxy_url(
        "https://pool.example/pl/master/variant.m3u8",
        base_url,
        use_full_url=True,
        is_playlist=True,
    )
    segment_url = await processor.proxy_url(
        "/content/hash/page-0.ts",
        "https://pool.example/pl/master/variant.m3u8?token=playback-token",
        is_playlist=False,
    )

    assert parse_qs(urlparse(variant_url).query)["d"] == [
        "https://pool.example/pl/master/variant.m3u8?token=playback-token"
    ]
    assert parse_qs(urlparse(segment_url).query)["d"] == [
        "https://pool.example/content/hash/page-0.ts?token=playback-token"
    ]


@pytest.mark.asyncio
async def test_proxy_url_does_not_leak_upstream_token_to_other_hosts():
    request = DummyRequest({"api_password": "devpass"})
    processor = M3U8Processor(request)

    url = await processor.proxy_url(
        "https://ads.example/segment.ts",
        "https://pool.example/pl/master?token=playback-token",
        use_full_url=True,
        is_playlist=False,
    )

    assert parse_qs(urlparse(url).query)["d"] == ["https://ads.example/segment.ts"]
