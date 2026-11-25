"""Tests for the server-to-server EarnVids upload bridge."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from fastapi import HTTPException, Request
from pydantic import SecretStr

from mediaflow_proxy.configs import settings
from mediaflow_proxy.routes import earnvids as earnvids_routes

SECRET = "shared-hmac-secret"
RAW_URL = "https://cdn.example/convert-h264/abc.mp4?sign=very-secret-token"


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

    seen: dict = {}

    async def fake_get(path, params):
        seen["path"] = path
        seen["params"] = params
        return {"status": 200, "result": {"filecode": "abc123xyz"}}

    monkeypatch.setattr(settings, "api_password", "unit-api-password")
    _patch_common(monkeypatch, fake_get)

    response = await earnvids_routes.earnvids_upload(
        _request(
            {"type": "movie", "id": "786892", "quality": "1080", "source": "VidLink"},
            app=app,
        )
    )
    body = response.body.decode()

    assert seen["path"] == "/upload/url"
    signed = seen["params"]["url"]
    assert signed.startswith("https://mfp.oplay.id/_token_")
    assert "api_password" not in signed
    assert RAW_URL not in signed
    assert "sign=very-secret-token" not in signed
    assert "earnvids-api-key-secret" not in signed

    assert json.loads(body) == {
        "filecode": "abc123xyz",
        "source": "VidLink",
        "quality": "1080",
        "media": {"type": "movie", "id": "786892", "season": "1", "episode": "1"},
    }
    assert RAW_URL not in body
    assert "earnvids-api-key-secret" not in body
    assert "unit-api-password" not in body
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_upload_uses_six_hour_token_and_direct_endpoint(monkeypatch):
    captured: dict = {}

    async def fake_get(path, params):
        return {"status": 200, "result": {"filecode": "abc123xyz"}}

    def fake_sign(request, result, proxy_headers, destination_url, **kwargs):
        captured.update(kwargs)
        captured["destination"] = destination_url
        return "https://mfp.oplay.id/_token_opaque/proxy/stream"

    monkeypatch.setattr(settings, "earnvids_source_token_ttl", 6 * 60 * 60)
    _patch_common(monkeypatch, fake_get)
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

    async def fake_get(path, params):
        return {"status": 200, "result": {"filecode": "not valid?!?"}}

    _patch_common(monkeypatch, fake_get)
    with pytest.raises(HTTPException) as exc:
        await earnvids_routes.earnvids_upload(
            _request({"type": "movie", "id": "786892"}, app=app)
        )
    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_status_parses_url_upload_result(monkeypatch):
    async def fake_get(path, params):
        assert path == "/file/url_uploads"
        assert params == {"file_code": "abc123xyz"}
        return {
            "status": 200,
            "result": [
                {"file_code": "other", "status": "ERROR", "progress": 0},
                {"file_code": "abc123xyz", "status": "CONVERTED", "progress": 100},
            ],
        }

    _patch_common(monkeypatch, fake_get)
    response = await earnvids_routes.earnvids_status(_request({"file_code": "abc123xyz"}))
    assert json.loads(response.body.decode()) == {
        "filecode": "abc123xyz",
        "status": "CONVERTED",
        "progress": 100,
    }


@pytest.mark.asyncio
async def test_status_rejects_bad_file_code(monkeypatch):
    async def fake_get(path, params):
        pytest.fail("EarnVids must not be called for an invalid file code")

    _patch_common(monkeypatch, fake_get)
    with pytest.raises(HTTPException) as exc:
        await earnvids_routes.earnvids_status(_request({"file_code": "../etc/passwd"}))
    assert exc.value.status_code == 400
