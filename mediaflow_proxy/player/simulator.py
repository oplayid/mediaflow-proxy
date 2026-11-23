"""One first-success host check when the mfp iframe mounts.

The player walks the shared host order at most once per host, skips hosts that
fail to resolve or pass a lightweight manifest/range probe, stops at the first
playable media URL and signs only that winner for the browser. There is no
post-playback failover: a failed winner shows a clean error in the player.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any
from urllib.parse import urljoin, urlparse

from aiohttp import ClientTimeout

from mediaflow_proxy.configs import settings
from mediaflow_proxy.extractors.embed_hosts import EMBED_HOSTS, HOST_ORDER, build_embed_url
from mediaflow_proxy.extractors.factory import ExtractorFactory
from mediaflow_proxy.gateway.ad_filter import AdFilterReport, filter_extractor_result
from mediaflow_proxy.player.models import Candidate, ProbeResult, session_to_dict
from mediaflow_proxy.player.transcoder import create_transcode_job, prepare_transcode
from mediaflow_proxy.routes.embed import _headers_for_destination, _sign_asset_url, _sign_subtitle_url
from mediaflow_proxy.subtitles import subdl
from mediaflow_proxy.utils.http_utils import create_streamer, get_proxy_headers
from mediaflow_proxy.utils.redis_utils import (
    delete_cached_extractor,
    get_cached_extractor,
    set_cached_extractor,
)

logger = logging.getLogger(__name__)

# Resolution/probe budget for the single host walk.
RESOLVE_TIMEOUT = 7.5
PROBE_TIMEOUT = 2.5
CACHE_PREFIX = "mfp:player-session:v3:"
SESSION_TTL = 600

_LANGUAGE_RANK = {"id": 0, "en": 1}
_CODEC_RE = re.compile(r'codecs=["\']([^"\']+)["\']', re.I)
_HEVC_RE = re.compile(r"\b(hvc1|hev1|hevc|h265)\b", re.I)
# Upstream paths often use ``_h265_`` where ``\b`` would not match after "_".
_HEVC_URL_RE = re.compile(r"(hvc1|hev1|hevc|h265)", re.I)
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _needs_transcode(probe: ProbeResult, destination: str) -> bool:
    """Return True when the source uses a codec browsers/TV boxes often lack."""
    if not settings.player_transcode_hevc:
        return False
    if any(_HEVC_RE.search(codec) for codec in probe.codecs):
        return True
    return bool(_HEVC_URL_RE.search(destination or ""))


def _audio_can_be_copied(probe: ProbeResult) -> bool:
    """HLS TS needs AAC; re-encode audio when the manifest says otherwise."""
    if not probe.codecs:
        return True
    return any("mp4a" in codec.lower() for codec in probe.codecs)


def _prewarm(job: str) -> None:
    """Start the winner's transcode without delaying the host walk."""
    task = asyncio.create_task(prepare_transcode(job))

    def _done(finished: asyncio.Task) -> None:
        _BACKGROUND_TASKS.discard(finished)
        if finished.cancelled():
            return
        exc = finished.exception()
        if exc:
            logger.info("[player] transcode prewarm failed: %s: %s", type(exc).__name__, exc)

    task.add_done_callback(_done)
    _BACKGROUND_TASKS.add(task)


def host_configs() -> list[Any]:
    """Return the shared first-success host order.

    ``HOST_ORDER`` in ``embed_hosts.py`` is the single source of truth. Adding a
    host is a one-line registry change there; the player needs no code change.
    """
    return [EMBED_HOSTS[name] for name in HOST_ORDER if name in EMBED_HOSTS]


async def _resolve_host(config, request, media_type: str, media_id: str, season: str, episode: str) -> dict:
    """Resolve one embed host into an ad-filtered extractor result."""
    embed_url = build_embed_url(config, media_type, media_id, season, episode)
    extractor = ExtractorFactory.get_extractor(config.canonical, get_proxy_headers(request).request)
    result = await extractor.extract(embed_url)
    return filter_extractor_result(result, AdFilterReport())


def _classify_manifest(text: str, content_type: str, url: str) -> str:
    lowered = text.lower()
    if "#extm3u" in lowered:
        return "hls"
    if "<mpd" in lowered or "application/dash+xml" in content_type.lower():
        return "dash"
    if content_type.lower().startswith("video/") or url.lower().split("?", 1)[0].endswith(".mp4"):
        return "mp4"
    return "unknown"


async def _fetch_small(streamer, url: str, headers: dict, limit: int, timeout: float) -> tuple[int, bytes, str]:
    request_headers = dict(headers)
    request_headers["Range"] = f"bytes=0-{limit}"
    request_headers.setdefault("Accept", "*/*")
    async with streamer.session.get(url, headers=request_headers, timeout=ClientTimeout(total=timeout)) as response:
        body = await response.content.read(limit + 1)
        return response.status, body, response.headers.get("content-type", "")


async def _probe_manifest(streamer, url: str, headers: dict, timeout: float) -> tuple[int, bytes, str]:
    async with streamer.session.get(url, headers=headers, timeout=ClientTimeout(total=timeout)) as response:
        body = await response.content.read(64 * 1024)
        return response.status, body, response.headers.get("content-type", "")


async def probe_candidate(result: dict, streamer, timeout: float = PROBE_TIMEOUT) -> ProbeResult:
    """Fetch the manifest, one small segment and a few subtitle headers."""
    started = time.monotonic()
    destination = result.get("destination_url")
    if not isinstance(destination, str) or not destination:
        return ProbeResult(reason="no destination URL")

    headers = _headers_for_destination(result, destination) or result.get("request_headers") or {}
    try:
        status, body, content_type = await _probe_manifest(streamer, destination, dict(headers), timeout)
    except Exception as exc:  # network/DNS/timeout
        return ProbeResult(reason=f"manifest fetch failed: {type(exc).__name__}")

    text = body.decode("utf-8", errors="ignore")
    manifest_type = _classify_manifest(text, content_type, destination)
    if status not in (200, 206) or manifest_type == "unknown":
        return ProbeResult(
            manifest_type=manifest_type,
            status=status,
            latency_ms=int((time.monotonic() - started) * 1000),
            reason=f"manifest rejected or unknown (HTTP {status})",
        )

    score = 40.0
    segment_status: int | None = None
    codecs: list[str] = []

    if manifest_type == "hls":
        segment = next(
            (
                line.strip()
                for line in text.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ),
            None,
        )
        if segment:
            segment_url = urljoin(destination, segment)
            # Relative variant URLs lose the manifest's signed query (VSEmbed
            # JWT token). The real proxy re-attaches it, so the probe must too.
            if not urlparse(segment_url).query and urlparse(destination).query:
                segment_url = f"{segment_url}?{urlparse(destination).query}"
            try:
                segment_status, _, _ = await _fetch_small(streamer, segment_url, headers, 1, timeout)
            except Exception:
                segment_status = 0
    elif manifest_type == "dash":
        codecs = [value.strip() for value in _CODEC_RE.findall(text)][:4]
    elif manifest_type == "mp4":
        segment_status = status

    if segment_status in (200, 206):
        score += 25
    elif segment_status is not None:
        score -= 20

    if manifest_type == "hls":
        score += 20
    elif manifest_type == "dash":
        score += 8
        if any(_HEVC_RE.search(codec) for codec in codecs):
            score -= 25  # dash.js can parse it, but browser HEVC decode is not guaranteed.
    elif manifest_type == "mp4":
        score += 12

    latency_ms = int((time.monotonic() - started) * 1000)
    score += max(0.0, 10.0 - latency_ms / 100.0)

    subtitle_status: dict[str, int] = {}
    for subtitle in (result.get("subtitles") or [])[:3]:
        subtitle_url = subtitle.get("url")
        if not isinstance(subtitle_url, str):
            continue
        key = subtitle.get("language") or subtitle.get("label") or "track"
        try:
            sub_status, _, _ = await _fetch_small(streamer, subtitle_url, headers, 512, min(timeout, 1.5))
            subtitle_status[str(key)] = sub_status
        except Exception:
            subtitle_status[str(key)] = 0

    if any(lang.startswith("id") and status == 200 for lang, status in subtitle_status.items()):
        score += 5

    return ProbeResult(
        ok=True,
        manifest_type=manifest_type,
        status=status,
        latency_ms=latency_ms,
        segment_status=segment_status,
        codecs=codecs,
        subtitle_status=subtitle_status,
        score=round(score, 2),
    )


def _language_rank(entry: dict) -> int:
    language = str(entry.get("language") or "").strip().lower()
    for prefix, rank in _LANGUAGE_RANK.items():
        if language.startswith(prefix):
            return rank
    return 99


async def _signed_candidate(request, result: dict, proxy_headers, expiration: int, host: str, probe: ProbeResult) -> Candidate:
    """Sign all URLs for one candidate while keeping headers server-side."""
    endpoint = result.get("mediaflow_endpoint") or "hls_manifest_proxy"
    destination = result["destination_url"]

    # A DASH source routed through the raw stream endpoint cannot resolve its
    # relative segments, and a DASH->HLS conversion endpoint returns an HLS
    # playlist even though the probe still reports `dash`. Correct both the
    # endpoint and the playback type here so the browser never hands an m3u8 to
    # dash.js (and FFmpeg always receives a playlist with absolute URLs).
    converted_dash = probe.manifest_type == "dash" and endpoint in (
        "proxy_stream_endpoint",
        "hls_manifest_proxy",
    )
    if converted_dash:
        endpoint = "mpd_manifest_proxy"
    manifest_type = "hls" if converted_dash else probe.manifest_type

    primary = _sign_asset_url(
        request,
        result,
        proxy_headers,
        destination,
        endpoint=endpoint,
        force_playlist_proxy=True,
        expiration=expiration,
    )

    sources: list[dict] = []
    for source in result.get("sources") or []:
        url = source.get("url")
        if not url:
            continue
        sources.append(
            {
                "id": source.get("id") or "auto",
                "label": source.get("label") or "Auto",
                "url": _sign_asset_url(
                    request,
                    result,
                    proxy_headers,
                    url,
                    endpoint=endpoint,
                    request_headers=source.get("request_headers"),
                    force_playlist_proxy=True,
                    expiration=expiration,
                ),
            }
        )

    subtitles: list[dict] = []
    for origin_key, origin in (("subtitles", "host"), ("provider_subtitles", "provider")):
        for item in result.get(origin_key) or []:
            url = item.get("url")
            if not url:
                continue
            subtitles.append(
                {
                    "id": f"{host.lower()}-{origin}-{item.get('id') or len(subtitles)}",
                    "label": item.get("label") or item.get("language") or "Subtitle",
                    "language": item.get("language") or "",
                    "format": item.get("format") or "vtt",
                    "origin": origin,
                    "host": host,
                    "url": _sign_asset_url(
                        request,
                        result,
                        proxy_headers,
                        url,
                        endpoint="hls_segment_proxy",
                        request_headers=item.get("request_headers"),
                        force_playlist_proxy=False,
                        expiration=expiration,
                    ),
                }
            )

    needs_transcode = _needs_transcode(probe, str(destination or ""))
    transcode_url = ""
    transcode_job = ""
    if needs_transcode:
        try:
            transcode_job, transcode_url = await create_transcode_job(
                request,
                primary,
                host,
                {
                    "type": request.query_params.get("type") or "",
                    "id": request.query_params.get("id") or "",
                    "host": host,
                    "manifest_type": probe.manifest_type,
                },
                audio_copy=_audio_can_be_copied(probe),
                manifest_type=probe.manifest_type,
            )
        except Exception as exc:
            # A failed job registration must not discard an otherwise playable
            # candidate: the player can still play the native source.
            logger.warning(
                "[player] %s transcode job failed: %s: %s",
                host,
                type(exc).__name__,
                exc,
            )
            needs_transcode = False

    return Candidate(
        host=host,
        url=primary,
        manifest_type=manifest_type,
        sources=sources,
        subtitles=subtitles,
        score=probe.score,
        probe=probe,
        needs_transcode=needs_transcode,
        transcode_url=transcode_url,
        transcode_job=transcode_job,
        playback_type="hls" if needs_transcode or converted_dash else probe.manifest_type,
    )


def has_indonesian_subtitle(items) -> bool:
    """True when the winning host already carries an Indonesian track."""
    for item in items or []:
        if not isinstance(item, dict):
            continue
        language = str(item.get("language") or "").strip().lower()
        label = str(item.get("label") or "").strip().lower()
        if language.startswith("id") or re.search(r"\b(indonesia|indonesian|bahasa)\b", label):
            return True
    return False


def _merge_subtitles(candidates: list[Candidate], limit: int | None = None) -> list[dict]:
    """Keep the whole subtitle package from each candidate, ID/EN first."""
    merged: dict[tuple[str, str], dict] = {}
    for candidate in candidates:
        for item in candidate.subtitles:
            key = (
                str(item.get("language") or "").strip().lower(),
                str(item.get("label") or "").strip().lower(),
            )
            if key in merged:
                continue
            merged[key] = item
    ordered = sorted(
        merged.values(),
        key=lambda item: (_language_rank(item), 0 if item.get("origin") == "host" else 1, item.get("label") or ""),
    )
    return ordered if limit is None else ordered[:limit]


async def simulate_session(
    request,
    media_type: str,
    media_id: str,
    season: str | None,
    episode: str | None,
    *,
    refresh: bool = False,
) -> dict:
    """Check hosts once, stop at the first playable one and return its session.

    ``refresh`` is set by ``?retry=1`` on the gateway; it clears the positive
    session cache so the host check runs again. There is no exclude/retry path:
    once playback starts, a failure is shown as an error in the player.
    """
    season = season or "1"
    episode = episode or "1"
    cache_key = f"{CACHE_PREFIX}{media_type}:{media_id}:{season}:{episode}"
    if refresh:
        await delete_cached_extractor(cache_key)
    else:
        cached = await get_cached_extractor(cache_key)
        if cached and isinstance(cached, dict) and cached.get("candidates"):
            logger.info("[player] session cache hit %s/%s s%s e%s", media_type, media_id, season, episode)
            return dict(cached)

    started = time.monotonic()
    budget = max(1.0, float(settings.player_simulation_budget))
    configs = host_configs()
    proxy_headers = get_proxy_headers(request)
    tried: list[dict] = []
    winner: Candidate | None = None

    if configs:
        streamer = await create_streamer(build_embed_url(configs[0], media_type, media_id, season, episode))
        try:
            for config in configs:
                remaining = budget - (time.monotonic() - started)
                if remaining <= 0.5:
                    tried.append({"host": config.canonical, "reason": "budget_exhausted"})
                    continue
                try:
                    raw = await asyncio.wait_for(
                        _resolve_host(config, request, media_type, media_id, season, episode),
                        timeout=min(RESOLVE_TIMEOUT, remaining),
                    )
                except Exception as exc:
                    reason = f"{type(exc).__name__}: {exc or '-'}"
                    logger.info("[player] %s unavailable: %s", config.canonical, reason)
                    tried.append({"host": config.canonical, "reason": reason})
                    continue

                remaining = budget - (time.monotonic() - started)
                if remaining <= 0.3:
                    tried.append({"host": config.canonical, "reason": "budget_exhausted"})
                    continue
                try:
                    probe = await asyncio.wait_for(
                        probe_candidate(raw, streamer, min(PROBE_TIMEOUT, remaining)),
                        timeout=min(PROBE_TIMEOUT, remaining),
                    )
                except Exception as exc:
                    probe = ProbeResult(reason=f"{type(exc).__name__}: {exc or '-'}")
                if not probe.ok:
                    logger.info("[player] %s rejected: %s", config.canonical, probe.reason)
                    tried.append({"host": config.canonical, "reason": probe.reason or "probe_failed"})
                    continue

                winner = await _signed_candidate(
                    request, raw, proxy_headers, SESSION_TTL, config.canonical, probe
                )
                break
        finally:
            await streamer.session.close()

    candidates = [winner] if winner else []
    if winner and winner.needs_transcode and winner.transcode_job:
        _prewarm(winner.transcode_job)

    subtitles = _merge_subtitles(candidates)
    native_has_indonesian = has_indonesian_subtitle(subtitles)
    subdl_used = False
    # Every source is offered in the one player menu. SubDL is collected even
    # when the winning host already has an Indonesian track: the default stay
    # native, but the viewer may switch to any alternative manually. Custom
    # tracks are added client-side and never enter this cached session.
    if winner:
        try:
            external = await asyncio.wait_for(
                subdl.collect(
                    media_type,
                    media_id,
                    season,
                    episode,
                    signer=lambda key: _sign_subtitle_url(request, key, expiration=SESSION_TTL),
                ),
                timeout=subdl.COLLECT_TIMEOUT,
            )
            if external:
                known_ids = {str(item.get("id") or "") for item in subtitles}
                for item in external:
                    if str(item.get("id") or "") not in known_ids:
                        subtitles.append(item)
                        known_ids.add(str(item.get("id") or ""))
                subdl_used = bool(external)
        except Exception as exc:
            logger.info("[player] SubDL collection skipped: %s: %s", type(exc).__name__, exc)

    audit = {
        "hosts_tried": len(tried) + (1 if winner else 0),
        "hosts_playable": len(candidates),
        "hosts_available": len(configs),
        "chosen_video_host": winner.host if winner else None,
        "chosen_subtitle_hosts": sorted({item.get("host") for item in subtitles if item.get("host")}),
        "transcode_hosts": [winner.host] if winner and winner.needs_transcode else [],
        "tried_hosts": tried,
        "subtitle_origins": sorted({str(item.get("origin") or "") for item in subtitles if item.get("origin")}),
        "subtitle_count": len(subtitles),
        "native_indonesian_subtitle": native_has_indonesian,
        "subdl_used": subdl_used,
        "simulation_ms": int((time.monotonic() - started) * 1000),
    }
    session = session_to_dict(
        media_type=media_type,
        media_id=media_id,
        season=season,
        episode=episode,
        candidates=candidates,
        subtitles=subtitles,
        audit=audit,
        generated_at=time.time(),
    )
    if candidates:
        await set_cached_extractor(cache_key, session, ttl=settings.player_cache_ttl)
    logger.info(
        "[player] checked %s/%s: winner=%s after %s/%s hosts, %sms",
        media_type,
        media_id,
        audit["chosen_video_host"],
        audit["hosts_tried"],
        len(configs),
        audit["simulation_ms"],
    )
    return session
