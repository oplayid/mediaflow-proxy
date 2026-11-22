"""Gateway/relay player for StreamFlix-style embed requests.

StreamFlix owns its own URL scheme (``/embed/auto/tv/<id>/<s>/<e>``). Each
external provider has a different scheme. This module translates the canonical
MediaFlow request into the provider's embed URL using ``EMBED_HOSTS`` and serves
the provider's own player inside an iframe, so browser-side playback works even
when server-side HLS extraction cannot (for example WASM-encrypted streams).

All navigation stays under ``mfp.oplay.id``: StreamFlix never sees a provider
URL, and no ``api_password`` is involved in this mode.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from mediaflow_proxy.extractors.embed_hosts import (
    AUTO_HOST_ORDER,
    FLOW_VIDSPARK,
    FLOW_VSEMBED,
    build_embed_url,
    get_config,
)
from mediaflow_proxy.routes.embed import _normalise_media, _origin
from mediaflow_proxy.utils.http_client import create_aiohttp_session

logger = logging.getLogger(__name__)
relay_router = APIRouter()

# Hosts that are actually embeddable pages are preferred. API-only entries are
# still listed in the switcher, but they are not used for automatic selection.
AUTO_RELAY_CANDIDATES = ("VidCore", "VSEmbed", "VidKing", "MultiEmbed")
RELAY_PROBE_TIMEOUT = 3.0


def _relay_path(
    slug: str,
    media_type: str,
    media_id: str,
    season: str | None,
    episode: str | None,
    autoplay: bool,
) -> str:
    mid = quote(media_id, safe="")
    if media_type == "tv":
        path = (
            f"/embed/relay/{slug}/tv/{mid}"
            f"/{quote(season or '1', safe='')}/{quote(episode or '1', safe='')}"
        )
    else:
        path = f"/embed/relay/{slug}/movie/{mid}"
    return f"{path}?autoplay=true" if autoplay else path


def _options(
    request: Request,
    media_type: str,
    media_id: str,
    season: str | None,
    episode: str | None,
    autoplay: bool,
    selected_slug: str,
    resolved_host_url: str,
) -> list[dict]:
    """Build the server switcher.

    ``url`` is the relay URL (navigation stays on mfp.oplay.id), while
    ``hostUrl`` is the translated provider embed URL loaded by the nested
    iframe. Keeping them separate prevents the relay from loading itself.
    """
    options: list[dict] = [
        {"id": "auto", "name": "Auto", "hostUrl": resolved_host_url},
    ]
    for canonical in AUTO_HOST_ORDER:
        config = get_config(canonical)
        if not config:
            continue
        options.append(
            {
                "id": config.slug,
                "name": config.canonical,
                "hostUrl": build_embed_url(config, media_type, media_id, season, episode),
            }
        )
    for option in options:
        option["url"] = (
            f"{_origin(request)}"
            f"{_relay_path(option['id'], media_type, media_id, season, episode, autoplay)}"
        )
        option["selected"] = option["id"] == selected_slug
    return options


def _relay_html(
    title: str,
    host_url: str,
    options: list[dict],
    season: str,
    episode: str,
    autoplay: bool,
    error: str | None = None,
) -> HTMLResponse:
    option_markup = "".join(
        '<option value="{url}"{selected}>{name}</option>'.format(
            url=html.escape(option["url"], quote=True),
            selected=" selected" if option["selected"] else "",
            name=html.escape(option["name"]),
        )
        for option in options
    )
    template = """<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>__MFP_TITLE__</title>
<style>
html,body{margin:0;height:100%;background:#000;overflow:hidden}
#host{border:0;width:100%;height:100%;display:block}
#panel{position:absolute;bottom:52px;right:8px;z-index:3;display:flex;flex-wrap:wrap;
       gap:4px;align-items:center;justify-content:flex-end;max-width:calc(100% - 16px)}
#panel select{max-width:190px;color:#fff;background:rgba(0,0,0,.7);
       border:1px solid rgba(255,255,255,.25);border-radius:6px;
       font:12px/1.4 system-ui,-apple-system,Arial,sans-serif;padding:4px 6px}
#err{position:absolute;inset:0;z-index:4;display:none;flex-direction:column;
       align-items:center;justify-content:center;gap:10px;background:rgba(0,0,0,.86);
       color:#ddd;font:14px/1.5 system-ui,-apple-system,Arial,sans-serif;text-align:center;padding:24px}
#err button{color:#fff;background:#222;border:1px solid rgba(255,255,255,.35);
       border-radius:6px;padding:7px 14px;cursor:pointer}
</style>
</head>
<body>
<iframe id="host" src="__MFP_HOST_URL__"
        allow="autoplay; encrypted-media; fullscreen; picture-in-picture"
        allowfullscreen referrerpolicy="strict-origin-when-cross-origin"></iframe>
<div id="panel"><select id="server" aria-label="Stream server">__MFP_OPTIONS__</select></div>
<div id="err"><span id="errmsg"></span><button id="next">Coba host berikutnya</button></div>
<script>
(function () {
  var OPTIONS = __MFP_OPTIONS_JSON__;
  var INDEX = __MFP_INDEX__;
  var MEDIA = __MFP_MEDIA__;
  var AUTOPLAY = __MFP_AUTOPLAY__;
  var INITIAL_ERROR = __MFP_ERROR_JSON__;
  var frame = document.getElementById('host');
  var serverSel = document.getElementById('server');
  var err = document.getElementById('err');
  var errmsg = document.getElementById('errmsg');
  var nextBtn = document.getElementById('next');
  var ready = false;

  function post(type, extra) {
    var msg = { type: type };
    if (extra) Object.keys(extra).forEach(function (k) {
      if (k !== 'type') msg[k] = extra[k];
    });
    try { window.parent.postMessage(msg, '*'); } catch (e) {}
  }
  function serverInfo() {
    return { id: OPTIONS[INDEX].id, name: OPTIONS[INDEX].name };
  }
  function load(index) {
    if (index < 0 || index >= OPTIONS.length) {
      errmsg.textContent = 'Semua host tidak menyediakan episode ini.';
      err.style.display = 'flex';
      post('OPLAY_EMBED_ERROR', { message: errmsg.textContent });
      return;
    }
    INDEX = index;
    ready = false;
    err.style.display = 'none';
    serverSel.value = OPTIONS[index].url;
    // Load the *provider* embed here. OPTIONS[index].url is the relay page
    // itself and must never be used as the nested iframe src.
    var target = OPTIONS[index].hostUrl;
    if (target && frame.src !== target) frame.src = target;
    post('OPLAY_EMBED_SERVER', serverInfo());
  }
  function nextHost() { load(INDEX + 1); }

  // Switching servers navigates the relay page, so the address bar, refresh
  // and history all keep pointing at mfp.oplay.id (never at a provider).
  serverSel.addEventListener('change', function () {
    var target = serverSel.value;
    if (target && target !== window.location.href) window.location.href = target;
  });
  nextBtn.addEventListener('click', nextHost);

  // Messages from the provider player are mirrored to StreamFlix. Provider
  // protocols (VSEmbed uses PLAYER_TITLE/TV_INFO/TV_STATE/MEDIA_NOT_FOUND) are
  // kept intact; only the StreamFlix-facing contract events are synthesised.
  window.addEventListener('message', function (event) {
    if (event.source !== frame.contentWindow) return;
    var d = event.data || {};
    if (!d.type) return;
    if (d.type.indexOf('OPLAY_EMBED_') === 0) return;
    if (d.type === 'MEDIA_NOT_FOUND') {
      errmsg.textContent = 'Media tidak tersedia di ' + OPTIONS[INDEX].name + '.';
      err.style.display = 'flex';
      post('OPLAY_EMBED_ERROR', { message: errmsg.textContent });
      return;
    }
    if (d.type === 'PLAYER_TITLE' || d.type === 'TV_INFO' || d.type === 'TV_STATE') {
      if (!ready) {
        ready = true;
        post('OPLAY_EMBED_READY');
      }
      if (d.type === 'TV_STATE') {
        post('OPLAY_EMBED_EPISODE', {
          season: d.season || MEDIA.season,
          episode: d.episode || MEDIA.episode
        });
      }
    }
    if (typeof d.currentTime === 'number' && typeof d.duration === 'number') {
      post('OPLAY_EMBED_PROGRESS', {
        currentTime: d.currentTime,
        duration: d.duration,
        isPlaying: !!d.isPlaying
      });
    }
    post(d.type, d);
  });

  // StreamFlix commands are forwarded down into the provider embed. VSEmbed
  // understands TV_SET, so keep its season/episode in sync with StreamFlix.
  window.addEventListener('message', function (event) {
    if (event.source !== window.parent) return;
    var d = event.data || {};
    if (d.type !== 'OPLAY_EMBED_PLAYBACK') return;
    try { frame.contentWindow.postMessage(d, '*'); } catch (e) {}
    if (d.command === 'handshake') {
      try {
        frame.contentWindow.postMessage({
          type: 'TV_SET', season: +MEDIA.season, episode: +MEDIA.episode
        }, '*');
      } catch (e) {}
      post('OPLAY_EMBED_SERVER', serverInfo());
      post('OPLAY_EMBED_READY');
    }
  });

  load(INDEX);
  if (INITIAL_ERROR) {
    errmsg.textContent = INITIAL_ERROR;
    err.style.display = 'flex';
    post('OPLAY_EMBED_ERROR', { message: INITIAL_ERROR });
  }
})();
</script>
</body>
</html>"""
    replacements = {
        "__MFP_TITLE__": html.escape(title),
        "__MFP_HOST_URL__": html.escape(host_url, quote=True),
        "__MFP_OPTIONS__": option_markup,
        "__MFP_OPTIONS_JSON__": _js_json(options),
        "__MFP_INDEX__": str(next(i for i, o in enumerate(options) if o["selected"])),
        "__MFP_MEDIA__": _js_json({"season": int(season), "episode": int(episode)}),
        "__MFP_AUTOPLAY__": "true" if autoplay else "false",
        "__MFP_ERROR_JSON__": _js_json(error) if error else "null",
    }
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    return HTMLResponse(template, headers={"Cache-Control": "no-store"})


def _js_json(value) -> str:
    """JSON-encode a value for safe embedding inside an inline <script>."""
    return json.dumps(value).replace("<", "\\u003c")


async def _relay_reachable(config, media_type: str, media_id: str, season: str, episode: str) -> bool:
    """Only check that the provider's embed page responds."""
    url = build_embed_url(config, media_type, media_id, season, episode)
    try:
        async with create_aiohttp_session(url, timeout=RELAY_PROBE_TIMEOUT) as (session, proxy_url):
            async with session.head(url, proxy=proxy_url, allow_redirects=True) as response:
                return response.status < 400 or response.status == 405
    except Exception as exc:
        logger.debug("[relay] %s reachability probe failed: %s", config.canonical, exc)
        return False


async def _relay_has_episode(config, media_type: str, media_id: str, season: str, episode: str) -> bool:
    """Ask the provider whether it really has this exact movie/episode."""
    if config.flow == FLOW_VIDSPARK and config.api_base:
        path = (
            f"/tv/{media_id}/{season}/{episode}"
            if media_type == "tv"
            else f"/movie/{media_id}"
        )
        api_url = f"{config.api_base.rstrip('/')}{path}"
        try:
            async with create_aiohttp_session(
                api_url, timeout=RELAY_PROBE_TIMEOUT, headers=config.api_headers or None
            ) as (session, proxy_url):
                async with session.get(api_url, proxy=proxy_url) as response:
                    payload = await response.json(content_type=None)
            return bool(
                isinstance(payload, dict)
                and payload.get("result")
                and payload.get("sources")
            )
        except Exception as exc:
            logger.debug("[relay] %s episode probe failed: %s", config.canonical, exc)
            return False

    if config.flow == FLOW_VSEMBED:
        origin = f"https://{config.domains[0]}"
        gate_url = f"{origin}/vs_src.php?type={media_type}&id={media_id}"
        try:
            async with create_aiohttp_session(
                gate_url, timeout=RELAY_PROBE_TIMEOUT
            ) as (session, proxy_url):
                async with session.get(
                    gate_url,
                    proxy=proxy_url,
                    headers={
                        "Referer": build_embed_url(config, media_type, media_id, season, episode),
                        "Accept": "application/json",
                    },
                ) as response:
                    payload = await response.json(content_type=None)
            return bool(isinstance(payload, dict) and payload.get("src"))
        except Exception as exc:
            logger.debug("[relay] %s gate probe failed: %s", config.canonical, exc)
            return False

    return True


async def _pick_auto_config(media_type: str, media_id: str, season: str, episode: str):
    """Pick a provider that is reachable *and* actually serves this episode.

    A plain HTTP probe is not enough: VidCore's embed page always answers 200,
    even when VidSpark's API answers ``Episode not found``. VSEmbed has a gate
    endpoint that only returns a player URL for media it can serve. Providers
    without such metadata fall back to the reachability check.
    """
    configs = [get_config(name) for name in AUTO_RELAY_CANDIDATES]
    configs = [config for config in configs if config]

    async def check(config):
        online = await _relay_reachable(config, media_type, media_id, season, episode)
        available = (
            await _relay_has_episode(config, media_type, media_id, season, episode)
            if online
            else False
        )
        return config, online, available

    if configs:
        results = await asyncio.gather(*(check(config) for config in configs))
        for config, online, available in results:
            if online and available:
                logger.info(
                    "[relay] auto selected %s for %s/%s/%s/%s",
                    config.canonical, media_type, media_id, season, episode,
                )
                return config
        # Nothing confirmed the episode exists: still render a usable switcher
        # with the first reachable provider rather than a hard 502.
        for config, online, _available in results:
            if online:
                return config
    return configs[0] if configs else get_config("VidCore")


async def relay_response(
    request: Request,
    slug: str,
    media_type: str,
    media_id: str,
    season: str | None,
    episode: str | None,
) -> HTMLResponse:
    """Translate a canonical MediaFlow request into a provider embed player."""
    media_id, season, episode = _normalise_media(media_type, media_id, season, episode)
    autoplay = request.query_params.get("autoplay", "").lower() in ("1", "true", "yes")
    config = get_config(slug)
    if slug == "auto":
        config = await _pick_auto_config(media_type, media_id, season, episode)
        if not config:
            raise HTTPException(status_code=502, detail="No relay host available")
    elif not config:
        raise HTTPException(status_code=404, detail=f"Unknown embed host: {slug}")

    # Always mark the resolved provider by slug, so explicit routes work with
    # canonical names, slugs or domain aliases, and the auto route highlights
    # the provider actually chosen by the probe.
    selected_slug = config.slug
    host_url = build_embed_url(config, media_type, media_id, season, episode)

    # For a manually selected host, tell the user immediately when upstream
    # reports that this exact episode does not exist. The auto route already
    # skipped such hosts during probing.
    error = None
    if slug != "auto" and not await _relay_has_episode(
        config, media_type, media_id, season, episode
    ):
        error = f"{config.canonical} tidak menyediakan episode ini."

    return _relay_html(
        f"{config.canonical} — {media_id}",
        host_url,
        _options(request, media_type, media_id, season, episode, autoplay, selected_slug, host_url),
        season or "1",
        episode or "1",
        autoplay,
        error,
    )


@relay_router.get("/hosts")
async def relay_hosts():
    """Debug JSON of the translation table used by the relay player."""
    return [
        {
            "id": config.slug,
            "name": config.canonical,
            "movie": config.movie_url,
            "tv": config.tv_url,
        }
        for config in (get_config(name) for name in AUTO_HOST_ORDER)
        if config
    ]


@relay_router.get("/{slug}/movie/{media_id}", response_class=HTMLResponse)
async def relay_movie(request: Request, slug: str, media_id: str):
    return await relay_response(request, slug, "movie", media_id, None, None)


@relay_router.get("/{slug}/tv/{media_id}", response_class=HTMLResponse)
async def relay_tv(request: Request, slug: str, media_id: str):
    return await relay_response(request, slug, "tv", media_id, None, None)


@relay_router.get("/{slug}/tv/{media_id}/{season}", response_class=HTMLResponse)
async def relay_tv_season(request: Request, slug: str, media_id: str, season: str):
    return await relay_response(request, slug, "tv", media_id, season, None)


@relay_router.get("/{slug}/tv/{media_id}/{season}/{episode}", response_class=HTMLResponse)
async def relay_tv_episode(
    request: Request, slug: str, media_id: str, season: str, episode: str
):
    return await relay_response(request, slug, "tv", media_id, season, episode)
