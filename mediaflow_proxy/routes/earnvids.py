"""Server-to-server bridge that hands mfp-signed media URLs to EarnVids.

StreamFlix never sees a provider URL or a signed query. It sends only media
identity and an HMAC made with ``MFP_EARNVIDS_SECRET``. mfp resolves the
requested source, signs a same-origin proxy URL with a 6-hour token, and asks
EarnVids to fetch that URL. The EarnVids API key and all upstream headers stay
on this origin.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from typing import Any

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


async def _earnvids_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
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

    if status != 200:
        logger.warning("[earnvids] HTTP %s", status)
        raise HTTPException(status_code=502, detail=f"EarnVids menolak permintaan (HTTP {status})")
    try:
        payload = json.loads(text)
    except ValueError:
        raise HTTPException(status_code=502, detail="Respons EarnVids bukan JSON")
    if not isinstance(payload, dict) or int(payload.get("status") or 0) != 200:
        raise HTTPException(status_code=502, detail="EarnVids menolak permintaan")
    return payload


@earnvids_router.post("/internal/earnvids/upload")
async def earnvids_upload(request: Request):
    """Resolve media and ask EarnVids to fetch the signed mfp proxy URL."""
    data = await _json_body(request)
    media_type, media_id, season, episode = _media_from(data)
    quality = str(data.get("quality") or "").strip()
    source = str(data.get("source") or "VidLink").strip()

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

    return JSONResponse(
        {
            "filecode": filecode,
            "source": host,
            "quality": chosen_quality,
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
    """Check the encoding state of a previously submitted filecode."""
    data = await _json_body(request)
    file_code = str(data.get("file_code") or "").strip()
    if not file_code or not _FILE_CODE_RE.match(file_code):
        raise HTTPException(status_code=400, detail="file_code tidak valid")

    payload = await _earnvids_get("/file/url_uploads", {"file_code": file_code})
    result = payload.get("result") or []
    item: dict[str, Any] = {}
    if isinstance(result, list):
        item = next(
            (
                entry
                for entry in result
                if isinstance(entry, dict) and entry.get("file_code") == file_code
            ),
            {},
        )

    return JSONResponse(
        {
            "filecode": str(item.get("file_code") or file_code),
            "status": str(item.get("status") or "PENDING"),
            "progress": int(item.get("progress") or 0),
        },
        headers={"Cache-Control": "no-store"},
    )
