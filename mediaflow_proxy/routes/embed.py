"""Iframe-compatible embed endpoints for StreamFlix.

StreamFlix now treats MediaFlow as the owner of the player and the embed-host
registry. It only renders an iframe pointed at ``/embed/auto/...`` and keeps its
existing ``OPLAY_EMBED_*`` postMessage handling. MediaFlow picks a healthy host,
falls back automatically, and signs the proxy URL internally so ``api_password``
is never exposed to the browser.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from mediaflow_proxy.configs import settings
from mediaflow_proxy.extractors.embed_hosts import (
    AUTO_HOST_ORDER,
    build_embed_url,
    get_config,
)
from mediaflow_proxy.extractors.factory import ExtractorFactory
from mediaflow_proxy.utils.crypto_utils import EncryptionHandler
from mediaflow_proxy.utils.http_utils import (
    encode_mediaflow_proxy_url,
    get_original_scheme,
    get_proxy_headers,
    ProxyRequestHeaders,
)

logger = logging.getLogger(__name__)
embed_router = APIRouter()

HLS_JS_URL = "https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js"
NEGATIVE_CACHE_TTL = 300.0

# (host, media_type, media_id, season, episode) -> monotonic expiry.
_negative_cache: dict[tuple[str, str, str, str, str], float] = {}
_cache_lock = asyncio.Lock()


def _js_json(value) -> str:
    """JSON-encode a value for safe embedding inside an inline <script>."""
    return json.dumps(value).replace("<", "\\u003c")


def _js_string(value: str) -> str:
    """JSON-encode a string for safe embedding inside an inline <script>."""
    return _js_json(value)


async def _cache_is_hot(key: tuple[str, str, str, str, str]) -> bool:
    now = time.monotonic()
    async with _cache_lock:
        expiry = _negative_cache.get(key)
        if expiry is None:
            return False
        if expiry > now:
            return True
        _negative_cache.pop(key, None)
        return False


async def _cache_failure(key: tuple[str, str, str, str, str]) -> None:
    async with _cache_lock:
        _negative_cache[key] = time.monotonic() + NEGATIVE_CACHE_TTL


def _origin(request: Request) -> str:
    host = request.headers.get("x-forwarded-host") or request.url.netloc
    return f"{get_original_scheme(request)}://{host}"


def _option_url(
    request: Request,
    slug: str,
    media_type: str,
    media_id: str,
    season: str | None,
    episode: str | None,
    autoplay: bool,
) -> str:
    mid = quote(media_id, safe="")
    if media_type == "tv":
        path = f"/embed/{slug}/tv/{mid}/{quote(season or '1', safe='')}/{quote(episode or '1', safe='')}"
    else:
        path = f"/embed/{slug}/movie/{mid}"
    if autoplay:
        path += "?autoplay=true"
    return f"{_origin(request)}{path}"


def _server_options(
    request: Request,
    media_type: str,
    media_id: str,
    season: str | None,
    episode: str | None,
    autoplay: bool,
    selected_slug: str,
) -> list[dict]:
    options: list[dict] = []
    for slug in ("auto", *AUTO_HOST_ORDER):
        config = get_config(slug)
        name = "Auto" if slug == "auto" else (config.canonical if config else slug)
        options.append(
            {
                "id": slug,
                "name": name,
                "url": _option_url(
                    request, slug, media_type, media_id, season, episode, autoplay
                ),
                "active": slug == selected_slug,
            }
        )
    return options


def _player_html(
    stream_url: str,
    title: str,
    autoplay: bool,
    selected: dict,
    server_options: list[dict],
) -> str:
    """Small HLS player implementing the OPLAY embed postMessage contract."""
    option_markup = "".join(
        '<option value="{url}"{selected}>{name}</option>'.format(
            url=html.escape(option["url"], quote=True),
            selected=" selected" if option["active"] else "",
            name=html.escape(option["name"]),
        )
        for option in server_options
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>{html.escape(title)}</title>
<style>
html,body{{margin:0;height:100%;background:#000;overflow:hidden}}
video{{width:100%;height:100%;display:block;object-fit:contain;background:#000}}
#err{{position:absolute;inset:0;display:none;align-items:center;justify-content:center;
      color:#bbb;font:14px/1.5 system-ui,-apple-system,Arial,sans-serif;text-align:center;padding:24px}}
#server{{position:absolute;top:6px;right:6px;z-index:2;max-width:45%;color:#fff;
         background:rgba(0,0,0,.65);border:1px solid rgba(255,255,255,.25);border-radius:6px;
         font:12px/1.4 system-ui,-apple-system,Arial,sans-serif;padding:4px 6px}}
</style>
<script src="{HLS_JS_URL}"></script>
</head>
<body>
<video id="video" playsinline webkit-playsinline controls autoplay="{'true' if autoplay else 'false'}"></video>
<select id="server" aria-label="Stream server">{option_markup}</select>
<div id="err">Stream unavailable</div>
<script>
(function () {{
  var SRC = {_js_string(stream_url)};
  var AUTOPLAY = {json.dumps(autoplay)};
  var SELECTED = {_js_json(selected)};
  var video = document.getElementById('video');
  var err = document.getElementById('err');
  var server = document.getElementById('server');
  var hls = null;
  var failed = false;

  function post(type, extra) {{
    var msg = {{ type: type }};
    if (extra) Object.keys(extra).forEach(function (k) {{ msg[k] = extra[k]; }});
    try {{ window.parent.postMessage(msg, '*'); }} catch (e) {{}}
  }}
  function progress() {{
    if (!isFinite(video.duration) || video.duration <= 0) return;
    post('OPLAY_EMBED_PROGRESS', {{
      currentTime: video.currentTime,
      duration: video.duration,
      isPlaying: !video.paused
    }});
  }}
  function fail(message) {{
    if (failed) return;
    failed = true;
    err.style.display = 'flex';
    err.textContent = message || 'Stream unavailable';
    post('OPLAY_EMBED_ERROR', {{ message: err.textContent }});
  }}

  server.addEventListener('change', function () {{
    if (server.value) window.location.href = server.value;
  }});

  if (video.canPlayType('application/vnd.apple.mpegurl')) {{
    video.src = SRC;
  }} else if (window.Hls && Hls.isSupported()) {{
    hls = new Hls({{ enableWorker: true, lowLatencyMode: false }});
    hls.loadSource(SRC);
    hls.attachMedia(video);
    hls.on(Hls.Events.ERROR, function (_, data) {{
      if (data && data.fatal) fail('Playback error: ' + (data.type || 'unknown'));
    }});
  }} else {{
    video.src = SRC;
  }}

  video.addEventListener('loadedmetadata', function () {{
    post('OPLAY_EMBED_SERVER', SELECTED);
    post('OPLAY_EMBED_READY');
    progress();
  }});
  video.addEventListener('timeupdate', progress);
  video.addEventListener('play', progress);
  video.addEventListener('pause', progress);
  video.addEventListener('ended', progress);
  video.addEventListener('error', function () {{ fail('Stream unavailable'); }});
  window.setInterval(progress, 1000);
  post('OPLAY_EMBED_SERVER', SELECTED);

  window.addEventListener('message', function (event) {{
    var d = event.data || {{}};
    if (d.type !== 'OPLAY_EMBED_PLAYBACK') return;
    switch (d.command) {{
      case 'handshake':
        post('OPLAY_EMBED_SERVER', SELECTED);
        post('OPLAY_EMBED_READY');
        progress();
        break;
      case 'play': video.play().catch(function () {{}}); break;
      case 'pause': video.pause(); break;
      case 'seek':
        if (typeof d.time === 'number' && isFinite(d.time)) video.currentTime = d.time;
        break;
      case 'requestProgress': progress(); break;
    }}
  }});

  if (AUTOPLAY) {{ video.muted = true; video.play().catch(function () {{}}); }}
}})();
</script>
</body>
</html>"""


async def _extract(
    request: Request,
    config,
    proxy_headers: ProxyRequestHeaders,
    media_type: str,
    media_id: str,
    season: str | None,
    episode: str | None,
) -> dict:
    embed_url = build_embed_url(config, media_type, media_id, season, episode)
    extractor = ExtractorFactory.get_extractor(config.canonical, proxy_headers.request)
    return await extractor.extract(embed_url)


def _encode_stream_url(
    request: Request, result: dict, proxy_headers: ProxyRequestHeaders
) -> str:
    endpoint = result.pop("mediaflow_endpoint")
    proxy_base = str(
        request.url_for(endpoint).replace(scheme=get_original_scheme(request))
    )

    query_params = result.get("query_params", {})
    query_params["api_password"] = settings.api_password or ""
    if result.pop("force_playlist_proxy", False):
        query_params["force_playlist_proxy"] = "1"

    encryption_handler = EncryptionHandler(settings.api_password) if settings.api_password else None
    return encode_mediaflow_proxy_url(
        mediaflow_proxy_url=proxy_base,
        destination_url=result["destination_url"],
        query_params=query_params,
        request_headers=result.get("request_headers"),
        response_headers=proxy_headers.response,
        encryption_handler=encryption_handler,
        expiration=result.get("expiration", 300),
    )


async def _resolve_proxy_url(
    request: Request,
    slug: str,
    media_type: str,
    media_id: str,
    season: str | None,
    episode: str | None,
) -> tuple[str, str, bool, str]:
    proxy_headers = get_proxy_headers(request)
    autoplay = request.query_params.get("autoplay", "").lower() in ("1", "true", "yes")
    auto = slug == "auto"

    if auto:
        configs = [get_config(name) for name in AUTO_HOST_ORDER]
        forced = get_config(request.query_params.get("server", ""))
        if forced:
            configs = [forced, *(c for c in configs if c and c.canonical != forced.canonical)]
        configs = [c for c in configs if c]
    else:
        config = get_config(slug)
        if not config:
            raise HTTPException(status_code=404, detail=f"Unknown embed host: {slug}")
        configs = [config]

    errors: list[str] = []
    for config in configs:
        key = (
            config.canonical,
            media_type,
            media_id,
            season or "",
            episode or "",
        )
        if auto and await _cache_is_hot(key):
            errors.append(f"{config.canonical}: recently failed")
            continue

        try:
            result = await _extract(
                request, config, proxy_headers, media_type, media_id, season, episode
            )
            stream_url = _encode_stream_url(request, result, proxy_headers)
        except Exception as exc:
            logger.warning("[embed/%s] %s failed: %s", slug, config.canonical, exc)
            if auto:
                await _cache_failure(key)
            errors.append(f"{config.canonical}: {exc}")
            continue

        logger.info("[embed/%s] selected %s for %s %s", slug, config.canonical, media_type, media_id)
        return stream_url, f"{config.canonical} — {media_id}", autoplay, config.slug

    detail = "; ".join(errors) or "No embed host available"
    raise HTTPException(status_code=502, detail=detail)


def _render(
    request: Request,
    stream_url: str,
    title: str,
    autoplay: bool,
    selected_slug: str,
    media_type: str,
    media_id: str,
    season: str | None,
    episode: str | None,
) -> HTMLResponse:
    config = get_config(selected_slug)
    selected = {
        "id": selected_slug,
        "name": "Auto" if selected_slug == "auto" else (config.canonical if config else selected_slug),
    }
    options = _server_options(
        request, media_type, media_id, season, episode, autoplay, selected_slug
    )
    return HTMLResponse(
        _player_html(stream_url, title, autoplay, selected, options),
        headers={"Cache-Control": "no-store"},
    )


@embed_router.get("/servers")
async def embed_servers():
    """Server list for clients that still want to render a picker."""
    servers = [{"id": "auto", "name": "Auto", "priority": 0}]
    for priority, name in enumerate(AUTO_HOST_ORDER, start=1):
        config = get_config(name)
        if config:
            servers.append(
                {"id": config.slug, "name": config.canonical, "priority": priority}
            )
    return servers


@embed_router.get("/{slug}/movie/{media_id}", response_class=HTMLResponse)
async def embed_movie(request: Request, slug: str, media_id: str):
    stream_url, title, autoplay, selected_slug = await _resolve_proxy_url(
        request, slug, "movie", media_id, None, None
    )
    return _render(
        request, stream_url, title, autoplay, selected_slug, "movie", media_id, None, None
    )


@embed_router.get("/{slug}/tv/{media_id}/{season}/{episode}", response_class=HTMLResponse)
async def embed_tv(request: Request, slug: str, media_id: str, season: str, episode: str):
    stream_url, title, autoplay, selected_slug = await _resolve_proxy_url(
        request, slug, "tv", media_id, season, episode
    )
    return _render(
        request, stream_url, title, autoplay, selected_slug, "tv", media_id, season, episode
    )
