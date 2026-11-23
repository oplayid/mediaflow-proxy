"""Tests for the server-side smart player and signed gateway handoff."""

from __future__ import annotations

import asyncio

from starlette.requests import Request
from starlette.responses import Response

from mediaflow_proxy.configs import settings
from mediaflow_proxy.player.models import Candidate, ProbeResult
from mediaflow_proxy.player.simulator import _classify_manifest, _merge_subtitles, host_configs
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
    assert names[:3] == ["VSEmbed", "VidCore", "VidLink"]
    assert "VidKing" in names


def test_classify_manifest():
    assert _classify_manifest("#EXTM3U\n#EXT-X-VERSION:3\n", "text/plain", "https://x/a") == "hls"
    assert _classify_manifest('<?xml version="1.0"?><MPD></MPD>', "application/dash+xml", "https://x/a") == "dash"
    assert _classify_manifest("", "video/mp4", "https://x/a") == "mp4"
    assert _classify_manifest("hello", "text/html", "https://x/a") == "unknown"


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
