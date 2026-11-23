"""Optional headless Chromium fallback for JavaScript-gated player pages.

Some VSEmbed / VidSrcTo player pages render with an empty ``apiToken`` and only
mint it in the browser after a gate (Cloudflare Turnstile / cstCaptcha). A plain
HTTP client cannot run that JavaScript, so the token stays empty and the host is
marked unavailable.

This module is the *last resort*: it opens the already-resolved player URL in a
real headless Chromium, lets the page script run, and returns only the small set
of config keys the extractor needs. It is opt-in via
``ENABLE_HEADLESS_FALLBACK=true`` and is only called while a player session is
being created — never as a failover after playback has started.

One browser is shared by the worker, pages are serialized, every step has a hard
timeout, and results are cached briefly to avoid a browser stampede when several
host checks happen at once.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import urlparse

from mediaflow_proxy.configs import settings

logger = logging.getLogger(__name__)

# Only copy the keys the extractor actually consumes. Nested page objects and
# challenge payloads must never leak into the player session.
_CONFIG_KEYS = (
    "api",
    "streamBase",
    "stream_base",
    "apiUrl",
    "api_url",
    "apiToken",
    "api_token",
    "token",
    "metaApi",
    "meta_api",
)

# A single expression used both for polling and for reading the result, so the
# page is never evaluated twice with different definitions of "ready".
_READY_JS = """() => {
  const cfg = window.CONFIG || window.CFG;
  if (!cfg) return null;
  const token = cfg.apiToken || cfg.api_token || cfg.token;
  if (!token) return null;
  const out = {};
  for (const key of %s) {
    const value = cfg[key];
    if (value !== undefined && value !== null && value !== '') out[key] = value;
  }
  out.apiToken = token;
  return out;
}""" % (
    '["' + '","'.join(_CONFIG_KEYS) + '"]'
)

# Headless Chromium advertises itself as "HeadlessChrome", which some gates
# reject outright. Use a normal desktop Chrome UA instead.
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_CACHE_TTL = 60.0  # Short dedupe window; long-term caching is the player session cache's job.

_browser: Any = None
_playwright: Any = None
_browser_lock = asyncio.Lock()
_page_lock = asyncio.Semaphore(1)
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _safe_url(url: str) -> str:
    """Strip query strings: tokens and gate tickets must not reach the logs."""
    try:
        parsed = urlparse(url)
        return parsed._replace(query="", fragment="").geturl()
    except Exception:
        return "<invalid-url>"


async def _get_browser() -> Any:
    """Launch Chromium once per worker, on first use only."""
    global _browser, _playwright
    async with _browser_lock:
        if _browser is not None and _browser.is_connected():
            return _browser
        # Imported lazily so environments that never enable the fallback do
        # not pay for Playwright at import time.
        from playwright.async_api import async_playwright

        if _playwright is None:
            _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
            ],
        )
        logger.info("Headless Chromium started for gated player fallback")
        return _browser


async def _click_turnstile_if_visible(page: Any) -> None:
    """Best-effort click for managed challenges that need one user gesture."""
    try:
        checkbox = page.frame_locator(
            "iframe[src*='challenges.cloudflare.com'], iframe[src*='turnstile']"
        ).locator("input[type=checkbox]")
        if await checkbox.count() and await checkbox.first.is_visible():
            await checkbox.first.click(timeout=2500)
            logger.debug("Clicked Turnstile checkbox in headless fallback")
    except Exception:
        # Interactive/image challenges are intentionally not solved here.
        pass


async def _resolve(
    page_url: str, *, referer: str, timeout: float, key: str
) -> dict[str, Any] | None:
    async with _page_lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < _CACHE_TTL:
            return dict(cached[1])

        browser = await _get_browser()
        context = await browser.new_context(
            extra_http_headers={"Referer": referer},
            user_agent=_USER_AGENT,
            locale="en-US",
            viewport={"width": 1280, "height": 720},
        )
        try:
            page = await context.new_page()
            await page.goto(
                page_url, wait_until="domcontentloaded", timeout=timeout * 1000
            )
            try:
                await page.wait_for_function(_READY_JS, timeout=timeout * 1000)
            except Exception:
                await _click_turnstile_if_visible(page)
                try:
                    await page.wait_for_function(
                        _READY_JS, timeout=min(timeout * 1000, 5000)
                    )
                except Exception:
                    logger.info(
                        "Headless fallback found no apiToken for %s",
                        _safe_url(page_url),
                    )
            config = await page.evaluate(_READY_JS)
        finally:
            await context.close()

    if not config:
        return None
    _cache[key] = (time.monotonic(), dict(config))
    logger.info("Headless fallback recovered gated config for %s", _safe_url(page_url))
    return dict(config)


async def resolve_gated_config(
    page_url: str,
    *,
    referer: str,
    timeout: float | None = None,
    cache_key: str | None = None,
) -> dict[str, Any] | None:
    """Return a gated player's config after letting its JavaScript run.

    Returns ``None`` when the fallback is disabled, unavailable, or cannot get a
    token. Callers must treat that as a normal host failure, not an exception.
    """
    if not settings.enable_headless_fallback:
        return None
    if not page_url.startswith(("http://", "https://")):
        return None

    timeout = float(timeout or settings.headless_fallback_timeout or 15)
    key = cache_key or page_url
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < _CACHE_TTL:
        return dict(cached[1])

    try:
        return await asyncio.wait_for(
            _resolve(page_url, referer=referer, timeout=timeout, key=key),
            timeout=timeout + 5,
        )
    except asyncio.TimeoutError:
        logger.warning("Headless fallback timed out for %s", _safe_url(page_url))
    except Exception as exc:
        # A missing Chromium binary or a browser crash must never break the
        # normal extractor path; the host simply fails cleanly.
        logger.warning(
            "Headless fallback failed for %s: %s", _safe_url(page_url), exc
        )
    return None
