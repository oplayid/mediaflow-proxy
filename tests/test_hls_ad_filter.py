"""Offline tests for HLS ad-range scrubbing."""

from __future__ import annotations

from urllib.parse import quote

import pytest
from starlette.datastructures import URL

from mediaflow_proxy.utils.m3u8_processor import AdRangeFilter, M3U8Processor


class DummyRequest:
    def __init__(self):
        self.headers = {}
        self.query_params = {}
        self.url = URL("http://localhost/test")

    def url_for(self, name: str, **kwargs):
        if name == "hls_manifest_proxy":
            return URL("http://localhost/proxy/hls/manifest.m3u8")
        raise KeyError(name)


def test_cue_out_in_marks_only_ad_segments():
    ad_filter = AdRangeFilter()
    assert ad_filter.observe_tag("#EXT-X-CUE-OUT:DURATION=20")
    assert ad_filter.should_skip(10) is True
    assert ad_filter.should_skip(10) is True
    assert ad_filter.observe_tag("#EXT-X-CUE-IN")
    assert ad_filter.should_skip(10) is False
    assert ad_filter.skipped_segments == 2


def test_daterange_ad_class_uses_playlist_clock_and_ignores_unrelated_classes():
    ad_filter = AdRangeFilter()
    assert ad_filter.should_skip(5) is False
    assert ad_filter.observe_tag('#EXT-X-DATERANGE:ID="x",CLASS="header",DURATION=10') is False
    assert ad_filter.observe_tag('#EXT-X-DATERANGE:ID="y",CLASS="ad-preroll",DURATION=10')
    assert ad_filter.should_skip(10) is True
    assert ad_filter.should_skip(10) is False
    assert ad_filter.skipped_segments == 1


@pytest.mark.asyncio
async def test_process_m3u8_removes_ad_segments_and_tags():
    content = "\n".join(
        [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXT-X-TARGETDURATION:10",
            "#EXTINF:10.0,",
            "content1.ts",
            "#EXT-X-CUE-OUT:DURATION=20",
            "#EXTINF:10.0,",
            "ad1.ts",
            "#EXTINF:10.0,",
            "ad2.ts",
            "#EXT-X-CUE-IN",
            "#EXTINF:10.0,",
            "content2.ts",
            "#EXT-X-ENDLIST",
            "",
        ]
    )
    processor = M3U8Processor(DummyRequest())
    result = await processor.process_m3u8(content, "https://cdn.example/master.m3u8")

    assert "ad1.ts" not in result
    assert "ad2.ts" not in result
    assert "CUE-OUT" not in result
    assert "CUE-IN" not in result
    assert "content1.ts" in result
    assert "content2.ts" in result
    assert processor.ad_filter.skipped_segments == 2


@pytest.mark.asyncio
async def test_hls_ad_filter_can_be_disabled():
    content = "\n".join(
        [
            "#EXTM3U",
            "#EXT-X-CUE-OUT:DURATION=10",
            "#EXTINF:10.0,",
            "ad.ts",
            "#EXT-X-CUE-IN",
            "#EXT-X-ENDLIST",
            "",
        ]
    )
    processor = M3U8Processor(DummyRequest(), filter_hls_ads=False)
    result = await processor.process_m3u8(content, "https://cdn.example/master.m3u8")

    assert "ad.ts" in result
    assert "CUE-OUT" in result


def _master_playlist() -> tuple[str, str, str]:
    variants = [
        "https://cdn.example/hls/video-v1.m3u8.js?token=abc",
        "https://cdn.example/hls/video-v2.m3u8.js?token=def",
    ]
    content = "\n".join(
        [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1280x720",
            variants[0],
            "#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1920x1080",
            variants[1],
            "",
        ]
    )
    return content, "https://cdn.example/hls/master.m3u8", variants[0]


@pytest.mark.asyncio
async def test_master_variant_uris_survive_ad_filter():
    content, base_url, variant = _master_playlist()
    processor = M3U8Processor(DummyRequest())

    result = await processor.process_m3u8(content, base_url)

    assert result.count("#EXT-X-STREAM-INF") == 2
    assert result.count("manifest.m3u8?d=") == 2
    assert quote(variant, safe="") in result
    assert "/proxy/hls/segment" not in result


async def _chunks(text: str):
    for start in range(0, len(text), 37):
        yield text[start : start + 37].encode("utf-8")


@pytest.mark.asyncio
async def test_streaming_master_variant_uris_survive_ad_filter():
    content, base_url, variant = _master_playlist()
    processor = M3U8Processor(DummyRequest())

    result = "".join(
        [part async for part in processor.process_m3u8_streaming(_chunks(content), base_url)]
    )

    assert result.count("#EXT-X-STREAM-INF") == 2
    assert result.count("manifest.m3u8?d=") == 2
    assert quote(variant, safe="") in result
    assert "/proxy/hls/segment" not in result


@pytest.mark.asyncio
async def test_streaming_ad_segments_are_still_removed():
    content = "\n".join(
        [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXT-X-TARGETDURATION:10",
            "#EXTINF:10.0,",
            "content.ts",
            "#EXT-X-CUE-OUT:DURATION=10",
            "#EXTINF:10.0,",
            "ad.ts",
            "#EXT-X-CUE-IN",
            "#EXT-X-ENDLIST",
            "",
        ]
    )
    processor = M3U8Processor(DummyRequest())

    result = "".join(
        [
            part
            async for part in processor.process_m3u8_streaming(
                _chunks(content), "https://cdn.example/hls/media.m3u8"
            )
        ]
    )

    assert "content.ts" in result
    assert "ad.ts" not in result
    assert "CUE-OUT" not in result
    assert processor.ad_filter.skipped_segments == 1
