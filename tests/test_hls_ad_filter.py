"""Offline tests for HLS ad-range scrubbing."""

from __future__ import annotations

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
