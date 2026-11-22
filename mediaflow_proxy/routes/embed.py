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
import re
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
# Indonesian first, English as the fallback. Everything else is only offered
# when the user explicitly selects it.
PLAYER_LANGUAGE_PRIORITY = [
    "id",
    "id-id",
    "ind",
    "indonesian",
    "in",
    "en",
    "en-us",
    "en-gb",
    "eng",
    "english",
]

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
    sources: list[dict] | None = None,
    subtitles: list[dict] | None = None,
    capabilities: dict | None = None,
) -> str:
    """Small HLS player implementing the OPLAY embed postMessage contract.

    All controls live inside this iframe: StreamFlix only renders the iframe
    and keeps its existing postMessage progress handling.
    """
    option_markup = "".join(
        '<option value="{url}"{selected}>{name}</option>'.format(
            url=html.escape(option["url"], quote=True),
            selected=" selected" if option["active"] else "",
            name=html.escape(option["name"]),
        )
        for option in server_options
    )

    template = """<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>__MFP_TITLE__</title>
<style>
html,body{margin:0;height:100%;background:#000;overflow:hidden}
video{width:100%;height:100%;display:block;object-fit:contain;background:#000}
#err{position:absolute;inset:0;display:none;align-items:center;justify-content:center;
      color:#bbb;font:14px/1.5 system-ui,-apple-system,Arial,sans-serif;text-align:center;padding:24px}
#panel{position:absolute;bottom:52px;right:8px;z-index:2;display:flex;flex-direction:row;
       flex-wrap:wrap;gap:4px;align-items:center;justify-content:flex-end;max-width:calc(100% - 16px)}
#panel select{max-width:190px;color:#fff;background:rgba(0,0,0,.68);
       border:1px solid rgba(255,255,255,.25);border-radius:6px;
       font:12px/1.4 system-ui,-apple-system,Arial,sans-serif;padding:4px 6px}
</style>
<script src="__MFP_HLS_JS__"></script>
</head>
<body>
<video id="video" playsinline webkit-playsinline controls autoplay="__MFP_AUTOPLAY_ATTR__"></video>
<div id="panel">
<select id="server" aria-label="Stream server">__MFP_OPTIONS__</select>
<select id="quality" aria-label="Video quality" hidden></select>
<select id="subtitle" aria-label="Subtitle" hidden></select>
<select id="audio" aria-label="Audio" hidden></select>
<select id="speed" aria-label="Playback speed" hidden></select>
</div>
<div id="err">Stream unavailable</div>
<script>
(function () {
  var SRC = __MFP_SRC__;
  var SOURCES = __MFP_SOURCES__;
  var SUBTITLES = __MFP_SUBTITLES__;
  var CAPS = __MFP_CAPS__;
  var PREFERRED = __MFP_PREFERRED__;
  var AUTOPLAY = __MFP_AUTOPLAY__;
  var SELECTED = __MFP_SELECTED__;
  var video = document.getElementById('video');
  var err = document.getElementById('err');
  var serverSel = document.getElementById('server');
  var qualitySel = document.getElementById('quality');
  var subtitleSel = document.getElementById('subtitle');
  var audioSel = document.getElementById('audio');
  var speedSel = document.getElementById('speed');
  var hls = null;
  var failed = false;
  var sourceIndex = 0;
  var externalTracks = [];
  var tracks = [];
  var activeSubtitle = -1;

  function post(type, extra) {
    var msg = { type: type };
    if (extra) Object.keys(extra).forEach(function (k) { msg[k] = extra[k]; });
    try { window.parent.postMessage(msg, '*'); } catch (e) {}
  }
  function progress() {
    if (!isFinite(video.duration) || video.duration <= 0) return;
    post('OPLAY_EMBED_PROGRESS', {
      currentTime: video.currentTime,
      duration: video.duration,
      isPlaying: !video.paused
    });
  }
  function fail(message) {
    if (failed) return;
    failed = true;
    err.style.display = 'flex';
    err.textContent = message || 'Stream unavailable';
    post('OPLAY_EMBED_ERROR', { message: err.textContent });
  }
  function resetSelect(select) {
    while (select.firstChild) select.removeChild(select.firstChild);
  }
  function addOption(select, value, label) {
    var option = document.createElement('option');
    option.value = value;
    option.textContent = label;
    select.appendChild(option);
  }
  function langRank(value) {
    var v = String(value || '').toLowerCase().trim();
    for (var i = 0; i < PREFERRED.length; i++) {
      var key = PREFERRED[i];
      if (v === key || v.indexOf(key + '-') === 0 || v.indexOf(key + ' ') === 0) {
        return i < 5 ? 0 : 1;
      }
    }
    if (v.indexOf('indonesia') >= 0) return 0;
    if (v.indexOf('english') >= 0 || v.indexOf('inggris') >= 0) return 1;
    return 2;
  }
  function initExternalSubtitles() {
    if (!CAPS.subtitles_from_api) return;
    SUBTITLES.forEach(function (s, i) {
      var el = document.createElement('track');
      el.kind = 'subtitles';
      el.label = s.label || s.language || ('Subtitle ' + (i + 1));
      el.srclang = s.language || '';
      el.src = s.url;
      el.default = false;
      video.appendChild(el);
      externalTracks.push({
        kind: 'vtt',
        id: s.id || ('vtt-' + i),
        label: el.label,
        language: s.language || '',
        rank: langRank(s.language || s.label),
        el: el
      });
    });
  }
  function applySubtitle(index) {
    activeSubtitle = index;
    tracks.forEach(function (t, i) {
      if (t.kind === 'vtt') t.el.mode = (i === index) ? 'showing' : 'disabled';
    });
    var hlsIndex = -1;
    if (index >= 0 && tracks[index] && tracks[index].kind === 'hls') {
      hlsIndex = tracks[index].index;
    }
    if (hls && hls.subtitleTrack !== hlsIndex) {
      try { hls.subtitleTrack = hlsIndex; } catch (e) {}
    }
    subtitleSel.value = index < 0 ? 'off' : String(index);
    if (index < 0) post('OPLAY_EMBED_SUBTITLE', { id: 'off', label: 'Off' });
    else post('OPLAY_EMBED_SUBTITLE', {
      id: tracks[index].id,
      label: tracks[index].label,
      language: tracks[index].language
    });
  }
  function refreshTracks() {
    tracks = externalTracks.slice();
    if (hls && hls.subtitleTracks) {
      hls.subtitleTracks.forEach(function (t, i) {
        var label = t.name || t.lang || ('Track ' + (i + 1));
        tracks.push({
          kind: 'hls',
          id: 'hls-' + i,
          label: label,
          language: t.lang || '',
          rank: langRank(t.lang || label),
          index: i
        });
      });
    }
    tracks.sort(function (a, b) { return a.rank - b.rank; });
    resetSelect(subtitleSel);
    addOption(subtitleSel, 'off', 'Off');
    tracks.forEach(function (t, i) { addOption(subtitleSel, String(i), t.label); });
    subtitleSel.hidden = tracks.length === 0;
    // Indonesian is preferred; English is used only when no Indonesian track
    // exists. Anything else stays Off until the viewer selects it.
    applySubtitle(tracks.length && tracks[0].rank <= 1 ? 0 : -1);
    postTracks();
  }
  function renderQualityOptions() {
    resetSelect(qualitySel);
    addOption(qualitySel, 'auto', 'Auto');
    if (CAPS.quality_from_api && SOURCES.length > 1) {
      SOURCES.forEach(function (s, i) {
        addOption(qualitySel, 'src:' + i, s.label || ('Source ' + (i + 1)));
      });
      qualitySel.hidden = false;
    } else if (hls && hls.levels && hls.levels.length > 1) {
      hls.levels.forEach(function (level, i) {
        var label = level.height ? (level.height + 'p') : (level.name || ('Level ' + (i + 1)));
        addOption(qualitySel, 'lvl:' + i, label);
      });
      qualitySel.hidden = false;
    } else {
      qualitySel.hidden = true;
    }
    qualitySel.value = CAPS.quality_from_api && SOURCES.length > 1 ? ('src:' + sourceIndex) : 'auto';
  }
  function renderAudioOptions() {
    var list = (hls && hls.audioTracks) || [];
    if (list.length <= 1) { audioSel.hidden = true; return; }
    resetSelect(audioSel);
    addOption(audioSel, 'auto', 'Auto');
    list.forEach(function (t, i) {
      addOption(audioSel, String(i), t.name || t.lang || ('Audio ' + (i + 1)));
    });
    audioSel.hidden = false;
  }
  function setupSpeed() {
    resetSelect(speedSel);
    ['0.75', '1', '1.25', '1.5', '2'].forEach(function (value) {
      addOption(speedSel, value, value + 'x');
    });
    speedSel.value = '1';
    speedSel.hidden = false;
  }
  function postTracks() {
    var qualities = [];
    if (CAPS.quality_from_api && SOURCES.length > 1) {
      qualities = SOURCES.map(function (s, i) {
        return { id: s.id || ('src-' + i), label: s.label };
      });
    } else if (hls && hls.levels) {
      qualities = hls.levels.map(function (level, i) {
        return {
          id: 'lvl-' + i,
          label: level.height ? (level.height + 'p') : (level.name || ('Level ' + (i + 1)))
        };
      });
    }
    var audio = ((hls && hls.audioTracks) || []).map(function (t, i) {
      return { id: 'audio-' + i, label: t.name || t.lang || ('Audio ' + (i + 1)) };
    });
    post('OPLAY_EMBED_TRACKS', {
      qualities: qualities,
      subtitles: tracks.map(function (t) {
        return { id: t.id, label: t.label, language: t.language };
      }),
      audio: audio
    });
  }
  function resume(at) {
    if (!at) return;
    var once = function () {
      try { video.currentTime = at; } catch (e) {}
      video.removeEventListener('loadedmetadata', once);
    };
    video.addEventListener('loadedmetadata', once);
  }
  function loadStream(url, at) {
    failed = false;
    err.style.display = 'none';
    if (AUTOPLAY) video.muted = true;
    if (hls) { try { hls.destroy(); } catch (e) {} hls = null; }
    tracks = externalTracks.slice();
    resume(at);
    if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = url;
      renderQualityOptions();
      renderAudioOptions();
      refreshTracks();
      if (AUTOPLAY) video.play().catch(function () {});
    } else if (window.Hls && Hls.isSupported()) {
      hls = new Hls({ enableWorker: true, lowLatencyMode: false });
      hls.loadSource(url);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, function () {
        renderQualityOptions();
        renderAudioOptions();
        refreshTracks();
        if (AUTOPLAY) video.play().catch(function () {});
      });
      hls.on(Hls.Events.ERROR, function (_, data) {
        if (data && data.fatal) fail('Playback error: ' + (data.type || 'unknown'));
      });
    } else {
      video.src = url;
      renderQualityOptions();
      renderAudioOptions();
      refreshTracks();
      if (AUTOPLAY) video.play().catch(function () {});
    }
  }

  serverSel.addEventListener('change', function () {
    if (serverSel.value) window.location.href = serverSel.value;
  });
  qualitySel.addEventListener('change', function () {
    var value = qualitySel.value;
    if (value.indexOf('src:') === 0) {
      sourceIndex = parseInt(value.slice(4), 10);
      var at = video.currentTime;
      loadStream(SOURCES[sourceIndex].url, at);
      post('OPLAY_EMBED_QUALITY', {
        id: SOURCES[sourceIndex].id,
        label: SOURCES[sourceIndex].label
      });
    } else if (value.indexOf('lvl:') === 0) {
      var level = parseInt(value.slice(4), 10);
      if (hls) hls.currentLevel = level;
      post('OPLAY_EMBED_QUALITY', { id: value, label: qualitySel.options[qualitySel.selectedIndex].text });
    } else {
      if (hls) hls.currentLevel = -1;
      post('OPLAY_EMBED_QUALITY', { id: 'auto', label: 'Auto' });
    }
  });
  subtitleSel.addEventListener('change', function () {
    applySubtitle(subtitleSel.value === 'off' ? -1 : parseInt(subtitleSel.value, 10));
  });
  audioSel.addEventListener('change', function () {
    var value = audioSel.value;
    if (hls) hls.audioTrack = value === 'auto' ? -1 : parseInt(value, 10);
  });
  speedSel.addEventListener('change', function () {
    video.playbackRate = parseFloat(speedSel.value) || 1;
  });

  video.addEventListener('loadedmetadata', function () {
    post('OPLAY_EMBED_SERVER', SELECTED);
    post('OPLAY_EMBED_READY');
    progress();
  });
  video.addEventListener('timeupdate', progress);
  video.addEventListener('play', progress);
  video.addEventListener('pause', progress);
  video.addEventListener('ended', progress);
  video.addEventListener('error', function () { fail('Stream unavailable'); });
  window.setInterval(progress, 1000);
  post('OPLAY_EMBED_SERVER', SELECTED);

  window.addEventListener('message', function (event) {
    var d = event.data || {};
    if (d.type !== 'OPLAY_EMBED_PLAYBACK') return;
    switch (d.command) {
      case 'handshake':
        post('OPLAY_EMBED_SERVER', SELECTED);
        post('OPLAY_EMBED_READY');
        progress();
        break;
      case 'play': video.play().catch(function () {}); break;
      case 'pause': video.pause(); break;
      case 'seek':
        if (typeof d.time === 'number' && isFinite(d.time)) video.currentTime = d.time;
        break;
      case 'requestProgress': progress(); break;
    }
  });

  initExternalSubtitles();
  setupSpeed();
  loadStream(SRC, 0);
})();
</script>
</body>
</html>"""

    replacements = {
        "__MFP_TITLE__": html.escape(title),
        "__MFP_HLS_JS__": HLS_JS_URL,
        "__MFP_AUTOPLAY_ATTR__": "true" if autoplay else "false",
        "__MFP_OPTIONS__": option_markup,
        "__MFP_SRC__": _js_string(stream_url),
        "__MFP_SOURCES__": _js_json(sources or []),
        "__MFP_SUBTITLES__": _js_json(subtitles or []),
        "__MFP_CAPS__": _js_json(capabilities or {}),
        "__MFP_PREFERRED__": _js_json(PLAYER_LANGUAGE_PRIORITY),
        "__MFP_AUTOPLAY__": json.dumps(autoplay),
        "__MFP_SELECTED__": _js_json(selected),
    }
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    return template


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


def _sign_asset_url(
    request: Request,
    result: dict,
    proxy_headers: ProxyRequestHeaders,
    destination_url: str,
    *,
    endpoint: str | None = None,
    force_playlist_proxy: bool | None = None,
    request_headers: dict | None = None,
) -> str:
    """Sign one gateway URL without mutating the extractor result.

    Every asset (playlist, quality variant, subtitle) goes through this helper
    so ``api_password`` and upstream headers stay inside the encrypted token
    and are never rendered into the player HTML.
    """
    endpoint = endpoint or result.get("mediaflow_endpoint") or "hls_manifest_proxy"
    if endpoint == "hls_segment_proxy":
        # url_for() with a path parameter can leave a placeholder in some
        # Starlette versions; derive the segment URL from the manifest route.
        proxy_base = str(
            request.url_for("hls_manifest_proxy").replace(
                scheme=get_original_scheme(request)
            )
        ).replace("/hls/manifest.m3u8", "/hls/segment.vtt")
    else:
        proxy_base = str(
            request.url_for(endpoint).replace(scheme=get_original_scheme(request))
        )

    query_params = dict(result.get("query_params") or {})
    query_params["api_password"] = settings.api_password or ""
    should_force = (
        result.get("force_playlist_proxy", False)
        if force_playlist_proxy is None
        else force_playlist_proxy
    )
    if should_force:
        query_params["force_playlist_proxy"] = "1"
    else:
        query_params.pop("force_playlist_proxy", None)

    encryption_handler = (
        EncryptionHandler(settings.api_password) if settings.api_password else None
    )
    return encode_mediaflow_proxy_url(
        mediaflow_proxy_url=proxy_base,
        destination_url=destination_url,
        query_params=query_params,
        request_headers=request_headers or result.get("request_headers"),
        response_headers=proxy_headers.response,
        encryption_handler=encryption_handler,
        expiration=result.get("expiration", 300),
    )


def _signed_metadata(
    request: Request, result: dict, proxy_headers: ProxyRequestHeaders
) -> tuple[list[dict], list[dict], dict]:
    """Normalise and sign API-provided quality/subtitle metadata for the player."""
    sources: list[dict] = []
    for source in result.get("sources") or []:
        url = source.get("url")
        if not url:
            continue
        sources.append(
            {
                "id": source.get("id") or _quality_id(source.get("label")),
                "label": source.get("label") or "Auto",
                "url": _sign_asset_url(
                    request,
                    result,
                    proxy_headers,
                    url,
                    request_headers=source.get("request_headers"),
                    force_playlist_proxy=True,
                ),
            }
        )

    subtitles: list[dict] = []
    for subtitle in result.get("subtitles") or []:
        url = subtitle.get("url")
        if not url:
            continue
        subtitles.append(
            {
                "id": subtitle.get("id") or f"sub-{len(subtitles)}",
                "label": subtitle.get("label") or subtitle.get("language") or "Subtitle",
                "language": subtitle.get("language") or "",
                "format": subtitle.get("format") or "vtt",
                "url": _sign_asset_url(
                    request,
                    result,
                    proxy_headers,
                    url,
                    endpoint="hls_segment_proxy",
                    request_headers=subtitle.get("request_headers"),
                    force_playlist_proxy=False,
                ),
            }
        )

    capabilities = dict(result.get("capabilities") or {})
    return sources, subtitles, capabilities


def _quality_id(label) -> str:
    """Small fallback id helper for sources created outside the extractor."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(label or "auto").lower()).strip("-")
    return slug or "auto"


async def _resolve_proxy_url(
    request: Request,
    slug: str,
    media_type: str,
    media_id: str,
    season: str | None,
    episode: str | None,
) -> tuple[str, str, bool, str, list[dict], list[dict], dict]:
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
            stream_url = _sign_asset_url(
                request, result, proxy_headers, result["destination_url"]
            )
            sources, subtitles, capabilities = _signed_metadata(
                request, result, proxy_headers
            )
        except Exception as exc:
            logger.warning("[embed/%s] %s failed: %s", slug, config.canonical, exc)
            if auto:
                await _cache_failure(key)
            errors.append(f"{config.canonical}: {exc}")
            continue

        logger.info("[embed/%s] selected %s for %s %s", slug, config.canonical, media_type, media_id)
        return (
            stream_url,
            f"{config.canonical} — {media_id}",
            autoplay,
            config.slug,
            sources,
            subtitles,
            capabilities,
        )

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
    sources: list[dict] | None = None,
    subtitles: list[dict] | None = None,
    capabilities: dict | None = None,
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
        _player_html(
            stream_url,
            title,
            autoplay,
            selected,
            options,
            sources=sources,
            subtitles=subtitles,
            capabilities=capabilities,
        ),
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
    (
        stream_url,
        title,
        autoplay,
        selected_slug,
        sources,
        subtitles,
        capabilities,
    ) = await _resolve_proxy_url(request, slug, "movie", media_id, None, None)
    return _render(
        request,
        stream_url,
        title,
        autoplay,
        selected_slug,
        "movie",
        media_id,
        None,
        None,
        sources,
        subtitles,
        capabilities,
    )


@embed_router.get("/{slug}/tv/{media_id}/{season}/{episode}", response_class=HTMLResponse)
async def embed_tv(request: Request, slug: str, media_id: str, season: str, episode: str):
    (
        stream_url,
        title,
        autoplay,
        selected_slug,
        sources,
        subtitles,
        capabilities,
    ) = await _resolve_proxy_url(request, slug, "tv", media_id, season, episode)
    return _render(
        request,
        stream_url,
        title,
        autoplay,
        selected_slug,
        "tv",
        media_id,
        season,
        episode,
        sources,
        subtitles,
        capabilities,
    )
