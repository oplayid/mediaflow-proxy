"""Convert a signed mfp stream to one MP4 and keep upload job state.

EarnVids' ``upload/url`` endpoint expects a direct video file. The embed
adapters resolve to HLS manifests, so mfp first remuxes the signed same-origin
proxy URL into a bounded temporary MP4. The signed URL is only passed to
ffmpeg in-process; it is never stored in Redis or written to a log.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any, Coroutine

from mediaflow_proxy.configs import settings
from mediaflow_proxy.utils import redis_utils
from mediaflow_proxy.utils.log_redaction import redact_sensitive

JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
UPLOAD_DIR = Path("/tmp/mfp-earnvids-upload")
_LOCAL_JOBS: dict[str, dict[str, Any]] = {}
_LOCAL_JOB_LIMIT = 256
_TASKS: set[asyncio.Task[Any]] = set()
logger = logging.getLogger(__name__)


def new_job_id() -> str:
    import uuid

    return uuid.uuid4().hex


def valid_job_id(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text and JOB_ID_RE.match(text))


def job_dir(job_id: str) -> Path:
    if not valid_job_id(job_id):
        raise ValueError("invalid job id")
    return UPLOAD_DIR / job_id


def build_ffmpeg_command(
    source_url: str, output_path: Path, *, copy: bool = False
) -> list[str]:
    """Build a bounded MP4 command; the source URL stays out of log output.

    ``copy=True`` remuxes without re-encoding, which is far faster for a full
    film. The default stays the bounded H.264/AAC transcode used as a fallback
    when the source codecs cannot be copied into MP4.
    """
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        source_url,
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-t",
        str(max(1, int(settings.earnvids_upload_max_seconds))),
        "-fs",
        str(max(1, int(settings.earnvids_upload_max_bytes))),
    ]
    if copy:
        command.extend(
            [
                "-c",
                "copy",
                "-bsf:a",
                "aac_adtstoasc",
                "-movflags",
                "+faststart",
                "-f",
                "mp4",
                str(output_path),
            ]
        )
        return command
    command.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            str(settings.earnvids_upload_preset or "veryfast"),
            "-crf",
            str(settings.earnvids_upload_crf),
            "-vf",
            f"scale=-2:{max(2, int(settings.earnvids_upload_height))}",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            str(output_path),
        ]
    )
    return command


async def _ffmpeg_attempt(job_id: str, command: list[str], timeout: int) -> bool:
    """Run one ffmpeg attempt; log only a redacted stderr tail on failure."""
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(
            process.communicate(), timeout=max(1, int(timeout))
        )
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise RuntimeError("ffmpeg timeout") from exc

    if process.returncode == 0:
        return True
    tail = redact_sensitive(stderr.decode("utf-8", "ignore")[-800:]).strip()
    if tail:
        logger.warning("[earnvids] job=%s ffmpeg: %s", job_id, tail[-400:])
    return False


def _valid_output(output: Path) -> bool:
    try:
        return output.is_file() and output.stat().st_size >= 1024
    except OSError:
        return False


async def convert_source_to_file(job_id: str, source_url: str) -> Path:
    """Remux the signed stream into one MP4, then transcode only if needed."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg unavailable")

    directory = job_dir(job_id)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = directory / "upload.mp4"
    output.unlink(missing_ok=True)

    copied = await _ffmpeg_attempt(
        job_id,
        build_ffmpeg_command(source_url, output, copy=True),
        settings.earnvids_upload_copy_timeout,
    )
    if copied and _valid_output(output):
        return output

    # Only codec/container incompatibility triggers a fallback. A stalled
    # remux raises RuntimeError("ffmpeg timeout") and never starts a second
    # long-running process against the same stalled source.
    logger.info("[earnvids] job=%s remux failed; falling back to transcode", job_id)
    output.unlink(missing_ok=True)
    if not await _ffmpeg_attempt(
        job_id,
        build_ffmpeg_command(source_url, output),
        settings.earnvids_upload_ffmpeg_timeout,
    ):
        raise RuntimeError("ffmpeg failed")
    if not _valid_output(output):
        raise RuntimeError("empty output")
    return output


def cleanup_job(job_id: str) -> None:
    directory = job_dir(job_id)
    try:
        shutil.rmtree(directory, ignore_errors=True)
    except (OSError, ValueError):
        pass


async def store_job(job_id: str, state: dict[str, Any]) -> None:
    if not valid_job_id(job_id):
        return
    record = dict(state)
    record["updated_at"] = record.get("updated_at") or time.time()
    _LOCAL_JOBS[job_id] = record
    if len(_LOCAL_JOBS) > _LOCAL_JOB_LIMIT:
        for stale in list(_LOCAL_JOBS)[: len(_LOCAL_JOBS) - _LOCAL_JOB_LIMIT]:
            _LOCAL_JOBS.pop(stale, None)
    await redis_utils.set_earnvids_job(
        job_id, record, ttl=max(60, int(settings.earnvids_job_ttl))
    )


async def load_job(job_id: str) -> dict[str, Any]:
    if not valid_job_id(job_id):
        return {}
    state = await redis_utils.get_earnvids_job(job_id)
    if isinstance(state, dict) and state:
        return state
    return dict(_LOCAL_JOBS.get(job_id) or {})


def spawn_job(coro: Coroutine[Any, Any, None]) -> None:
    task = asyncio.create_task(coro)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
