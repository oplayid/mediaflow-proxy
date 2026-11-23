"""Pluggable server adapters for the mfp player gateway.

An adapter pairs one declarative ``EmbedHostConfig`` with an optional resolver.
The built-in adapters use the config-driven ``GenericEmbedExtractor``, but any
server with a bespoke API can register its own resolver without touching the
player or the gateway.

``PLAYER_HOSTS`` picks the active adapters and their priority order. It accepts
comma-separated canonical names, slugs or domains; when unset, ``HOST_ORDER``
from ``embed_hosts.py`` keeps the historical default.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from mediaflow_proxy.configs import settings
from mediaflow_proxy.extractors.embed_hosts import (
    EMBED_HOSTS,
    HOST_ORDER,
    EmbedHostConfig,
    build_embed_url,
    canonicalize_host,
)
from mediaflow_proxy.extractors.factory import ExtractorFactory
from mediaflow_proxy.gateway.ad_filter import AdFilterReport, filter_extractor_result
from mediaflow_proxy.utils.http_utils import get_proxy_headers

logger = logging.getLogger(__name__)

Resolver = Callable[..., Awaitable[dict]]


@dataclass(frozen=True)
class ServerAdapter:
    """One server the player knows how to resolve into a playable URL."""

    config: EmbedHostConfig
    resolver: Resolver | None = None

    @property
    def name(self) -> str:
        return self.config.canonical

    def build_url(self, media_type: str, media_id: str, season: str, episode: str) -> str:
        return build_embed_url(self.config, media_type, media_id, season, episode)

    async def resolve(
        self,
        request,
        media_type: str,
        media_id: str,
        season: str,
        episode: str,
    ) -> dict:
        """Return an ad-filtered extractor result for this server."""
        if self.resolver is not None:
            return await self.resolver(self, request, media_type, media_id, season, episode)

        embed_url = self.build_url(media_type, media_id, season, episode)
        extractor = ExtractorFactory.get_extractor(
            self.config.canonical, get_proxy_headers(request).request
        )
        result = await extractor.extract(embed_url)
        return filter_extractor_result(result, AdFilterReport())


_REGISTRY: dict[str, ServerAdapter] = {}


def register_server(
    config_or_adapter: EmbedHostConfig | ServerAdapter,
    *,
    resolver: Resolver | None = None,
) -> ServerAdapter:
    """Register (or replace) a server adapter by canonical name."""
    adapter = (
        config_or_adapter
        if isinstance(config_or_adapter, ServerAdapter)
        else ServerAdapter(config_or_adapter, resolver=resolver)
    )
    _REGISTRY[adapter.name] = adapter
    return adapter


def get_adapter(name: str) -> ServerAdapter | None:
    """Look up a registered adapter by canonical name, slug or domain."""
    canonical = canonicalize_host(name)
    return _REGISTRY.get(canonical) if canonical else None


def _active_names() -> list[str]:
    """Resolve PLAYER_HOSTS (or the default order) to canonical names."""
    raw = str(getattr(settings, "player_hosts", "") or "").strip()
    names = [part.strip() for part in raw.split(",") if part.strip()] if raw else list(HOST_ORDER)
    result: list[str] = []
    for name in names:
        canonical = canonicalize_host(name) or name
        if canonical not in result:
            result.append(canonical)
    return result


def configured_servers() -> list[ServerAdapter]:
    """Return the active adapters in priority order.

    Unknown names are skipped with a warning so one stale env entry cannot
    break the whole player.
    """
    servers: list[ServerAdapter] = []
    for name in _active_names():
        adapter = get_adapter(name)
        if adapter is None:
            logger.warning("[player] unknown server %s in PLAYER_HOSTS; skipped", name)
            continue
        servers.append(adapter)
    return servers


def configured_server_names() -> list[str]:
    return [adapter.name for adapter in configured_servers()]


# Every declarative host is available by default. Only the names selected by
# PLAYER_HOSTS (or HOST_ORDER) are actually probed.
for _config in EMBED_HOSTS.values():
    register_server(_config)
