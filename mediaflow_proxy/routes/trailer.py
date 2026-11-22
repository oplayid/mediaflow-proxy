"""Public trailer and banner endpoints backed by TMDB metadata.

MediaFlow selects the YouTube trailer (Indonesian first, English fallback) and
renders either a plain trailer iframe or a poster-first banner page. The YouTube
stream itself is played by the viewer's browser, so the gateway only performs
small cached TMDB JSON lookups.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse

from mediaflow_proxy.configs import settings
from mediaflow_proxy.utils.http_client import create_aiohttp_session

logger = logging.getLogger(__name__)
trailer_router = APIRouter()
banner_router = APIRouter()

TMDB_BASE = "https://api.themoviedb.org/3"
CACHE_TTL = 6 * 60 * 60
NEGATIVE_CACHE_TTL = 5 * 60
YOUTUBE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
TMDB_ID_RE = re.compile(r"^\d+$")

_cache: dict[tuple[str, str], tuple[float, dict[str, Any] | None]] = {}
_cache_lock = asyncio.Lock()


def _language_rank(video: dict) -> int:
    """Indonesian is preferred, English is the fallback, others come last."""
    lang = str(video.get("iso_639_1") or "").lower()
    name = str(video.get("name") or "").lower()
    if lang == "id" or "indonesia" in name:
        return 0
    if lang == "en" or "english" in name:
        return 1
    return 2


def _type_rank(video: dict) -> int:
    kind = str(video.get("type") or "").lower()
    if kind == "trailer":
        return 0
    if kind == "teaser":
        return 1
    return 2


def select_youtube_trailer(videos: list[dict]) -> dict[str, Any] | None:
    """Pick the best YouTube trailer from a TMDB ``/videos`` result list."""
    candidates = [
        video
        for video in videos
        if isinstance(video, dict)
        and video.get("site") == "YouTube"
        and YOUTUBE_KEY_RE.match(str(video.get("key") or ""))
        and _type_rank(video) < 2
    ]
    if not candidates:
        return None

    ordered = sorted(
        candidates,
        key=lambda video: (
            _type_rank(video),
            _language_rank(video),
            0 if video.get("official") else 1,
        ),
    )
    chosen = ordered[0]
    return {
        "key": chosen["key"],
        "title": chosen.get("name") or "Trailer",
        "language": chosen.get("iso_639_1") or "",
        "type": chosen.get("type") or "Trailer",
        "official": bool(chosen.get("official")),
    }


async def _fetch_tmdb_videos(media_type: str, tmdb_id: str) -> list[dict]:
    url = f"{TMDB_BASE}/{media_type}/{tmdb_id}/videos"
    params = {"language": "en-US"}
    if settings.tmdb_api_key:
        params["api_key"] = settings.tmdb_api_key

    async with create_aiohttp_session(url, timeout=10) as (session, proxy_url):
        async with session.get(url, params=params, proxy=proxy_url) as response:
            if response.status != 200:
                logger.warning("[trailer] TMDB %s returned %s", url, response.status)
                return []
            payload = await response.json(content_type=None)

    results = payload.get("results") if isinstance(payload, dict) else None
    return results if isinstance(results, list) else []


async def _get_trailer(media_type: str, tmdb_id: str) -> dict[str, Any] | None:
    cache_key = (media_type, tmdb_id)
    now = time.monotonic()
    async with _cache_lock:
        entry = _cache.get(cache_key)
        if entry and entry[0] > now:
            return entry[1]

    try:
        trailer = select_youtube_trailer(await _fetch_tmdb_videos(media_type, tmdb_id))
    except Exception as exc:
        logger.warning("[trailer] failed to resolve %s/%s: %s", media_type, tmdb_id, exc)
        trailer = None
        expiry = now + NEGATIVE_CACHE_TTL
    else:
        expiry = now + (CACHE_TTL if trailer else NEGATIVE_CACHE_TTL)

    async with _cache_lock:
        _cache[cache_key] = (expiry, trailer)
    return trailer


_detail_cache: dict[tuple[str, str], tuple[float, dict[str, str]]] = {}


async def _fetch_tmdb_detail(media_type: str, tmdb_id: str) -> dict[str, str]:
    url = f"{TMDB_BASE}/{media_type}/{tmdb_id}"
    params = {"language": "en-US"}
    if settings.tmdb_api_key:
        params["api_key"] = settings.tmdb_api_key

    async with create_aiohttp_session(url, timeout=10) as (session, proxy_url):
        async with session.get(url, params=params, proxy=proxy_url) as response:
            if response.status != 200:
                logger.warning("[banner] TMDB %s returned %s", url, response.status)
                return {}
            payload = await response.json(content_type=None)

    if not isinstance(payload, dict):
        return {}
    backdrop = payload.get("backdrop_path") or payload.get("poster_path") or ""
    return {
        "backdrop": f"https://image.tmdb.org/t/p/w1280{backdrop}" if backdrop else "",
        "title": str(payload.get("title") or payload.get("name") or ""),
    }


async def _get_detail(media_type: str, tmdb_id: str) -> dict[str, str]:
    key = (media_type, tmdb_id)
    now = time.monotonic()
    async with _cache_lock:
        entry = _detail_cache.get(key)
        if entry and entry[0] > now:
            return entry[1]

    try:
        detail = await _fetch_tmdb_detail(media_type, tmdb_id)
    except Exception as exc:
        logger.warning("[banner] failed to resolve %s/%s: %s", media_type, tmdb_id, exc)
        detail = {}
        expiry = now + NEGATIVE_CACHE_TTL
    else:
        expiry = now + (CACHE_TTL if detail else NEGATIVE_CACHE_TTL)

    async with _cache_lock:
        _detail_cache[key] = (expiry, detail)
    return detail


def _validate(media_type: str, tmdb_id: str) -> None:
    if media_type not in ("movie", "tv"):
        raise HTTPException(status_code=404, detail="Unknown trailer media type")
    if not TMDB_ID_RE.match(tmdb_id):
        raise HTTPException(status_code=400, detail="TMDB id must be numeric")
    if not settings.tmdb_api_key:
        raise HTTPException(status_code=503, detail="TMDB_API_KEY is not configured")


def _embed_url(
    key: str,
    *,
    autoplay: bool,
    mute: bool,
    controls: bool,
    loop: bool,
) -> str:
    params = {
        "autoplay": "1" if autoplay else "0",
        "mute": "1" if mute else "0",
        "controls": "1" if controls else "0",
        "loop": "1" if loop else "0",
        "playsinline": "1",
        "rel": "0",
        "modestbranding": "1",
    }
    if loop:
        # YouTube only loops a single video when the playlist matches the id.
        params["playlist"] = key
    return f"https://www.youtube-nocookie.com/embed/{key}?{urlencode(params)}"


def _player_html(src: str | None, title: str) -> HTMLResponse:
    if not src:
        body = (
            '<div style="display:flex;height:100%;align-items:center;justify-content:center;'
            'color:#bbb;font:14px system-ui;text-align:center">Trailer tidak tersedia</div>'
        )
    else:
        body = (
            f'<iframe src="{html.escape(src, quote=True)}" title="{html.escape(title, quote=True)}" '
            'style="border:0;width:100%;height:100%" allow="autoplay; encrypted-media; '
            'picture-in-picture" allowfullscreen referrerpolicy="strict-origin-when-cross-origin"></iframe>'
        )
    page = f"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>{html.escape(title)}</title>
<style>html,body{{margin:0;height:100%;background:#000;overflow:hidden}}</style>
</head>
<body>{body}</body>
</html>"""
    return HTMLResponse(page, headers={"Cache-Control": "public, max-age=3600"})


async def _render_trailer(
    media_type: str,
    tmdb_id: str,
    autoplay: bool,
    mute: bool,
    controls: bool,
    loop: bool,
) -> HTMLResponse:
    _validate(media_type, tmdb_id)
    trailer = await _get_trailer(media_type, tmdb_id)
    src = (
        _embed_url(
            trailer["key"],
            autoplay=autoplay,
            mute=mute,
            controls=controls,
            loop=loop,
        )
        if trailer
        else None
    )
    title = trailer["title"] if trailer else f"Trailer {tmdb_id}"
    return _player_html(src, title)


async def _trailer_info(media_type: str, tmdb_id: str):
    """Debug endpoint: selected YouTube key without exposing the TMDB API key."""
    _validate(media_type, tmdb_id)
    trailer = await _get_trailer(media_type, tmdb_id)
    if not trailer:
        raise HTTPException(status_code=404, detail="No YouTube trailer found")
    return trailer


@trailer_router.get("/info/movie/{tmdb_id}")
async def movie_trailer_info(tmdb_id: str):
    return await _trailer_info("movie", tmdb_id)


@trailer_router.get("/info/tv/{tmdb_id}")
async def tv_trailer_info(tmdb_id: str):
    return await _trailer_info("tv", tmdb_id)


def trailer_options(
    autoplay: bool = Query(True),
    mute: bool = Query(True),
    controls: bool = Query(False),
    loop: bool = Query(True),
) -> dict:
    return {
        "autoplay": autoplay,
        "mute": mute,
        "controls": controls,
        "loop": loop,
    }


@trailer_router.get("/movie/{tmdb_id}", response_class=HTMLResponse)
async def movie_trailer(tmdb_id: str, options: dict = Depends(trailer_options)):
    """Iframe page that autoplays the movie's YouTube trailer."""
    return await _render_trailer("movie", tmdb_id, **options)


@trailer_router.get("/tv/{tmdb_id}", response_class=HTMLResponse)
async def tv_trailer(tmdb_id: str, options: dict = Depends(trailer_options)):
    """Iframe page that autoplays the show's YouTube trailer."""
    return await _render_trailer("tv", tmdb_id, **options)


@trailer_router.get("/tv/{tmdb_id}/{season}/{episode}", response_class=HTMLResponse)
async def tv_episode_trailer(
    tmdb_id: str,
    season: str,
    episode: str,
    options: dict = Depends(trailer_options),
):
    """TV route kept for symmetry with the embed endpoints; uses the show trailer."""
    return await _render_trailer("tv", tmdb_id, **options)


def _banner_html(
    poster: str,
    trailer_src: str | None,
    target: str,
    title: str,
    delay: int,
) -> HTMLResponse:
    """Poster-first banner that swaps in a muted trailer after a short delay."""
    template = """<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>__MFP_TITLE__</title>
<style>
html,body{margin:0;height:100%;background:#111;overflow:hidden}
#poster{position:absolute;inset:0;z-index:0;background-color:#111;
        background-image:url(__MFP_POSTER__);background-size:cover;background-position:center}
#poster:after{content:"";position:absolute;inset:0;
        background:linear-gradient(180deg,rgba(0,0,0,.05),rgba(0,0,0,.55))}
#stage{position:absolute;inset:0;z-index:1}
#stage iframe{border:0;width:100%;height:100%;display:block}
#hit{position:absolute;inset:0;z-index:2;display:block}
#hint{position:absolute;left:12px;bottom:10px;z-index:2;color:#fff;pointer-events:none;
      font:12px/1.4 system-ui,-apple-system,Arial,sans-serif;text-shadow:0 1px 3px #000}
</style>
</head>
<body>
<div id="poster"></div>
<div id="stage"></div>
<a id="hit" href="__MFP_TARGET__" target="_top" aria-label="Putar penuh"></a>
<div id="hint">Klik untuk memutar penuh</div>
<script>
(function () {
  if (!__MFP_HAS_TRAILER__) return;
  window.setTimeout(function () {
    var stage = document.getElementById('stage');
    var frame = document.createElement('iframe');
    frame.src = __MFP_TRAILER_SRC__;
    frame.setAttribute('allow', 'autoplay; encrypted-media; fullscreen');
    frame.setAttribute('allowfullscreen', '');
    frame.referrerPolicy = 'strict-origin-when-cross-origin';
    stage.appendChild(frame);
  }, __MFP_DELAY__);
})();
</script>
</body>
</html>"""
    replacements = {
        "__MFP_TITLE__": html.escape(title or "Banner", quote=True),
        "__MFP_POSTER__": json.dumps(poster) if poster else '""',
        "__MFP_TARGET__": html.escape(target, quote=True),
        "__MFP_HAS_TRAILER__": "true" if trailer_src else "false",
        "__MFP_TRAILER_SRC__": json.dumps(trailer_src or ""),
        "__MFP_DELAY__": str(max(0, delay)),
    }
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    return HTMLResponse(template, headers={"Cache-Control": "public, max-age=3600"})


async def _render_banner(
    media_type: str,
    tmdb_id: str,
    *,
    delay: int,
    autoplay_trailer: bool,
    mute: bool,
    season: str,
    episode: str,
) -> HTMLResponse:
    _validate(media_type, tmdb_id)
    trailer, detail = await asyncio.gather(
        _get_trailer(media_type, tmdb_id),
        _get_detail(media_type, tmdb_id),
    )

    trailer_src = None
    if trailer and autoplay_trailer:
        params = {
            "autoplay": "1",
            "mute": "1" if mute else "0",
            "controls": "0",
            "loop": "1",
        }
        trailer_src = f"/trailer/{media_type}/{tmdb_id}?{urlencode(params)}"

    if media_type == "tv":
        target = f"/embed/auto/tv/{tmdb_id}/{season}/{episode}?autoplay=true"
    else:
        target = f"/embed/auto/movie/{tmdb_id}?autoplay=true"

    title = detail.get("title") or f"Banner {tmdb_id}"
    return _banner_html(
        detail.get("backdrop") or "",
        trailer_src,
        target,
        title,
        delay,
    )


def banner_options(
    delay: int = Query(3000, ge=0, le=30000),
    autoplay_trailer: bool = Query(True),
    mute: bool = Query(True),
    season: str = Query("1"),
    episode: str = Query("1"),
) -> dict:
    return {
        "delay": delay,
        "autoplay_trailer": autoplay_trailer,
        "mute": mute,
        "season": season,
        "episode": episode,
    }


@banner_router.get("/movie/{tmdb_id}", response_class=HTMLResponse)
async def movie_banner(tmdb_id: str, options: dict = Depends(banner_options)):
    """Poster-first banner for a movie; click opens the full MediaFlow player."""
    return await _render_banner("movie", tmdb_id, **options)


@banner_router.get("/tv/{tmdb_id}", response_class=HTMLResponse)
async def tv_banner(tmdb_id: str, options: dict = Depends(banner_options)):
    """Poster-first banner for a show; click opens the full MediaFlow player."""
    return await _render_banner("tv", tmdb_id, **options)


@banner_router.get("/tv/{tmdb_id}/{season}/{episode}", response_class=HTMLResponse)
async def tv_episode_banner(
    tmdb_id: str,
    season: str,
    episode: str,
    options: dict = Depends(banner_options),
):
    """TV banner variant carrying season/episode for the full-player target."""
    options = {**options, "season": season, "episode": episode}
    return await _render_banner("tv", tmdb_id, **options)
