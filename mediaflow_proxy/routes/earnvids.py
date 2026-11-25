"""Server-to-server bridge that uploads one converted MP4 to EarnVids.

StreamFlix never sees a provider URL or a signed query. It sends only media
identity and an HMAC made with ``MFP_EARNVIDS_SECRET``. mfp resolves the
requested source, signs a same-origin proxy URL, remuxes it into a bounded
temporary MP4 and uploads that file to EarnVids' ``upload/server`` endpoint.
The EarnVids API key and all upstream headers stay on this origin.

Uploads run as background jobs so a feature-length film is not bound by the
120-second gunicorn request timeout. Job state is shared through Redis and
never contains the signed source URL. Final playback state is derived from
``file/info``, ``file/encodings`` and ``file/url_uploads``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse, urlunparse

from aiohttp import ClientTimeout, FormData
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from mediaflow_proxy.configs import settings
from mediaflow_proxy.player.adapters import configured_servers, get_adapter
from mediaflow_proxy.routes.embed import _sign_asset_url
from mediaflow_proxy.utils.earnvids_upload import (
    cleanup_job,
    convert_source_to_file,
    load_job,
    new_job_id,
    spawn_job,
    store_job,
    valid_job_id,
)
from mediaflow_proxy.utils.http_utils import create_streamer, get_proxy_headers
from mediaflow_proxy.utils.log_redaction import redact_sensitive

logger = logging.getLogger(__name__)
earnvids_router = APIRouter()

EARNVIDS_API = "https://earnvidsapi.com/api"
TMDB_BASE = "https://api.themoviedb.org/3"
TIMESTAMP_SKEW = 300
TMDB_CACHE_TTL = 6 * 60 * 60
_FILE_CODE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_TMDB_CACHE: dict[tuple[str, str], tuple[float, dict[str, str]]] = {}


def _secret(name: str) -> str:
    value = getattr(settings, name, None)
    if value is None:
        return ""
    return value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)


def _signature(secret: str, timestamp: str, body: bytes) -> str:
    """HMAC over ``timestamp + '.' + raw body``; both sides use the same bytes."""
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


async def _authorize(request: Request, body: bytes) -> None:
    secret = _secret("mfp_earnvids_secret")
    if not secret:
        raise HTTPException(status_code=503, detail="EarnVids bridge belum dikonfigurasi")
    timestamp = request.headers.get("x-mfp-timestamp", "")
    supplied = request.headers.get("x-mfp-signature", "")
    if not timestamp.isdigit() or abs(int(time.time()) - int(timestamp)) > TIMESTAMP_SKEW:
        raise HTTPException(status_code=401, detail="Timestamp permintaan tidak valid")
    if not supplied or not hmac.compare_digest(supplied, _signature(secret, timestamp, body)):
        raise HTTPException(status_code=401, detail="Tanda tangan permintaan tidak valid")


async def _json_body(request: Request) -> dict[str, Any]:
    body = await request.body()
    await _authorize(request, body)
    try:
        data = json.loads(body or b"{}")
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Body JSON tidak valid")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Body JSON tidak valid")
    return data


def _media_from(data: dict[str, Any]) -> tuple[str, str, str, str]:
    media_type = str(data.get("type") or "").strip().lower()
    if media_type not in ("movie", "tv"):
        raise HTTPException(status_code=400, detail="type harus movie atau tv")
    media_id = re.sub(r"^tv-", "", str(data.get("id") or "").strip(), flags=re.IGNORECASE)
    if not media_id.isdigit():
        raise HTTPException(status_code=400, detail="id harus berupa TMDB id numerik")

    def positive(value: Any, default: str = "1") -> str:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return str(number if number > 0 else 1)

    return media_type, media_id, positive(data.get("season")), positive(data.get("episode"))


def _file_code_from(data: dict[str, Any]) -> str:
    file_code = str(data.get("file_code") or "").strip()
    if not file_code or not _FILE_CODE_RE.match(file_code):
        raise HTTPException(status_code=400, detail="file_code tidak valid")
    return file_code


def _job_id_from(data: dict[str, Any]) -> str:
    job_id = str(data.get("job_id") or "").strip()
    if not valid_job_id(job_id):
        raise HTTPException(status_code=400, detail="job_id tidak valid")
    return job_id


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _verified_player_link(value: Any, file_code: str) -> str:
    """Return an https player link that actually belongs to ``file_code``."""
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    name = re.escape(file_code)
    if not re.search(rf"/(?:embed-)?{name}\.html?$", parsed.path, flags=re.IGNORECASE):
        return ""
    return urlunparse(("https", parsed.netloc, parsed.path, parsed.params, parsed.query, ""))


async def _tmdb_metadata(media_type: str, media_id: str) -> dict[str, str]:
    """Fetch browser-safe title metadata from TMDB; never raise on lookup failure."""
    cache_key = (media_type, media_id)
    cached = _TMDB_CACHE.get(cache_key)
    if cached and time.time() - cached[0] < TMDB_CACHE_TTL:
        return dict(cached[1])

    api_key = _secret("tmdb_api_key")
    if not api_key:
        return {}

    url = f"{TMDB_BASE}/{media_type}/{media_id}"
    streamer = await create_streamer(url)
    try:
        async with streamer.session.get(
            url,
            params={"api_key": api_key},
            timeout=ClientTimeout(total=float(settings.earnvids_request_timeout)),
        ) as response:
            if response.status != 200:
                logger.warning("[earnvids] TMDB returned HTTP %s", response.status)
                return {}
            payload = json.loads(await response.text(errors="ignore"))
    except Exception as exc:
        # Never log the URL: it contains the TMDB API key.
        logger.warning("[earnvids] TMDB lookup failed: %s", type(exc).__name__)
        return {}
    finally:
        await streamer.session.close()

    metadata = {
        "title": str(payload.get("title") or payload.get("name") or "").strip(),
        "year": str(payload.get("release_date") or payload.get("first_air_date") or "")[:4],
        "overview": str(payload.get("overview") or "").strip(),
    }
    _TMDB_CACHE[cache_key] = (time.time(), metadata)
    return dict(metadata)


async def _sign_source(
    request: Request,
    media_type: str,
    media_id: str,
    season: str,
    episode: str,
    quality: str,
    source: str,
) -> tuple[str, str, str]:
    """Resolve source(s) and return (signed URL, host name, quality label).

    With ``source='auto'`` the first configured embed server that resolves to a
    playable URL wins. Host names stay server-side; only the signed proxy URL is
    handed to EarnVids.
    """
    requested = str(source or "").strip()
    if requested and requested.lower() != "auto":
        adapter = get_adapter(requested)
        if adapter is None:
            adapter = next(
                (item for item in configured_servers() if item.name.lower() == requested.lower()),
                None,
            )
        if adapter is None:
            raise HTTPException(status_code=400, detail="Server tidak dikenal")
        candidates = [adapter]
    else:
        candidates = configured_servers()
        if not candidates:
            raise HTTPException(status_code=502, detail="Tidak ada server embed yang aktif")

    raw: dict[str, Any] | None = None
    selected = None
    for candidate in candidates:
        try:
            resolved = await candidate.resolve(request, media_type, media_id, season, episode)
        except HTTPException:
            if len(candidates) == 1:
                raise
            # Never echo the exception text: it can contain the provider URL.
            logger.warning("[earnvids] resolve failed: HTTP error")
            continue
        except Exception as exc:
            logger.warning("[earnvids] resolve failed: %s", type(exc).__name__)
            continue
        destination = resolved.get("destination_url")
        if isinstance(destination, str) and destination:
            raw = resolved
            selected = candidate
            break
        logger.warning("[earnvids] empty resolve result")

    if raw is None or selected is None:
        raise HTTPException(
            status_code=502, detail="Semua server embed gagal me-resolve sumber video"
        )

    adapter = selected
    destination = raw["destination_url"]

    chosen: dict[str, Any] | None = None
    if quality:
        for item in raw.get("sources") or []:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or item.get("id") or "")
            if label == str(quality):
                chosen = item
                break
        if chosen and isinstance(chosen.get("url"), str) and chosen["url"]:
            destination = chosen["url"]

    endpoint = raw.get("mediaflow_endpoint") or "proxy_stream_endpoint"
    signed = _sign_asset_url(
        request,
        raw,
        get_proxy_headers(request),
        destination,
        endpoint=endpoint,
        force_playlist_proxy=False,
        request_headers=(chosen or {}).get("request_headers"),
        expiration=max(1, int(settings.earnvids_source_token_ttl)),
    )
    return signed, adapter.name, str((chosen or {}).get("label") or "auto")


def _request_context(request: Request) -> dict[str, Any]:
    """Capture only what adapter resolution needs after the request returns."""
    return {
        "headers": dict(request.headers),
        "query_params": dict(request.query_params),
    }


def _context_request(context: dict[str, Any]) -> Any:
    """A minimal request stand-in for ``get_proxy_headers`` and adapters."""
    return SimpleNamespace(
        headers=context.get("headers") or {},
        query_params=context.get("query_params") or {},
    )


async def _earnvids_get(
    path: str, params: dict[str, Any], *, allow_missing: bool = False
) -> dict[str, Any]:
    key = _secret("earnvids_api_key")
    if not key:
        raise HTTPException(status_code=503, detail="EARNVIDS_API_KEY belum dikonfigurasi")

    streamer = await create_streamer(EARNVIDS_API + path)
    try:
        async with streamer.session.get(
            EARNVIDS_API + path,
            params={"key": key, **params},
            timeout=ClientTimeout(total=float(settings.earnvids_request_timeout)),
        ) as response:
            status = response.status
            text = await response.text(errors="ignore")
    except Exception as exc:
        logger.warning("[earnvids] request failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Gagal menghubungi EarnVids") from exc
    finally:
        await streamer.session.close()

    if allow_missing and status in (400, 404):
        # A future filecode has no file/info or encodings row yet.
        return {}
    if status != 200:
        logger.warning("[earnvids] HTTP %s", status)
        raise HTTPException(status_code=502, detail=f"EarnVids menolak permintaan (HTTP {status})")
    try:
        payload = json.loads(text)
    except ValueError:
        raise HTTPException(status_code=502, detail="Respons EarnVids bukan JSON")
    if not isinstance(payload, dict) or int(payload.get("status") or 0) != 200:
        raise HTTPException(status_code=502, detail="EarnVids menolak permintaan")
    if allow_missing and int(payload.get("status") or 0) == 404:
        return {}
    return payload


def _queue_item(result: Any, file_code: str) -> dict[str, Any]:
    entries = _dict_list(result)
    for entry in entries:
        if str(entry.get("file_code") or "").strip() == file_code:
            return entry
    # A pending URL upload can have an empty file_code. Only trust it when it
    # is the sole queued request, otherwise it may belong to another asset.
    if len(entries) == 1 and not str(entries[0].get("file_code") or "").strip():
        return entries[0]
    return {}


async def _player_link(file_code: str, title: str) -> str:
    """Use API-provided links only; never fabricate a player URL."""
    encodings = await _earnvids_get(
        "/file/encodings", {"file_code": file_code}, allow_missing=True
    )
    for entry in _dict_list(encodings.get("result")):
        link = _verified_player_link(entry.get("link"), file_code)
        if link:
            return link

    if title:
        listing = await _earnvids_get("/file/list", {"title": title}, allow_missing=True)
        files = (listing.get("result") or {}).get("files") if isinstance(listing.get("result"), dict) else None
        for entry in _dict_list(files):
            if str(entry.get("file_code") or "").strip() != file_code:
                continue
            link = _verified_player_link(entry.get("link"), file_code)
            if link:
                return link
    return ""


async def _patch_job(job_id: str, **changes: Any) -> dict[str, Any]:
    state = await load_job(job_id)
    state.update(changes)
    state["updated_at"] = time.time()
    await store_job(job_id, state)
    return state


def _safe_filename(title: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", title).strip("-")[:60]
    return cleaned or "video"


async def _earnvids_post_file(
    upload_url: str,
    path: Path,
    title: str,
    metadata: dict[str, str],
    options: dict[str, Any],
) -> str:
    """POST one MP4 to the server URL returned by EarnVids' upload/server API."""
    key = _secret("earnvids_api_key")
    if not key:
        raise RuntimeError("EARNVIDS_API_KEY belum dikonfigurasi")

    parsed = urlparse(upload_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RuntimeError("Upload server EarnVids tidak valid")

    form = FormData()
    form.add_field("key", key)
    form.add_field("file_public", "1" if options.get("file_public", True) else "0")
    if title:
        form.add_field("file_title", title)
    if metadata.get("overview"):
        form.add_field("file_descr", metadata["overview"])
    if settings.earnvids_fld_id:
        form.add_field("fld_id", settings.earnvids_fld_id)
    if settings.earnvids_cat_id:
        form.add_field("cat_id", settings.earnvids_cat_id)
    if settings.earnvids_tags:
        form.add_field("tags", settings.earnvids_tags)

    streamer = await create_streamer(upload_url)
    try:
        with path.open("rb") as handle:
            form.add_field(
                "file",
                handle,
                filename=f"{_safe_filename(title)}.mp4",
                content_type="video/mp4",
            )
            async with streamer.session.post(
                upload_url,
                data=form,
                timeout=ClientTimeout(total=max(60, int(settings.earnvids_upload_ffmpeg_timeout))),
            ) as response:
                status = response.status
                text = await response.text(errors="ignore")
    except Exception as exc:
        # Never echo the body: it can contain the API key echo or provider URLs.
        logger.warning("[earnvids] multipart upload failed: %s", type(exc).__name__)
        raise RuntimeError("Gagal mengunggah file ke EarnVids") from exc
    finally:
        await streamer.session.close()

    if status != 200:
        logger.warning("[earnvids] upload server returned HTTP %s", status)
        raise RuntimeError("EarnVids menolak unggahan file")
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise RuntimeError("Respons unggahan EarnVids bukan JSON") from exc
    if not isinstance(payload, dict) or int(payload.get("status") or 0) != 200:
        raise RuntimeError("EarnVids menolak unggahan file")

    files = [item for item in _dict_list(payload.get("files"))]
    filecode = str(files[0].get("filecode") or "").strip() if files else ""
    if not filecode or not _FILE_CODE_RE.match(filecode):
        raise RuntimeError("EarnVids tidak mengembalikan filecode yang valid")
    return filecode


async def _run_upload_job(
    job_id: str,
    source_url: str,
    initial: dict[str, Any],
    options: dict[str, Any],
    request_context: dict[str, Any] | None = None,
) -> None:
    title = str(initial.get("title") or "").strip()
    metadata = {
        "title": title,
        "year": str(initial.get("year") or ""),
        "overview": str(initial.get("overview") or ""),
    }

    async def refresh_source() -> str:
        """Re-resolve the same adapter when a signed upstream URL goes stale."""
        if not request_context:
            raise RuntimeError("konteks sumber tidak tersedia")
        signed, _host, _quality = await _sign_source(
            _context_request(request_context),
            str(options.get("type") or "movie"),
            str(options.get("id") or ""),
            str(options.get("season") or "1"),
            str(options.get("episode") or "1"),
            str(options.get("quality") or ""),
            str(initial.get("source") or options.get("source") or "auto"),
        )
        return signed

    try:
        await _patch_job(job_id, state="downloading", progress=5, error="")
        path = await convert_source_to_file(
            job_id, source_url, refresh_source=refresh_source
        )
        await _patch_job(job_id, state="uploading", progress=45)

        server = await _earnvids_get("/upload/server", {})
        upload_url = str(server.get("result") or "").strip()
        filecode = await _earnvids_post_file(upload_url, path, title, metadata, options)
        await _patch_job(job_id, state="encoding", filecode=filecode, progress=70)

        if title:
            try:
                await _earnvids_get("/file/edit", {"file_code": filecode, "file_title": title})
            except HTTPException:
                # Cosmetic metadata must never fail a completed upload.
                logger.warning("[earnvids] failed to set title for %s", filecode)

        final = await _collect_status(filecode, title, job_id)
        await _patch_job(
            job_id,
            state=final["state"],
            status=final["status"],
            progress=final["progress"],
            canplay=final["canplay"],
            player_url=final["player_url"],
            title=final["title"] or title,
            duration=final["duration"],
            error=final["error"] or "",
        )
    except Exception as exc:
        detail = redact_sensitive(str(exc))[:200].replace("\n", " ").strip()
        logger.warning(
            "[earnvids] job=%s failed: %s detail=%s",
            job_id,
            type(exc).__name__,
            detail or "-",
        )
        await _patch_job(
            job_id,
            state="failed",
            error="Gagal mengunggah video ke EarnVids",
        )
    finally:
        cleanup_job(job_id)


async def _collect_status(
    file_code: str = "", title: str = "", job_id: str = ""
) -> dict[str, Any]:
    job_id = job_id if valid_job_id(job_id) else ""
    job = await load_job(job_id) if job_id else {}

    if not file_code:
        candidate = str(job.get("filecode") or "").strip()
        file_code = candidate if candidate and _FILE_CODE_RE.match(candidate) else ""

    info: dict[str, Any] = {}
    enc_items: list[dict[str, Any]] = []
    queue_entry: dict[str, Any] = {}
    queue_status = ""
    enc_status = ""
    canplay = False
    requests_available = _integer(job.get("requests_available"), -1)

    if file_code:
        info_payload = await _earnvids_get(
            "/file/info", {"file_code": file_code}, allow_missing=True
        )
        info_items = _dict_list(info_payload.get("result"))
        info = next(
            (item for item in info_items if str(item.get("file_code") or "").strip() == file_code),
            info_items[0] if info_items else {},
        )
        canplay = _integer(info.get("canplay")) == 1 and _integer(info.get("status")) == 200

        encodings = await _earnvids_get(
            "/file/encodings", {"file_code": file_code}, allow_missing=True
        )
        enc_items = _dict_list(encodings.get("result"))
        enc_status = next(
            (str(item.get("status") or "").upper() for item in enc_items if item.get("status")),
            "",
        )

        queue = await _earnvids_get(
            "/file/url_uploads", {"file_code": file_code}, allow_missing=True
        )
        queue_entry = _queue_item(queue.get("result"), file_code)
        queue_status = str(queue_entry.get("status") or "").upper()
        requests_available = _integer(queue.get("requests_available"), -1)

    progress = max((_integer(item.get("progress")) for item in enc_items), default=0)
    if not progress:
        progress = _integer(job.get("progress"))
    if not requests_available or requests_available < 0:
        requests_available = _integer(job.get("requests_available"), -1)

    job_state = str(job.get("state") or "").strip().lower()
    combined = f"{queue_status} {enc_status}".upper()
    if canplay:
        state = "uploaded"
        progress = max(progress, 100)
    elif any(word in combined for word in ("ERROR", "FAILED", "DELETED")):
        state = "failed"
    elif job_state == "failed":
        state = "failed"
    elif "ENCODING" in combined or "CONVERTING" in combined or job_state == "encoding":
        state = "encoding"
    elif any(word in combined for word in ("PENDING", "UPLOADING", "DOWNLOADING")) or job_state in (
        "uploading",
        "downloading",
        "queued",
    ):
        state = "uploading"
    else:
        state = "queued"

    player_url = await _player_link(file_code, title) if canplay else str(job.get("player_url") or "")
    status = queue_status or enc_status or job_state.upper() or ("READY" if canplay else "PENDING")
    if state == "failed":
        if queue_status or enc_status:
            error = f"EarnVids: {queue_status or enc_status}"
        else:
            error = str(job.get("error") or "EarnVids: ERROR")
    else:
        error = None

    return {
        "filecode": file_code,
        "job_id": job_id,
        "state": state,
        "status": status,
        "progress": min(100, max(0, progress)),
        "canplay": canplay,
        "player_url": player_url,
        "title": str(info.get("file_title") or job.get("title") or title or "").strip(),
        "duration": _integer(info.get("file_length"), _integer(job.get("duration"))),
        "error": error,
        "requests_available": requests_available,
    }


@earnvids_router.post("/internal/earnvids/upload")
async def earnvids_upload(request: Request):
    """Start a background MP4 upload and return an opaque job id immediately."""
    data = await _json_body(request)
    media_type, media_id, season, episode = _media_from(data)
    quality = str(data.get("quality") or "").strip()
    source = str(data.get("source") or "auto").strip()
    title = str(data.get("title") or "").strip()
    metadata = await _tmdb_metadata(media_type, media_id)
    title = str(metadata.get("title") or title).strip()

    signed, host, chosen_quality = await _sign_source(
        request, media_type, media_id, season, episode, quality, source
    )
    job_id = new_job_id()
    initial = {
        "job_id": job_id,
        "state": "queued",
        "status": "PENDING",
        "filecode": "",
        "progress": 0,
        "canplay": False,
        "player_url": "",
        "title": title,
        "year": metadata.get("year", ""),
        "overview": metadata.get("overview", ""),
        "duration": 0,
        "error": "",
        # Server-only: used by the background job, never returned to a browser.
        "source": host,
        "quality": chosen_quality,
    }
    await store_job(job_id, initial)
    spawn_job(
        _run_upload_job(job_id, signed, initial, data, _request_context(request))
    )

    return JSONResponse(
        {
            "job_id": job_id,
            "status": "queued",
            "title": title,
            "year": metadata.get("year", ""),
            "overview": metadata.get("overview", ""),
            "media": {
                "type": media_type,
                "id": media_id,
                "season": season,
                "episode": episode,
            },
        },
        status_code=202,
        headers={"Cache-Control": "no-store"},
    )


@earnvids_router.post("/internal/earnvids/status")
async def earnvids_status(request: Request):
    """Report job/upload state and the API-verified player URL."""
    data = await _json_body(request)
    raw_code = str(data.get("file_code") or "").strip()
    file_code = _file_code_from(data) if raw_code else ""
    job_id = _job_id_from(data) if data.get("job_id") else ""
    if not file_code and not job_id:
        raise HTTPException(status_code=400, detail="file_code atau job_id diperlukan")
    title = str(data.get("title") or "").strip()
    return JSONResponse(
        await _collect_status(file_code, title, job_id),
        headers={"Cache-Control": "no-store"},
    )


@earnvids_router.post("/internal/earnvids/action")
async def earnvids_action(request: Request):
    """Delete one failed URL-upload queue entry without touching other files."""
    data = await _json_body(request)
    action = str(data.get("action") or "delete").strip().lower()
    if action != "delete":
        raise HTTPException(status_code=400, detail="action harus delete")
    file_code = _file_code_from(data)
    payload = await _earnvids_get("/file/url_actions", {"delete_code": file_code})
    return JSONResponse(
        {
            "deleted": file_code,
            "requests_available": _integer(payload.get("requests_available"), -1),
        },
        headers={"Cache-Control": "no-store"},
    )


@earnvids_router.post("/internal/earnvids/info")
async def earnvids_info(request: Request):
    """Return only final, browser-safe metadata for a playable EarnVids file."""
    data = await _json_body(request)
    file_code = _file_code_from(data)
    status = await _collect_status(file_code, str(data.get("title") or "").strip())
    return JSONResponse(
        {
            "filecode": status["filecode"],
            "state": status["state"],
            "canplay": status["canplay"],
            "player_url": status["player_url"],
            "title": status["title"],
            "duration": status["duration"],
        },
        headers={"Cache-Control": "no-store"},
    )
