"""Tests for the server-side smart player and signed gateway handoff."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from mediaflow_proxy.configs import settings
from mediaflow_proxy.extractors.embed_hosts import EMBED_HOSTS
from mediaflow_proxy.player import simulator
from mediaflow_proxy.player.adapters import (
    configured_server_names,
    configured_servers,
    register_server,
)
from mediaflow_proxy.player.models import Candidate, ProbeResult
from mediaflow_proxy.player.simulator import (
    _audio_can_be_copied,
    _classify_manifest,
    _merge_subtitles,
    _needs_transcode,
    has_indonesian_subtitle,
    host_configs,
)
from mediaflow_proxy.player.transcoder import (
    build_ffmpeg_command,
    serve_transcode_file,
    sign_transcode_url,
)
from mediaflow_proxy.routes.embed import (
    _headers_for_destination,
    _sign_player_url,
    _sign_subtitle_upload_url,
)
from mediaflow_proxy.utils import crypto_utils
from mediaflow_proxy.utils.crypto_utils import EncryptionHandler


def _request(path: str = "/gateway/resolve/movie/550") -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"host", b"mfp.oplay.id")],
        "client": ("10.0.0.1", 12345),
    }
    return Request(scope)


def test_headers_for_destination_prefers_source_headers():
    result = {
        "request_headers": {"User-Agent": "base", "Referer": "https://host/"},
        "destination_url": "https://cdn.example/index.mpd",
        "sources": [
            {
                "url": "https://cdn.example/index.mpd",
                "request_headers": {
                    "User-Agent": "base",
                    "Referer": "https://host/",
                    "Cookie": "CloudFront-Policy=signed",
                },
            }
        ],
    }
    headers = _headers_for_destination(result, result["destination_url"])
    assert headers and headers["Cookie"] == "CloudFront-Policy=signed"


def test_headers_for_destination_falls_back_to_base_headers():
    result = {
        "request_headers": {"User-Agent": "base"},
        "destination_url": "https://cdn.example/index.mpd",
        "sources": [{"url": "https://other.example/x.m3u8", "request_headers": {"Cookie": "wrong"}}],
    }
    assert _headers_for_destination(result, result["destination_url"]) == {"User-Agent": "base"}


def test_sign_player_url_is_opaque_and_decryptable(monkeypatch):
    monkeypatch.setattr(settings, "api_password", "unit-secret")
    url = _sign_player_url(_request(), "tv", "246473", "1", "1")
    assert url.startswith("https://mfp.oplay.id/_token_")
    assert "/player/tv/246473/1/1" in url
    assert "api_password" not in url

    token = url.split("/_token_", 1)[1].split("/player/", 1)[0]
    payload = EncryptionHandler("unit-secret").decrypt_data(token, None)
    assert payload["type"] == "tv"
    assert payload["id"] == "246473"
    assert payload["s"] == "1"
    assert payload["e"] == "1"


def test_sign_subtitle_upload_url_is_opaque_and_long_lived(monkeypatch):
    from mediaflow_proxy.routes import embed as embed_routes

    monkeypatch.setattr(settings, "api_password", "unit-secret")
    seen: dict[str, object] = {}

    class SpyHandler(EncryptionHandler):
        def encrypt_data(self, data, expiration=None, ip=None):
            seen["expiration"] = expiration
            return super().encrypt_data(data, expiration=expiration, ip=ip)

    monkeypatch.setattr(embed_routes, "EncryptionHandler", SpyHandler)
    url = _sign_subtitle_upload_url(_request(), "movie", "550")

    assert url.startswith("https://mfp.oplay.id/_token_")
    assert url.endswith("/player/subtitle/upload")
    assert "api_password" not in url

    token = url.split("/_token_", 1)[1].split("/player/", 1)[0]
    payload = EncryptionHandler("unit-secret").decrypt_data(token, None)
    assert payload["type"] == "movie"
    assert payload["id"] == "550"
    assert payload["upload"] is True
    assert seen["expiration"] == 6 * 60 * 60


def test_host_configs_uses_only_the_shared_host_order():
    from mediaflow_proxy.extractors.embed_hosts import HOST_ORDER

    names = [config.canonical for config in host_configs()]
    assert names == list(HOST_ORDER)
    assert "VidKing" not in names  # historical registry entries are never probed


def test_configured_servers_accept_slugs_and_skip_unknown_names(monkeypatch):
    monkeypatch.setattr(settings, "player_hosts", "vidcore, not-a-server, stream.oplay.id")
    assert configured_server_names() == ["VidCore", "VSEmbed"]
    assert [adapter.name for adapter in configured_servers()] == ["VidCore", "VSEmbed"]


@pytest.mark.asyncio
async def test_server_adapter_can_override_resolution(monkeypatch):
    config = EMBED_HOSTS["VidKing"]
    calls: list[tuple] = []

    async def resolver(adapter, request, media_type, media_id, season, episode):
        calls.append((adapter.name, media_type, media_id))
        return {"destination_url": "https://cdn.example/custom.m3u8"}

    adapter = register_server(config, resolver=resolver)
    try:
        monkeypatch.setattr(settings, "player_hosts", "vidking")
        assert configured_server_names() == ["VidKing"]
        result = await adapter.resolve(_request(), "movie", "42", "1", "1")
        assert result["destination_url"] == "https://cdn.example/custom.m3u8"
        assert calls == [("VidKing", "movie", "42")]
    finally:
        register_server(config)  # restore the generic adapter for other tests


def _patch_simulation(monkeypatch, ok_hosts: set[str]) -> list[tuple[str, str]]:
    """Stub network work so first-success ordering can be asserted offline."""
    calls: list[tuple[str, str]] = []

    class _DummySession:
        async def close(self):
            pass

    class _DummyStreamer:
        session = _DummySession()

    async def fake_streamer(url):
        return _DummyStreamer()

    async def fake_resolve(config, request, media_type, media_id, season, episode):
        calls.append(("resolve", config.canonical))
        return {
            "destination_url": f"https://cdn.example/{config.canonical}/master.m3u8",
            "sources": [],
            "subtitles": [],
        }

    async def fake_probe(result, streamer, timeout):
        host = result["destination_url"].split("/")[-2]
        calls.append(("probe", host))
        return ProbeResult(ok=host in ok_hosts, manifest_type="hls", score=80)

    async def fake_signed(request, result, proxy_headers, expiration, host, probe):
        calls.append(("sign", host))
        return Candidate(
            host=host,
            url="https://mfp.example/hls/master.m3u8",
            manifest_type="hls",
            playback_type="hls",
            probe=probe,
        )

    async def no_cache(*args, **kwargs):
        return None

    async def no_set(*args, **kwargs):
        return None

    async def no_delete(*args, **kwargs):
        return None

    async def no_subtitles(*args, **kwargs):
        return []

    monkeypatch.setattr(simulator, "create_streamer", fake_streamer)
    monkeypatch.setattr(simulator, "_resolve_host", fake_resolve)
    monkeypatch.setattr(simulator, "probe_candidate", fake_probe)
    monkeypatch.setattr(simulator, "_signed_candidate", fake_signed)
    monkeypatch.setattr(simulator, "get_cached_extractor", no_cache)
    monkeypatch.setattr(simulator, "set_cached_extractor", no_set)
    monkeypatch.setattr(simulator, "delete_cached_extractor", no_delete)
    monkeypatch.setattr(simulator.subdl, "collect", no_subtitles)
    monkeypatch.setattr(simulator, "_prewarm", lambda job: None)
    return calls


@pytest.mark.asyncio
async def test_simulate_session_checks_each_server_once_and_keeps_all_playable(monkeypatch):
    calls = _patch_simulation(monkeypatch, {"VidCore", "VSEmbed"})

    session = await simulator.simulate_session(
        _request("/player/movie/1263337"), "movie", "1263337", None, None
    )

    resolved = [host for kind, host in calls if kind == "resolve"]
    assert resolved == ["VidCore", "VSEmbed", "VidSrcTo", "VidLink"]
    assert len(resolved) == len(set(resolved))  # each server checked at most once
    assert [item["host"] for item in session["candidates"]] == ["VidCore", "VSEmbed"]
    assert session["audit"]["chosen_video_host"] == "VidCore"  # first success is the default
    assert session["audit"]["available_video_hosts"] == ["VidCore", "VSEmbed"]
    assert session["audit"]["hosts_playable"] == 2
    assert "retry_available" not in session["audit"]
    assert "excluded_hosts" not in session["audit"]
    assert "start_time" not in session


@pytest.mark.asyncio
async def test_simulate_session_honours_player_max_servers(monkeypatch):
    _patch_simulation(monkeypatch, {"VidCore", "VSEmbed", "VidSrcTo", "VidLink"})
    monkeypatch.setattr(settings, "player_max_servers", 2)

    session = await simulator.simulate_session(
        _request("/player/movie/1263337"), "movie", "1263337", None, None
    )

    assert [item["host"] for item in session["candidates"]] == ["VidCore", "VSEmbed"]
    assert {"host": "VidSrcTo", "reason": "candidate_limit"} in session["audit"]["tried_hosts"]


@pytest.mark.asyncio
async def test_simulate_session_refresh_bypasses_cached_session(monkeypatch):
    calls = _patch_simulation(monkeypatch, {"VidCore"})
    deleted: list[str] = []

    async def fake_delete(key):
        deleted.append(key)

    monkeypatch.setattr(simulator, "delete_cached_extractor", fake_delete)
    await simulator.simulate_session(
        _request("/player/movie/1263337"), "movie", "1263337", None, None, refresh=True
    )

    assert len(deleted) == 1
    assert "movie:1263337" in deleted[0]
    assert [host for kind, host in calls if kind == "resolve"] == [
        "VidCore",
        "VSEmbed",
        "VidSrcTo",
        "VidLink",
    ]


def _native_track(language: str, label: str) -> dict:
    return {
        "id": f"native-{language}",
        "language": language,
        "label": label,
        "host": "VidCore",
        "origin": "host",
        "url": f"https://proxy/vs/{language}",
    }


def _patch_signed_with_subtitles(monkeypatch, tracks: list[dict]) -> None:
    async def fake_signed(request, result, proxy_headers, expiration, host, probe):
        return Candidate(
            host=host,
            url="https://mfp.example/hls/master.m3u8",
            manifest_type="hls",
            playback_type="hls",
            subtitles=tracks,
            probe=probe,
        )

    monkeypatch.setattr(simulator, "_signed_candidate", fake_signed)


@pytest.mark.asyncio
async def test_simulate_session_keeps_native_default_but_collects_subdl_anyway(monkeypatch):
    _patch_simulation(monkeypatch, {"VidCore"})
    _patch_signed_with_subtitles(monkeypatch, [_native_track("id", "Bahasa Indonesia")])
    collect_calls: list[tuple] = []

    async def fake_collect(*args, **kwargs):
        collect_calls.append(args)
        return [{"id": "subdl-1", "language": "id", "label": "SubDL • ID", "origin": "subdl", "url": "https://proxy/sub/1"}]

    monkeypatch.setattr(simulator.subdl, "collect", fake_collect)

    session = await simulator.simulate_session(
        _request("/player/movie/550"), "movie", "550", None, None
    )

    # The viewer may pick either source manually; the Indonesian host track is
    # only the default, never a reason to hide SubDL from the merged menu.
    assert len(collect_calls) == 1
    assert session["audit"]["native_indonesian_subtitle"] is True
    assert session["audit"]["subdl_used"] is True
    assert session["audit"]["subtitle_count"] == 2
    assert [item["origin"] for item in session["subtitles"]] == ["host", "subdl"]
    assert session["subtitles"][0]["id"] == "native-id"


@pytest.mark.asyncio
async def test_simulate_session_offers_subdl_without_native_indonesian(monkeypatch):
    _patch_simulation(monkeypatch, {"VidCore"})
    _patch_signed_with_subtitles(monkeypatch, [_native_track("en", "English")])
    collect_calls: list[tuple] = []

    async def fake_collect(*args, **kwargs):
        collect_calls.append(args)
        return [{"id": "subdl-1", "language": "id", "label": "SubDL • ID", "origin": "subdl", "url": "https://proxy/sub/1"}]

    monkeypatch.setattr(simulator.subdl, "collect", fake_collect)

    session = await simulator.simulate_session(
        _request("/player/movie/550"), "movie", "550", None, None
    )

    assert len(collect_calls) == 1
    assert session["audit"]["native_indonesian_subtitle"] is False
    assert session["audit"]["subdl_used"] is True
    assert [item["origin"] for item in session["subtitles"]] == ["host", "subdl"]


def test_player_ui_has_manual_server_picker_but_no_retry_controls():
    static_dir = Path(__file__).resolve().parents[1] / "mediaflow_proxy" / "static"
    html = (static_dir / "player.html").read_text(encoding="utf-8")
    js = (static_dir / "player.js").read_text(encoding="utf-8")

    # Server switching is internal to the iframe: no gateway override, no retry.
    assert 'id="server-select"' in html
    assert 'id="host-select"' not in html
    assert 'id="retry-button"' not in html
    assert 'id="error-retry"' not in html
    assert "simulasi host" not in html
    assert "Cari ulang host" not in html
    assert "/retry" not in js
    assert "retryExcluding" not in js
    assert "mfp:host" not in js
    assert "fillServers" in js
    assert "playCandidate(Number(serverSelect.value))" in js
    # Video settings stay in the mfp player.
    assert 'id="quality-select"' in html
    assert 'id="subtitle-select"' in html
    assert 'id="subtitle-upload-button"' in html
    assert 'id="subtitle-file"' in html
    assert 'id="subtitle-language"' in html
    assert 'id="subtitle-status"' in html
    assert "preferredSubtitle" in js
    assert 'item.origin === "subdl"' in js
    assert 'item.origin === "custom"' in js
    assert 'subtitleGroup' in js
    assert 'document.createElement("optgroup")' in js
    assert "/^subdl\\b/i.test(label)" in js  # avoid "SubDL • SubDL • ..." labels
    assert "/^custom\\b/i.test(label)" in js
    assert 'data.id' in js  # mfp:setSubtitle can target one merged track by id
    assert "session.upload_url" in js  # upload survives iframe token expiry



def test_player_session_is_csp_safe():
    static_dir = Path(__file__).resolve().parents[1] / "mediaflow_proxy" / "static"
    html = (static_dir / "player.html").read_text(encoding="utf-8")
    js = (static_dir / "player.js").read_text(encoding="utf-8")

    assert '<script type="application/json" id="mfp-session">/*__MFP_SESSION__*/null</script>' in html
    # `script-src 'self'` has no unsafe-inline: every script tag must either be
    # an external file or a non-executable JSON data block.
    for tag in re.findall(r"<script\b[^>]*>", html, re.IGNORECASE):
        assert "src=" in tag or 'type="application/json"' in tag, tag
    assert 'getElementById("mfp-session")' in js
    assert "JSON.parse(element.textContent" in js


def test_render_player_injects_session_into_data_block():
    from mediaflow_proxy.routes.player import _render_player

    rendered = _render_player(
        {
            "candidates": [{"host": "VidCore", "url": "https://mfp.example/proxy?d=</script>"}],
            "subtitles": [],
            "audit": {},
            "generated_at": 0,
        }
    )
    marker = '<script type="application/json" id="mfp-session">'
    assert marker in rendered
    block = rendered.split(marker, 1)[1].split("</script>", 1)[0]
    assert "<\\/script>" in block
    assert "</script>" not in block


@pytest.mark.asyncio
async def test_player_page_injects_long_lived_upload_url_without_mutating_cache(monkeypatch):
    from mediaflow_proxy.routes import player as player_routes

    cached = {"candidates": [], "subtitles": [], "audit": {}, "generated_at": 0}

    async def fake_session(*args, **kwargs):
        return cached

    monkeypatch.setattr(player_routes, "simulate_session", fake_session)
    monkeypatch.setattr(settings, "api_password", "unit-secret")

    response = await player_routes.player_page(_request("/player/movie/550"), "movie", "550")
    rendered = response.body.decode()
    assert "/_token_" in rendered
    assert "player/subtitle/upload" in rendered
    assert "upload_url" not in cached


def test_classify_manifest():
    assert _classify_manifest("#EXTM3U\n#EXT-X-VERSION:3\n", "text/plain", "https://x/a") == "hls"
    assert _classify_manifest('<?xml version="1.0"?><MPD></MPD>', "application/dash+xml", "https://x/a") == "dash"
    assert _classify_manifest("", "video/mp4", "https://x/a") == "mp4"
    assert _classify_manifest("hello", "text/html", "https://x/a") == "unknown"


@pytest.mark.asyncio
async def test_signed_candidate_converts_dash_to_hls_playback(monkeypatch):
    endpoints: list[str] = []

    def fake_sign(request, result, proxy_headers, destination, *, endpoint=None, **kwargs):
        endpoints.append(endpoint or "")
        return f"https://mfp.oplay.id/proxy/{endpoint}"

    monkeypatch.setattr(simulator, "_sign_asset_url", fake_sign)
    monkeypatch.setattr(simulator.settings, "player_transcode_hevc", False)

    candidate = await simulator._signed_candidate(
        _request("/player/movie/533535"),
        {
            "destination_url": "https://cdn.example/1080_h265/index_web.mpd",
            "mediaflow_endpoint": "proxy_stream_endpoint",
            "sources": [],
            "subtitles": [],
            "provider_subtitles": [],
        },
        {},
        600,
        "VidLink",
        ProbeResult(ok=True, manifest_type="dash", score=60),
    )

    assert endpoints[0] == "mpd_manifest_proxy"
    assert candidate.manifest_type == "hls"
    assert candidate.playback_type == "hls"


def test_merge_subtitles_prefers_indonesian_and_deduplicates():
    def sub(host: str, language: str, label: str) -> dict:
        return {
            "id": f"{host}-{label}",
            "language": language,
            "label": label,
            "host": host,
            "origin": "host",
            "url": f"https://proxy/{host}/{label}",
        }

    candidates = [
        Candidate(
            host="VSEmbed",
            url="https://proxy/vs",
            manifest_type="hls",
            subtitles=[sub("VSEmbed", "en", "English"), sub("VSEmbed", "pt", "Portuguese")],
            probe=ProbeResult(ok=True, score=90),
        ),
        Candidate(
            host="VidLink",
            url="https://proxy/vl",
            manifest_type="dash",
            subtitles=[sub("VidLink", "id", "Indonesian"), sub("VidLink", "en", "English")],
            probe=ProbeResult(ok=True, score=70),
        ),
    ]
    merged = _merge_subtitles(candidates)
    assert merged[0]["language"] == "id"
    assert len([item for item in merged if item["language"] == "en"]) == 1


def test_merge_subtitles_keeps_the_whole_native_package():
    tracks = [
        {
            "id": f"sub-{index}",
            "language": "en" if index else "id",
            "label": f"Track {index}",
            "host": "VSEmbed",
            "origin": "host",
            "url": f"https://proxy/vs/{index}",
        }
        for index in range(25)
    ]
    candidate = Candidate(
        host="VSEmbed",
        url="https://proxy/vs",
        manifest_type="hls",
        subtitles=tracks,
        probe=ProbeResult(ok=True, score=90),
    )

    merged = _merge_subtitles([candidate])
    assert len(merged) == 25
    assert has_indonesian_subtitle(merged) is True
    assert has_indonesian_subtitle([item for item in merged if item["language"] == "en"]) is False


def test_srt_to_vtt_converts_timestamps_and_keeps_webvtt():
    from mediaflow_proxy.subtitles.subdl import srt_to_vtt

    converted = srt_to_vtt(
        "\ufeff1\r\n00:00:00,500 --> 00:00:02,000\r\nHalo\r\n\r\n"
    )
    assert converted.startswith("WEBVTT\n\n")
    assert "00:00:00.500 --> 00:00:02.000" in converted
    assert ",500" not in converted

    already_vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nTeks\n"
    assert srt_to_vtt(already_vtt).strip() == already_vtt.strip()


@pytest.mark.asyncio
async def test_hls_segment_vtt_converts_upstream_srt_for_browser_tracks():
    from mediaflow_proxy.routes.proxy import _as_webvtt_response

    upstream = Response(
        content=b"1\r\n00:00:00,000 --> 00:00:01,000\r\nHalo\r\n",
        status_code=200,
        headers={"content-type": "application/octet-stream", "x-upstream": "keep"},
    )
    converted = await _as_webvtt_response(upstream)

    assert converted.status_code == 200
    assert converted.headers["content-type"] == "text/vtt; charset=utf-8"
    assert converted.headers["x-upstream"] == "keep"
    body = converted.body.decode("utf-8")
    assert converted.headers["content-length"] == str(len(converted.body))
    assert body.startswith("WEBVTT\n\n")
    assert "00:00:00.000 --> 00:00:01.000" in body

    from mediaflow_proxy.utils.http_utils import EnhancedStreamingResponse

    async def upstream_chunks():
        yield b"1\r\n00:00:00,000 --> 00:00:01,000\r\nHalo\r\n"

    streamed = await _as_webvtt_response(
        EnhancedStreamingResponse(upstream_chunks(), media_type="application/octet-stream")
    )
    assert streamed.body.decode("utf-8").startswith("WEBVTT\n\n")

    error = Response(status_code=404, content=b"missing")
    assert await _as_webvtt_response(error) is error


@pytest.mark.asyncio
async def test_subdl_collect_returns_signed_same_origin_tracks(monkeypatch):
    from mediaflow_proxy.subtitles import subdl

    monkeypatch.setattr(settings, "subdl_api_key", "secret-subdl-key")
    monkeypatch.setattr(
        subdl,
        "search",
        lambda *args, **kwargs: asyncio.sleep(
            0,
            result=[
                {
                    "id": "track1",
                    "label": "SubDL • ID • Release",
                    "language": "id",
                    "download_path": "/subtitle/123/4.srt",
                }
            ],
        ),
    )
    monkeypatch.setattr(
        subdl, "_download", lambda item: asyncio.sleep(0, result="WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHalo\n")
    )
    monkeypatch.setattr(
        subdl, "store_vtt", lambda content: asyncio.sleep(0, result="a" * 32)
    )

    tracks = await subdl.collect(
        "movie", "550", None, None, signer=lambda key: f"https://mfp.oplay.id/_token_x/player/subtitle/{key}.vtt"
    )

    assert len(tracks) == 1
    assert tracks[0] == {
        "id": "subdl-track1",
        "label": "SubDL • ID • Release",
        "language": "id",
        "format": "vtt",
        "origin": "subdl",
        "url": f"https://mfp.oplay.id/_token_x/player/subtitle/{'a' * 32}.vtt",
    }
    assert "secret-subdl-key" not in str(tracks)


@pytest.mark.asyncio
async def test_player_subtitle_endpoint_serves_signed_vtt(monkeypatch):
    from mediaflow_proxy.routes import player as player_routes

    key = "b" * 32
    monkeypatch.setattr(
        player_routes, "load_vtt", lambda value: asyncio.sleep(0, result="WEBVTT\n\nHalo\n" if value == key else None)
    )
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": f"/player/subtitle/{key}.vtt",
        "raw_path": f"/player/subtitle/{key}.vtt".encode(),
        "query_string": f"sub={key}".encode(),
        "headers": [(b"host", b"mfp.oplay.id")],
    }
    response = await player_routes.player_subtitle(Request(scope), f"{key}.vtt")

    assert response.status_code == 200
    assert "text/vtt" in response.media_type
    assert response.body == b"WEBVTT\n\nHalo\n"
    assert "frame-ancestors https://movie.oplay.id" in response.headers["content-security-policy"]
    assert "api_password" not in response.body.decode()


def _upload_request(query: bytes, body: bytes) -> Request:
    path = "/player/subtitle/upload"
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "headers": [(b"host", b"mfp.oplay.id"), (b"content-type", b"application/octet-stream")],
        "client": ("10.0.0.1", 12345),
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


@pytest.mark.asyncio
async def test_player_subtitle_upload_returns_signed_custom_track(monkeypatch):
    from mediaflow_proxy.routes import player as player_routes

    key = "c" * 32
    stored: dict[str, object] = {}

    async def fake_store(content, ttl=player_routes.SUBTITLE_CACHE_TTL):
        stored["content"] = content
        stored["ttl"] = ttl
        return key

    monkeypatch.setattr(player_routes, "store_vtt", fake_store)
    monkeypatch.setattr(settings, "api_password", "unit-secret")

    srt = b"1\r\n00:00:00,000 --> 00:00:01,000\r\nHalo\r\n"
    response = await player_routes.player_subtitle_upload(
        _upload_request(b"type=movie&id=550&language=id&label=Film+Saya", srt)
    )

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["id"] == f"custom-{key[:12]}"
    assert payload["origin"] == "custom"
    assert payload["language"] == "id"
    assert payload["label"] == "Custom • Film Saya"
    assert stored["content"].startswith("WEBVTT\n\n")
    assert "00:00:00.000 --> 00:00:01.000" in stored["content"]
    assert stored["ttl"] == player_routes.SUBTITLE_CACHE_TTL
    assert payload["url"].startswith("https://mfp.oplay.id/_token_")
    assert "/player/subtitle/" in payload["url"]
    assert "api_password" not in payload["url"]
    assert "frame-ancestors https://movie.oplay.id" in response.headers["content-security-policy"]


@pytest.mark.asyncio
async def test_player_subtitle_upload_rejects_bad_input(monkeypatch):
    from mediaflow_proxy.routes import player as player_routes

    with pytest.raises(HTTPException) as missing:
        await player_routes.player_subtitle_upload(
            _upload_request(b"type=movie", b"1\r\n00:00 --> 00:01\r\nHalo\r\n")
        )
    assert missing.value.status_code == 400

    with pytest.raises(HTTPException) as invalid:
        await player_routes.player_subtitle_upload(
            _upload_request(b"type=movie&id=550", b"bukan subtitle")
        )
    assert invalid.value.status_code == 400

    with pytest.raises(HTTPException) as oversize:
        await player_routes.player_subtitle_upload(
            _upload_request(b"type=movie&id=550", b"x" * (player_routes._MAX_CUSTOM_SUBTITLE_BYTES + 1))
        )
    assert oversize.value.status_code == 400


def test_encryption_middleware_restores_tokenized_scope(monkeypatch):
    handler = EncryptionHandler("unit-secret")
    monkeypatch.setattr(crypto_utils, "encryption_handler", handler)
    token = handler.encrypt_data({"api_password": "unit-secret", "d": "https://cdn.example/a.m3u8"}, 60)
    path = f"/_token_{token}/proxy/stream"

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"host", b"mfp.oplay.id")],
        "client": ("10.0.0.1", 12345),
    }
    request = Request(scope)
    seen: dict[str, dict] = {}

    async def call_next(captured: Request):
        seen["query"] = dict(captured.query_params)
        return Response("ok")

    middleware = crypto_utils.EncryptionMiddleware(app=None)
    response = asyncio.run(middleware.dispatch(request, call_next))

    assert response.status_code == 200
    assert seen["query"]["api_password"] == "unit-secret"
    assert seen["query"]["has_encrypted"] is True
    # Uvicorn access logs read the scope afterwards; no plaintext should remain.
    assert scope["path"] == path
    assert scope["query_string"] == b""


def test_encryption_middleware_keeps_signed_refresh_flag_only(monkeypatch):
    handler = EncryptionHandler("unit-secret")
    monkeypatch.setattr(crypto_utils, "encryption_handler", handler)
    token = handler.encrypt_data(
        {"api_password": "unit-secret", "refresh": True, "type": "movie", "id": "550"}, 60
    )
    path = f"/_token_{token}/player/movie/550"

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"host", b"mfp.oplay.id")],
        "client": ("10.0.0.1", 12345),
    }
    request = Request(scope)
    seen: dict[str, dict] = {}

    async def call_next(captured: Request):
        seen["query"] = dict(captured.query_params)
        return Response("ok")

    middleware = crypto_utils.EncryptionMiddleware(app=None)
    response = asyncio.run(middleware.dispatch(request, call_next))

    assert response.status_code == 200
    assert str(seen["query"]["refresh"]).lower() == "true"
    assert seen["query"]["type"] == "movie"
    assert "exclude" not in seen["query"]
    assert "t" not in seen["query"]

    from mediaflow_proxy.main import app

    assert not any("/retry" in getattr(route, "path", "") for route in app.routes)


def test_needs_transcode_detects_hevc_codec_and_url(monkeypatch):
    monkeypatch.setattr(settings, "player_transcode_hevc", True)
    assert _needs_transcode(ProbeResult(codecs=["hev1.1.6.L90.90"]), "https://cdn/a/index.mpd")
    assert _needs_transcode(ProbeResult(), "https://cdn/h265/index_web.mpd")
    assert not _needs_transcode(ProbeResult(codecs=["avc1.640028"]), "https://cdn/a/index.mpd")

    monkeypatch.setattr(settings, "player_transcode_hevc", False)
    assert not _needs_transcode(ProbeResult(codecs=["hev1.1.6.L90.90"]), "https://cdn/h265/index.mpd")


def test_audio_can_be_copied_only_for_aac():
    assert _audio_can_be_copied(ProbeResult(codecs=["hev1.1.6.L90.90", "mp4a.40.2"]))
    assert not _audio_can_be_copied(ProbeResult(codecs=["hev1.1.6.L90.90", "ac-3"]))
    assert _audio_can_be_copied(ProbeResult())


def test_build_ffmpeg_command_bounds_h264_hls_output(monkeypatch):
    monkeypatch.setattr(settings, "player_transcode_preset", "ultrafast")
    monkeypatch.setattr(settings, "player_transcode_crf", 21)
    monkeypatch.setattr(settings, "player_transcode_height", 480)
    monkeypatch.setattr(settings, "player_transcode_hls_time", 3)
    monkeypatch.setattr(settings, "player_transcode_hls_list_size", 0)

    command = build_ffmpeg_command("https://proxy/source.mpd", Path("/tmp/job"))
    assert command[0] == "ffmpeg"
    assert command[command.index("-i") + 1] == "https://proxy/source.mpd"
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-preset") + 1] == "ultrafast"
    assert command[command.index("-crf") + 1] == "21"
    assert command[command.index("-vf") + 1] == "scale=-2:480"
    assert command[command.index("-pix_fmt") + 1] == "yuv420p"
    assert command[command.index("-c:a") + 1] == "copy"
    assert command[command.index("-hls_time") + 1] == "3"
    assert command[command.index("-hls_list_size") + 1] == "0"
    assert "delete_segments" not in command
    assert command[-1] == "/tmp/job/master.m3u8"

    reencoded = build_ffmpeg_command("https://proxy/source.mpd", Path("/tmp/job"), audio_copy=False)
    assert reencoded[reencoded.index("-c:a") + 1] == "aac"


def test_sign_transcode_url_is_opaque_and_decryptable(monkeypatch):
    monkeypatch.setattr(settings, "api_password", "unit-secret")
    url = sign_transcode_url(_request(), "job-123", expiration=120)
    assert url.startswith("https://mfp.oplay.id/_token_")
    assert "/player/transcode/job-123/master.m3u8" in url
    assert "api_password" not in url

    token = url.split("/_token_", 1)[1].split("/player/", 1)[0]
    payload = EncryptionHandler("unit-secret").decrypt_data(token, None)
    assert payload["job"] == "job-123"


def test_serve_transcode_file_rejects_mismatched_token_and_filename():
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/player/transcode/job-123/master.m3u8",
        "raw_path": b"/player/transcode/job-123/master.m3u8",
        "query_string": b"job=other",
        "headers": [(b"host", b"mfp.oplay.id")],
        "client": ("10.0.0.1", 12345),
    }
    request = Request(scope)
    with pytest.raises(HTTPException) as mismatch:
        asyncio.run(serve_transcode_file(request, "job-123", "master.m3u8"))
    assert mismatch.value.status_code == 400

    scope["query_string"] = b""
    request = Request(scope)
    with pytest.raises(HTTPException) as invalid:
        asyncio.run(serve_transcode_file(request, "job-123", "../../etc/passwd"))
    assert invalid.value.status_code == 400
