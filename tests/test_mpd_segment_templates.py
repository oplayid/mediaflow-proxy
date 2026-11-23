"""DASH SegmentTemplate expansion used by the MPD -> HLS conversion endpoint."""

from mediaflow_proxy.utils.mpd_utils import create_segment_data


def test_padded_number_template_is_expanded():
    item = {"@media": "chunk-stream3-$Number%05d$.m4s"}
    profile = {"id": "3", "bandwidth": 128000}

    data = create_segment_data(
        {"number": 12}, item, profile, "https://cdn.example/dash/index_web.mpd"
    )

    assert data["media"] == "https://cdn.example/dash/chunk-stream3-00012.m4s"
    assert "$Number" not in data["media"]


def test_short_padded_number_template_is_expanded():
    item = {"@media": "seg-$Number%2d$.m4s"}
    profile = {"id": "v", "bandwidth": 1_000_000}

    data = create_segment_data({"number": 7}, item, profile, "https://cdn.example/dash/index.mpd")

    assert data["media"] == "https://cdn.example/dash/seg-07.m4s"


def test_padded_time_template_is_expanded():
    item = {"@media": "seg-$Time%05d$.m4s", "@timescale": "1000", "@duration": "4000"}
    profile = {"id": "v", "bandwidth": 1_000_000, "segment_template_start_number": 1}

    data = create_segment_data(
        {"number": 1, "time": 120},
        item,
        profile,
        "https://cdn.example/dash/index.mpd",
        timescale=1000,
    )

    assert data["media"] == "https://cdn.example/dash/seg-00120.m4s"
