"""Iframe player endpoints served by MediaFlow itself.

StreamFlix only embeds the opaque ``player_url`` returned by the gateway. All
provider URLs, CloudFront cookies and proxy tokens stay on this origin, and all
video/quality/subtitle settings belong to this player.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from mediaflow_proxy.configs import settings
from mediaflow_proxy.player import simulate_session
from mediaflow_proxy.player.transcoder import serve_transcode_file
from mediaflow_proxy.subtitles.subdl import load_vtt

player_router = APIRouter()
transcode_router = APIRouter()

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_PLAYER_HTML = _STATIC_DIR / "player.html"
_VALID_TYPES = {"movie", "tv", "anime"}


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
    return HTMLResponse(_render_player(session), headers=_security_headers())
