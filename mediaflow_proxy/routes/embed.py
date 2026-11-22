"""Gateway endpoints for StreamFlix.

StreamFlix sends only media identity. MediaFlow visits provider embed pages
server-side, scrubs ads, resolves a clean HLS/MP4 URL plus subtitles, caches the
result and returns MediaFlow-signed proxy URLs as JSON. The browser never sees
a provider page, a server list, or ``api_password``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from mediaflow_proxy.configs import settings
from mediaflow_proxy.extractors.embed_hosts import AUTO_HOST_ORDER, build_embed_url, get_config
from mediaflow_proxy.extractors.factory import ExtractorFactory
from mediaflow_proxy.gateway.ad_filter import AdFilterReport, filter_extractor_result
from mediaflow_proxy.utils.crypto_utils import EncryptionHandler
from mediaflow_proxy.utils.http_utils import (
    ProxyRequestHeaders,
    encode_mediaflow_proxy_url,
    get_original_scheme,
    get_proxy_headers,
)

logger = logging.getLogger(__name__)
gateway_router = APIRouter()

# Three hosts are resolved sequentially, so each gets a generous budget while
# the total stays bounded. Failover order: VidCore -> VSEmbed -> VidLink.
PROBE_TIMEOUT = 10.0
AUTO_TOTAL_TIMEOUT = 40.0
AUTO_FIRST_WAVE = 1
NEGATIVE_CACHE_TTL = 30.0
# Resolve the provider once, then serve the cached result for at most 6 hours
# (or less when the upstream URL advertises its own expiry).
RESOLVE_CACHE_TTL = 6 * 60 * 60.0

# (host, media_type, media_id, season, episode) -> monotonic expiry.
_negative_cache: dict[tuple[str, str, str, str, str], float] = {}
# Same key -> {"result": extractor result, "expires_at": monotonic expiry}.
_resolved_cache: dict[tuple[str, str, str, str, str], dict] = {}
_cache_lock = asyncio.Lock()


def _cache_key(config, media_type: str, media_id: str, season: str | None, episode: str | None):
    return (config.canonical, media_type, media_id, season or "", episode or "")


async def _cache_is_hot(key: tuple[str, str, str, str, str]) -> bool:
    now = time.monotonic()
    async with _cache_lock:
        expiry = _negative_cache.get(key)
        if expiry is None:
            return False
        if expiry > now:
            return True
        _negative_cache.pop(key, None)
        return False


async def _cache_failure(key: tuple[str, str, str, str, str]) -> None:
    async with _cache_lock:
        _negative_cache[key] = time.monotonic() + NEGATIVE_CACHE_TTL


async def _clear_failures(media_type: str, media_id: str, season: str | None, episode: str | None) -> None:
    """``?retry=1`` clears both positive and negative cache for this media."""
    suffix = (media_type, media_id, season or "", episode or "")
    async with _cache_lock:
        for key in [k for k in _negative_cache if k[1:] == suffix]:
            _negative_cache.pop(key, None)
        for key in [k for k in _resolved_cache if k[1:] == suffix]:
            _resolved_cache.pop(key, None)


def _upstream_ttl(result: dict) -> float:
    """Use the upstream URL's own expiry when it is shorter than our cap."""
    params = parse_qs(urlparse(result.get("destination_url") or "").query)
    now = time.time()
    for name in ("expires", "exp", "e"):
        values = params.get(name) or []
        if not values:
            continue
        try:
            raw = float(values[0])
        except (TypeError, ValueError):
            continue
        if raw > 1_000_000_000:
            remaining = raw - now
        else:
            start = None
            for start_name in ("s", "start", "iat"):
                candidate = params.get(start_name) or []
                if not candidate:
                    continue
                try:
                    value = float(candidate[0])
                except (TypeError, ValueError):
                    continue
                if value > 1_000_000_000:
                    start = value
                    break
            remaining = (start + raw - now) if start else raw
        if 0 < remaining < RESOLVE_CACHE_TTL:
            return max(min(remaining - 30, RESOLVE_CACHE_TTL), 60.0)
    return RESOLVE_CACHE_TTL


async def _cache_get(key: tuple[str, str, str, str, str]) -> dict | None:
    now = time.monotonic()
    async with _cache_lock:
        entry = _resolved_cache.get(key)
        if not entry:
            return None
        if entry["expires_at"] <= now:
            _resolved_cache.pop(key, None)
            return None
        return entry["result"]


async def _cache_set(key: tuple[str, str, str, str, str], result: dict) -> None:
    async with _cache_lock:
        _resolved_cache[key] = {
            "result": dict(result),
            "expires_at": time.monotonic() + _upstream_ttl(result),
        }


async def _cache_ttl(key: tuple[str, str, str, str, str]) -> int:
    async with _cache_lock:
        entry = _resolved_cache.get(key)
        if not entry:
            return int(RESOLVE_CACHE_TTL)
        return max(60, int(entry["expires_at"] - time.monotonic()))


async def _extract(
    request: Request,
    config,
    proxy_headers: ProxyRequestHeaders,
    media_type: str,
    media_id: str,
    season: str | None,
    episode: str | None,
) -> dict:
    """Fetch one embed host and return only clean media/subtitle metadata."""
    embed_url = build_embed_url(config, media_type, media_id, season, episode)
    extractor = ExtractorFactory.get_extractor(config.canonical, proxy_headers.request)
    result = await extractor.extract(embed_url)
    return filter_extractor_result(result, AdFilterReport())


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
        proxy_base = str(
            request.url_for(endpoint).replace(scheme=get_original_scheme(request))
        )

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
        request_headers=request_headers or result.get("request_headers"),
        response_headers=proxy_headers.response,
        encryption_handler=encryption_handler,
        expiration=expiration or result.get("expiration", 300),
    )


def _signed_subtitles(
    request: Request,
    result: dict,
    proxy_headers: ProxyRequestHeaders,
    items: list[dict],
    expiration: int | None,
    *,
    origin: str,
) -> list[dict]:
    signed: list[dict] = []
    for subtitle in items:
        url = subtitle.get("url")
        if not url:
            continue
        signed.append(
            {
                "id": f"{origin}-{subtitle.get('id') or len(signed)}",
                "label": subtitle.get("label") or subtitle.get("language") or "Subtitle",
                "language": subtitle.get("language") or "",
                "format": subtitle.get("format") or "vtt",
                "origin": origin,
                "url": _sign_asset_url(
                    request,
                    result,
                    proxy_headers,
                    url,
                    endpoint="hls_segment_proxy",
                    request_headers=subtitle.get("request_headers"),
                    force_playlist_proxy=False,
                    expiration=expiration,
                ),
            }
        )
    return signed


def _signed_metadata(
    request: Request,
    result: dict,
    proxy_headers: ProxyRequestHeaders,
    expiration: int | None = None,
) -> tuple[list[dict], list[dict], list[dict], dict]:
    sources: list[dict] = []
    for source in result.get("sources") or []:
        url = source.get("url")
        if not url:
            continue
        sources.append(
            {
                "id": source.get("id") or _quality_id(source.get("label")),
                "label": source.get("label") or "Auto",
                "url": _sign_asset_url(
                    request,
                    result,
                    proxy_headers,
                    url,
                    request_headers=source.get("request_headers"),
                    force_playlist_proxy=True,
                    expiration=expiration,
                ),
            }
        )

    host_subtitles = _signed_subtitles(
        request, result, proxy_headers, result.get("subtitles") or [], expiration, origin="host"
    )
    provider_subtitles = _signed_subtitles(
        request,
        result,
        proxy_headers,
        result.get("provider_subtitles") or [],
        expiration,
        origin="provider",
    )

    return sources, host_subtitles, provider_subtitles, dict(result.get("capabilities") or {})


def _quality_id(label) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(label or "auto").lower()).strip("-")
    return slug or "auto"


class EmbedUnavailable(Exception):
    """Every configured provider was unable to serve this media."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _error_reason(exc: Exception) -> str:
    """Turn upstream exception text into a stable, loggable reason code."""
    text = str(exc).lower()
    if any(
        marker in text
        for marker in (
            "movie not found",
            "episode not found",
            "no vidora link found",
            "stream unavailable",
            "http error 404",
            "http error 410",
        )
    ):
        return "content_unavailable"
    if "ad-filter" in text:
        return "ad_filter_blocked"
    if "config missing" in text or "config object not found" in text:
        return "upstream_changed"
    if "invalid api token" in text or "403" in text:
        return "token_invalid"
    if "name or service not known" in text or "name resolution" in text:
        return "dns"
    if "ssl" in text or "certificate" in text or "tls" in text:
        return "tls"
    if "timed out" in text or "timeout" in text:
        return "timeout"
    return "extract_failed"


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


async def _probe_gateway(
    request: Request,
    config,
    proxy_headers: ProxyRequestHeaders,
    media_type: str,
    media_id: str,
    season: str | None,
    episode: str | None,
):
    """Resolve from cache or provider once, then sign a long-lived URL."""
    key = _cache_key(config, media_type, media_id, season, episode)
    result = await _cache_get(key)
    if result is None:
        try:
            result = await asyncio.wait_for(
                _extract(request, config, proxy_headers, media_type, media_id, season, episode),
                timeout=PROBE_TIMEOUT,
            )
        except Exception as exc:
            reason = _error_reason(exc)
            logger.warning("[gateway] %s failed (%s): %s", config.canonical, reason, exc)
            return config, None, f"{config.canonical}: {reason}: {exc}"
        await _cache_set(key, result)

    ttl = await _cache_ttl(key)
    try:
        stream_url = _sign_asset_url(
            request, result, proxy_headers, result["destination_url"], expiration=ttl
        )
        sources, subtitles, provider_subtitles, capabilities = _signed_metadata(
            request, result, proxy_headers, expiration=ttl
        )
    except Exception as exc:
        logger.warning("[gateway] %s metadata signing failed: %s", config.canonical, exc)
        return config, None, f"{config.canonical}: {exc}"

    audit = dict(result.get("ad_filter") or {})
    if result.get("sandbox_used"):
        audit["sandbox_used"] = True
    audit["hls_filter_enabled"] = settings.filter_hls_ads
    if any(value for key, value in audit.items() if key != "hls_filter_enabled"):
        logger.info(
            "[ad-filter] audit host=%s media=%s/%s s%s e%s %s",
            config.canonical,
            media_type,
            media_id,
            season or "-",
            episode or "-",
            audit,
        )
    return (
        config,
        {
            "host": config.canonical,
            "stream_url": stream_url,
            "sources": sources,
            # Native/host captions: safe to auto-enable.
            "subtitles": subtitles,
            # Subtitles from a separate provider API: opt-in only.
            "provider_subtitles": provider_subtitles,
            "capabilities": capabilities,
            "expires_in": ttl,
            "audit": audit,
        },
        None,
    )


# Alias kept for callers/tests that used the old probe name.
_probe_direct = _probe_gateway


async def _resolve_gateway(
    request: Request,
    media_type: str,
    media_id: str,
    season: str | None,
    episode: str | None,
    host: str | None = None,
) -> dict:
    media_id, season, episode = _normalise_media(media_type, media_id, season, episode)
    proxy_headers = get_proxy_headers(request)
    retry = request.query_params.get("retry") == "1"
    if retry:
        await _clear_failures(media_type, media_id, season, episode)

    if host:
        selected = get_config(host)
        configs = [selected] if selected else []
    else:
        configs = [get_config(name) for name in AUTO_HOST_ORDER]
        configs = [config for config in configs if config]

    errors: list[str] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + AUTO_TOTAL_TIMEOUT

    for start in range(0, len(configs), AUTO_FIRST_WAVE):
        wave = configs[start : start + AUTO_FIRST_WAVE]
        pending: list[asyncio.Task] = []
        for config in wave:
            if not retry and await _cache_is_hot(
                _cache_key(config, media_type, media_id, season, episode)
            ):
                errors.append(f"{config.canonical}: recently failed")
                continue
            pending.append(
                asyncio.create_task(
                    _probe_gateway(
                        request, config, proxy_headers, media_type, media_id, season, episode
                    )
                )
            )
        if not pending:
            continue

        try:
            for finished in asyncio.as_completed(
                pending, timeout=max(0.1, deadline - loop.time())
            ):
                config, payload, error = await finished
                if payload is not None:
                    for task in pending:
                        if not task.done():
                            task.cancel()
                    logger.info(
                        "[gateway] selected %s for %s %s s%s e%s",
                        config.canonical,
                        media_type,
                        media_id,
                        season or "-",
                        episode or "-",
                    )
                    return payload
                errors.append(error or f"{config.canonical}: failed")
                await _cache_failure(
                    _cache_key(config, media_type, media_id, season, episode)
                )
        except asyncio.TimeoutError:
            errors.append("gateway selection timed out")
        finally:
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    raise EmbedUnavailable("; ".join(errors) or "No embed provider available")


async def _gateway_response(
    request: Request,
    media_type: str,
    media_id: str,
    season: str | None,
    episode: str | None,
    host: str | None = None,
) -> JSONResponse:
    if host and get_config(host) is None:
        raise HTTPException(status_code=400, detail=f"unknown embed host: {host}")
    try:
        payload = await _resolve_gateway(
            request, media_type, media_id, season, episode, host=host
        )
    except EmbedUnavailable as exc:
        raise HTTPException(status_code=503, detail=exc.detail) from exc

    media_id, season, episode = _normalise_media(media_type, media_id, season, episode)
    response = dict(payload)
    response["media"] = {
        "type": media_type,
        "id": media_id,
        "season": season,
        "episode": episode,
    }
    return JSONResponse(response, headers={"Cache-Control": "no-store"})


@gateway_router.get("/cache")
async def gateway_cache_stats():
    """Debug statistics only; never exposes signed URLs."""
    now = time.monotonic()
    async with _cache_lock:
        resolved = [
            {
                "host": key[0],
                "type": key[1],
                "id": key[2],
                "season": key[3],
                "episode": key[4],
                "ttl": int(entry["expires_at"] - now),
            }
            for key, entry in _resolved_cache.items()
            if entry["expires_at"] > now
        ]
        failed = sum(1 for expiry in _negative_cache.values() if expiry > now)
    return {"resolved": resolved, "failed": failed}


@gateway_router.get("/resolve/movie/{media_id}")
async def resolve_movie(request: Request, media_id: str, host: str | None = None):
    return await _gateway_response(request, "movie", media_id, None, None, host=host)


@gateway_router.get("/resolve/tv/{media_id}")
async def resolve_tv(request: Request, media_id: str, host: str | None = None):
    return await _gateway_response(request, "tv", media_id, None, None, host=host)


@gateway_router.get("/resolve/tv/{media_id}/{season}")
async def resolve_tv_season(
    request: Request, media_id: str, season: str, host: str | None = None
):
    return await _gateway_response(request, "tv", media_id, season, None, host=host)


@gateway_router.get("/resolve/tv/{media_id}/{season}/{episode}")
async def resolve_tv_episode(
    request: Request, media_id: str, season: str, episode: str, host: str | None = None
):
    return await _gateway_response(request, "tv", media_id, season, episode, host=host)
