"""Simulate playback across every known embed host and merge the best result.

The simulation intentionally stays lightweight: resolve each host, fetch the
manifest plus one segment with a tiny range, inspect codecs/subtitles, then rank
the candidates. It never downloads a full video and never exposes upstream URLs
to the browser.
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
from mediaflow_proxy.extractors.embed_hosts import EMBED_HOSTS, build_embed_url, get_config
from mediaflow_proxy.extractors.factory import ExtractorFactory
from mediaflow_proxy.gateway.ad_filter import AdFilterReport, filter_extractor_result
from mediaflow_proxy.player.models import Candidate, ProbeResult, session_to_dict
from mediaflow_proxy.player.transcoder import create_transcode_job, prepare_transcode
from mediaflow_proxy.routes.embed import _headers_for_destination, _sign_asset_url
from mediaflow_proxy.utils.http_utils import create_streamer, get_proxy_headers
from mediaflow_proxy.utils.redis_utils import get_cached_extractor, set_cached_extractor

logger = logging.getLogger(__name__)

# Hosts that historically produce a playable HLS stream. They are tried first
# so the 10 second budget is spent on the most likely winners.
PREFERRED_HOSTS = ("VSEmbed", "VidCore", "VidLink", "VidSrcTo")
# vidsrc.to chains through vidsrc.sh before reaching the VSEmbed player, so
# resolution alone can take ~6s. Keep room for PROBE_TIMEOUT within the 10s
# simulation budget (7.5 + 2.5).
RESOLVE_TIMEOUT = 7.5
PROBE_TIMEOUT = 2.5
PROBE_CONCURRENCY = 4
CACHE_PREFIX = "mfp:player-session:"
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
    """Start the winner's transcode without delaying the 10s simulation."""
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
    """Return configured hosts, preferred ones first.

    ``PLAYER_HOSTS`` can override the list with a comma-separated set of
    canonical names. Adding a new host is therefore a one-line registry change
    in ``embed_hosts.py`` plus an optional env override — no player changes.
    """
    raw = (settings.player_hosts or "").strip()
    if raw:
        names = [item.strip() for item in re.split(r"[,\s]+", raw) if item.strip()]
        configs = [get_config(name) for name in names]
    else:
        ordered = list(PREFERRED_HOSTS) + [name for name in EMBED_HOSTS if name not in PREFERRED_HOSTS]
        configs = [EMBED_HOSTS.get(name) for name in ordered]
    return [config for config in configs if config]


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
    """Fetch the manifest, one segment and a few subtitle headers."""
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
                },
                audio_copy=_audio_can_be_copied(probe),
            )
        except Exception as exc:
            # A failed job registration must not discard an otherwise playable
            # candidate: the player can still try native playback/fallback.
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
        manifest_type=probe.manifest_type,
        sources=sources,
        subtitles=subtitles,
        score=probe.score,
        probe=probe,
        needs_transcode=needs_transcode,
        transcode_url=transcode_url,
        transcode_job=transcode_job,
        playback_type="hls" if needs_transcode else probe.manifest_type,
    )


def _merge_subtitles(candidates: list[Candidate], limit: int = 16) -> list[dict]:
    """Merge native tracks from every host, preferring Indonesian/English."""
    merged: dict[tuple[str, str], dict] = {}
    for candidate in candidates:
        for item in candidate.subtitles:
            # Same language/label from two hosts is one user-visible track. Keep
            # the first (highest-scored) candidate's signed URL but preserve its
            # host/origin metadata for the audit trail.
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
    return ordered[:limit]


async def simulate_session(
    request,
    media_type: str,
    media_id: str,
    season: str | None,
    episode: str | None,
    *,
    retry: bool = False,
) -> dict:
    """Run the bounded simulation and return a browser-safe session dict."""
    season = season or "1"
    episode = episode or "1"
    cache_key = f"{CACHE_PREFIX}{media_type}:{media_id}:{season}:{episode}"
    if not retry:
        cached = await get_cached_extractor(cache_key)
        if cached and isinstance(cached, dict) and cached.get("candidates"):
            logger.info("[player] session cache hit %s/%s s%s e%s", media_type, media_id, season, episode)
            return cached

    started = time.monotonic()
    budget = max(1.0, float(settings.player_simulation_budget))
    configs = host_configs()
    proxy_headers = get_proxy_headers(request)
    streamer = await create_streamer(build_embed_url(configs[0], media_type, media_id, season, episode))
    semaphore = asyncio.Semaphore(PROBE_CONCURRENCY)

    async def worker(config) -> Candidate | None:
        async with semaphore:
            remaining = budget - (time.monotonic() - started)
            if remaining <= 0.5:
                return None
            try:
                raw = await asyncio.wait_for(
                    _resolve_host(config, request, media_type, media_id, season, episode),
                    timeout=min(RESOLVE_TIMEOUT, remaining),
                )
                remaining = budget - (time.monotonic() - started)
                if remaining <= 0.3:
                    return None
                probe = await asyncio.wait_for(
                    probe_candidate(raw, streamer, min(PROBE_TIMEOUT, remaining)),
                    timeout=min(PROBE_TIMEOUT, remaining),
                )
                if not probe.ok:
                    logger.info("[player] %s rejected: %s", config.canonical, probe.reason)
                    return None
                return await _signed_candidate(request, raw, proxy_headers, SESSION_TTL, config.canonical, probe)
            except Exception as exc:
                logger.info(
                    "[player] %s unavailable: %s: %s",
                    config.canonical,
                    type(exc).__name__,
                    exc or "-",
                )
                return None

    try:
        tasks = [asyncio.create_task(worker(config)) for config in configs]
        # Leave a small margin so the measured simulation stays under the hard
        # 10 second budget even after task cancellation and serialization.
        wait_timeout = max(1.0, budget - 0.15)
        done, pending = await asyncio.wait(tasks, timeout=wait_timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        candidates: list[Candidate] = []
        for task in done:
            if task.cancelled() or task.exception():
                continue
            candidate = task.result()
            if candidate:
                candidates.append(candidate)
        candidates.sort(key=lambda item: item.score, reverse=True)
    finally:
        await streamer.session.close()

    winner = candidates[0] if candidates else None
    if winner and winner.needs_transcode and winner.transcode_job:
        _prewarm(winner.transcode_job)

    if not candidates:
        # Keep the contract stable: the player renders a clear error instead of
        # receiving an exception or an upstream/provider URL.
        candidates = []

    subtitles = _merge_subtitles(candidates)
    audit = {
        "hosts_tried": len(configs),
        "hosts_playable": len(candidates),
        "chosen_video_host": candidates[0].host if candidates else None,
        "chosen_subtitle_hosts": sorted({item.get("host") for item in subtitles if item.get("host")}),
        "transcode_hosts": sorted(item.host for item in candidates if item.needs_transcode),
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
        "[player] simulated %s/%s: %s/%s hosts playable, winner=%s (%sms)",
        media_type,
        media_id,
        len(candidates),
        len(configs),
        audit["chosen_video_host"],
        audit["simulation_ms"],
    )
    return session
