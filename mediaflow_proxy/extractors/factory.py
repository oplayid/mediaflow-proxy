from typing import Dict, Type

from mediaflow_proxy.extractors.base import BaseExtractor, ExtractorError
from mediaflow_proxy.extractors.doodstream import DoodStreamExtractor
from mediaflow_proxy.extractors.city import CityExtractor
from mediaflow_proxy.extractors.sportsonline import SportsonlineExtractor
from mediaflow_proxy.extractors.filelions import FileLionsExtractor
from mediaflow_proxy.extractors.filemoon import FileMoonExtractor
from mediaflow_proxy.extractors.F16Px import F16PxExtractor
from mediaflow_proxy.extractors.gupload import GuploadExtractor
from mediaflow_proxy.extractors.livetv import LiveTVExtractor
from mediaflow_proxy.extractors.lulustream import LuluStreamExtractor
from mediaflow_proxy.extractors.maxstream import MaxstreamExtractor
from mediaflow_proxy.extractors.mixdrop import MixdropExtractor
from mediaflow_proxy.extractors.okru import OkruExtractor
from mediaflow_proxy.extractors.streamtape import StreamtapeExtractor
from mediaflow_proxy.extractors.streamwish import StreamWishExtractor
from mediaflow_proxy.extractors.supervideo import SupervideoExtractor
from mediaflow_proxy.extractors.turbovidplay import TurboVidPlayExtractor
from mediaflow_proxy.extractors.uqload import UqloadExtractor
from mediaflow_proxy.extractors.vavoo import VavooExtractor
from mediaflow_proxy.extractors.vidmoly import VidmolyExtractor
from mediaflow_proxy.extractors.vidoza import VidozaExtractor
from mediaflow_proxy.extractors.vixcloud import VixCloudExtractor
from mediaflow_proxy.extractors.fastream import FastreamExtractor
from mediaflow_proxy.extractors.voe import VoeExtractor
from mediaflow_proxy.extractors.vidfast import VidFastExtractor
from mediaflow_proxy.extractors.streamhg import StreamHGExtractor
from mediaflow_proxy.extractors.embed_hosts import canonicalize_host, get_config
from mediaflow_proxy.extractors.generic_embed import GenericEmbedExtractor


class ExtractorFactory:
    """Factory for creating URL extractors."""

    _extractors: Dict[str, Type[BaseExtractor]] = {
        "City": CityExtractor,
        "Doodstream": DoodStreamExtractor,
        "FileLions": FileLionsExtractor,
        "FileMoon": FileMoonExtractor,
        "F16Px": F16PxExtractor,
        "Gupload": GuploadExtractor,
        "Uqload": UqloadExtractor,
        "Mixdrop": MixdropExtractor,
        "Streamtape": StreamtapeExtractor,
        "StreamWish": StreamWishExtractor,
        "Supervideo": SupervideoExtractor,
        "TurboVidPlay": TurboVidPlayExtractor,
        "VixCloud": VixCloudExtractor,
        "Okru": OkruExtractor,
        "Maxstream": MaxstreamExtractor,
        "LiveTV": LiveTVExtractor,
        "LuluStream": LuluStreamExtractor,
        "Vavoo": VavooExtractor,
        "Vidmoly": VidmolyExtractor,
        "Vidoza": VidozaExtractor,
        "Fastream": FastreamExtractor,
        "Voe": VoeExtractor,
        "Sportsonline": SportsonlineExtractor,
        "VidFast": VidFastExtractor,
        "StreamHG": StreamHGExtractor,
    }

    # Lower-cased lookup for the built-in extractors, so ``vidfast`` and
    # ``VidFast`` resolve identically (docs promise case-insensitive hosts).
    _extractors_lower: Dict[str, Type[BaseExtractor]] = {
        name.lower(): extractor for name, extractor in _extractors.items()
    }

    @classmethod
    def get_extractor(cls, host: str, request_headers: dict) -> BaseExtractor:
        """Get appropriate extractor instance for the given host.

        Hosts declared in ``embed_hosts.py`` are handled by the config-driven
        GenericEmbedExtractor; everything else uses the original class registry.
        Both paths are case-insensitive.
        """
        config = get_config(host)
        if config:
            return GenericEmbedExtractor(request_headers, config)

        canonical = canonicalize_host(host)
        extractor_class = cls._extractors_lower.get((canonical or str(host)).lower())
        if not extractor_class:
            raise ExtractorError(f"Unsupported host: {host}")
        return extractor_class(request_headers)
