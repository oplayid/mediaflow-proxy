"""Optional SubDL subtitle tracks for the MediaFlow player.

Everything here runs server-side. The SubDL API key is never sent to the
browser: only signed, same-origin URLs that serve converted WebVTT content.
Failures are non-fatal — native tracks from the winning video host remain the
default and the player still works without any external subtitle service.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from typing import Callable
from urllib.parse import urlencode

from aiohttp import ClientTimeout

from mediaflow_proxy.configs import settings
from mediaflow_proxy.utils.http_utils import create_streamer
from mediaflow_proxy.utils.redis_utils import get_redis

logger = logging.getLogger(__name__)

SEARCH_URL = "https://api.subdl.com/api/v1/subtitles"
DOWNLOAD_BASE = "https://dl.subdl.com"
USER_AGENT = "OPLAY.ID-SubDL/1.0"
SEARCH_TIMEOUT = 4.0
DOWNLOAD_TIMEOUT = 5.0
COLLECT_TIMEOUT = 9.0
MAX_TRACKS = 8
SUBTITLE_CACHE_TTL = 6 * 60 * 60  # Cap at 6h, matching the extraction cache.

_SAFE_PATH = re.compile(r"^/subtitle/[A-Za-z0-9/_-]+$")
_KEY_RE = re.compile(r"^[0-9a-f]{32}$")
_TIMESTAMP_RE = re.compile(r"(\d{2}:\d{2}:\d{2}),(\d{3})")
_STORE_PREFIX = "mfp:subdl:vtt:"
_local_store: dict[str, tuple[float, str]] = {}
_LOCAL_MAX = 128


def srt_to_vtt(text: str) -> str:
    """Convert SubtitleRip timestamp commas to WebVTT periods."""
    stripped = (text or "").replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    stripped = _TIMESTAMP_RE.sub(r"\1.\2", stripped).strip("\x00\n ")
    if not stripped:
        return ""
    if stripped.upper().startswith("WEBVTT"):
        return stripped + "\n"
    return "WEBVTT\n\n" + stripped + "\n"


def _normalise_language(value: str) -> str:
    cleaned = (value or "").strip().lower()
    names = {"indonesian": "id", "english": "en", "bahasa indonesia": "id"}
    if cleaned in names:
        return names[cleaned]
    match = re.match(r"^([a-z]{2})(?:[-_]|$)", cleaned)
    return match.group(1) if match else (cleaned[:2] or "und")


def _clean_path(raw: str) -> str | None:
    path = (raw or "").split("?", 1)[0].strip()
    return path if _SAFE_PATH.match(path) else None


async def store_vtt(content: str, ttl: int = SUBTITLE_CACHE_TTL) -> str:
    """Store converted VTT and return its opaque lookup key."""
    key = hashlib.sha256(content.encode("utf-8")).hexdigest()[:32]
    redis = await get_redis()
    if redis is not None:
        await redis.set(_STORE_PREFIX + key, content, ex=ttl)
        return key
    now = time.monotonic()
    expired = [name for name, (expires, _) in _local_store.items() if expires <= now]
    for name in expired:
        _local_store.pop(name, None)
    if len(_local_store) >= _LOCAL_MAX:
        oldest = min(_local_store, key=lambda name: _local_store[name][0])
        _local_store.pop(oldest, None)
    _local_store[key] = (now + min(ttl, SUBTITLE_CACHE_TTL), content)
    return key


async def load_vtt(key: str) -> str | None:
    """Read a stored VTT track; used by the tokenized /player/subtitle route."""
    if not _KEY_RE.match(key or ""):
        return None
    redis = await get_redis()
    if redis is not None:
        value = await redis.get(_STORE_PREFIX + key)
        return str(value) if value else None
    entry = _local_store.get(key)
    if not entry:
        return None
    if entry[0] <= time.monotonic():
        _local_store.pop(key, None)
        return None
    return entry[1]


async def search(media_type: str, media_id: str, season: str | None, episode: str | None) -> list[dict]:
    """Search SubDL by TMDB id. Returns [] on any error or empty result."""
    api_key = (settings.subdl_api_key or "").strip()
    if not api_key:
        return []

    tmdb_id = re.sub(r"^tv-", "", str(media_id).strip(), flags=re.IGNORECASE)
    if not tmdb_id.isdigit():
        return []
    is_tv = media_type == "tv"
    params: dict[str, str] = {
        "api_key": api_key,
        "tmdb_id": tmdb_id,
        "type": "tv" if is_tv else "movie",
        "languages": "ID,EN",
        "subs_per_page": "30",
        "unpack": "1",
    }
    if is_tv:
        params["season_number"] = str(season or 1)
        params["episode_number"] = str(episode or 1)

    streamer = await create_streamer(SEARCH_URL)
    try:
        url = f"{SEARCH_URL}?{urlencode(params)}"
        async with streamer.session.get(
            url,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=ClientTimeout(total=SEARCH_TIMEOUT),
        ) as response:
            if response.status != 200:
                logger.info("[subdl] search HTTP %s", response.status)
                return []
            payload = await response.json(content_type=None)
    except Exception as exc:
        logger.info("[subdl] search failed: %s: %s", type(exc).__name__, exc)
        return []
    finally:
        await streamer.session.close()

    if not isinstance(payload, dict) or not payload.get("status"):
        message = str(payload.get("error") or payload.get("message") or "") if isinstance(payload, dict) else ""
        if message and not re.search(r"can't find|not found", message, re.I):
            logger.info("[subdl] search rejected: %s", message)
        return []

    found: list[dict] = []
    seen: set[str] = set()
    for item in payload.get("subtitles") or []:
        if not isinstance(item, dict):
            continue
        files = item.get("unpack_files") or [{}]
        for file in files:
            if not isinstance(file, dict):
                continue
            path = _clean_path(str(file.get("url") or item.get("url") or ""))
            if not path or path in seen:
                continue
            seen.add(path)
            language = _normalise_language(str(file.get("language") or item.get("language") or item.get("lang") or "en"))
            release = str(
                file.get("release_name") or file.get("name") or item.get("release_name") or item.get("name") or "Subtitle"
            )[:60]
            found.append(
                {
                    "id": hashlib.sha1(f"{path}:{language}".encode()).hexdigest()[:12],
                    "language": language,
                    "label": f"SubDL • {language.upper()} • {release}",
                    "download_path": path,
                }
            )
            if len(found) >= MAX_TRACKS:
                return found
    return found


async def _download(item: dict) -> str | None:
    api_key = (settings.subdl_api_key or "").strip()
    if not api_key:
        return None
    url = f"{DOWNLOAD_BASE}{item['download_path']}?api_key={api_key}"
    streamer = await create_streamer(DOWNLOAD_BASE)
    try:
        async with streamer.session.get(
            url,
            headers={"Accept": "*/*", "User-Agent": USER_AGENT},
            timeout=ClientTimeout(total=DOWNLOAD_TIMEOUT),
        ) as response:
            if response.status != 200:
                return None
            raw = await response.text(errors="ignore")
    except Exception as exc:
        logger.info("[subdl] download failed: %s: %s", type(exc).__name__, exc)
        return None
    finally:
        await streamer.session.close()
    return srt_to_vtt(raw)


async def collect(
    media_type: str,
    media_id: str,
    season: str | None,
    episode: str | None,
    *,
    signer: Callable[[str], str],
) -> list[dict]:
    """Return signed, same-origin VTT tracks for the player session."""
    if not (settings.subdl_api_key or "").strip():
        return []
    try:
        results = await search(media_type, media_id, season, episode)
    except Exception as exc:  # keep native subtitles/playback unaffected
        logger.info("[subdl] collect failed: %s: %s", type(exc).__name__, exc)
        return []
    if not results:
        return []

    semaphore = asyncio.Semaphore(3)

    async def one(item: dict) -> dict | None:
        async with semaphore:
            content = await _download(item)
        if not content:
            return None
        key = await store_vtt(content)
        return {
            "id": f"subdl-{item['id']}",
            "label": item["label"],
            "language": item["language"],
            "format": "vtt",
            "origin": "subdl",
            "url": signer(key),
        }

    tracks = await asyncio.gather(*(one(item) for item in results), return_exceptions=True)
    signed = [track for track in tracks if isinstance(track, dict)]
    if signed:
        logger.info("[subdl] added %s external tracks for %s/%s", len(signed), media_type, media_id)
    return signed
