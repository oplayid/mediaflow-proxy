"""Tests for the server-to-server EarnVids upload bridge."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import pytest
from fastapi import HTTPException, Request
from pydantic import SecretStr

from mediaflow_proxy.configs import settings
from mediaflow_proxy.routes import earnvids as earnvids_routes
from mediaflow_proxy.utils import earnvids_upload as upload_util
from mediaflow_proxy.utils.log_redaction import redact_sensitive

SECRET = "shared-hmac-secret"
RAW_URL = "https://cdn.example/convert-h264/abc.mp4?sign=very-secret-token"
FILE_CODE = "abc123xyz"
PLAYER_LINK = f"https://earnvids.com/{FILE_CODE}.html"
SIGNED_URL = "https://mfp.oplay.id/_token_opaque/proxy/hls/manifest.m3u8"


def _request(
    body: dict,
    *,
    timestamp: str | None = None,
    signature: str | None = None,
    secret: str = SECRET,
    app=None,
    path: str = "/internal/earnvids/upload",
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
        "path": path,
        "raw_path": path.encode(),
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
    def __init__(self, name: str = "VidLink"):
        self.name = name

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


class _FailingAdapter:
    def __init__(self, name: str = "VidCore"):
        self.name = name

    async def resolve(self, request, media_type, media_id, season, episode):
        raise RuntimeError("resolver failed")


def _fake_earnvids(
    calls: list[tuple[str, dict[str, Any]]],
    *,
    requests_available: int = 5,
    queue: list[dict] | None = None,
    info: list[dict] | None = None,
    encodings: list[dict] | None = None,
):
    async def fake_get(path: str, params: dict[str, Any], **kwargs):
        calls.append((path, dict(params)))
        if path == "/file/url_uploads":
            return {"status": 200, "requests_available": requests_available, "result": queue or []}
        if path == "/upload/server":
            return {"status": 200, "result": "https://s1.earnvids.example/upload/01"}
        if path == "/file/encodings":
            return {"status": 200, "result": encodings if encodings is not None else []}
        if path == "/file/info":
            return {"status": 200, "result": info if info is not None else []}
        if path == "/file/list":
            return {"status": 200, "result": {"files": []}}
        if path == "/file/edit":
            return {"status": 200, "result": "true"}
        if path == "/file/url_actions":
            return {"status": 200, "requests_available": requests_available}
        raise AssertionError(f"unexpected EarnVids path: {path}")

    return fake_get


def _patch_common(monkeypatch, earnvids_get=None):
    monkeypatch.setattr(settings, "mfp_earnvids_secret", SecretStr(SECRET))
    monkeypatch.setattr(settings, "earnvids_api_key", SecretStr("earnvids-api-key-secret"))
    monkeypatch.setattr(settings, "tmdb_api_key", None)
    monkeypatch.setattr(earnvids_routes, "get_adapter", lambda name: _FakeAdapter())
    monkeypatch.setattr(
        earnvids_routes,
        "configured_servers",
        lambda: [_FakeAdapter("VidCore"), _FakeAdapter("VidLink")],
    )
    if earnvids_get is not None:
        monkeypatch.setattr(earnvids_routes, "_earnvids_get", earnvids_get)
    # Background jobs are tested separately; never start ffmpeg from a route test.
    monkeypatch.setattr(earnvids_routes, "spawn_job", lambda coroutine: coroutine.close())
    monkeypatch.setattr(earnvids_routes, "store_job", lambda job_id, state: _noop_async())
    monkeypatch.setattr(earnvids_routes, "load_job", lambda job_id: _noop_async({}))


async def _noop_async(value=None):
    return value


async def _noop_download(*_args, **_kwargs):
    return None


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
async def test_upload_returns_opaque_job_and_does_not_call_earnvids(monkeypatch):
    from mediaflow_proxy.main import app

    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(settings, "api_password", "unit-api-password")
    _patch_common(monkeypatch, _fake_earnvids(calls))

    response = await earnvids_routes.earnvids_upload(
        _request(
            {"type": "movie", "id": "786892", "quality": "1080", "source": "VidLink"},
            app=app,
        )
    )
    body = response.body.decode()
    payload = json.loads(body)

    assert response.status_code == 202
    assert payload["status"] == "queued"
    assert upload_util.valid_job_id(payload["job_id"])
    assert payload["media"] == {
        "type": "movie",
        "id": "786892",
        "season": "1",
        "episode": "1",
    }
    assert "filecode" not in payload
    assert "source" not in payload
    assert "player_url" not in payload
    assert calls == []
    for secret in (RAW_URL, "sign=very-secret-token", "earnvids-api-key-secret", "unit-api-password"):
        assert secret not in body


@pytest.mark.asyncio
async def test_upload_signs_source_with_six_hour_token(monkeypatch):
    captured: dict[str, Any] = {}
    calls: list[tuple[str, dict[str, Any]]] = []
    _patch_common(monkeypatch, _fake_earnvids(calls))

    def fake_sign(request, result, proxy_headers, destination_url, **kwargs):
        captured.update(kwargs)
        captured["destination"] = destination_url
        return SIGNED_URL

    monkeypatch.setattr(settings, "earnvids_source_token_ttl", 6 * 60 * 60)
    monkeypatch.setattr(earnvids_routes, "_sign_asset_url", fake_sign)
    await earnvids_routes.earnvids_upload(
        _request({"type": "tv", "id": "246473", "season": "2", "episode": "3"})
    )

    assert captured["endpoint"] == "proxy_stream_endpoint"
    assert captured["force_playlist_proxy"] is False
    assert captured["expiration"] == 6 * 60 * 60
    assert captured["destination"] == RAW_URL


@pytest.mark.asyncio
async def test_upload_uses_tmdb_metadata_without_client_title(monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []
    _patch_common(monkeypatch, _fake_earnvids(calls))

    async def fake_tmdb(media_type, media_id):
        return {"title": "Judul Dari TMDB", "year": "2024", "overview": "Sinopsis resmi"}

    monkeypatch.setattr(earnvids_routes, "_tmdb_metadata", fake_tmdb)
    from mediaflow_proxy.main import app

    response = await earnvids_routes.earnvids_upload(
        _request({"type": "movie", "id": "786892", "title": "Judul Dari Client"}, app=app)
    )
    payload = json.loads(response.body.decode())

    assert payload["title"] == "Judul Dari TMDB"
    assert payload["year"] == "2024"
    assert payload["overview"] == "Sinopsis resmi"
    assert "Judul Dari Client" not in response.body.decode()
    assert calls == []


@pytest.mark.asyncio
async def test_auto_upload_uses_first_successful_server(monkeypatch):
    from mediaflow_proxy.main import app

    stored: list[dict[str, Any]] = []
    calls: list[tuple[str, dict[str, Any]]] = []
    _patch_common(monkeypatch, _fake_earnvids(calls))
    monkeypatch.setattr(
        earnvids_routes,
        "configured_servers",
        lambda: [_FailingAdapter("VidCore"), _FakeAdapter("VidSrc")],
    )

    async def fake_store(job_id, state):
        stored.append(dict(state))

    monkeypatch.setattr(earnvids_routes, "store_job", fake_store)
    await earnvids_routes.earnvids_upload(
        _request({"type": "movie", "id": "786892"}, app=app)
    )

    assert stored[0]["source"] == "VidSrc"
    assert stored[0]["state"] == "queued"
    assert SIGNED_URL not in json.dumps(stored)


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
        "job_id": "",
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
async def test_status_follows_job_state_before_filecode_exists(monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []
    _patch_common(monkeypatch, _fake_earnvids(calls))

    async def fake_load(job_id):
        return {
            "job_id": job_id,
            "state": "uploading",
            "status": "UPLOADING",
            "filecode": "",
            "progress": 45,
            "title": "Sedang diunggah",
            "error": "",
        }

    monkeypatch.setattr(earnvids_routes, "load_job", fake_load)
    response = await earnvids_routes.earnvids_status(
        _request({"job_id": "job123"}, path="/internal/earnvids/status")
    )
    payload = json.loads(response.body.decode())

    assert payload["filecode"] == ""
    assert payload["job_id"] == "job123"
    assert payload["state"] == "uploading"
    assert payload["progress"] == 45
    assert payload["title"] == "Sedang diunggah"
    assert calls == []


@pytest.mark.asyncio
async def test_status_requires_file_code_or_job_id(monkeypatch):
    _patch_common(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await earnvids_routes.earnvids_status(
            _request({}, path="/internal/earnvids/status")
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_status_rejects_bad_file_code(monkeypatch):
    async def fake_get(path, params, **kwargs):
        pytest.fail("EarnVids must not be called for an invalid file code")

    _patch_common(monkeypatch, fake_get)
    with pytest.raises(HTTPException) as exc:
        await earnvids_routes.earnvids_status(
            _request({"file_code": "../etc/passwd"}, path="/internal/earnvids/status")
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_action_deletes_only_requested_code(monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []
    _patch_common(monkeypatch, _fake_earnvids(calls))
    response = await earnvids_routes.earnvids_action(
        _request(
            {"action": "delete", "file_code": FILE_CODE},
            path="/internal/earnvids/action",
        )
    )

    payload = json.loads(response.body.decode())
    assert payload["deleted"] == FILE_CODE
    assert calls == [("/file/url_actions", {"delete_code": FILE_CODE})]
    assert all("restart_errors" not in params for _, params in calls)


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

    response = await earnvids_routes.earnvids_info(
        _request({"file_code": FILE_CODE}, path="/internal/earnvids/info")
    )
    payload = json.loads(response.body.decode())

    assert payload == {
        "filecode": FILE_CODE,
        "state": "uploaded",
        "canplay": True,
        "player_url": PLAYER_LINK,
        "title": "Judul Aman",
        "duration": 120,
    }
    assert RAW_URL not in response.body.decode()


@pytest.mark.asyncio
async def test_upload_job_converts_and_stores_filecode(monkeypatch, tmp_path):
    output = tmp_path / "upload.mp4"
    output.write_bytes(b"0" * 2048)
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_convert(job_id, source_url):
        assert source_url == SIGNED_URL
        return output

    async def fake_post(upload_url, path, title, metadata, options):
        assert upload_url == "https://s1.earnvids.example/upload/01"
        return FILE_CODE

    monkeypatch.setattr(settings, "mfp_earnvids_secret", SecretStr(SECRET))
    monkeypatch.setattr(settings, "earnvids_api_key", SecretStr("earnvids-api-key-secret"))
    monkeypatch.setattr(settings, "tmdb_api_key", None)
    monkeypatch.setattr(earnvids_routes, "_earnvids_get", _fake_earnvids(calls))
    monkeypatch.setattr(earnvids_routes, "convert_source_to_file", fake_convert)
    monkeypatch.setattr(earnvids_routes, "_earnvids_post_file", fake_post)
    monkeypatch.setattr(earnvids_routes, "cleanup_job", lambda job_id: None)

    states: dict[str, dict[str, Any]] = {}

    async def fake_store(job_id, state):
        states[job_id] = dict(state)

    async def fake_load(job_id):
        return dict(states.get(job_id) or {})

    monkeypatch.setattr(earnvids_routes, "store_job", fake_store)
    monkeypatch.setattr(earnvids_routes, "load_job", fake_load)

    await earnvids_routes._run_upload_job(
        "job123",
        SIGNED_URL,
        {"job_id": "job123", "title": "Judul", "year": "2024", "overview": "Sinopsis"},
        {"file_public": True},
    )

    final = states["job123"]
    assert final["filecode"] == FILE_CODE
    assert final["state"] in ("uploaded", "encoding")
    assert final["progress"] >= 70
    assert SIGNED_URL not in json.dumps(final)


@pytest.mark.asyncio
async def test_upload_job_failure_logs_redacted_detail(monkeypatch, caplog):
    async def fake_convert(*_args, **_kwargs):
        raise RuntimeError(
            f"segment HTTP 403 url={SIGNED_URL} sign=very-secret-token api_password=hunter2"
        )

    async def fake_store(job_id, state):
        return None

    async def fake_load(job_id):
        return {}

    monkeypatch.setattr(earnvids_routes, "convert_source_to_file", fake_convert)
    monkeypatch.setattr(earnvids_routes, "store_job", fake_store)
    monkeypatch.setattr(earnvids_routes, "load_job", fake_load)
    monkeypatch.setattr(earnvids_routes, "cleanup_job", lambda job_id: None)

    with caplog.at_level(logging.WARNING, logger="mediaflow_proxy.routes.earnvids"):
        await earnvids_routes._run_upload_job(
            "job456", SIGNED_URL, {}, {"file_public": True}
        )

    logged = caplog.text
    assert "failed: RuntimeError" in logged
    assert "segment HTTP 403" in logged
    assert SIGNED_URL not in logged
    assert "very-secret-token" not in logged
    assert "hunter2" not in logged
    assert "<url-redacted:" in logged


@pytest.mark.asyncio
async def test_multipart_upload_returns_filecode(monkeypatch, tmp_path):
    path = tmp_path / "upload.mp4"
    path.write_bytes(b"0" * 2048)
    captured: dict[str, Any] = {}

    class _FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def text(self, errors="ignore"):
            return json.dumps(
                {"status": 200, "msg": "OK", "files": [{"filecode": FILE_CODE, "status": "OK"}]}
            )

    class _FakeSession:
        def post(self, url, data=None, timeout=None):
            captured["url"] = url
            captured["form"] = data
            return _FakeResponse()

        async def close(self):
            return None

    class _FakeStreamer:
        session = _FakeSession()

    async def fake_create_streamer(url):
        return _FakeStreamer()

    monkeypatch.setattr(settings, "earnvids_api_key", SecretStr("earnvids-api-key-secret"))
    monkeypatch.setattr(earnvids_routes, "create_streamer", fake_create_streamer)

    result = await earnvids_routes._earnvids_post_file(
        "https://s1.earnvids.example/upload/01",
        path,
        "Judul Aman",
        {"overview": "Sinopsis"},
        {"file_public": True},
    )

    assert result == FILE_CODE
    assert captured["url"] == "https://s1.earnvids.example/upload/01"


def test_ffmpeg_command_is_bounded_and_redaction_hides_source():
    command = upload_util.build_ffmpeg_command(SIGNED_URL, Path("/tmp/upload.mp4"))

    assert command[command.index("-i") + 1] == SIGNED_URL
    assert "-t" in command
    assert "-fs" in command
    assert "+faststart" in command
    assert "mp4" in command

    redacted = redact_sensitive(
        f"[get_or_download] CACHE MISS: {RAW_URL} token={SECRET} api_password=hunter2"
    )
    assert "cdn.example" not in redacted
    assert "very-secret-token" not in redacted
    assert SECRET not in redacted
    assert "hunter2" not in redacted
    assert "<url-redacted:" in redacted


def test_ffmpeg_copy_command_remuxes_without_reencode():
    command = upload_util.build_ffmpeg_command(SIGNED_URL, Path("/tmp/upload.mp4"), copy=True)

    assert command[command.index("-i") + 1] == SIGNED_URL
    assert command[command.index("-c") + 1] == "copy"
    assert "aac_adtstoasc" in command
    assert "-t" in command
    assert "-fs" in command
    assert "+faststart" in command
    assert "libx264" not in command


@pytest.mark.asyncio
async def test_convert_prefers_copy_without_transcoding(monkeypatch, tmp_path):
    directory = tmp_path / "job"
    output = directory / "upload.mp4"
    attempts: list[str] = []

    async def fake_attempt(job_id, command, timeout):
        attempts.append("copy" if "copy" in command else "transcode")
        directory.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"0" * 2048)
        return True

    monkeypatch.setattr(upload_util.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(upload_util, "job_dir", lambda job_id: directory)
    monkeypatch.setattr(upload_util, "_ffmpeg_attempt", fake_attempt)
    monkeypatch.setattr(upload_util, "download_hls_source", _noop_download)

    result = await upload_util.convert_source_to_file("job123", SIGNED_URL)

    assert result == output
    assert attempts == ["copy"]


@pytest.mark.asyncio
async def test_convert_falls_back_to_transcode_when_copy_output_invalid(monkeypatch, tmp_path):
    directory = tmp_path / "job"
    output = directory / "upload.mp4"
    attempts: list[str] = []

    async def fake_attempt(job_id, command, timeout):
        mode = "copy" if "copy" in command else "transcode"
        attempts.append(mode)
        directory.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"0" * (100 if mode == "copy" else 2048))
        return True

    monkeypatch.setattr(upload_util.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(upload_util, "job_dir", lambda job_id: directory)
    monkeypatch.setattr(upload_util, "_ffmpeg_attempt", fake_attempt)
    monkeypatch.setattr(upload_util, "download_hls_source", _noop_download)

    result = await upload_util.convert_source_to_file("job123", SIGNED_URL)

    assert result == output
    assert attempts == ["copy", "transcode"]


@pytest.mark.asyncio
async def test_convert_timeout_does_not_start_transcode(monkeypatch, tmp_path):
    async def fake_attempt(job_id, command, timeout):
        if "copy" in command:
            raise RuntimeError("ffmpeg timeout")
        pytest.fail("transcode must not start after a stalled remux")

    monkeypatch.setattr(upload_util.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(upload_util, "job_dir", lambda job_id: tmp_path / "job")
    monkeypatch.setattr(upload_util, "_ffmpeg_attempt", fake_attempt)
    monkeypatch.setattr(upload_util, "download_hls_source", _noop_download)

    with pytest.raises(RuntimeError, match="ffmpeg timeout"):
        await upload_util.convert_source_to_file("job123", SIGNED_URL)


def test_parallel_download_defaults_are_configured():
    fields = type(settings).model_fields
    assert fields["earnvids_download_concurrency"].default == 8
    assert fields["earnvids_stall_timeout"].default == 600


class _FakeResponse:
    def __init__(
        self,
        *,
        text: str = "",
        payload: bytes = b"",
        headers: dict[str, str] | None = None,
        tracker: Any = None,
        url: str = "",
    ) -> None:
        self.status = 200
        self._text = text
        self._payload = payload
        self.headers = headers or {}
        self.content = self
        self._tracker = tracker
        self._url = url

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def text(self, errors: str = "ignore") -> str:
        return self._text

    async def read(self) -> bytes:
        return self._payload

    def iter_chunked(self, size: int):
        async def chunks():
            if self._tracker is None:
                if self._payload:
                    yield self._payload
                return
            tracker = self._tracker
            async with tracker.condition:
                tracker.active += 1
                tracker.peak = max(tracker.peak, tracker.active)
                tracker.condition.notify_all()
            try:
                # Hold every segment until the configured number of requests
                # is in flight. A sequential downloader times out here.
                async with tracker.condition:
                    await asyncio.wait_for(
                        tracker.condition.wait_for(
                            lambda: tracker.active >= tracker.concurrency
                        ),
                        timeout=1.0,
                    )
                index = int(self._url.rsplit("seg-", 1)[1].split(".")[0])
                # Finish in reverse order to prove the assembled file keeps
                # playlist order rather than completion order.
                await asyncio.sleep((tracker.total - index) * 0.01)
                for offset in range(0, len(self._payload), size):
                    yield self._payload[offset : offset + size]
            finally:
                async with tracker.condition:
                    tracker.active -= 1

        return chunks()


class _FakeSegmentSession:
    def __init__(
        self,
        texts: dict[str, str],
        bodies: dict[str, bytes],
        concurrency: int,
        keys: dict[str, bytes] | None = None,
    ) -> None:
        self.texts = texts
        self.bodies = bodies
        self.keys = keys or {}
        self.concurrency = concurrency
        self.total = len(bodies)
        self.condition = asyncio.Condition()
        self.active = 0
        self.peak = 0
        self.requests: list[str] = []

    def get(self, url: str, *, proxy=None, timeout=None) -> _FakeResponse:
        self.requests.append(url)
        if url in self.texts:
            return _FakeResponse(text=self.texts[url])
        if url in self.keys:
            payload = self.keys[url]
            return _FakeResponse(
                payload=payload,
                headers={"Content-Length": str(len(payload))},
            )
        payload = self.bodies[url]
        return _FakeResponse(
            payload=payload,
            headers={"Content-Length": str(len(payload))},
            tracker=self,
            url=url,
        )


@pytest.mark.asyncio
async def test_hls_download_is_parallel_and_ordered(monkeypatch, tmp_path):
    variant_url = urljoin(SIGNED_URL, "variant.m3u8")
    segment_urls = [urljoin(variant_url, f"seg-{index}.ts") for index in range(6)]
    master = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000\nvariant.m3u8\n"
    variant = "#EXTM3U\n#EXT-X-TARGETDURATION:4\n" + "".join(
        f"#EXTINF:4,\nseg-{index}.ts\n" for index in range(6)
    )
    bodies = {
        url: f"seg{index}:".encode() + bytes([65 + index]) * 64
        for index, url in enumerate(segment_urls)
    }
    tracker = _FakeSegmentSession(
        {SIGNED_URL: master, variant_url: variant}, bodies, concurrency=3
    )

    @contextlib.asynccontextmanager
    async def fake_session(*_args, **_kwargs):
        yield tracker, None

    monkeypatch.setattr(settings, "earnvids_download_concurrency", 3)
    monkeypatch.setattr(upload_util, "create_aiohttp_session", fake_session)
    monkeypatch.setattr(upload_util, "job_dir", lambda job_id: tmp_path / "job")

    result = await upload_util.download_hls_source("job123", SIGNED_URL)

    assert result == tmp_path / "job" / "input.ts"
    assert result.read_bytes() == b"".join(bodies[url] for url in segment_urls)
    assert tracker.peak == 3
    assert sum(url.endswith(".ts") for url in tracker.requests) == 6
    assert not list((tmp_path / "job").glob("segment-*.ts"))


def test_ffmpeg_command_allows_local_encrypted_playlist():
    playlist = str(Path("/tmp/job") / "local.m3u8")
    command = upload_util.build_ffmpeg_command(
        playlist, Path("/tmp/upload.mp4"), copy=True, local_playlist=True
    )

    assert command[command.index("-i") + 1] == playlist
    assert command[command.index("-allowed_extensions") + 1] == "ALL"
    assert "crypto" in command[command.index("-protocol_whitelist") + 1]
    assert command.index("-allowed_extensions") < command.index("-i")
    assert command[command.index("-c") + 1] == "copy"


@pytest.mark.asyncio
async def test_encrypted_hls_download_uses_parallel_local_playlist(
    monkeypatch, tmp_path, caplog
):
    variant_url = urljoin(SIGNED_URL, "variant.m3u8")
    key_url = urljoin(variant_url, "key.bin")
    segment_urls = [urljoin(variant_url, f"seg-{index}.ts") for index in range(4)]
    master = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000\nvariant.m3u8\n"
    variant = (
        "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:7\n"
        '#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x0102030405060708090a0b0c0d0e0f10\n'
        + "".join(f"#EXTINF:4,\nseg-{index}.ts\n" for index in range(4))
        + "#EXT-X-ENDLIST\n"
    )
    key_bytes = bytes(range(16))
    bodies = {
        url: f"seg{index}:".encode() + bytes([65 + index]) * 32
        for index, url in enumerate(segment_urls)
    }
    tracker = _FakeSegmentSession(
        {SIGNED_URL: master, variant_url: variant},
        bodies,
        concurrency=2,
        keys={key_url: key_bytes},
    )

    @contextlib.asynccontextmanager
    async def fake_session(*_args, **_kwargs):
        yield tracker, None

    monkeypatch.setattr(settings, "earnvids_download_concurrency", 2)
    monkeypatch.setattr(upload_util, "create_aiohttp_session", fake_session)
    monkeypatch.setattr(upload_util, "job_dir", lambda job_id: tmp_path / "job")

    with caplog.at_level(logging.INFO, logger="mediaflow_proxy.utils.earnvids_upload"):
        result = await upload_util.download_hls_source("job456", SIGNED_URL)

    job_path = tmp_path / "job"
    assert result == job_path / "local.m3u8"
    assert not (job_path / "input.ts").exists()
    assert (job_path / "key-000000.bin").read_bytes() == key_bytes
    assert (job_path / "key-000000.bin").stat().st_mode & 0o777 == 0o600
    assert all((job_path / f"segment-{index:06d}.ts").exists() for index in range(4))

    playlist = result.read_text(encoding="utf-8")
    assert 'URI="key-000000.bin"' in playlist
    assert 'URI="key.bin"' not in playlist
    assert "#EXT-X-MEDIA-SEQUENCE:7" in playlist
    assert "#EXT-X-ENDLIST" in playlist
    assert "segment-000000.ts" in playlist
    assert "segment-000003.ts" in playlist

    assert tracker.peak == 2
    assert tracker.requests.count(key_url) == 1
    assert sum(url.endswith(".ts") for url in tracker.requests) == 4

    logged = caplog.text
    assert "keys=1" in logged
    assert "stage=manifest" in logged
    assert "stage=variant" in logged
    assert "stage=key bytes=16" in logged
    assert "stage=segment count=4 concurrency=2" in logged
    assert "key.bin" not in logged
    assert "cdn.example" not in logged
    assert SIGNED_URL not in logged


@pytest.mark.asyncio
async def test_unsupported_hls_key_method_falls_back(monkeypatch, tmp_path):
    variant_url = urljoin(SIGNED_URL, "variant.m3u8")
    key_url = urljoin(variant_url, "key.bin")
    segment_urls = [urljoin(variant_url, f"seg-{index}.ts") for index in range(2)]
    master = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000\nvariant.m3u8\n"
    variant = (
        "#EXTM3U\n"
        '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key.bin"\n'
        + "".join(f"#EXTINF:4,\nseg-{index}.ts\n" for index in range(2))
    )
    bodies = {url: b"x" * 16 for url in segment_urls}
    tracker = _FakeSegmentSession({SIGNED_URL: master, variant_url: variant}, bodies, 2)

    @contextlib.asynccontextmanager
    async def fake_session(*_args, **_kwargs):
        yield tracker, None

    monkeypatch.setattr(upload_util, "create_aiohttp_session", fake_session)
    monkeypatch.setattr(upload_util, "job_dir", lambda job_id: tmp_path / "job")

    result = await upload_util.download_hls_source("job789", SIGNED_URL)

    assert result is None
    assert key_url not in tracker.requests
    assert not list((tmp_path / "job").glob("segment-*.ts"))


@pytest.mark.asyncio
async def test_convert_passes_local_playlist_flags(monkeypatch, tmp_path, caplog):
    directory = tmp_path / "job"
    playlist = directory / "local.m3u8"
    output = directory / "upload.mp4"
    captured: list[list[str]] = []

    async def fake_download(*_args, **_kwargs):
        return playlist

    async def fake_attempt(job_id, command, timeout):
        captured.append(command)
        output.write_bytes(b"0" * 2048)
        return True

    monkeypatch.setattr(upload_util.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(upload_util, "job_dir", lambda job_id: directory)
    monkeypatch.setattr(upload_util, "download_hls_source", fake_download)
    monkeypatch.setattr(upload_util, "_ffmpeg_attempt", fake_attempt)

    with caplog.at_level(logging.INFO, logger="mediaflow_proxy.utils.earnvids_upload"):
        result = await upload_util.convert_source_to_file("job123", SIGNED_URL)

    assert result == output
    assert captured and all("-allowed_extensions" in command for command in captured)
    assert captured[0][captured[0].index("-i") + 1] == str(playlist)
    assert "stage=download" in caplog.text
    assert "stage=remux local_playlist=true" in caplog.text
