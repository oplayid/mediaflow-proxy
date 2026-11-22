"""Config-driven extractors for StreamFlix-style embed hosts."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from mediaflow_proxy.extractors.base import BaseExtractor, ExtractorError
from mediaflow_proxy.extractors.embed_hosts import (
    FLOW_VIDSPARK,
    FLOW_VSEMBED,
    EmbedHostConfig,
    is_public_http_url,
    parse_media_path,
)
from mediaflow_proxy.gateway.ad_filter import (
    AdFilterReport,
    filter_candidate_url,
    filter_extractor_result,
    sanitize_html,
)
from mediaflow_proxy.utils.packed import eval_solver

logger = logging.getLogger(__name__)

_CFG_RE = re.compile(r"window\.CFG\s*=\s*(\{.*?\})\s*;", re.DOTALL)
_CONFIG_RE = re.compile(r"window\.CONFIG\s*=\s*(\{.*?\})\s*;", re.DOTALL)
_IFRAME_RE = re.compile(r"<iframe\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.I | re.S)

_M3U8_PATTERNS = [
    r"[\"'](https?:[^\"']+\.m3u8[^\"']*)[\"']",
    r"file\s*:\s*[\"']([^\"']+)[\"']",
    r"sources\s*:\s*\[\s*[\"']([^\"']+)[\"']",
]

# Indonesian is the default subtitle language, English is the fallback. Codes
# here are normalised from the many labels embed hosts use.
LANGUAGE_ALIASES = {
    "id": "id",
    "id-id": "id",
    "ind": "id",
    "indonesian": "id",
    "bahasa": "id",
    "in": "id",
    "en": "en",
    "en-us": "en",
    "en-gb": "en",
    "eng": "en",
    "english": "en",
}
PREFERRED_LANGUAGES = ("id", "en")


def _language_code(*values) -> str | None:
    """Normalise a host-provided language label to a canonical code."""
    text = " ".join(str(value) for value in values if value).strip().lower()
    if not text:
        return None
    cleaned = re.sub(r"[\(\)\[\]]", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    for candidate in (cleaned, cleaned.split(" ")[0], cleaned.split("-")[0]):
        if candidate in LANGUAGE_ALIASES:
            return LANGUAGE_ALIASES[candidate]
    for token in re.split(r"[\s\-_,/]+", cleaned):
        if token in LANGUAGE_ALIASES:
            return LANGUAGE_ALIASES[token]
    if "indonesia" in cleaned:
        return "id"
    if "english" in cleaned or "inggris" in cleaned:
        return "en"
    return cleaned.split(" ")[0] or None


def _language_rank(code: str | None) -> int:
    try:
        return PREFERRED_LANGUAGES.index(code or "")
    except ValueError:
        return len(PREFERRED_LANGUAGES)


def _quality_id(label, index: int) -> str:
    if not label:
        return f"source-{index + 1}"
    slug = re.sub(r"[^a-z0-9]+", "-", str(label).lower()).strip("-")
    return slug or f"source-{index + 1}"


def _asset_headers(raw: dict, referer: str, base_headers: dict) -> dict:
    headers = dict(base_headers)
    extra = raw.get("headers") if isinstance(raw.get("headers"), dict) else {}
    headers.update({str(key): str(value) for key, value in extra.items()})
    headers["Referer"] = str(raw.get("referer") or raw.get("Referer") or referer)
    return headers


def _normalise_sources(items, referer: str, base_headers: dict) -> list[dict]:
    normalised: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(items or []):
        if isinstance(item, str):
            url, raw = item, {}
        elif isinstance(item, dict):
            raw = item
            url = (
                item.get("url")
                or item.get("src")
                or item.get("file")
                or item.get("playlist")
            )
        else:
            continue
        if not isinstance(url, str) or not is_public_http_url(url) or url in seen:
            continue
        seen.add(url)

        label = (
            raw.get("label")
            or raw.get("name")
            or raw.get("quality")
            or raw.get("resolution")
            or raw.get("height")
            or ""
        )
        if isinstance(label, (int, float)) and label < 10000:
            label = f"{int(label)}p"
        label = str(label).strip()
        if not label and raw.get("width") and raw.get("height"):
            label = f"{raw['height']}p"
        normalised.append(
            {
                "id": _quality_id(label or raw.get("quality"), index),
                "label": label or f"Source {index + 1}",
                "url": url,
                "request_headers": _asset_headers(raw, referer, base_headers),
            }
        )
    return normalised


def _normalise_subtitles(items, referer: str, base_headers: dict) -> list[dict]:
    normalised: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(items or []):
        if isinstance(item, str):
            raw = {"url": item}
        elif isinstance(item, dict):
            raw = item
        else:
            continue
        url = (
            raw.get("url")
            or raw.get("src")
            or raw.get("file")
            or raw.get("path")
        )
        if not isinstance(url, str) or not is_public_http_url(url) or url in seen:
            continue
        seen.add(url)

        label = raw.get("label") or raw.get("name") or raw.get("title") or ""
        language = _language_code(
            raw.get("language"), raw.get("lang"), raw.get("code"), label
        )
        normalised.append(
            {
                "language": language or "",
                "label": str(label or language or f"Subtitle {index + 1}").strip(),
                "format": str(raw.get("format") or raw.get("type") or "vtt").lower(),
                "url": url,
                "request_headers": _asset_headers(raw, referer, base_headers),
            }
        )

    normalised.sort(key=lambda entry: _language_rank(entry["language"]))
    for index, entry in enumerate(normalised):
        entry["id"] = f"sub-{entry['language'] or index}-{index}"
    return normalised


def _collect_api_subtitles(payload: dict) -> list:
    """Collect subtitles from the common `subtitles`/`captions`/`tracks` shapes."""
    collected: list = []
    for key in ("subtitles", "captions", "tracks", "default_subs"):
        value = payload.get(key)
        if isinstance(value, list):
            collected.extend(value)
        elif isinstance(value, dict):
            for label, item in value.items():
                if isinstance(item, str):
                    collected.append({"url": item, "label": label})
                elif isinstance(item, dict):
                    merged = dict(item)
                    merged.setdefault("label", label)
                    collected.append(merged)
    return collected


def _normalise_audio(items) -> list[dict]:
    normalised: list[dict] = []
    for index, item in enumerate(items or []):
        if isinstance(item, str):
            label, url = item, None
        elif isinstance(item, dict):
            label = item.get("label") or item.get("name") or item.get("language") or ""
            url = item.get("url") or item.get("src") or None
        else:
            continue
        if not label and not url:
            continue
        normalised.append(
            {
                "id": f"audio-{index + 1}",
                "label": str(label or f"Audio {index + 1}").strip(),
                "language": _language_code(label) or "",
                "url": url if isinstance(url, str) and is_public_http_url(url) else None,
            }
        )
    return normalised


def _extract_json_object(pattern: re.Pattern, html: str, name: str) -> Dict[str, Any]:
    match = pattern.search(html)
    if not match:
        raise ExtractorError(f"{name}: config object not found")
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ExtractorError(f"{name}: invalid config JSON") from exc


def _set_query_param(url: str, key: str, value: str) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query[key] = value
    return urlunparse(parsed._replace(query=urlencode(query)))


class GenericEmbedExtractor(BaseExtractor):
    """One extractor for every host declared in ``embed_hosts.py``.

    The concrete upstream strategy is selected by ``config.flow``:
      * ``vidspark_api`` — VidCore iframe -> VidSpark JSON API
      * ``vsembed_chain`` — VSEmbed gate -> player -> stream API
      * ``generic_html`` — follow iframes and look for a direct playlist URL
    """

    def __init__(self, request_headers: dict, config: EmbedHostConfig):
        super().__init__(request_headers)
        self.config = config
        self.mediaflow_endpoint = config.endpoint
        self._ad_report = AdFilterReport()

    async def _browser_fetch_playlist(self, url: str, referer: str) -> str | None:
        """Fetch a challenge-protected playlist in an isolated browser context.

        This is a tightly scoped sandbox: a fresh curl_cffi session with a real
        Chrome TLS fingerprint, no shared cookies, and media-only validation.
        It never executes page JavaScript and is only used for URLs that have
        already passed the gateway ad/SSRF filters.
        """
        try:
            from curl_cffi.requests import AsyncSession
        except ImportError:
            logger.debug("[%s] curl_cffi unavailable; browser probe skipped", self.config.canonical)
            return None

        proxy = self._get_proxy(url)
        try:
            async with AsyncSession() as session:
                response = await session.get(
                    url,
                    headers={
                        "User-Agent": self.base_headers.get("user-agent", ""),
                        "Accept": "application/vnd.apple.mpegurl,application/x-mpegURL,*/*",
                        "Referer": referer,
                    },
                    impersonate="chrome131",
                    timeout=8,
                    **({"proxy": proxy} if proxy else {}),
                )
                text = response.text or ""
                if response.status_code == 200 and "#EXTM3U" in text:
                    return text
                logger.debug(
                    "[%s] browser probe returned no playlist (status=%s) for %s",
                    self.config.canonical,
                    response.status_code,
                    url,
                )
        except Exception as exc:
            logger.debug("[%s] browser probe failed for %s: %s", self.config.canonical, url, exc)
        return None

    async def extract(self, url: str, **kwargs) -> Dict[str, Any]:
        if not is_public_http_url(url):
            raise ExtractorError(f"{self.config.canonical}: unsafe destination URL")

        hostname = (urlparse(url).hostname or "").lower()
        if not self.config.allowed_host(hostname):
            raise ExtractorError(
                f"{self.config.canonical}: host {hostname!r} is not allowed for this extractor"
            )

        self._ad_report = AdFilterReport()
        if self.config.flow == FLOW_VIDSPARK:
            result = await self._extract_vidspark(url)
        elif self.config.flow == FLOW_VSEMBED:
            result = await self._extract_vsembed(url)
        else:
            result = await self._extract_generic(url)
        return filter_extractor_result(result, self._ad_report)

    def _result(
        self,
        destination_url: str,
        referer: str,
        sources=None,
        subtitles=None,
        audio=None,
        capabilities=None,
    ) -> Dict[str, Any]:
        if not is_public_http_url(destination_url):
            raise ExtractorError(f"{self.config.canonical}: resolved unsafe stream URL")

        base_headers = {
            "User-Agent": self.base_headers.get("user-agent", ""),
            "Referer": referer,
        }
        normalised_sources = _normalise_sources(sources, referer, base_headers)
        if not any(source["url"] == destination_url for source in normalised_sources):
            normalised_sources.insert(
                0,
                {
                    "id": "auto",
                    "label": "Auto",
                    "url": destination_url,
                    "request_headers": dict(base_headers),
                },
            )
        normalised_subtitles = _normalise_subtitles(subtitles, referer, base_headers)
        normalised_audio = _normalise_audio(audio)

        default_capabilities = {
            "quality_from_api": self.config.quality_from_api,
            "subtitles_from_api": self.config.subtitles_from_api,
            "audio_from_api": self.config.audio_from_api,
            "audio_from_manifest": True,
        }
        if capabilities:
            default_capabilities.update(capabilities)

        return {
            "destination_url": destination_url,
            "request_headers": base_headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
            "expiration": 120,
            "force_playlist_proxy": True,
            "sources": normalised_sources,
            "subtitles": normalised_subtitles,
            "audio": normalised_audio,
            "capabilities": default_capabilities,
        }

    # ------------------------------------------------------------------
    # VidCore -> VidSpark
    # ------------------------------------------------------------------
    async def _extract_vidspark(self, url: str) -> Dict[str, Any]:
        media_type, media_id, season, episode = parse_media_path(url)
        season = season or "1"
        episode = episode or "1"

        if media_type == "tv":
            path = f"/tv/{media_id}/{season}/{episode}"
        else:
            path = f"/movie/{media_id}"

        api_url = f"{(self.config.api_base or '').rstrip('/')}{path}"
        response = await self._make_request(url=api_url, headers=self.config.api_headers)
        try:
            payload = response.json()
        except Exception as exc:
            raise ExtractorError(f"{self.config.canonical}: invalid API response") from exc

        if not isinstance(payload, dict) or not payload.get("result"):
            message = payload.get("message") if isinstance(payload, dict) else None
            unavailable = any(
                marker in str(message).lower()
                for marker in ("not found", "no vidora link", "unavailable")
            )
            reason = "content_unavailable" if unavailable else "stream_unavailable"
            raise ExtractorError(f"{self.config.canonical}: {reason}: {message or reason}")

        sources = payload.get("sources") or []
        if not isinstance(sources, list):
            sources = [sources]
        cleaned_sources = []
        for item in sources:
            raw = (item.get("url") or item.get("src") or item.get("file")) if isinstance(item, dict) else item
            clean = filter_candidate_url(raw, api_url, self._ad_report)
            if clean and clean not in cleaned_sources:
                cleaned_sources.append(clean)
        destination = cleaned_sources[0] if cleaned_sources else None
        if not destination:
            reason = (
                "ad-filter blocked every source"
                if self._ad_report.blocked_urls
                else "no playable source returned"
            )
            raise ExtractorError(f"{self.config.canonical}: {reason}")

        subtitles = _collect_api_subtitles(payload)
        audio = payload.get("audio") or payload.get("audioTracks") or []
        if isinstance(audio, dict):
            audio = list(audio.values())

        logger.info(
            "[%s] resolved %s -> %s (%s sources, %s subtitles)",
            self.config.canonical,
            url,
            destination,
            len(sources),
            len(subtitles),
        )
        return self._result(
            destination,
            "https://vidspark.to/",
            sources=sources,
            subtitles=subtitles,
            audio=audio,
        )

    # ------------------------------------------------------------------
    # VSEmbed gate chain
    # ------------------------------------------------------------------
    async def _extract_vsembed(self, url: str) -> Dict[str, Any]:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        media_type, media_id, _season, _episode = parse_media_path(url)

        gate_url = f"{origin}/vs_src.php?type={media_type}&id={media_id}"
        gate = await self._make_request(
            url=gate_url,
            headers={"Referer": url, "Accept": "application/json"},
        )
        try:
            nested_url = gate.json().get("src")
        except Exception as exc:
            raise ExtractorError(f"{self.config.canonical}: invalid gate response") from exc
        nested_url = filter_candidate_url(nested_url, gate_url, self._ad_report)
        if not nested_url:
            raise ExtractorError(f"{self.config.canonical}: gate did not return a usable player")

        landing = await self._make_request(
            url=nested_url,
            headers={"Referer": origin, "Accept": "text/html"},
        )
        cfg = _extract_json_object(_CFG_RE, landing.text, self.config.canonical)
        player_url = filter_candidate_url(cfg.get("playerUrl"), nested_url, self._ad_report)
        if not player_url:
            raise ExtractorError(f"{self.config.canonical}: player URL missing")

        player = await self._make_request(
            url=player_url,
            headers={"Referer": nested_url, "Accept": "text/html"},
        )
        player_cfg = _extract_json_object(_CONFIG_RE, player.text, self.config.canonical)
        # Movie pages still expose `api`; TV pages now expose `streamBase`.
        api_url = player_cfg.get("api") or player_cfg.get("streamBase")
        api_token = player_cfg.get("apiToken")
        if not api_url or not api_token:
            raise ExtractorError(f"{self.config.canonical}: stream API config missing")

        # The stream API only returns decrypted/cast URLs when the bare
        # `stream_urls` flag is present. keep_blank_values in _set_query_param
        # preserves it; do not add season/episode because the token is already
        # bound to the requested media identity.
        if player_cfg.get("streamBase"):
            api_url = _set_query_param(api_url, "stream_urls", "")

        # The player first reads metadata to learn which pool host serves the
        # stream_urls API for this request. Mirror that before calling the API.
        meta_url = player_cfg.get("metaApi")
        if meta_url:
            try:
                meta = await self._make_request(
                    url=meta_url,
                    headers={"Referer": player_url, "Accept": "application/json"},
                )
                pool_host = (meta.json().get("data") or {}).get("stream_urls_api_host")
                if pool_host:
                    api_parsed = urlparse(api_url)
                    api_url = urlunparse(api_parsed._replace(netloc=pool_host))
            except ExtractorError:
                logger.debug("[%s] metadata lookup failed, using default API host", self.config.canonical)

        api_url = _set_query_param(api_url, "api_token", api_token)
        stream_response = await self._make_request(
            url=api_url,
            headers={"Referer": player_url, "Accept": "application/json"},
        )
        try:
            stream_payload = stream_response.json()
        except Exception as exc:
            raise ExtractorError(f"{self.config.canonical}: invalid stream API response") from exc

        data = stream_payload.get("data") or {}
        stream_urls = data.get("stream_urls") or []
        cast_urls = data.get("cast_urls") or []
        destination: Optional[str] = None
        browser_used = False

        if isinstance(stream_urls, list):
            destination = next(
                (
                    clean
                    for clean in (
                        filter_candidate_url(u, player_url, self._ad_report) for u in stream_urls
                    )
                    if clean
                ),
                None,
            )

        # Encrypted responses carry plaintext cast URLs. The public /pl/ route
        # is often playable without the cast token; when it is behind a bot
        # challenge, retry in the isolated browser-fingerprint sandbox.
        if not destination and cast_urls:
            for cast_url in cast_urls:
                candidate = filter_candidate_url(
                    str(cast_url).replace("/plc/", "/pl/"), player_url, self._ad_report
                )
                if not candidate:
                    continue
                try:
                    probe = await self._make_request(
                        url=candidate,
                        headers={"Referer": player_url, "Accept": "application/vnd.apple.mpegurl"},
                        raise_on_status=False,
                        retries=1,
                    )
                    if probe.status == 200 and "#EXTM3U" in probe.text:
                        destination = candidate
                        break
                except Exception:
                    pass

                if await self._browser_fetch_playlist(candidate, player_url):
                    destination = candidate
                    browser_used = True
                    break

        if not destination:
            if isinstance(stream_urls, str) or cast_urls:
                raise ExtractorError(
                    f"{self.config.canonical}: stream URLs are WASM-encrypted and no plaintext "
                    "cast playlist was available"
                )
            raise ExtractorError(f"{self.config.canonical}: no playable source returned")

        logger.info("[%s] resolved %s -> %s", self.config.canonical, url, destination)
        subtitles = _collect_api_subtitles(stream_payload) + _collect_api_subtitles(data)
        result = self._result(destination, player_url, subtitles=subtitles)
        if browser_used:
            result["sandbox_used"] = True
        return result

    # ------------------------------------------------------------------
    # Generic HTML / iframe hosts
    # ------------------------------------------------------------------
    async def _extract_generic(self, url: str) -> Dict[str, Any]:
        current = url
        referer = f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
        visited = set()

        for _hop in range(self.config.max_iframe_hops + 1):
            if current in visited or not is_public_http_url(current):
                break
            visited.add(current)

            response = await self._make_request(
                url=current,
                headers={"Referer": referer, "Accept": "text/html,application/json"},
            )
            html = sanitize_html(response.text, current, self._ad_report)

            for pattern in _M3U8_PATTERNS:
                for match in re.finditer(pattern, html, re.I | re.S):
                    destination = filter_candidate_url(
                        urljoin(current, match.group(1)), current, self._ad_report
                    )
                    if not destination:
                        continue
                    logger.info("[%s] resolved %s -> %s", self.config.canonical, url, destination)
                    return self._result(destination, referer)

            iframe = _IFRAME_RE.search(html)
            if not iframe:
                break
            nxt = filter_candidate_url(iframe.group(1), current, self._ad_report)
            if not nxt or nxt in visited:
                break
            referer = current
            current = nxt

        try:
            destination = await eval_solver(self, current, {"Referer": referer}, _M3U8_PATTERNS)
            clean = filter_candidate_url(destination, current, self._ad_report)
            if clean:
                return self._result(clean, referer)
        except Exception:
            raise ExtractorError(
                f"{self.config.canonical}: no direct stream found in embed page"
            )
