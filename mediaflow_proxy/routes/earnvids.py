"""Server-to-server bridge that hands mfp-signed media URLs to EarnVids.

StreamFlix never sees a provider URL or a signed query. It sends only media
identity and an HMAC made with ``MFP_EARNVIDS_SECRET``. mfp resolves the
requested source, signs a same-origin proxy URL with a 6-hour token, and asks
EarnVids to fetch that URL. The EarnVids API key and all upstream headers stay
on this origin.

Status is derived from three EarnVids endpoints: ``file/info`` decides whether
a file is playable, ``file/encodings`` supplies progress and the verified player
link, and ``file/url_uploads`` exposes queue errors and remaining URL quota.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from typing import Any
from urllib.parse import urlparse, urlunparse

from aiohttp import ClientTimeout
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from mediaflow_proxy.configs import settings
from mediaflow_proxy.player.adapters import configured_servers, get_adapter
from mediaflow_proxy.routes.embed import _sign_asset_url
from mediaflow_proxy.utils.http_utils import create_streamer, get_proxy_headers

logger = logging.getLogger(__name__)
earnvids_router = APIRouter()

EARNVIDS_API = "https://earnvidsapi.com/api"
TIMESTAMP_SKEW = 300
_FILE_CODE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


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


async def _sign_source(
    request: Request,
    media_type: str,
    media_id: str,
    season: str,
    episode: str,
    quality: str,
    source: str,
) -> tuple[str, str, str]:
    """Resolve one source and return (signed URL, host name, quality label)."""
    adapter = get_adapter(source) if source else None
    if adapter is None and source:
        adapter = next(
            (item for item in configured_servers() if item.name.lower() == source.lower()),
            None,
        )
    if adapter is None:
        raise HTTPException(status_code=400, detail="Server tidak dikenal")

    try:
        raw = await adapter.resolve(request, media_type, media_id, season, episode)
    except HTTPException:
        raise
    except Exception as exc:
        # Never echo the exception text: it can contain the provider URL.
        logger.warning("[earnvids] resolve failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Gagal me-resolve sumber video") from exc

    destination = raw.get("destination_url")
    if not isinstance(destination, str) or not destination:
        raise HTTPException(status_code=502, detail="Sumber tidak mengembalikan URL video")

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


async def _collect_status(file_code: str, title: str = "") -> dict[str, Any]:
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
    progress = max((_integer(item.get("progress")) for item in enc_items), default=0)
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

    combined = f"{queue_status} {enc_status}".upper()
    if canplay:
        state = "uploaded"
        progress = max(progress, 100)
    elif any(word in combined for word in ("ERROR", "FAILED", "DELETED")):
        state = "failed"
    elif "ENCODING" in combined or "CONVERTING" in combined:
        state = "encoding"
    elif any(word in combined for word in ("PENDING", "UPLOADING", "DOWNLOADING")):
        state = "uploading"
    else:
        state = "queued"

    player_url = await _player_link(file_code, title) if canplay else ""

    return {
        "filecode": file_code,
        "state": state,
        "status": queue_status or enc_status or ("READY" if canplay else "PENDING"),
        "progress": min(100, max(0, progress)),
        "canplay": canplay,
        "player_url": player_url,
        "title": str(info.get("file_title") or "").strip(),
        "duration": _integer(info.get("file_length")),
        "error": f"EarnVids: {queue_status or enc_status}" if state == "failed" else None,
        "requests_available": requests_available,
    }


@earnvids_router.post("/internal/earnvids/upload")
async def earnvids_upload(request: Request):
    """Resolve media and ask EarnVids to fetch the signed mfp proxy URL."""
    data = await _json_body(request)
    media_type, media_id, season, episode = _media_from(data)
    quality = str(data.get("quality") or "").strip()
    source = str(data.get("source") or "VidLink").strip()
    title = str(data.get("title") or "").strip()

    quota = await _earnvids_get("/file/url_uploads", {})
    if _integer(quota.get("requests_available"), -1) == 0:
        raise HTTPException(status_code=429, detail="Kuota upload URL EarnVids habis")

    signed, host, chosen_quality = await _sign_source(
        request, media_type, media_id, season, episode, quality, source
    )

    params: dict[str, Any] = {
        "url": signed,
        "file_public": "1" if data.get("file_public", True) else "0",
    }
    if settings.earnvids_fld_id:
        params["fld_id"] = settings.earnvids_fld_id
    if settings.earnvids_cat_id:
        params["cat_id"] = settings.earnvids_cat_id
    if settings.earnvids_tags:
        params["tags"] = settings.earnvids_tags

    payload = await _earnvids_get("/upload/url", params)
    filecode = str((payload.get("result") or {}).get("filecode") or "").strip()
    if not filecode or not _FILE_CODE_RE.match(filecode):
        raise HTTPException(status_code=502, detail="EarnVids tidak mengembalikan filecode yang valid")

    if title:
        try:
            await _earnvids_get("/file/edit", {"file_code": filecode, "file_title": title})
        except HTTPException:
            # Metadata is cosmetic; the upload itself must not fail because of it.
            logger.warning("[earnvids] failed to set title for %s", filecode)

    return JSONResponse(
        {
            "filecode": filecode,
            "source": host,
            "quality": chosen_quality,
            "player_url": await _player_link(filecode, title),
            "media": {
                "type": media_type,
                "id": media_id,
                "season": season,
                "episode": episode,
            },
        },
        headers={"Cache-Control": "no-store"},
    )


@earnvids_router.post("/internal/earnvids/status")
async def earnvids_status(request: Request):
    """Report upload/encoding state and the API-verified player URL."""
    data = await _json_body(request)
    file_code = _file_code_from(data)
    title = str(data.get("title") or "").strip()
    return JSONResponse(await _collect_status(file_code, title), headers={"Cache-Control": "no-store"})


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
