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

    async def extract(self, url: str, **kwargs) -> Dict[str, Any]:
        if not is_public_http_url(url):
            raise ExtractorError(f"{self.config.canonical}: unsafe destination URL")

        hostname = (urlparse(url).hostname or "").lower()
        if not self.config.allowed_host(hostname):
            raise ExtractorError(
                f"{self.config.canonical}: host {hostname!r} is not allowed for this extractor"
            )

        if self.config.flow == FLOW_VIDSPARK:
            return await self._extract_vidspark(url)
        if self.config.flow == FLOW_VSEMBED:
            return await self._extract_vsembed(url)
        return await self._extract_generic(url)

    def _result(self, destination_url: str, referer: str) -> Dict[str, Any]:
        if not is_public_http_url(destination_url):
            raise ExtractorError(f"{self.config.canonical}: resolved unsafe stream URL")
        return {
            "destination_url": destination_url,
            "request_headers": {
                "User-Agent": self.base_headers.get("user-agent", ""),
                "Referer": referer,
            },
            "mediaflow_endpoint": self.mediaflow_endpoint,
            "expiration": 120,
            "force_playlist_proxy": True,
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
            raise ExtractorError(f"{self.config.canonical}: {message or 'stream unavailable'}")

        sources = payload.get("sources") or []
        destination = next((s.get("url") for s in sources if s.get("url")), None)
        if not destination:
            raise ExtractorError(f"{self.config.canonical}: no playable source returned")

        logger.info("[%s] resolved %s -> %s", self.config.canonical, url, destination)
        return self._result(destination, "https://vidspark.to/")

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
        if not nested_url or not is_public_http_url(nested_url):
            raise ExtractorError(f"{self.config.canonical}: gate did not return a usable player")

        landing = await self._make_request(
            url=nested_url,
            headers={"Referer": origin, "Accept": "text/html"},
        )
        cfg = _extract_json_object(_CFG_RE, landing.text, self.config.canonical)
        player_url = urljoin(nested_url, cfg.get("playerUrl", ""))
        if not player_url or not is_public_http_url(player_url):
            raise ExtractorError(f"{self.config.canonical}: player URL missing")

        player = await self._make_request(
            url=player_url,
            headers={"Referer": nested_url, "Accept": "text/html"},
        )
        player_cfg = _extract_json_object(_CONFIG_RE, player.text, self.config.canonical)
        api_url = player_cfg.get("api")
        api_token = player_cfg.get("apiToken")
        if not api_url or not api_token:
            raise ExtractorError(f"{self.config.canonical}: stream API config missing")

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
        destination: Optional[str] = None

        if isinstance(stream_urls, list):
            destination = next((u for u in stream_urls if u), None)
        elif isinstance(stream_urls, str):
            # Encrypted responses carry plaintext cast URLs. The public /pl/
            # route is often playable without the cast token.
            for cast_url in data.get("cast_urls") or []:
                candidate = cast_url.replace("/plc/", "/pl/")
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
                    continue
            if not destination:
                raise ExtractorError(
                    f"{self.config.canonical}: stream URLs are WASM-encrypted and no plaintext "
                    "cast playlist was available"
                )
        if not destination:
            raise ExtractorError(f"{self.config.canonical}: no playable source returned")

        logger.info("[%s] resolved %s -> %s", self.config.canonical, url, destination)
        return self._result(destination, player_url)

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
            html = response.text

            for pattern in _M3U8_PATTERNS:
                match = re.search(pattern, html, re.I | re.S)
                if match:
                    destination = urljoin(current, match.group(1))
                    logger.info("[%s] resolved %s -> %s", self.config.canonical, url, destination)
                    return self._result(destination, referer)

            iframe = _IFRAME_RE.search(html)
            if not iframe:
                break
            nxt = urljoin(current, iframe.group(1))
            if nxt in visited:
                break
            referer = current
            current = nxt

        try:
            destination = await eval_solver(self, current, {"Referer": referer}, _M3U8_PATTERNS)
            return self._result(urljoin(current, destination), referer)
        except Exception:
            raise ExtractorError(
                f"{self.config.canonical}: no direct stream found in embed page"
            )
