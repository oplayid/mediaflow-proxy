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
from typing import Any, Awaitable, Callable, Coroutine
from urllib.parse import urljoin, urlsplit

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

_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_RETRY_DELAYS = (2.0,)
_BATCH_MIN = 16
_BATCH_MAX = 32


class SourceRefreshNeeded(RuntimeError):
    """Raised when segments stay stale after the source has been refreshed."""


class HlsKeyFetchError(RuntimeError):
    """An AES-128 key could not be fetched, so no local playlist can be built."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HLS key HTTP {status}")
        self.status = status


class _RetryableStatus(Exception):
    """One segment returned a gateway/rate-limit status after all retries."""

    def __init__(self, status: int) -> None:
        super().__init__(f"segment HTTP {status}")
        self.status = status


def _segment_path(directory: Path, index: int) -> Path:
    return directory / f"segment-{index:06d}.ts"


def _segment_ready(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size >= 1024
    except OSError:
        return False


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
    referer: str = "",
) -> bytes:
    """Fetch one AES-128 key, retrying with playlist referer headers."""
    origin = ""
    if referer:
        parts = urlsplit(referer)
        origin = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""

    last_status = 0
    for attempt in range(2):
        headers = None
        if attempt == 1 and (referer or origin):
            headers = {}
            if referer:
                headers["referer"] = referer
            if origin:
                headers["origin"] = origin
        try:
            async with session.get(
                url, proxy=proxy_url, timeout=timeout, headers=headers
            ) as response:
                if response.status != 200:
                    last_status = response.status
                    logger.info(
                        "[earnvids] job=%s stage=key http=%s attempt=%d",
                        job_id,
                        response.status,
                        attempt + 1,
                    )
                else:
                    payload = await response.read()
                    logger.info("[earnvids] job=%s stage=key bytes=%d", job_id, len(payload))
                    return payload
        except Exception as exc:
            logger.info(
                "[earnvids] job=%s stage=key error=%s attempt=%d",
                job_id,
                type(exc).__name__,
                attempt + 1,
            )
        if attempt == 0:
            await asyncio.sleep(0.5)

    raise HlsKeyFetchError(last_status or 0)


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


async def _load_playlist(
    session: Any,
    proxy_url: str | None,
    source_url: str,
    timeout: ClientTimeout,
    job_id: str,
    *,
    log_stage: bool = True,
) -> tuple[str, str, list[str]] | None:
    """Fetch a playlist and return its body, base URL and segment URLs."""
    body = await _fetch_manifest(session, proxy_url, source_url, timeout)
    if not body or not body.lstrip().startswith("#EXTM3U"):
        return None
    if log_stage:
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
        if log_stage:
            logger.info("[earnvids] job=%s stage=variant", job_id)

    segments = _playlist_segments(body, manifest_url)
    if len(segments) <= 1:
        return None
    return body, manifest_url, segments


async def _prepare_keys(
    job_id: str,
    session: Any,
    proxy_url: str | None,
    manifest_url: str,
    body: str,
    timeout: ClientTimeout,
    directory: Path,
) -> dict[str, str] | None:
    """Write AES-128 keys locally. ``None`` means fall back to ffmpeg."""
    key_lines = [
        line.strip()
        for line in body.splitlines()
        if line.strip().startswith("#EXT-X-KEY:")
    ]
    if key_lines and (HLS_MAP_RE.search(body) or "#EXT-X-BYTERANGE" in body):
        logger.info("[earnvids] job=%s keyed HLS uses ffmpeg path", job_id)
        return None

    key_files: dict[str, str] = {}
    for line in key_lines:
        method_match = HLS_KEY_METHOD_RE.search(line)
        method = method_match.group(1).upper() if method_match else "AES-128"
        if method == "NONE":
            continue
        if method != "AES-128":
            logger.info("[earnvids] job=%s HLS key method uses ffmpeg path", job_id)
            return None
        uri_match = HLS_KEY_URI_RE.search(line)
        if not uri_match:
            logger.info("[earnvids] job=%s HLS key URI missing", job_id)
            return None

        uri = uri_match.group(1)
        if uri in key_files:
            continue
        payload = await _fetch_key(
            job_id,
            session,
            proxy_url,
            urljoin(manifest_url, uri),
            timeout,
            referer=manifest_url,
        )
        if len(payload) != 16:
            logger.info(
                "[earnvids] job=%s HLS key unusable bytes=%d", job_id, len(payload)
            )
            raise RuntimeError("HLS key unusable")

        local_name = f"key-{len(key_files):06d}.bin"
        key_path = directory / local_name
        key_path.write_bytes(payload)
        key_path.chmod(0o600)
        key_files[uri] = local_name
    return key_files


async def download_hls_source(
    job_id: str,
    source_url: str,
    *,
    refresh_source: Callable[[], Awaitable[str]] | None = None,
    max_refreshes: int = 3,
    request_headers: dict[str, str] | None = None,
) -> Path | None:
    """Download HLS in parallel, keeping completed segments across refreshes.

    A signed upstream URL can expire while a long film is downloading. When
    segments repeatedly return a gateway/rate-limit status, ``refresh_source``
    is called to resolve a fresh signed manifest and downloading resumes from
    the first missing segment instead of starting over.
    """
    timeout = ClientTimeout(
        connect=10,
        sock_read=max(60, int(settings.earnvids_stall_timeout)),
        total=None,
    )
    directory = job_dir(job_id)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    async with create_aiohttp_session(
        source_url, timeout=timeout, headers=request_headers or None
    ) as (session, proxy_url):
        loaded = await _load_playlist(session, proxy_url, source_url, timeout, job_id)
        if loaded is None:
            return None
        body, manifest_url, segments = loaded

        key_files = await _prepare_keys(
            job_id, session, proxy_url, manifest_url, body, timeout, directory
        )
        if key_files is None:
            return None

        primary = max(1, min(32, int(settings.earnvids_download_concurrency)))
        expected_count = len(segments)
        refresh_count = 0
        completed: set[int] = set()

        while True:
            pending = [
                index
                for index in range(len(segments))
                if index not in completed and not _segment_ready(_segment_path(directory, index))
            ]
            if not pending:
                break

            concurrency = primary if refresh_count == 0 else max(1, min(primary, 4))
            batch_size = min(_BATCH_MAX, max(_BATCH_MIN, concurrency * 2))
            batch = pending[:batch_size]
            semaphore = asyncio.Semaphore(concurrency)
            logger.info(
                "[earnvids] job=%s stage=segment count=%d pending=%d batch=%d concurrency=%d refresh=%d",
                job_id,
                len(segments),
                len(pending),
                len(batch),
                concurrency,
                refresh_count,
            )

            async def grab(index: int, url: str) -> int:
                async with semaphore:
                    target = _segment_path(directory, index)
                    for attempt in range(2):
                        written = 0
                        expected = 0
                        try:
                            status = 0
                            async with session.get(url, proxy=proxy_url, timeout=timeout) as response:
                                status = response.status
                                if status == 200:
                                    expected = int(response.headers.get("Content-Length") or 0)
                                    with target.open("wb") as handle:
                                        async for chunk in response.content.iter_chunked(1 << 20):
                                            handle.write(chunk)
                                            written += len(chunk)
                                elif status not in _RETRYABLE_STATUSES:
                                    logger.info(
                                        "[earnvids] job=%s segment=%d attempt=%d http=%d",
                                        job_id,
                                        index,
                                        attempt + 1,
                                        status,
                                    )
                                    raise RuntimeError(f"segment HTTP {status}")

                            if status in _RETRYABLE_STATUSES:
                                logger.info(
                                    "[earnvids] job=%s segment=%d attempt=%d http=%d retry=true",
                                    job_id,
                                    index,
                                    attempt + 1,
                                    status,
                                )
                                if attempt == 1:
                                    raise _RetryableStatus(status)
                                await asyncio.sleep(_RETRY_DELAYS[0])
                                continue

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
                        except _RetryableStatus:
                            raise
                        except Exception as exc:
                            target.unlink(missing_ok=True)
                            logger.info(
                                "[earnvids] job=%s segment=%d attempt=%d error=%s",
                                job_id,
                                index,
                                attempt + 1,
                                type(exc).__name__,
                            )
                            if attempt == 1:
                                raise
                            await asyncio.sleep(_RETRY_DELAYS[0])
                    return 0

            results = await asyncio.gather(
                *(grab(index, segments[index]) for index in batch),
                return_exceptions=True,
            )
            for index, value in zip(batch, results, strict=True):
                if not isinstance(value, BaseException) and value > 0:
                    completed.add(index)
            errors = [value for value in results if isinstance(value, BaseException)]
            retry_errors = [
                error for error in errors if isinstance(error, _RetryableStatus)
            ]

            fatal = next(
                (error for error in errors if not isinstance(error, _RetryableStatus)),
                None,
            )
            if fatal is not None:
                raise fatal

            if retry_errors:
                if refresh_source and refresh_count < max_refreshes:
                    refresh_count += 1
                    logger.info("[earnvids] job=%s source refresh=%d", job_id, refresh_count)
                    source_url = await refresh_source()
                    loaded = await _load_playlist(
                        session,
                        proxy_url,
                        source_url,
                        timeout,
                        job_id,
                        log_stage=False,
                    )
                    if loaded is None:
                        return None
                    body, manifest_url, segments = loaded
                    refreshed_keys = await _prepare_keys(
                        job_id, session, proxy_url, manifest_url, body, timeout, directory
                    )
                    if refreshed_keys is None:
                        return None
                    key_files = refreshed_keys
                    if len(segments) != expected_count:
                        for path in directory.glob("segment-*.ts"):
                            path.unlink(missing_ok=True)
                        completed.clear()
                        expected_count = len(segments)
                        logger.info(
                            "[earnvids] job=%s manifest changed; restarting download", job_id
                        )
                    continue

                status = retry_errors[0].status
                raise SourceRefreshNeeded(
                    f"segment HTTP {status} after {refresh_count} refreshes"
                )

        total = sum(_segment_path(directory, index).stat().st_size for index in range(len(segments)))
        if key_files:
            playlist_path = directory / "local.m3u8"
            playlist_path.write_text(
                _rewrite_encrypted_playlist(body, key_files), encoding="utf-8"
            )
            logger.info(
                "[earnvids] job=%s downloaded %d segments (%d bytes) concurrency=%d keys=%d refresh=%d",
                job_id,
                len(segments),
                total,
                concurrency,
                len(key_files),
                refresh_count,
            )
            return playlist_path

        combined = directory / "input.ts"
        with combined.open("wb") as output:
            for index in range(len(segments)):
                target = _segment_path(directory, index)
                with target.open("rb") as handle:
                    shutil.copyfileobj(handle, output)
                target.unlink(missing_ok=True)

        logger.info(
            "[earnvids] job=%s downloaded %d segments (%d bytes) concurrency=%d refresh=%d",
            job_id,
            len(segments),
            total,
            concurrency,
            refresh_count,
        )
        return combined


async def convert_source_to_file(
    job_id: str,
    source_url: str,
    *,
    refresh_source: Callable[[], Awaitable[str]] | None = None,
    request_headers: dict[str, str] | None = None,
) -> Path:
    """Remux the signed stream into one MP4, then transcode only if needed."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg unavailable")

    logger.info("[earnvids] job=%s stage=download", job_id)
    prepared = await download_hls_source(
        job_id,
        source_url,
        refresh_source=refresh_source,
        request_headers=request_headers,
    )
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
