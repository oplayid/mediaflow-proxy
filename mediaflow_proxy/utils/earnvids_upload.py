"""Convert a signed mfp stream to one MP4 and keep upload job state.

EarnVids' ``upload/url`` endpoint expects a direct video file. The embed
adapters resolve to HLS manifests, so mfp first downloads those segments in
parallel and remuxes them into a bounded temporary MP4. AES-128 playlists keep
their key local so ffmpeg can decrypt from disk. The signed URL is only passed
to ffmpeg in-process; it is never stored in Redis or written to a log.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any, Coroutine
from urllib.parse import urljoin

from aiohttp import ClientTimeout

from mediaflow_proxy.configs import settings
from mediaflow_proxy.utils import redis_utils
from mediaflow_proxy.utils.http_utils import create_aiohttp_session
from mediaflow_proxy.utils.log_redaction import redact_sensitive

JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
HLS_MAP_RE = re.compile(r'^#EXT-X-MAP:URI="([^"]+)"', re.MULTILINE)
HLS_KEY_METHOD_RE = re.compile(r"METHOD=([A-Za-z0-9-]+)")
HLS_KEY_URI_RE = re.compile(r'URI="([^"]+)"')
HLS_VARIANT_TAG = "#EXT-X-STREAM-INF"
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
    source_url: str, output_path: Path, *, copy: bool = False, local_playlist: bool = False
) -> list[str]:
    """Build a bounded MP4 command; the source URL stays out of log output.

    ``copy=True`` remuxes without re-encoding, which is far faster for a full
    film. The default stays the bounded H.264/AAC transcode used as a fallback
    when the source codecs cannot be copied into MP4.

    ``local_playlist=True`` enables ffmpeg's local HLS key handling for the
    rewritten playlist produced by :func:`download_hls_source`.
    """
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
    ]
    if local_playlist:
        command.extend(
            [
                "-allowed_extensions",
                "ALL",
                "-protocol_whitelist",
                "file,crypto,data,tcp,tls,http,https",
            ]
        )
    command.extend(
        [
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
    )
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


async def _fetch_manifest(
    session: Any, proxy_url: str | None, url: str, timeout: ClientTimeout
) -> str | None:
    """Fetch a small playlist without ever logging its signed URL."""
    try:
        async with session.get(url, proxy=proxy_url, timeout=timeout) as response:
            if response.status != 200:
                logger.info("[earnvids] manifest HTTP %s", response.status)
                return None
            return await response.text(errors="ignore")
    except Exception as exc:
        logger.info("[earnvids] manifest fetch failed: %s", type(exc).__name__)
        return None


async def _fetch_key(
    job_id: str,
    session: Any,
    proxy_url: str | None,
    url: str,
    timeout: ClientTimeout,
) -> bytes | None:
    """Fetch one AES-128 key without ever logging its signed URL."""
    try:
        async with session.get(url, proxy=proxy_url, timeout=timeout) as response:
            if response.status != 200:
                logger.info("[earnvids] job=%s stage=key http=%s", job_id, response.status)
                return None
            payload = await response.read()
            logger.info("[earnvids] job=%s stage=key bytes=%d", job_id, len(payload))
            return payload
    except Exception as exc:
        logger.info(
            "[earnvids] job=%s stage=key error=%s", job_id, type(exc).__name__
        )
        return None


def _rewrite_encrypted_playlist(body: str, key_files: dict[str, str]) -> str:
    """Point a keyed playlist at local segment and key files."""
    lines: list[str] = []
    segment = 0
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#EXT-X-KEY:"):
            match = HLS_KEY_URI_RE.search(line)
            if match:
                local_name = key_files.get(match.group(1))
                if local_name:
                    line = line.replace(
                        f'URI="{match.group(1)}"', f'URI="{local_name}"'
                    )
        elif stripped and not stripped.startswith("#"):
            line = f"segment-{segment:06d}.ts"
            segment += 1
        lines.append(line)
    return "\n".join(lines) + "\n"


def _playlist_segments(body: str, base_url: str) -> list[str]:
    entries = [
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    init = HLS_MAP_RE.search(body)
    if init:
        entries.insert(0, init.group(1))
    return [urljoin(base_url, entry) for entry in entries if entry]


async def download_hls_source(job_id: str, source_url: str) -> Path | None:
    """Download an HLS source in parallel and return a local ffmpeg input.

    Plain HLS is assembled into ``input.ts``. AES-128 HLS is downloaded as
    ciphertext segments with a local key file and a rewritten ``local.m3u8`` so
    ffmpeg can decrypt at disk speed without touching the signed origin URLs
    again. Other key methods, byte ranges and init segments fall back to the
    existing ffmpeg path. Only counts and byte totals are logged.
    """
    timeout = ClientTimeout(
        connect=10,
        sock_read=max(60, int(settings.earnvids_stall_timeout)),
        total=None,
    )
    async with create_aiohttp_session(source_url, timeout=timeout) as (session, proxy_url):
        body = await _fetch_manifest(session, proxy_url, source_url, timeout)
        if not body or not body.lstrip().startswith("#EXTM3U"):
            return None
        logger.info(
            "[earnvids] job=%s stage=manifest lines=%d", job_id, len(body.splitlines())
        )

        manifest_url = source_url
        if HLS_VARIANT_TAG in body:
            variants = _playlist_segments(body, source_url)
            if not variants:
                return None
            manifest_url = variants[0]
            body = await _fetch_manifest(session, proxy_url, manifest_url, timeout)
            if not body:
                return None
            logger.info("[earnvids] job=%s stage=variant", job_id)

        segments = _playlist_segments(body, manifest_url)
        if len(segments) <= 1:
            return None

        key_lines = [
            line.strip()
            for line in body.splitlines()
            if line.strip().startswith("#EXT-X-KEY:")
        ]
        if key_lines and (HLS_MAP_RE.search(body) or "#EXT-X-BYTERANGE" in body):
            logger.info("[earnvids] job=%s keyed HLS uses ffmpeg path", job_id)
            return None

        directory = job_dir(job_id)
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)

        key_files: dict[str, str] = {}
        for line in key_lines:
            method_match = HLS_KEY_METHOD_RE.search(line)
            method = method_match.group(1).upper() if method_match else "AES-128"
            if method == "NONE":
                continue
            if method != "AES-128":
                logger.info("[earnvids] job=%s HLS key method uses ffmpeg path", job_id)
                shutil.rmtree(directory, ignore_errors=True)
                return None
            uri_match = HLS_KEY_URI_RE.search(line)
            if not uri_match:
                logger.info("[earnvids] job=%s HLS key URI missing", job_id)
                shutil.rmtree(directory, ignore_errors=True)
                return None

            uri = uri_match.group(1)
            if uri in key_files:
                continue
            payload = await _fetch_key(
                job_id, session, proxy_url, urljoin(manifest_url, uri), timeout
            )
            if payload is None or len(payload) != 16:
                logger.info(
                    "[earnvids] job=%s HLS key unusable bytes=%s",
                    job_id,
                    len(payload) if payload is not None else 0,
                )
                shutil.rmtree(directory, ignore_errors=True)
                return None

            local_name = f"key-{len(key_files):06d}.bin"
            key_path = directory / local_name
            key_path.write_bytes(payload)
            key_path.chmod(0o600)
            key_files[uri] = local_name

        concurrency = max(1, min(32, int(settings.earnvids_download_concurrency)))
        semaphore = asyncio.Semaphore(concurrency)
        logger.info(
            "[earnvids] job=%s stage=segment count=%d concurrency=%d",
            job_id,
            len(segments),
            concurrency,
        )

        async def grab(index: int, url: str) -> int:
            async with semaphore:
                target = directory / f"segment-{index:06d}.ts"
                for attempt in range(3):
                    try:
                        async with session.get(url, proxy=proxy_url, timeout=timeout) as response:
                            if response.status != 200:
                                logger.info(
                                    "[earnvids] job=%s segment=%d attempt=%d http=%d",
                                    job_id,
                                    index,
                                    attempt + 1,
                                    response.status,
                                )
                                raise RuntimeError(f"segment HTTP {response.status}")
                            expected = int(response.headers.get("Content-Length") or 0)
                            written = 0
                            with target.open("wb") as handle:
                                async for chunk in response.content.iter_chunked(1 << 20):
                                    handle.write(chunk)
                                    written += len(chunk)
                        if written == 0 or (expected and written != expected):
                            logger.info(
                                "[earnvids] job=%s segment=%d attempt=%d short expected=%d written=%d",
                                job_id,
                                index,
                                attempt + 1,
                                expected,
                                written,
                            )
                            raise RuntimeError("short segment")
                        return written
                    except Exception as exc:
                        target.unlink(missing_ok=True)
                        logger.info(
                            "[earnvids] job=%s segment=%d attempt=%d error=%s",
                            job_id,
                            index,
                            attempt + 1,
                            type(exc).__name__,
                        )
                        if attempt == 2:
                            raise
                        await asyncio.sleep(1 + attempt)
                return 0

        try:
            results = await asyncio.gather(
                *(grab(index, url) for index, url in enumerate(segments))
            )
        except Exception:
            for index in range(len(segments)):
                (directory / f"segment-{index:06d}.ts").unlink(missing_ok=True)
            raise

        total = sum(results)
        if key_files:
            playlist_path = directory / "local.m3u8"
            playlist_path.write_text(
                _rewrite_encrypted_playlist(body, key_files), encoding="utf-8"
            )
            logger.info(
                "[earnvids] job=%s downloaded %d segments (%d bytes) concurrency=%d keys=%d",
                job_id,
                len(segments),
                total,
                concurrency,
                len(key_files),
            )
            return playlist_path

        combined = directory / "input.ts"
        with combined.open("wb") as output:
            for index in range(len(segments)):
                target = directory / f"segment-{index:06d}.ts"
                with target.open("rb") as handle:
                    shutil.copyfileobj(handle, output)
                target.unlink(missing_ok=True)

        logger.info(
            "[earnvids] job=%s downloaded %d segments (%d bytes) concurrency=%d",
            job_id,
            len(segments),
            total,
            concurrency,
        )
        return combined


async def convert_source_to_file(job_id: str, source_url: str) -> Path:
    """Remux the signed stream into one MP4, then transcode only if needed."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg unavailable")

    logger.info("[earnvids] job=%s stage=download", job_id)
    prepared = await download_hls_source(job_id, source_url)
    local_playlist = bool(prepared and prepared.suffix.lower() == ".m3u8")
    logger.info(
        "[earnvids] job=%s stage=remux local_playlist=%s",
        job_id,
        str(local_playlist).lower(),
    )
    if prepared is not None:
        source_url = str(prepared)

    directory = job_dir(job_id)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = directory / "upload.mp4"
    output.unlink(missing_ok=True)

    copied = await _ffmpeg_attempt(
        job_id,
        build_ffmpeg_command(source_url, output, copy=True, local_playlist=local_playlist),
        settings.earnvids_upload_copy_timeout,
    )
    if copied and _valid_output(output):
        return output

    # Only codec/container incompatibility triggers a fallback. A stalled
    # remux raises RuntimeError("ffmpeg timeout") and never starts a second
    # long-running process against the same stalled source.
    logger.info("[earnvids] job=%s remux failed; falling back to transcode", job_id)
    logger.info("[earnvids] job=%s stage=transcode", job_id)
    output.unlink(missing_ok=True)
    if not await _ffmpeg_attempt(
        job_id,
        build_ffmpeg_command(source_url, output, local_playlist=local_playlist),
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
