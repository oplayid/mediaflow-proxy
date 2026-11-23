"""Configuration registry for StreamFlix-style embed hosts.

The goal of this module is to make adding another embed host a one-line change:
all host-specific data (allowed domains, URL templates, upstream flow and
headers) lives in EMBED_HOSTS below.

MediaFlow exposes these hosts through:
  * ``/extractor/video.m3u8?host=<host>&d=<embed_url>`` (standard extractor API)
  * ``/embed/<slug>/movie/<id>`` and ``/embed/<slug>/tv/<id>/<s>/<e>``
    (iframe-compatible player for StreamFlix)
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

# Flow identifiers used by GenericEmbedExtractor.
FLOW_VIDSPARK = "vidspark_api"
FLOW_VSEMBED = "vsembed_chain"
FLOW_VIDLINK = "vidlink_api"
FLOW_VIDSRC_TO = "vidsrc_to_api"
FLOW_GENERIC = "generic_html"

VIDSPARK_API_BASE = "https://vidspark.to/api/vidora/v1"
VIDSPARK_PLAYER_KEY = "3a67e8866ae1d2bb9e81fe7f73315a56eb3bdf5e3e755c7554c8be6910aa6b13"


@dataclass(frozen=True)
class EmbedHostConfig:
    """Declarative description of one embed host."""

    canonical: str
    slug: str
    domains: Tuple[str, ...]
    movie_url: str
    tv_url: str
    flow: str = FLOW_GENERIC
    # Optional template for anime players that use `/anime/{id}/{episode}/{variant}`.
    anime_url: Optional[str] = None
    api_base: Optional[str] = None
    api_headers: Dict[str, str] = field(default_factory=dict)
    endpoint: str = "hls_manifest_proxy"
    max_iframe_hops: int = 3
    # Capabilities consumed by the player. API metadata is only trusted for
    # hosts whose response shape we normalise explicitly; every host can still
    # expose quality/audio/subtitle tracks from the HLS manifest itself.
    quality_from_api: bool = False
    subtitles_from_api: bool = False
    audio_from_api: bool = False

    def allowed_host(self, hostname: str | None) -> bool:
        """Case-insensitive suffix match against this host's domains."""
        if not hostname:
            return False
        host = hostname.lower().rstrip(".")
        return any(host == d or host.endswith("." + d) for d in self.domains)


def _cfg(
    canonical: str,
    slug: str,
    domains: Tuple[str, ...],
    movie_url: str,
    tv_url: str,
    **kwargs,
) -> EmbedHostConfig:
    return EmbedHostConfig(
        canonical=canonical,
        slug=slug,
        domains=tuple(d.lower() for d in domains),
        movie_url=movie_url,
        tv_url=tv_url,
        **kwargs,
    )


# All hosts used by StreamFlix, current and historical. Domains here are used
# as an SSRF allowlist: an extractor will only fetch URLs on these hosts.
EMBED_HOSTS: Dict[str, EmbedHostConfig] = {
    c.canonical: c
    for c in [
        _cfg(
            "VSEmbed",
            "vsembed",
            ("stream.oplay.id", "vsembed.su", "vsembed.ru", "stellarconductornexus.com"),
            "https://stream.oplay.id/embed/movie/{id}",
            "https://stream.oplay.id/embed/tv/{id}/{season}/{episode}",
            flow=FLOW_VSEMBED,
        ),
        _cfg(
            "VidCore",
            "vidcore",
            ("vidcore.org", "vidspark.to", "paretocdn.site"),
            "https://vidcore.org/embed/movie/{id}?autoplay=true&lang=id&theme=FCC217",
            "https://vidcore.org/embed/tv/{id}/{season}/{episode}?autoplay=true&lang=id&theme=FCC217",
            flow=FLOW_VIDSPARK,
            api_base=VIDSPARK_API_BASE,
            api_headers={"x-player-key": VIDSPARK_PLAYER_KEY, "Referer": "https://vidspark.to/"},
            quality_from_api=True,
            subtitles_from_api=True,
            audio_from_api=True,
        ),
        _cfg(
            "VidLink",
            "vidlink",
            ("vidlink.pro", "www.vidlink.pro"),
            "https://vidlink.pro/movie/{id}",
            "https://vidlink.pro/tv/{id}/{season}/{episode}",
            anime_url="https://vidlink.pro/anime/{id}/{episode}/{variant}",
            flow=FLOW_VIDLINK,
            # VidLink's playback API returns direct MP4 files, not HLS playlists.
            endpoint="proxy_stream_endpoint",
        ),
        _cfg(
            "VidKing",
            "vidking",
            ("www.vidking.net", "vidking.ws", "www.vidking.ws"),
            "https://www.vidking.net/embed/movie/{id}?color=e50914&autoPlay=true",
            "https://www.vidking.net/embed/tv/{id}/{season}/{episode}?color=e50914&autoPlay=true&nextEpisode=true",
        ),
        _cfg(
            "AutoEmbed",
            "autoembed",
            ("player.autoembed.cc",),
            "https://player.autoembed.cc/embed/movie/{id}",
            "https://player.autoembed.cc/embed/tv/{id}/{season}/{episode}",
        ),
        _cfg(
            "TwoEmbed",
            "2embed",
            ("www.2embed.cc", "2embed.cc"),
            "https://www.2embed.cc/embed/{id}",
            "https://www.2embed.cc/embedtv/{id}&s={season}&e={episode}",
        ),
        _cfg(
            "PrimeWire",
            "primewire",
            ("www.primewire.tf", "primewire.tf"),
            "https://www.primewire.tf/embed/movie?tmdb={id}",
            "https://www.primewire.tf/embed/tv?tmdb={id}&season={season}&episode={episode}",
        ),
        _cfg(
            "MultiEmbed",
            "multiembed",
            ("multiembed.mov",),
            "https://multiembed.mov/?video_id={id}&tmdb=1",
            "https://multiembed.mov/?video_id={id}&tmdb=1&s={season}&e={episode}",
        ),
        _cfg(
            "VidEasy",
            "videasy",
            ("player.videasy.net",),
            "https://player.videasy.net/movie/{id}",
            "https://player.videasy.net/tv/{id}/{season}/{episode}",
        ),
        _cfg(
            "SmashyStream",
            "smashystream",
            ("embed.smashystream.com",),
            "https://embed.smashystream.com/playere.php?tmdb={id}",
            "https://embed.smashystream.com/playere.php?tmdb={id}&season={season}&episode={episode}",
        ),
        _cfg(
            "PStream",
            "pstream",
            ("iframe.pstream.org",),
            "https://iframe.pstream.org/embed/tmdb-movie-{id}",
            "https://iframe.pstream.org/embed/tmdb-tv-{id}/{season}/{episode}",
        ),
        _cfg(
            "VidSrcCC",
            "vidsrccc",
            ("vidsrc.cc",),
            "https://vidsrc.cc/v2/embed/movie/{id}",
            "https://vidsrc.cc/v2/embed/tv/{id}/{season}/{episode}",
        ),
        _cfg(
            "VidSrcTo",
            "vidsrcto",
            ("vidsrc.to", "www.vidsrc.to", "vidsrc.sh", "www.vidsrc.sh"),
            "https://vidsrc.to/embed/movie/{id}",
            "https://vidsrc.to/embed/tv/{id}/{season}/{episode}",
            flow=FLOW_VIDSRC_TO,
        ),
        _cfg(
            "VidSrcRip",
            "vidsrcrip",
            ("vidsrc.rip",),
            "https://vidsrc.rip/embed/movie/{id}",
            "https://vidsrc.rip/embed/tv/{id}/{season}/{episode}",
        ),
        _cfg(
            "VidSrcSu",
            "vidsrcsu",
            ("vidsrc.su",),
            "https://vidsrc.su/embed/movie/{id}",
            "https://vidsrc.su/embed/tv/{id}/{season}/{episode}",
        ),
        _cfg(
            "VidSrcVip",
            "vidsrcvip",
            ("vidsrc.vip",),
            "https://vidsrc.vip/embed/movie/{id}",
            "https://vidsrc.vip/embed/tv/{id}/{season}/{episode}",
        ),
        _cfg(
            "EmbedSu",
            "embedsu",
            ("embed.su",),
            "https://embed.su/embed/movie/{id}",
            "https://embed.su/embed/tv/{id}/{season}/{episode}",
        ),
        _cfg(
            "Frembed",
            "frembed",
            ("frembed.cc",),
            "https://frembed.cc/api/film.php?id={id}",
            "https://frembed.cc/api/serie.php?id={id}&sa={season}&epi={episode}",
        ),
        _cfg(
            "MoviesAPI",
            "moviesapi",
            ("moviesapi.club",),
            "https://moviesapi.club/movie/{id}",
            "https://moviesapi.club/tv/{id}-{season}-{episode}",
        ),
    ]
}

# The gateway resolves these hosts in failover order. Historical hosts stay in
# the registry for backwards-compatible lookups, but they are never probed by
# the StreamFlix gateway.
AUTO_HOST_ORDER: Tuple[str, ...] = (
    "VidCore",
    "VSEmbed",
    "VidLink",
    "VidSrcTo",
)

# Every lower-case spelling accepted as ``host`` / embed slug.
HOST_ALIASES: Dict[str, str] = {}
for _config in EMBED_HOSTS.values():
    for _alias in (_config.canonical, _config.slug, *_config.domains):
        HOST_ALIASES[_alias.lower()] = _config.canonical


def canonicalize_host(value: str) -> Optional[str]:
    """Return the canonical host name for a slug, domain or mixed-case name."""
    if not isinstance(value, str):
        return None
    return HOST_ALIASES.get(value.strip().lower())


def get_config(host: str) -> Optional[EmbedHostConfig]:
    """Look up a config by canonical name, slug or domain (case-insensitive)."""
    canonical = canonicalize_host(host)
    return EMBED_HOSTS.get(canonical) if canonical else None


def build_embed_url(
    config: EmbedHostConfig,
    media_type: str,
    media_id: str,
    season: Optional[int | str] = None,
    episode: Optional[int | str] = None,
    variant: Optional[str] = None,
) -> str:
    """Build the original embed URL from TMDB id and optional season/episode.

    Anime URLs use ``config.anime_url`` and a ``sub``/``dub`` variant. When a
    host has no anime template, ``anime`` falls back to its movie template.
    """
    if media_type == "anime":
        template = config.anime_url or config.movie_url
    else:
        template = config.tv_url if media_type == "tv" else config.movie_url
    return (
        template.replace("{id}", str(media_id))
        .replace("{season}", str(season or 1))
        .replace("{episode}", str(episode or 1))
        .replace("{variant}", str(variant or "sub"))
    )


ANIME_VARIANTS = ("sub", "dub")


def anime_variant_candidates(variant: Optional[str]) -> Tuple[str, ...]:
    """Return the requested anime variant first, then the alternative.

    VidLink exposes both ``/anime/{id}/{episode}/sub`` and ``/dub``. When one
    variant is unavailable the gateway can try the other automatically.
    """
    requested = (variant or "sub").strip().lower()
    if requested not in ANIME_VARIANTS:
        requested = "sub"
    fallback = "dub" if requested == "sub" else "sub"
    return (requested, fallback)


# Accept numeric TMDB ids and IMDb-style ``tt`` ids. vidsrc.sh writes TV
# season/episode as ``1-2`` while other hosts use ``1/2``.
_MEDIA_PATH_RE = re.compile(r"/(movie|tv)/(\d+|tt\d+)(?:/(\d+)[/-](\d+))?", re.I)
_ANIME_PATH_RE = re.compile(r"/anime/(\d+)/(\d+)(?:/(sub|dub))?", re.I)


def parse_media_path(url: str) -> Tuple[str, str, Optional[str], Optional[str], Optional[str]]:
    """Extract (type, id, season, episode, anime_variant) from an embed URL path.

    Anime paths (``/anime/{id}/{episode}/sub|dub``) are mapped to media type
    ``anime`` with ``episode`` populated and ``season`` left as ``None``.
    """
    path = urlparse(url).path
    anime = _ANIME_PATH_RE.search(path)
    if anime:
        anime_id, episode, variant = anime.groups()
        return "anime", anime_id, None, episode, (variant or "sub").lower()

    match = _MEDIA_PATH_RE.search(path)
    if not match:
        raise ValueError(f"Cannot determine media type/id from URL: {url}")
    media_type, media_id, season, episode = match.groups()
    return media_type, media_id, season, episode, None


def is_public_http_url(url: str) -> bool:
    """Reject non-HTTP(S), private, loopback and link-local destinations (SSRF)."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return True  # a normal DNS name; domain allowlist is checked separately
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved)
