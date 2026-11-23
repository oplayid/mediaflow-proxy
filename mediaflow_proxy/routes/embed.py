"""Identity-only gateway for the StreamFlix shell.

StreamFlix sends just a media ID and receives just an opaque ``player_url``.
The gateway does not contact embed hosts, does not cache extraction results and
does not sign media URLs. Host selection happens exactly once, when the mfp
iframe mounts; all video/quality/subtitle settings belong to that player.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from mediaflow_proxy.configs import settings
from mediaflow_proxy.utils.crypto_utils import EncryptionHandler
from mediaflow_proxy.utils.http_utils import (
    ProxyRequestHeaders,
    encode_mediaflow_proxy_url,
    get_original_scheme,
)

logger = logging.getLogger(__name__)
gateway_router = APIRouter()


def _headers_for_destination(result: dict, destination_url: str) -> dict | None:
    """Prefer per-source headers (e.g. VidLink CloudFront cookies).

    ``result["request_headers"]`` only carries generic base headers. Signing the
    primary stream URL with those silently dropped VidLink's
    ``Cookie: CloudFront-Policy=...`` and made MPD requests return 403.
    """
    for source in result.get("sources") or []:
        if isinstance(source, dict) and source.get("url") == destination_url:
            return source.get("request_headers") or None
    return result.get("request_headers") or None


def _sign_player_url(
    request: Request,
    media_type: str,
    media_id: str,
    season: str | None,
    episode: str | None,
    *,
    refresh: bool = False,
) -> str:
    """Build the opaque iframe URL consumed by StreamFlix.

    The token is encrypted with ``api_password`` server-side, so the browser
    only ever sees an opaque path. ``refresh`` is signed into the token (rather
    than appended as a query param) because EncryptionMiddleware merges the
    decrypted payload over the query string.
    """
    scheme = get_original_scheme(request)
    authority = request.headers.get("host") or request.url.netloc
    path = f"/player/{media_type}/{media_id}"
    if media_type == "tv":
        path += f"/{season or '1'}/{episode or '1'}"
    payload = {
        "type": media_type,
        "id": media_id,
        "s": season or "",
        "e": episode or "",
        "refresh": bool(refresh),
    }
    handler = EncryptionHandler(settings.api_password) if settings.api_password else None
    if handler:
        token = handler.encrypt_data(payload, expiration=600)
        return f"{scheme}://{authority}/_token_{token}{path}"
    return f"{scheme}://{authority}{path}?{urlencode(payload)}"


def _sign_asset_url(
    request: Request,
    result: dict,
    proxy_headers: ProxyRequestHeaders,
    destination_url: str,
    *,
    endpoint: str | None = None,
    force_playlist_proxy: bool | None = None,
    request_headers: dict | None = None,
    expiration: int | None = None,
) -> str:
    """Sign one gateway URL so headers and ``api_password`` stay server-side."""
    endpoint = endpoint or result.get("mediaflow_endpoint") or "hls_manifest_proxy"
    if endpoint == "hls_segment_proxy":
        proxy_base = str(
            request.url_for("hls_manifest_proxy").replace(scheme=get_original_scheme(request))
        ).replace("/hls/manifest.m3u8", "/hls/segment.vtt")
    else:
        proxy_base = str(request.url_for(endpoint).replace(scheme=get_original_scheme(request)))

    query_params = dict(result.get("query_params") or {})
    query_params["api_password"] = settings.api_password or ""
    should_force = (
        result.get("force_playlist_proxy", False)
        if force_playlist_proxy is None
        else force_playlist_proxy
    )
    if should_force:
        query_params["force_playlist_proxy"] = "1"
    else:
        query_params.pop("force_playlist_proxy", None)

    encryption_handler = (
        EncryptionHandler(settings.api_password) if settings.api_password else None
    )
    return encode_mediaflow_proxy_url(
        mediaflow_proxy_url=proxy_base,
        destination_url=destination_url,
        query_params=query_params,
        request_headers=request_headers if request_headers is not None else _headers_for_destination(result, destination_url),
        response_headers=proxy_headers.response,
        encryption_handler=encryption_handler,
        expiration=expiration or result.get("expiration", 300),
    )


def _sign_subtitle_url(request: Request, key: str, expiration: int = 600) -> str:
    """Sign a same-origin URL that serves a server-side converted VTT track."""
    scheme = get_original_scheme(request)
    authority = request.headers.get("host") or request.url.netloc
    path = f"/player/subtitle/{key}.vtt"
    handler = EncryptionHandler(settings.api_password) if settings.api_password else None
    if handler:
        token = handler.encrypt_data({"sub": key}, expiration=expiration)
        return f"{scheme}://{authority}/_token_{token}{path}"
    return f"{scheme}://{authority}{path}?sub={key}"


def _normalise_media(
    media_type: str,
    media_id: str,
    season: str | None,
    episode: str | None,
) -> tuple[str, str | None, str | None]:
    media_id = re.sub(r"^tv-", "", str(media_id).strip(), flags=re.IGNORECASE)
    if media_type != "tv":
        return media_id, season, episode

    def positive(value, default: str = "1") -> str:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return str(number if number > 0 else 1)

    return media_id, positive(season), positive(episode)


async def _gateway_response(
    request: Request,
    media_type: str,
    media_id: str,
    season: str | None,
    episode: str | None,
) -> JSONResponse:
    if "host" in request.query_params:
        raise HTTPException(status_code=400, detail="host selection is no longer supported")

    media_id, season, episode = _normalise_media(media_type, media_id, season, episode)
    refresh = request.query_params.get("retry") == "1"
    response = {
        "player_url": _sign_player_url(
            request, media_type, media_id, season, episode, refresh=refresh
        ),
        "media": {
            "type": media_type,
            "id": media_id,
            "season": season,
            "episode": episode,
        },
    }
    return JSONResponse(response, headers={"Cache-Control": "no-store"})


@gateway_router.get("/resolve/movie/{media_id}")
async def resolve_movie(request: Request, media_id: str):
    return await _gateway_response(request, "movie", media_id, None, None)


@gateway_router.get("/resolve/tv/{media_id}")
async def resolve_tv(request: Request, media_id: str):
    return await _gateway_response(request, "tv", media_id, None, None)


@gateway_router.get("/resolve/tv/{media_id}/{season}")
async def resolve_tv_season(request: Request, media_id: str, season: str):
    return await _gateway_response(request, "tv", media_id, season, None)


@gateway_router.get("/resolve/tv/{media_id}/{season}/{episode}")
async def resolve_tv_episode(request: Request, media_id: str, season: str, episode: str):
    return await _gateway_response(request, "tv", media_id, season, episode)
