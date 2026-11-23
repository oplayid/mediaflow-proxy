"""Iframe player endpoints served by MediaFlow itself.

StreamFlix only embeds the opaque ``player_url`` returned by the gateway. All
provider URLs, CloudFront cookies and proxy tokens stay on this origin.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from mediaflow_proxy.configs import settings
from mediaflow_proxy.player import simulate_session

player_router = APIRouter()

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_PLAYER_HTML = _STATIC_DIR / "player.html"
_VALID_TYPES = {"movie", "tv", "anime"}


def _render_player(session: dict) -> str:
    """Inject the serialised session into the static player shell."""
    template = _PLAYER_HTML.read_text(encoding="utf-8")
    payload = json.dumps(session, separators=(",", ":")).replace("</", "<\\/")
    return template.replace("/*__MFP_SESSION__*/null", payload)


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
    }


@player_router.get("/{media_type}/{media_id}/{season}/{episode}", name="player_page")
@player_router.get("/{media_type}/{media_id}", name="player_page_short")
@player_router.get("/{media_type}/{media_id}/{season}/{episode}/retry", name="player_page_retry")
@player_router.get("/{media_type}/{media_id}/{season}/retry", name="player_page_season_retry")
@player_router.get("/{media_type}/{media_id}/retry", name="player_page_short_retry")
async def player_page(
    request: Request,
    media_type: str,
    media_id: str,
    season: str | None = None,
    episode: str | None = None,
):
    """Render the smart player after a bounded multi-host simulation."""
    media_type = (media_type or "").lower()
    if media_type not in _VALID_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported media type")

    token_type = request.query_params.get("type")
    token_id = request.query_params.get("id")
    if token_type and token_type.lower() != media_type:
        raise HTTPException(status_code=400, detail="Player token media type mismatch")
    if token_id and str(token_id) != str(media_id):
        raise HTTPException(status_code=400, detail="Player token media id mismatch")

    is_retry = request.url.path.rstrip("/").endswith("/retry") or request.query_params.get("retry") is True
    session = await simulate_session(
        request,
        media_type,
        media_id,
        season or request.query_params.get("s"),
        episode or request.query_params.get("e"),
        retry=is_retry,
    )
    return HTMLResponse(_render_player(session), headers=_security_headers())
