"""Lazy HEVC -> H.264 transcoding for the smart player.

The browser only ever sees an opaque, tokenized HLS URL. The source URL (with
all signed headers) stays server-side in the job registry, and FFmpeg reads it
from inside this container. Jobs are shared through Redis so the four Gunicorn
workers do not start duplicate transcodes.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

from mediaflow_proxy.configs import settings
from mediaflow_proxy.utils.crypto_utils import EncryptionHandler
from mediaflow_proxy.utils.http_utils import get_original_scheme
from mediaflow_proxy.utils.redis_utils import (
    acquire_lock,
    get_cached_extractor,
    release_lock,
    set_cached_extractor,
)

logger = logging.getLogger(__name__)

JOB_PREFIX = "mfp:transcode:"
OUTPUT_DIR = Path("/tmp/mfp-transcode")
_SAFE_FILE = re.compile(r"^(master\.m3u8|seg_\d+\.ts|init\.mp4)$")


@dataclass
class _RunningJob:
    job: str
    process: asyncio.subprocess.Process
    started_at: float
    last_seen: float
    log_path: Path
    log_file: BinaryIO


_RUNNING: dict[str, _RunningJob] = {}
_CLEANUP_TASKS: set[asyncio.Task] = set()
# Fallback for single-worker installs without Redis. Redis remains the source
# of truth when configured so Gunicorn workers share the same job registry.
_JOBS: dict[str, dict[str, Any]] = {}
_JOB_TTL = 3600


def _job_key(job: str) -> str:
    return f"{JOB_PREFIX}{job}"


def _job_dir(job: str) -> Path:
    return OUTPUT_DIR / job


def build_ffmpeg_command(source_url: str, job_dir: Path, *, audio_copy: bool = True) -> list[str]:
    """Build a bounded H.264 HLS command.

    Audio is copied when the source is already AAC, otherwise re-encoded. Video
    is capped at the configured height so a 4K HEVC source cannot saturate CPU.
    """
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        source_url,
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-preset",
        settings.player_transcode_preset,
        "-crf",
        str(settings.player_transcode_crf),
        "-vf",
        f"scale=-2:{settings.player_transcode_height}",
        "-pix_fmt",
        "yuv420p",
    ]
    if audio_copy:
        command += ["-c:a", "copy"]
    else:
        command += ["-c:a", "aac", "-b:a", "128k", "-ac", "2"]
    command += [
        "-f",
        "hls",
        "-hls_time",
        str(settings.player_transcode_hls_time),
        # VOD: keep every segment so hls.js starts at the beginning instead of
        # the live edge. Idle jobs are removed by the TTL cleanup below.
        "-hls_list_size",
        str(settings.player_transcode_hls_list_size),
        "-hls_segment_filename",
        str(job_dir / "seg_%06d.ts"),
        str(job_dir / "master.m3u8"),
    ]
    return command


def sign_transcode_url(request: Request, job: str, *, expiration: int = 3600) -> str:
    """Return the opaque browser-facing URL for a transcode job."""
    scheme = get_original_scheme(request)
    authority = request.headers.get("host") or request.url.netloc
    path = f"/player/transcode/{job}/master.m3u8"
    handler = EncryptionHandler(settings.api_password) if settings.api_password else None
    if handler:
        token = handler.encrypt_data({"job": job}, expiration=expiration)
        return f"{scheme}://{authority}/_token_{token}{path}"
    return f"{scheme}://{authority}{path}?job={job}"


async def create_transcode_job(
    request: Request,
    source_url: str,
    host: str,
    media: dict[str, Any],
    *,
    audio_copy: bool = True,
    expiration: int = 3600,
) -> tuple[str, str]:
    """Create a job and return ``(job_id, browser_url)``."""
    job = secrets.token_urlsafe(18)
    payload = {
        "job": job,
        "source_url": source_url,
        "host": host,
        "media": media,
        "audio_copy": bool(audio_copy),
        "created_at": time.time(),
        "last_seen": time.time(),
    }
    _JOBS[job] = payload
    await set_cached_extractor(_job_key(job), payload, ttl=expiration)
    return job, sign_transcode_url(request, job, expiration=expiration)


async def get_job(job: str) -> dict[str, Any] | None:
    payload = await get_cached_extractor(_job_key(job))
    if isinstance(payload, dict):
        _JOBS[job] = payload
        return payload
    cached = _JOBS.get(job)
    if cached and time.time() - float(cached.get("created_at", 0)) > _JOB_TTL:
        _JOBS.pop(job, None)
        return None
    return cached


async def touch_job(job: str) -> None:
    payload = await get_job(job)
    if payload:
        payload["last_seen"] = time.time()
        _JOBS[job] = payload
        await set_cached_extractor(_job_key(job), payload, ttl=_JOB_TTL)


def _cleanup_idle() -> None:
    now = time.monotonic()
    idle = max(60, int(settings.player_transcode_idle_ttl))
    for job, running in list(_RUNNING.items()):
        if now - running.last_seen > idle or running.process.returncode is not None:
            task = asyncio.create_task(_stop_job(job))
            _CLEANUP_TASKS.add(task)
            task.add_done_callback(_CLEANUP_TASKS.discard)


async def _stop_job(job: str) -> None:
    running = _RUNNING.pop(job, None)
    if not running:
        await release_lock(_job_key(job))
        return

    logger.info("[transcode] job=%s stopped", job)
    if running.process.returncode is None:
        try:
            running.process.terminate()
            await asyncio.wait_for(running.process.wait(), timeout=5)
        except (ProcessLookupError, asyncio.TimeoutError):
            try:
                running.process.kill()
            except ProcessLookupError:
                pass
    await release_lock(_job_key(job))
    try:
        running.log_file.close()
    except Exception:
        pass
    shutil.rmtree(running.log_path.parent, ignore_errors=True)


async def ensure_started(job: str, payload: dict[str, Any]) -> None:
    """Start FFmpeg once per job, coordinating across workers with Redis."""
    if shutil.which("ffmpeg") is None:
        raise HTTPException(status_code=500, detail="Transcoder unavailable (ffmpeg missing)")
    _cleanup_idle()
    running = _RUNNING.get(job)
    if running and running.process.returncode is None:
        running.last_seen = time.monotonic()
        return

    if len(_RUNNING) >= max(1, int(settings.player_transcode_max_jobs)):
        oldest = min(_RUNNING, key=lambda key: _RUNNING[key].last_seen)
        await _stop_job(oldest)

    # The lock is held for the lifetime of the process, so other workers only
    # serve the shared /tmp output instead of starting a duplicate FFmpeg.
    acquired = await acquire_lock(_job_key(job), ttl=600, timeout=2)
    if not acquired:
        return

    job_dir = _job_dir(job)
    job_dir.mkdir(parents=True, exist_ok=True)
    log_path = job_dir / "ffmpeg.log"
    try:
        command = build_ffmpeg_command(
            payload["source_url"],
            job_dir,
            audio_copy=bool(payload.get("audio_copy", True)),
        )
        log_file = log_path.open("ab")
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=log_file,
        )
        _RUNNING[job] = _RunningJob(
            job, process, time.monotonic(), time.monotonic(), log_path, log_file
        )
        logger.info("[transcode] job=%s host=%s hevc->h264 started", job, payload.get("host"))
    except Exception as exc:
        logger.error("[transcode] job=%s failed to start: %s: %s", job, type(exc).__name__, exc)
        await release_lock(_job_key(job))
        raise HTTPException(status_code=500, detail="Transcode failed to start")


async def wait_ready(job: str, timeout: int | None = None) -> bool:
    """Wait until the HLS playlist and its first segment are usable."""
    limit = time.monotonic() + int(timeout or settings.player_transcode_startup_timeout)
    playlist = _job_dir(job) / "master.m3u8"
    while time.monotonic() < limit:
        running = _RUNNING.get(job)
        if running and running.process.returncode not in (None, 0):
            return False
        if playlist.exists() and playlist.stat().st_size > 0:
            if any(
                path.exists() and path.stat().st_size > 0
                for path in _job_dir(job).glob("seg_*.ts")
            ):
                return True
        await asyncio.sleep(0.25)
    return False


async def prepare_transcode(job: str) -> bool:
    """Prewarm a job in the background; returns whether it became ready."""
    payload = await get_job(job)
    if not payload:
        return False
    await ensure_started(job, payload)
    return await wait_ready(job)


async def serve_transcode_file(request: Request, job: str, filename: str) -> FileResponse:
    """Serve a whitelisted HLS file for an active job."""
    token_job = request.query_params.get("job")
    if token_job and str(token_job) != job:
        raise HTTPException(status_code=400, detail="Transcode token mismatch")
    if not _SAFE_FILE.match(filename):
        raise HTTPException(status_code=400, detail="Invalid transcode file")

    payload = await get_job(job)
    if not payload:
        raise HTTPException(status_code=404, detail="Transcode job expired")

    await ensure_started(job, payload)
    path = _job_dir(job) / filename
    if not path.exists():
        if not await wait_ready(job):
            raise HTTPException(status_code=503, detail="Transcode is still starting")
    await touch_job(job)

    media_type = "application/vnd.apple.mpegurl" if filename.endswith(".m3u8") else "video/mp2t"
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})
