"""Iframe-compatible embed endpoints for StreamFlix.

StreamFlix currently renders an <iframe> for every embed server and talks to it
through ``OPLAY_EMBED_*`` postMessage events. These endpoints let MediaFlow take
over that iframe slot: the iframe still behaves exactly like an embed player,
but the video itself is direct HLS proxied by MediaFlow.

Result: StreamFlix only needs to change the four embed URL templates; no player
logic or postMessage code has to be rewritten.
"""

from __future__ import annotations

import html
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from mediaflow_proxy.configs import settings
from mediaflow_proxy.extractors.base import ExtractorError
from mediaflow_proxy.extractors.embed_hosts import build_embed_url, get_config
from mediaflow_proxy.extractors.factory import ExtractorFactory
from mediaflow_proxy.utils.crypto_utils import EncryptionHandler
from mediaflow_proxy.utils.http_utils import (
    encode_mediaflow_proxy_url,
    get_original_scheme,
    get_proxy_headers,
)

logger = logging.getLogger(__name__)
embed_router = APIRouter()

HLS_JS_URL = "https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js"


def _js_string(value: str) -> str:
    """JSON-encode a string for safe embedding inside an inline <script>."""
    return json.dumps(value).replace("<", "\\u003c")


def _player_html(stream_url: str, title: str, autoplay: bool) -> str:
    """Small HLS player implementing the OPLAY embed postMessage contract."""
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
</style>
<script src="{HLS_JS_URL}"></script>
</head>
<body>
<video id="video" playsinline webkit-playsinline controls autoplay="{'true' if autoplay else 'false'}"></video>
<div id="err">Stream unavailable</div>
<script>
(function () {{
  var SRC = {_js_string(stream_url)};
  var AUTOPLAY = {json.dumps(autoplay)};
  var video = document.getElementById('video');
  var err = document.getElementById('err');
  var hls = null;

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
  function fail() {{ err.style.display = 'flex'; }}

  if (video.canPlayType('application/vnd.apple.mpegurl')) {{
    video.src = SRC;
  }} else if (window.Hls && Hls.isSupported()) {{
    hls = new Hls({{ enableWorker: true, lowLatencyMode: false }});
    hls.loadSource(SRC);
    hls.attachMedia(video);
    hls.on(Hls.Events.ERROR, function (_, data) {{ if (data && data.fatal) fail(); }});
  }} else {{
    video.src = SRC;
  }}

  video.addEventListener('loadedmetadata', function () {{ post('OPLAY_EMBED_READY'); progress(); }});
  video.addEventListener('timeupdate', progress);
  video.addEventListener('play', progress);
  video.addEventListener('pause', progress);
  video.addEventListener('ended', progress);
  video.addEventListener('error', fail);
  window.setInterval(progress, 1000);

  window.addEventListener('message', function (event) {{
    var d = event.data || {{}};
    if (d.type !== 'OPLAY_EMBED_PLAYBACK') return;
    switch (d.command) {{
      case 'handshake': post('OPLAY_EMBED_READY'); progress(); break;
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


async def _resolve_proxy_url(request: Request, slug: str, media_type: str, media_id: str,
                             season: str | None, episode: str | None) -> tuple[str, str, bool]:
    config = get_config(slug)
    if not config:
        raise HTTPException(status_code=404, detail=f"Unknown embed host: {slug}")

    embed_url = build_embed_url(config, media_type, media_id, season, episode)
    proxy_headers = get_proxy_headers(request)

    try:
        extractor = ExtractorFactory.get_extractor(config.canonical, proxy_headers.request)
        result = await extractor.extract(embed_url)
    except ExtractorError as exc:
        logger.warning("[embed/%s] extraction failed: %s", slug, exc)
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        logger.exception("[embed/%s] unexpected extraction error", slug)
        raise HTTPException(status_code=502, detail=f"Extraction failed: {exc}")

    endpoint = result.pop("mediaflow_endpoint")
    proxy_base = str(
        request.url_for(endpoint).replace(scheme=get_original_scheme(request))
    )

    query_params = result.get("query_params", {})
    query_params["api_password"] = settings.api_password or ""
    if result.pop("force_playlist_proxy", False):
        query_params["force_playlist_proxy"] = "1"

    encryption_handler = EncryptionHandler(settings.api_password) if settings.api_password else None
    stream_url = encode_mediaflow_proxy_url(
        mediaflow_proxy_url=proxy_base,
        destination_url=result["destination_url"],
        query_params=query_params,
        request_headers=result.get("request_headers"),
        response_headers=proxy_headers.response,
        encryption_handler=encryption_handler,
        expiration=result.get("expiration", 300),
    )
    autoplay = request.query_params.get("autoplay", "").lower() in ("1", "true", "yes")
    return stream_url, f"{config.canonical} — {media_id}", autoplay


@embed_router.get("/{slug}/movie/{media_id}", response_class=HTMLResponse)
async def embed_movie(request: Request, slug: str, media_id: str):
    stream_url, title, autoplay = await _resolve_proxy_url(
        request, slug, "movie", media_id, None, None
    )
    return HTMLResponse(
        _player_html(stream_url, title, autoplay),
        headers={"Cache-Control": "no-store"},
    )


@embed_router.get("/{slug}/tv/{media_id}/{season}/{episode}", response_class=HTMLResponse)
async def embed_tv(request: Request, slug: str, media_id: str, season: str, episode: str):
    stream_url, title, autoplay = await _resolve_proxy_url(
        request, slug, "tv", media_id, season, episode
    )
    return HTMLResponse(
        _player_html(stream_url, title, autoplay),
        headers={"Cache-Control": "no-store"},
    )
