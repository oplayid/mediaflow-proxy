"""Tests for the server-to-server EarnVids upload bridge."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import pytest
from fastapi import HTTPException, Request
from pydantic import SecretStr

from mediaflow_proxy.configs import settings
from mediaflow_proxy.routes import earnvids as earnvids_routes

SECRET = "shared-hmac-secret"
RAW_URL = "https://cdn.example/convert-h264/abc.mp4?sign=very-secret-token"
FILE_CODE = "abc123xyz"
PLAYER_LINK = f"https://earnvids.com/{FILE_CODE}.html"


def _request(
    body: dict,
    *,
    timestamp: str | None = None,
    signature: str | None = None,
    secret: str = SECRET,
    app=None,
) -> Request:
    payload = json.dumps(body).encode()
    stamp = timestamp if timestamp is not None else str(int(time.time()))
    if signature is None:
        signature = hmac.new(
            secret.encode(), stamp.encode() + b"." + payload, hashlib.sha256
        ).hexdigest()
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/internal/earnvids/upload",
        "raw_path": b"/internal/earnvids/upload",
        "query_string": b"",
        "headers": [
            (b"host", b"mfp.oplay.id"),
            (b"content-type", b"application/json"),
            (b"x-mfp-timestamp", stamp.encode()),
            (b"x-mfp-signature", signature.encode()),
        ],
        "client": ("10.0.0.1", 12345),
    }
    if app is not None:
        scope["app"] = app

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(scope, receive)


class _FakeAdapter:
    name = "VidLink"

    async def resolve(self, request, media_type, media_id, season, episode):
        return {
            "destination_url": RAW_URL,
            "request_headers": {"User-Agent": "Mozilla/5.0", "Referer": "https://vidlink.pro/"},
            "mediaflow_endpoint": "proxy_stream_endpoint",
            "sources": [
                {
                    "url": RAW_URL,
                    "label": "1080",
                    "request_headers": {"User-Agent": "Mozilla/5.0", "Referer": "https://vidlink.pro/"},
                }
            ],
        }


def _fake_earnvids(
    calls: list[tuple[str, dict[str, Any]]],
    *,
    requests_available: int = 5,
    queue: list[dict] | None = None,
    info: list[dict] | None = None,
    encodings: list[dict] | None = None,
    filecode: str = FILE_CODE,
):
    async def fake_get(path: str, params: dict[str, Any], **kwargs):
        calls.append((path, dict(params)))
        if path == "/file/url_uploads":
            return {"status": 200, "requests_available": requests_available, "result": queue or []}
        if path == "/upload/url":
            return {"status": 200, "result": {"filecode": filecode}}
        if path == "/file/encodings":
            return {"status": 200, "result": encodings if encodings is not None else []}
        if path == "/file/info":
            return {"status": 200, "result": info if info is not None else []}
        if path == "/file/list":
            return {"status": 200, "result": {"files": []}}
        if path == "/file/edit":
            return {"status": 200, "result": "true"}
        raise AssertionError(f"unexpected EarnVids path: {path}")

    return fake_get


def _patch_common(monkeypatch, earnvids_get):
    monkeypatch.setattr(settings, "mfp_earnvids_secret", SecretStr(SECRET))
    monkeypatch.setattr(settings, "earnvids_api_key", SecretStr("earnvids-api-key-secret"))
    monkeypatch.setattr(earnvids_routes, "get_adapter", lambda name: _FakeAdapter())
    monkeypatch.setattr(earnvids_routes, "_earnvids_get", earnvids_get)


@pytest.mark.asyncio
async def test_upload_requires_hmac_configuration(monkeypatch):
    monkeypatch.setattr(settings, "mfp_earnvids_secret", None)
    with pytest.raises(HTTPException) as exc:
        await earnvids_routes.earnvids_upload(_request({"type": "movie", "id": "786892"}))
    assert exc.value.status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "status"),
    [
        ({"signature": "bad-signature"}, 401),
        ({"secret": "another-secret"}, 401),
        ({"timestamp": str(int(time.time()) - 3600)}, 401),
    ],
)
async def test_upload_rejects_bad_hmac(monkeypatch, kwargs, status):
    monkeypatch.setattr(settings, "mfp_earnvids_secret", SecretStr(SECRET))
    with pytest.raises(HTTPException) as exc:
        await earnvids_routes.earnvids_upload(
            _request({"type": "movie", "id": "786892"}, **kwargs)
        )
    assert exc.value.status_code == status


@pytest.mark.asyncio
async def test_upload_signs_real_proxy_url_without_leaking_secrets(monkeypatch):
    from mediaflow_proxy.main import app

    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(settings, "api_password", "unit-api-password")
    _patch_common(
        monkeypatch,
        _fake_earnvids(calls, encodings=[{"link": PLAYER_LINK, "status": "ENCODING", "progress": 15}]),
    )

    response = await earnvids_routes.earnvids_upload(
        _request(
            {"type": "movie", "id": "786892", "quality": "1080", "source": "VidLink"},
            app=app,
        )
    )
    body = response.body.decode()

    upload = next(params for path, params in calls if path == "/upload/url")
    signed = upload["url"]
    assert signed.startswith("https://mfp.oplay.id/_token_")
    assert "api_password" not in signed
    assert RAW_URL not in signed
    assert "sign=very-secret-token" not in signed
    assert "earnvids-api-key-secret" not in signed

    assert json.loads(body) == {
        "filecode": FILE_CODE,
        "source": "VidLink",
        "quality": "1080",
        "player_url": PLAYER_LINK,
        "media": {"type": "movie", "id": "786892", "season": "1", "episode": "1"},
    }
    assert RAW_URL not in body
    assert "earnvids-api-key-secret" not in body
    assert "unit-api-password" not in body
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_upload_checks_url_quota_before_resolving(monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []
    _patch_common(monkeypatch, _fake_earnvids(calls, requests_available=0))
    monkeypatch.setattr(
        earnvids_routes,
        "_sign_asset_url",
        lambda *args, **kwargs: pytest.fail("resolve must not run when quota is exhausted"),
    )

    with pytest.raises(HTTPException) as exc:
        await earnvids_routes.earnvids_upload(_request({"type": "movie", "id": "786892"}))

    assert exc.value.status_code == 429
    assert [path for path, _ in calls] == ["/file/url_uploads"]


@pytest.mark.asyncio
async def test_upload_uses_six_hour_token_and_direct_endpoint(monkeypatch):
    captured: dict = {}
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_sign(request, result, proxy_headers, destination_url, **kwargs):
        captured.update(kwargs)
        captured["destination"] = destination_url
        return "https://mfp.oplay.id/_token_opaque/proxy/stream"

    monkeypatch.setattr(settings, "earnvids_source_token_ttl", 6 * 60 * 60)
    _patch_common(monkeypatch, _fake_earnvids(calls))
    monkeypatch.setattr(earnvids_routes, "_sign_asset_url", fake_sign)

    await earnvids_routes.earnvids_upload(
        _request({"type": "tv", "id": "246473", "season": "2", "episode": "3"})
    )

    assert captured["endpoint"] == "proxy_stream_endpoint"
    assert captured["force_playlist_proxy"] is False
    assert captured["expiration"] == 6 * 60 * 60
    assert captured["destination"] == RAW_URL


@pytest.mark.asyncio
async def test_upload_rejects_invalid_filecode(monkeypatch):
    from mediaflow_proxy.main import app

    calls: list[tuple[str, dict[str, Any]]] = []
    _patch_common(
        monkeypatch, _fake_earnvids(calls, filecode="not valid?!?")
    )
    with pytest.raises(HTTPException) as exc:
        await earnvids_routes.earnvids_upload(
            _request({"type": "movie", "id": "786892"}, app=app)
        )
    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_status_uses_file_info_as_source_of_truth(monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []
    _patch_common(
        monkeypatch,
        _fake_earnvids(
            calls,
            info=[
                {
                    "file_code": FILE_CODE,
                    "canplay": 1,
                    "status": 200,
                    "file_title": "Big Buck Bunny",
                    "file_length": "60",
                }
            ],
            encodings=[
                {"link": PLAYER_LINK, "status": "CONVERTED", "progress": 100, "file_code": FILE_CODE}
            ],
            queue=[{"file_code": FILE_CODE, "status": "CONVERTED", "progress": 100}],
        ),
    )

    response = await earnvids_routes.earnvids_status(_request({"file_code": FILE_CODE}))
    payload = json.loads(response.body.decode())

    assert payload == {
        "filecode": FILE_CODE,
        "state": "uploaded",
        "status": "CONVERTED",
        "progress": 100,
        "canplay": True,
        "player_url": PLAYER_LINK,
        "title": "Big Buck Bunny",
        "duration": 60,
        "error": None,
        "requests_available": 5,
    }
    assert {path for path, _ in calls} == {
        "/file/info",
        "/file/encodings",
        "/file/url_uploads",
    }


@pytest.mark.asyncio
async def test_status_tracks_pending_upload_with_empty_file_code(monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []
    _patch_common(
        monkeypatch,
        _fake_earnvids(
            calls,
            queue=[{"file_code": "", "status": "PENDING", "progress": 0}],
        ),
    )

    response = await earnvids_routes.earnvids_status(_request({"file_code": FILE_CODE}))
    payload = json.loads(response.body.decode())

    assert payload["state"] == "uploading"
    assert payload["status"] == "PENDING"
    assert payload["canplay"] is False
    assert payload["player_url"] == ""


@pytest.mark.asyncio
async def test_status_reports_failed_upload(monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []
    _patch_common(
        monkeypatch,
        _fake_earnvids(
            calls,
            queue=[{"file_code": FILE_CODE, "status": "ERROR", "progress": 0}],
        ),
    )

    response = await earnvids_routes.earnvids_status(_request({"file_code": FILE_CODE}))
    payload = json.loads(response.body.decode())

    assert payload["state"] == "failed"
    assert payload["error"] == "EarnVids: ERROR"


@pytest.mark.asyncio
async def test_info_exposes_only_safe_metadata(monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []
    _patch_common(
        monkeypatch,
        _fake_earnvids(
            calls,
            info=[
                {
                    "file_code": FILE_CODE,
                    "canplay": 1,
                    "status": 200,
                    "file_title": "Judul Aman",
                    "file_length": "120",
                }
            ],
            encodings=[{"link": PLAYER_LINK, "status": "CONVERTED", "progress": 100}],
        ),
    )

    response = await earnvids_routes.earnvids_info(_request({"file_code": FILE_CODE}))
    body = response.body.decode()
    payload = json.loads(body)

    assert payload == {
        "filecode": FILE_CODE,
        "state": "uploaded",
        "canplay": True,
        "player_url": PLAYER_LINK,
        "title": "Judul Aman",
        "duration": 120,
    }
    assert "api_password" not in body
    assert RAW_URL not in body


@pytest.mark.asyncio
async def test_status_rejects_bad_file_code(monkeypatch):
    async def fake_get(path, params, **kwargs):
        pytest.fail("EarnVids must not be called for an invalid file code")

    _patch_common(monkeypatch, fake_get)
    with pytest.raises(HTTPException) as exc:
        await earnvids_routes.earnvids_status(_request({"file_code": "../etc/passwd"}))
    assert exc.value.status_code == 400
