"""Iframe player endpoints served by MediaFlow itself.

StreamFlix only embeds the opaque ``player_url`` returned by the gateway. All
provider URLs, CloudFront cookies and proxy tokens stay on this origin, and all
video/quality/subtitle settings belong to this player.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from mediaflow_proxy.configs import settings
from mediaflow_proxy.player import simulate_session
from mediaflow_proxy.player.transcoder import serve_transcode_file
from mediaflow_proxy.routes.embed import _sign_subtitle_upload_url, _sign_subtitle_url
from mediaflow_proxy.subtitles.subdl import (
    SUBTITLE_CACHE_TTL,
    load_vtt,
    normalise_language,
    srt_to_vtt,
    store_vtt,
)

player_router = APIRouter()
transcode_router = APIRouter()

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_PLAYER_HTML = _STATIC_DIR / "player.html"
_VALID_TYPES = {"movie", "tv", "anime"}
_MAX_CUSTOM_SUBTITLE_BYTES = 5 * 1024 * 1024


def _decode_custom_subtitle(raw: bytes) -> str | None:
    """Decode an uploaded SRT/VTT and return browser-ready WebVTT."""
    text = ""
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    vtt = srt_to_vtt(text)
    if not vtt.strip() or "-->" not in vtt:
        return None
    return vtt


def _render_player(session: dict) -> str:
    """Inject the serialised session into the static player shell."""
    template = _PLAYER_HTML.read_text(encoding="utf-8")
    payload = json.dumps(session, separators=(",", ":")).replace("</", "<\\/")
    return template.replace("/*__MFP_SESSION__*/null", payload)


@transcode_router.get("/transcode/{job}/{filename}", name="player_transcode_file")
async def player_transcode_file(request: Request, job: str, filename: str):
    """Serve tokenized HLS output produced by the server-side transcoder."""
    return await serve_transcode_file(request, job, filename)


def _security_headers() -> dict[str, str]:
    ancestors = (settings.player_frame_ancestors or "https://movie.oplay.id").strip()
    return {
        "Content-Security-Policy": (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "media-src 'self' blob:; "
            "connect-src 'self' blob:; "
            "worker-src 'self' blob:; "
            "frame-src 'none'; "
            f"frame-ancestors {ancestors}"
        ),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "same-origin",
        "Cache-Control": "no-store",
    }


@player_router.get("/subtitle/{filename}", name="player_subtitle")
async def player_subtitle(request: Request, filename: str):
    """Serve a signed, server-side converted SubDL WebVTT track."""
    key = request.query_params.get("sub")
    content = await load_vtt(str(key or ""))
    if content is None:
        raise HTTPException(status_code=404, detail="Subtitle not found")
    return Response(
        content=content,
        media_type="text/vtt; charset=utf-8",
        headers=_security_headers(),
    )


@player_router.post("/subtitle/upload", name="player_subtitle_upload")
async def player_subtitle_upload(request: Request):
    """Store a viewer-uploaded SRT/VTT and return a signed player track.

    The upload is posted to the same opaque tokenized URL as the player page,
    so EncryptionMiddleware restores the media identity from the signed token.
    No provider URL or API password is exposed to the browser.
    """
    media_type = str(request.query_params.get("type") or "").strip().lower()
    media_id = str(request.query_params.get("id") or "").strip()
    if media_type not in _VALID_TYPES or not media_id:
        raise HTTPException(status_code=400, detail="Player token media identity missing")

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > _MAX_CUSTOM_SUBTITLE_BYTES:
        raise HTTPException(status_code=413, detail="Subtitle terlalu besar (maksimal 5 MB)")
    raw = await request.body()
    if not raw or len(raw) > _MAX_CUSTOM_SUBTITLE_BYTES:
        raise HTTPException(status_code=400, detail="Subtitle kosong atau terlalu besar (maksimal 5 MB)")

    vtt = _decode_custom_subtitle(raw)
    if vtt is None:
        raise HTTPException(status_code=400, detail="File subtitle tidak valid (gunakan .srt atau .vtt)")

    language = normalise_language(str(request.query_params.get("language") or "id"))
    label = re.sub(r"\s+", " ", str(request.query_params.get("label") or "Subtitle")).strip()[:80]
    key = await store_vtt(vtt, ttl=SUBTITLE_CACHE_TTL)
    return JSONResponse(
        {
            "id": f"custom-{key[:12]}",
            "label": f"Custom • {label or language.upper()}",
            "language": language,
            "format": "vtt",
            "origin": "custom",
            "url": _sign_subtitle_url(request, key, expiration=SUBTITLE_CACHE_TTL),
        },
        headers=_security_headers(),
    )


@player_router.get("/{media_type}/{media_id}/{season}/{episode}", name="player_page")
@player_router.get("/{media_type}/{media_id}", name="player_page_short")
async def player_page(
    request: Request,
    media_type: str,
    media_id: str,
    season: str | None = None,
    episode: str | None = None,
):
    """Render the player after one bounded first-success host check."""
    media_type = (media_type or "").lower()
    if media_type not in _VALID_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported media type")

    token_type = request.query_params.get("type")
    token_id = request.query_params.get("id")
    if token_type and token_type.lower() != media_type:
        raise HTTPException(status_code=400, detail="Player token media type mismatch")
    if token_id and str(token_id) != str(media_id):
        raise HTTPException(status_code=400, detail="Player token media id mismatch")

    refresh_value = request.query_params.get("refresh")
    refresh = refresh_value is True or str(refresh_value or "").lower() in ("1", "true")
    session = await simulate_session(
        request,
        media_type,
        media_id,
        season or request.query_params.get("s"),
        episode or request.query_params.get("e"),
        refresh=refresh,
    )
    # Copy the cached session: the upload token is request-specific and must
    # not be written back into the shared cache entry.
    view = dict(session)
    view["upload_url"] = _sign_subtitle_upload_url(request, media_type, media_id)
    return HTMLResponse(_render_player(view), headers=_security_headers())
