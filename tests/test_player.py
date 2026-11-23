"""Tests for the server-side smart player and signed gateway handoff."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from mediaflow_proxy.configs import settings
from mediaflow_proxy.player import simulator
from mediaflow_proxy.player.models import Candidate, ProbeResult
from mediaflow_proxy.player.simulator import (
    _audio_can_be_copied,
    _classify_manifest,
    _merge_subtitles,
    _needs_transcode,
    host_configs,
)
from mediaflow_proxy.player.transcoder import (
    build_ffmpeg_command,
    serve_transcode_file,
    sign_transcode_url,
)
from mediaflow_proxy.routes.embed import _headers_for_destination, _sign_player_url
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


def test_host_configs_honours_env_override(monkeypatch):
    monkeypatch.setattr(settings, "player_hosts", "VidLink, VSEmbed")
    names = [config.canonical for config in host_configs()]
    assert names == ["VidLink", "VSEmbed"]


def test_host_configs_default_prefers_known_good_hosts(monkeypatch):
    monkeypatch.setattr(settings, "player_hosts", "")
    names = [config.canonical for config in host_configs()]
    assert names[:4] == ["VidCore", "VSEmbed", "VidSrcTo", "VidLink"]
    assert "VidKing" in names


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

    monkeypatch.setattr(simulator, "create_streamer", fake_streamer)
    monkeypatch.setattr(simulator, "_resolve_host", fake_resolve)
    monkeypatch.setattr(simulator, "probe_candidate", fake_probe)
    monkeypatch.setattr(simulator, "_signed_candidate", fake_signed)
    monkeypatch.setattr(simulator, "get_cached_extractor", no_cache)
    monkeypatch.setattr(simulator, "set_cached_extractor", no_set)
    monkeypatch.setattr(simulator, "_prewarm", lambda job: None)
    return calls


@pytest.mark.asyncio
async def test_simulate_session_stops_at_first_playable_host(monkeypatch):
    monkeypatch.setattr(settings, "player_hosts", "VidCore,VSEmbed,VidLink")
    calls = _patch_simulation(monkeypatch, {"VidCore"})

    session = await simulator.simulate_session(
        _request("/player/movie/1263337"), "movie", "1263337", None, None, start_time=42
    )

    assert [item for item in calls if item[0] in ("resolve", "probe")] == [
        ("resolve", "VidCore"),
        ("probe", "VidCore"),
    ]
    assert len(session["candidates"]) == 1
    assert session["audit"]["chosen_video_host"] == "VidCore"
    assert session["audit"]["retry_available"] is True
    assert session["start_time"] == 42.0


@pytest.mark.asyncio
async def test_simulate_session_skips_excluded_host(monkeypatch):
    monkeypatch.setattr(settings, "player_hosts", "VidCore,VSEmbed,VidLink")
    calls = _patch_simulation(monkeypatch, {"VSEmbed"})

    session = await simulator.simulate_session(
        _request("/player/movie/1263337/retry"),
        "movie",
        "1263337",
        None,
        None,
        retry=True,
        exclude="VidCore",
    )

    assert [host for kind, host in calls if kind == "resolve"] == ["VSEmbed"]
    assert session["audit"]["chosen_video_host"] == "VSEmbed"
    assert session["audit"]["excluded_hosts"] == ["VidCore"]


def test_player_js_retries_inside_iframe():
    player_js = Path(__file__).resolve().parents[1] / "mediaflow_proxy" / "static" / "player.js"
    text = player_js.read_text(encoding="utf-8")
    assert 'query.set("exclude"' in text
    assert 'query.set("t"' in text
    assert "/retry?" in text
    assert "mfp:host" not in text
    assert "start_time" in text


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


def test_encryption_middleware_keeps_retry_query_params(monkeypatch):
    handler = EncryptionHandler("unit-secret")
    monkeypatch.setattr(crypto_utils, "encryption_handler", handler)
    token = handler.encrypt_data({"api_password": "unit-secret"}, 60)
    path = f"/_token_{token}/player/movie/550/retry"

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"exclude=VidCore&t=42",
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
    assert seen["query"]["exclude"] == "VidCore"
    assert seen["query"]["t"] == "42"


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
