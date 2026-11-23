"""Manual SubDL subtitle search/import for the MediaFlow player.

Everything here runs server-side. The SubDL API key is never sent to the
browser: only signed, same-origin URLs that serve converted WebVTT content.
Search and import are viewer-initiated, mirroring Streamflix's original flow;
nothing is collected automatically when the iframe mounts.
"""

from __future__ import annotations

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


class SubdlImportError(Exception):
    """A manual SubDL import failure with a browser-safe explanation."""

    def __init__(self, detail: str, *, code: str = "import_failed", http_status: int = 502):
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.http_status = http_status


SEARCH_URL = "https://api.subdl.com/api/v1/subtitles"
DOWNLOAD_BASE = "https://dl.subdl.com"
USER_AGENT = "OPLAY.ID-SubDL/1.0"
SEARCH_TIMEOUT = 15.0
DOWNLOAD_TIMEOUT = 20.0
MAX_RESULTS = 40
MAX_SUBTITLE_BYTES = 2 * 1024 * 1024
SUBTITLE_CACHE_TTL = 6 * 60 * 60  # Cap at 6h, matching the extraction cache.
LANGUAGE_LABELS = {"id": "Bahasa Indonesia", "en": "English"}

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


def normalise_language(value: str) -> str:
    cleaned = (value or "").strip().lower()
    names = {"indonesian": "id", "english": "en", "bahasa indonesia": "id"}
    if cleaned in names:
        return names[cleaned]
    match = re.match(r"^([a-z]{2})(?:[-_]|$)", cleaned)
    return match.group(1) if match else (cleaned[:2] or "und")


def language_label(value: str) -> str:
    code = normalise_language(value)
    return LANGUAGE_LABELS.get(code, value)




def to_webvtt(raw: str) -> str:
    """Convert SRT/VTT to validated WebVTT, matching Streamflix's converter."""
    text = (raw or "").replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise ValueError("Subtitle kosong")
    if len(text.encode("utf-8")) > MAX_SUBTITLE_BYTES:
        raise ValueError("Subtitle terlalu besar (maksimal 2 MB)")
    if re.match(r"^WEBVTT\b", text, re.I):
        # Normalise the header but keep the converter idempotent.
        return "WEBVTT" + text[6:]

    cues: list[str] = []
    for block in re.split(r"\n{2,}", text):
        lines = [line for line in block.split("\n") if line.strip()]
        index = next((i for i, line in enumerate(lines) if "-->" in line), -1)
        if index < 0:
            continue
        # Same normalisation as Streamflix's toWebVtt: exactly one space around
        # the arrow, then comma milliseconds to periods.
        times = re.sub(r"\s*-->\s*", " --> ", lines[index])
        times = re.sub(r"(\d),(\d{3})", r"\1.\2", times)
        payload = "\n".join(lines[index + 1 :]).strip()
        if not payload:
            continue
        cues.append(f"{times}\n{payload}".strip())
    if not cues:
        raise ValueError("Tidak ada cue subtitle yang valid")
    return "WEBVTT\n\n" + "\n\n".join(cues) + "\n"


def _clean_path(raw: str) -> str | None:
    path = (raw or "").split("?", 1)[0].strip()
    return path if _SAFE_PATH.match(path) else None


def safe_path(raw: str) -> bool:
    """Public validation used by the import endpoint."""
    return _clean_path(raw) is not None


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
        # Streamflix's proven flow only uses unpack_files. The item-level
        # ``url`` is a .zip wrapper and is intentionally never downloaded.
        for file in item.get("unpack_files") or []:
            if not isinstance(file, dict):
                continue
            path = _clean_path(str(file.get("url") or ""))
            if not path or path in seen:
                continue
            seen.add(path)
            language = normalise_language(
                str(file.get("language") or item.get("language") or item.get("lang") or "en")
            )
            hi = bool(file.get("hi") if file.get("hi") is not None else item.get("hi"))
            release = str(
                file.get("release_name")
                or file.get("name")
                or item.get("release_name")
                or item.get("name")
                or "Subtitle"
            )[:200]
            label = f"{language_label(language)}{' (HI)' if hi else ''} · {release}"[:120]
            found.append(
                {
                    "id": f"{path}:{language}:{'hi' if hi else 'n'}",
                    "lang": language,
                    "label": label,
                    "releaseName": release,
                    "author": str(item.get("author") or "")[:120],
                    "hi": hi,
                    "downloadPath": path,
                    "format": str(file.get("format") or "srt").lower()[:10],
                }
            )
            if len(found) >= MAX_RESULTS:
                return found
    return found


async def _download(download_path: str) -> str:
    """Download one unpacked SubDL file, raising a browser-safe error."""
    api_key = (settings.subdl_api_key or "").strip()
    if not api_key:
        raise SubdlImportError(
            "SUBDL_API_KEY belum dikonfigurasi di server",
            code="missing_key",
            http_status=503,
        )
    path = _clean_path(download_path)
    if not path:
        raise SubdlImportError(
            "Path subtitle SubDL tidak valid", code="invalid_path", http_status=400
        )
    url = f"{DOWNLOAD_BASE}{path}?api_key={api_key}"
    streamer = await create_streamer(DOWNLOAD_BASE)
    try:
        async with streamer.session.get(
            url,
            headers={"Accept": "*/*", "User-Agent": USER_AGENT, "x-api-key": api_key},
            timeout=ClientTimeout(total=DOWNLOAD_TIMEOUT),
        ) as response:
            if response.status != 200:
                logger.warning("[subdl] download HTTP %s", response.status)
                if response.status in (401, 403, 429):
                    raise SubdlImportError(
                        f"SubDL menolak unduhan (HTTP {response.status}). "
                        "Periksa kuota atau API key SubDL.",
                        code="download_rejected",
                    )
                raise SubdlImportError(
                    f"SubDL mengembalikan HTTP {response.status} saat mengunduh subtitle.",
                    code="download_failed",
                )
            text = await response.text(errors="ignore")
    except SubdlImportError:
        raise
    except Exception as exc:
        # Never echo the exception text: aiohttp may include the signed URL.
        logger.warning("[subdl] download failed: %s", type(exc).__name__)
        raise SubdlImportError(
            "Gagal menghubungi server download SubDL.", code="network"
        ) from exc
    finally:
        await streamer.session.close()
    if not text.strip():
        raise SubdlImportError("File SubDL kosong.", code="empty")
    return text


def track_id(download_path: str, language: str) -> str:
    """Stable id used for dedupe in the player and in Streamflix Mongo."""
    digest = hashlib.sha1(f"{download_path}:{language}".encode()).hexdigest()[:12]
    return f"subdl-{digest}"


async def import_one(result: dict, *, signer: Callable[[str], str]) -> dict:
    """Download one manually chosen SubDL result and return a signed VTT track."""
    path = _clean_path(str(result.get("downloadPath") or ""))
    if not path:
        raise SubdlImportError(
            "Path subtitle SubDL tidak valid", code="invalid_path", http_status=400
        )
    raw = await _download(path)
    try:
        vtt = to_webvtt(raw)
    except ValueError as exc:
        message = str(exc)
        logger.warning("[subdl] import rejected: %s", message)
        lowered = message.lower()
        if "kosong" in lowered:
            detail = "File SubDL kosong."
        elif "terlalu besar" in lowered:
            detail = "Subtitle SubDL terlalu besar (maksimal 2 MB)."
        else:
            detail = "File SubDL bukan subtitle SRT/VTT yang valid."
        raise SubdlImportError(detail, code="invalid_content") from exc

    language = normalise_language(str(result.get("lang") or "en"))
    label = str(result.get("label") or language_label(language)).strip()[:120]
    try:
        key = await store_vtt(vtt)
        url = signer(key)
    except Exception as exc:
        logger.warning("[subdl] store failed: %s", type(exc).__name__)
        raise SubdlImportError(
            "Gagal menyimpan subtitle di server.", code="storage"
        ) from exc
    return {
        "id": track_id(path, language),
        "label": f"SubDL • {label}" if not label.lower().startswith("subdl") else label,
        "language": language,
        "format": "vtt",
        "origin": "subdl",
        "url": url,
        "downloadPath": path,
    }
