"""Ad/scrubbing layer used by the embed gateway.

The gateway visits provider embed pages server-side. Before any URL is
accepted as a stream, quality variant or subtitle, it is checked against an
ad/tracker denylist and the fetched HTML is stripped of ad scripts, ad
iframes and redirect shims. The filter is deliberately conservative: it only
rejects known advertising infrastructure, never ordinary media CDNs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from mediaflow_proxy.extractors.base import ExtractorError

# Domains whose sole purpose is advertising, tracking, popunders or link
# wrappers. Matching is on hostname suffixes, so ``ads.example.com`` is
# covered when ``ads.example.com`` is listed but ordinary CDNs are not.
AD_DOMAINS: tuple[str, ...] = (
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "google-analytics.com",
    "googletagmanager.com",
    "adservice.google.com",
    "ads.google.com",
    "adnxs.com",
    "amazon-adsystem.com",
    "adcolony.com",
    "admob.com",
    "popads.net",
    "popunder.network",
    "propellerads.com",
    "taboola.com",
    "outbrain.com",
    "criteo.com",
    "pubmatic.com",
    "rubiconproject.com",
    "openx.net",
    "casalemedia.com",
    "scorecardresearch.com",
    "quantserve.com",
    "adsrvr.org",
    "adblade.com",
    "exoclick.com",
    "juicyads.com",
    "adsterra.com",
    "hilltopads.net",
    "trafficjunky.com",
    "adfly.com",
    "ouo.io",
    "shorte.st",
    "linkvertise.com",
    "work.ink",
    "installomatic.net",
    "onclickads.net",
    "clicksor.com",
    "yieldmo.com",
    "smartadserver.com",
    "sovrn.com",
    "mortarads.com",
    "adform.net",
    "bidswitch.net",
    "sharethrough.com",
    "nativeads.com",
    "revcontent.com",
    "mgid.com",
    "zeroredirect1.com",
    "redirectmethod.org",
)

# Path segments that identify an ad interstitial rather than media content.
AD_PATH_RE = re.compile(
    r"(?:^|/)(?:ads?|advert(?:ising|isement)?|adtag|adtags|adzone|"
    r"preroll|midroll|postroll|popunder|popups?|vast|vmap|"
    r"interstitial)(?:/|$|\.)",
    re.IGNORECASE,
)
AD_QUERY_RE = re.compile(
    r"(?:^|[?&])(?:ad|ads|adtag|preroll|midroll|popunder|popunderid)=[^&]*",
    re.IGNORECASE,
)

_SCRIPT_RE = re.compile(r"<script\b[^>]*>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)
_IFRAME_RE = re.compile(r"<iframe\b[^>]*>", re.IGNORECASE | re.DOTALL)
_META_REFRESH_RE = re.compile(r"<meta\b[^>]*http-equiv\s*=\s*['\"]?refresh['\"]?[^>]*>", re.IGNORECASE | re.DOTALL)
_META_URL_RE = re.compile(r"url\s*=\s*([^'\">\s]+)", re.IGNORECASE)
_WINDOW_OPEN_RE = re.compile(r"window\.open\s*\([^;]*?\)", re.IGNORECASE | re.DOTALL)
_LOCATION_RE = re.compile(
    r"(?:window\.)?location\s*\.\s*(?:href|replace|assign)\s*=\s*(['\"])(.*?)\1",
    re.IGNORECASE | re.DOTALL,
)
_QUOTED_URL_RE = re.compile(r"['\"](https?://[^'\"]+)['\"]", re.IGNORECASE)
_AD_TEXT_RE = re.compile(
    r"ads?by|adsterra|popunder|doubleclick|googletag|adsense|adtag|"
    r"preroll|midroll|show_?ad|display_?ad|ad_?break|advertisement",
    re.IGNORECASE,
)


@dataclass
class AdFilterReport:
    """Counters collected while cleaning one provider response."""

    blocked_domains: set[str] = field(default_factory=set)
    blocked_urls: int = 0
    sanitized_scripts: int = 0
    sanitized_iframes: int = 0
    sanitized_redirects: int = 0

    def to_dict(self) -> dict:
        return {
            "blocked_domains": sorted(self.blocked_domains),
            "blocked_urls": self.blocked_urls,
            "sanitized_scripts": self.sanitized_scripts,
            "sanitized_iframes": self.sanitized_iframes,
            "sanitized_redirects": self.sanitized_redirects,
        }

    def absorb(self, other: "AdFilterReport") -> None:
        """Merge a second pass so repeated filtering keeps the full audit."""
        self.blocked_domains.update(other.blocked_domains)
        self.blocked_urls += other.blocked_urls
        self.sanitized_scripts += other.sanitized_scripts
        self.sanitized_iframes += other.sanitized_iframes
        self.sanitized_redirects += other.sanitized_redirects

    @classmethod
    def from_dict(cls, data: dict | None) -> "AdFilterReport":
        report = cls()
        if not isinstance(data, dict):
            return report
        report.blocked_domains.update(str(d) for d in data.get("blocked_domains") or [])
        for key in ("blocked_urls", "sanitized_scripts", "sanitized_iframes", "sanitized_redirects"):
            try:
                setattr(report, key, int(data.get(key) or 0))
            except (TypeError, ValueError):
                pass
        return report


def _hostname(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


def is_ad_url(url: str) -> bool:
    """Return True for known ad/tracker/wrapper destinations."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host:
        for suffix in AD_DOMAINS:
            if host == suffix or host.endswith("." + suffix):
                return True
    if AD_PATH_RE.search(parsed.path or "") or AD_QUERY_RE.search(parsed.query or ""):
        return True
    return False


def _looks_like_ad_text(text: str) -> bool:
    return bool(_AD_TEXT_RE.search(text or ""))


def _attr_urls(tag: str) -> list[str]:
    return [match.group(1) for match in _QUOTED_URL_RE.finditer(tag or "")]


def sanitize_html(html: str, base_url: str | None = None, report: AdFilterReport | None = None) -> str:
    """Remove ad scripts, ad iframes and ad redirects from fetched HTML."""
    if not html:
        return html
    report = report or AdFilterReport()

    def clean_script(match: re.Match) -> str:
        body = match.group(0)
        if _looks_like_ad_text(match.group(1)) or any(is_ad_url(u) for u in _attr_urls(body)):
            report.sanitized_scripts += 1
            return ""
        return body

    html = _SCRIPT_RE.sub(clean_script, html)

    def clean_iframe(match: re.Match) -> str:
        tag = match.group(0)
        if any(is_ad_url(urljoin(base_url or "", u)) for u in _attr_urls(tag)):
            report.sanitized_iframes += 1
            return ""
        return tag

    html = _IFRAME_RE.sub(clean_iframe, html)

    def clean_meta(match: re.Match) -> str:
        tag = match.group(0)
        targets = [urljoin(base_url or "", u) for u in _attr_urls(tag)]
        meta_target = _META_URL_RE.search(tag)
        if meta_target:
            targets.append(urljoin(base_url or "", meta_target.group(1)))
        if any(is_ad_url(target) for target in targets):
            report.sanitized_redirects += 1
            return ""
        return tag

    html = _META_REFRESH_RE.sub(clean_meta, html)

    def clean_window_open(match: re.Match) -> str:
        if any(is_ad_url(urljoin(base_url or "", u)) for u in _attr_urls(match.group(0))):
            report.sanitized_redirects += 1
            return ""
        return match.group(0)

    html = _WINDOW_OPEN_RE.sub(clean_window_open, html)

    def clean_location(match: re.Match) -> str:
        target = urljoin(base_url or "", match.group(2))
        if is_ad_url(target):
            report.sanitized_redirects += 1
            return ""
        return match.group(0)

    html = _LOCATION_RE.sub(clean_location, html)
    return html


def filter_candidate_url(
    url: str | None,
    base_url: str | None = None,
    report: AdFilterReport | None = None,
) -> str | None:
    """Resolve and validate one candidate URL; None means it was rejected."""
    if not isinstance(url, str) or not url.strip():
        return None
    absolute = urljoin(base_url or "", url.strip())
    parsed = urlparse(absolute)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        if report:
            report.blocked_urls += 1
        return None
    if is_ad_url(absolute):
        if report:
            report.blocked_urls += 1
            report.blocked_domains.add(_hostname(absolute))
        return None
    return absolute


def filter_extractor_result(result: dict, report: AdFilterReport | None = None) -> dict:
    """Strip ads from an extractor result or fail the host if its stream is ad."""
    report = report or AdFilterReport()
    # Extractors already filter while following the provider chain. Carry that
    # audit forward instead of replacing it with an empty second-pass report.
    if isinstance(result.get("ad_filter"), dict):
        report.absorb(AdFilterReport.from_dict(result["ad_filter"]))
    destination = filter_candidate_url(result.get("destination_url"), report=report)
    if not destination:
        raise ExtractorError("ad-filter: resolved stream was an ad/wrapper URL")

    cleaned = dict(result)
    cleaned["destination_url"] = destination

    sources = []
    for source in result.get("sources") or []:
        if not isinstance(source, dict):
            continue
        url = filter_candidate_url(source.get("url"), report=report)
        if not url:
            continue
        item = dict(source)
        item["url"] = url
        sources.append(item)
    cleaned["sources"] = sources

    subtitles = []
    for subtitle in result.get("subtitles") or []:
        if not isinstance(subtitle, dict):
            continue
        url = filter_candidate_url(subtitle.get("url"), report=report)
        if not url:
            continue
        item = dict(subtitle)
        item["url"] = url
        subtitles.append(item)
    cleaned["subtitles"] = subtitles

    cleaned["ad_filter"] = report.to_dict()
    return cleaned
