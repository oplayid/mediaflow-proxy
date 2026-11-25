"""Central redaction for application and access logs.

Upstream media URLs, signed ``/_token_...`` paths and API credentials must
never be written to stdout/stderr. Installing one filter is safer than trying
to remember to scrub every individual ``logger.info`` call.
"""

from __future__ import annotations

import hashlib
import logging
import re
from urllib.parse import urlparse

_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_TOKEN_RE = re.compile(r"/_token_[^/\s\"'<>]+", re.IGNORECASE)
_SECRET_RE = re.compile(
    r"(?i)\b(api_password|api[_-]?key|tmdb[_-]?api[_-]?key|earnvids[_-]?api[_-]?key|"
    r"mfp[_-]?earnvids[_-]?secret|token|secret|sign)\b\s*[:=]\s*[^\s&,;\"']+"
)


def _redact_url(match: re.Match[str]) -> str:
    raw = match.group(0).rstrip("),.;")
    try:
        parsed = urlparse(raw)
    except ValueError:
        return "<url-redacted>"
    if not parsed.scheme or not parsed.netloc:
        return "<url-redacted>"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"<url-redacted:{digest}>"


def _redact_token(match: re.Match[str]) -> str:
    raw = match.group(0)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"/_token_<redacted:{digest}>"


def redact_sensitive(text: str) -> str:
    """Remove URLs, signed paths and credential values from a log record."""
    safe = _URL_RE.sub(_redact_url, text)
    safe = _TOKEN_RE.sub(_redact_token, safe)
    safe = _SECRET_RE.sub(lambda match: f"{match.group(1)}=<redacted>", safe)
    return safe


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        safe = redact_sensitive(message)
        if safe != message:
            record.msg = safe
            record.args = ()
        return True


_LOGGERS = (
    "mediaflow_proxy",
    "uvicorn.access",
    "gunicorn.access",
    "gunicorn.error",
)


def install_log_redaction() -> None:
    """Attach the filter once; repeated calls are harmless."""
    filter_ = RedactFilter()
    for name in _LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(item, RedactFilter) for item in logger.filters):
            logger.addFilter(filter_)
    root = logging.getLogger()
    if not any(isinstance(item, RedactFilter) for item in root.filters):
        root.addFilter(filter_)
