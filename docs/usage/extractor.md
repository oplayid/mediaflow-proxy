# Video extractor endpoint

## Video Extractor Endpoint

The extractor endpoint extracts direct video stream URLs from various video hosting services. It supports an optional file extension in the URL for better player compatibility.

### Supported extractors (`host` parameter)

Use these exact strings as the `host` query parameter. The canonical list is defined in [`mediaflow_proxy/extractors/factory.py`](https://github.com/mhdzumair/mediaflow-proxy/blob/main/mediaflow_proxy/extractors/factory.py) (`ExtractorFactory._extractors`) and in [`mediaflow_proxy/extractors/embed_hosts.py`](https://github.com/mhdzumair/mediaflow-proxy/blob/main/mediaflow_proxy/extractors/embed_hosts.py) (`EMBED_HOSTS`).

The matching is **case-insensitive** — `vidfast`, `VidFast`, and `VIDFAST` are all accepted.

| `host` value | Notes |
|---|---|
| `City` | |
| `Doodstream` | |
| `F16Px` | |
| `Fastream` | |
| `FileLions` | |
| `FileMoon` | |
| `Gupload` | |
| `LiveTV` | |
| `LuluStream` | |
| `Maxstream` | |
| `Mixdrop` | |
| `Okru` | ok.ru / odnoklassniki |
| `Sportsonline` | Sportsonline / Sportzonline live streams |
| `Streamtape` | |
| `StreamWish` | |
| `Supervideo` | |
| `TurboVidPlay` | |
| `Uqload` | |
| `Vavoo` | Vavoo.to streams |
| `VidFast` | vidfast.pro (ythd.org → cloudnestra.com chain) |
| `Vidmoly` | |
| `Vidoza` | |
| `VixCloud` | |
| `Voe` | |

### StreamFlix embed hosts

These hosts are also accepted as `host` values. They are handled by the config-driven `GenericEmbedExtractor`, and `d` must be the original embed page URL (not the final `.m3u8`). Both the canonical name and the short slug are accepted, case-insensitively, as are the registered domains.

| `host` value | Embed slug | Registered domains |
|---|---|---|
| `VSEmbed` | `vsembed` | `stream.oplay.id`, `vsembed.su`, `vsembed.ru`, `stellarconductornexus.com` |
| `VidCore` | `vidcore` | `vidcore.org`, `vidspark.to`, `paretocdn.site` |
| `VidLink` | `vidlink` | `vidlink.pro`, `www.vidlink.pro` |
| `VidKing` | `vidking` | `www.vidking.net`, `vidking.ws`, `www.vidking.ws` |
| `AutoEmbed` | `autoembed` | `player.autoembed.cc` |
| `TwoEmbed` | `2embed` | `www.2embed.cc`, `2embed.cc` |
| `PrimeWire` | `primewire` | `www.primewire.tf`, `primewire.tf` |
| `MultiEmbed` | `multiembed` | `multiembed.mov` |
| `VidEasy` | `videasy` | `player.videasy.net` |
| `SmashyStream` | `smashystream` | `embed.smashystream.com` |
| `PStream` | `pstream` | `iframe.pstream.org` |
| `VidSrcCC` | `vidsrccc` | `vidsrc.cc` |
| `VidSrcTo` | `vidsrcto` | `vidsrc.to` |
| `VidSrcRip` | `vidsrcrip` | `vidsrc.rip` |
| `VidSrcSu` | `vidsrcsu` | `vidsrc.su` |
| `VidSrcVip` | `vidsrcvip` | `vidsrc.vip` |
| `EmbedSu` | `embedsu` | `embed.su` |
| `Frembed` | `frembed` | `frembed.cc` |
| `MoviesAPI` | `moviesapi` | `moviesapi.club` |

Example:

```
GET /extractor/video.m3u8?host=vidcore&d=https://vidcore.org/embed/movie/550&api_password=your_password
```

### Endpoints

| Endpoint | Description |
|----------|-------------|
| `/extractor/video` | Base endpoint (generic, backward compatible) |
| `/extractor/video.m3u8` | HLS streams - helps ExoPlayer detect HLS |
| `/extractor/video.mp4` | MP4 streams |
| `/extractor/video.mkv` | MKV streams |
| `/extractor/video.ts` | MPEG-TS streams |
| `/extractor/video.webm` | WebM streams |
| `/extractor/video.avi` | AVI streams |

### Why Use Extensions?

Some video players (notably Android's ExoPlayer used in Stremio) determine the media source type from the URL before making any HTTP requests. Without the correct extension:

- ExoPlayer sees `/extractor/video?...` → Uses `ProgressiveMediaSource`
- ExoPlayer sees `/extractor/video.m3u8?...` → Uses `HlsMediaSource` ✓

For HLS streams, using the `.m3u8` extension ensures the player uses the correct HLS playback pipeline.

### Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `host` | Yes | Extractor host name (e.g., `TurboVidPlay`, `Vidoza`) |
| `d` | Yes | Destination URL (the video page URL to extract) |
| `api_password` | Yes* | API password (*if configured) |
| `redirect_stream` | No | If `true`, returns 302 redirect to the proxied stream URL |

### Example Usage

**Get extraction result as JSON:**
```
GET /extractor/video?host=Vidoza&d=https://videzz.net/example.html&api_password=your_password
```

**Redirect directly to stream (for players):**
```
GET /extractor/video.m3u8?host=TurboVidPlay&d=https://turbovidhls.com/t/abc123&api_password=your_password&redirect_stream=true
```

This redirects to the proxied HLS manifest URL, and because the request URL contains `.m3u8`, players like ExoPlayer will correctly use HLS playback.

## StreamFlix gateway and smart player

`/gateway/resolve/...` is kept for backwards compatibility, but Streamflix no longer plays the returned `stream_url`. The important field is `player_url`: an opaque, short-lived URL that Streamflix renders directly in an iframe. Playback, host selection, quality, subtitles, failover, and proxy URLs are fully owned by the player on `mfp.oplay.id`.

| Endpoint | Description |
|---|---|
| `/gateway/resolve/movie/<tmdb_id>` | Resolve a movie and return `player_url` |
| `/gateway/resolve/tv/<tmdb_id>` | Show, defaults to season 1 episode 1 |
| `/gateway/resolve/tv/<tmdb_id>/<season>` | Show, defaults to episode 1 |
| `/gateway/resolve/tv/<tmdb_id>/<season>/<episode>` | Exact TV episode |
| `/_token_<token>/player/<type>/<id>[/<season>/<episode>]` | Smart player iframe (tokenized, no `api_password`) |
| `/_token_<token>/player/<type>/<id>[/<season>[/<episode>]]/retry` | Re-run simulation with a clean cache |
| `/gateway/cache` | Debug-only cache statistics (no signed URLs) |

TV IDs accept an optional `tv-` prefix; invalid or zero season/episode values fall back to `1`. The compatibility gateway still resolves **VidCore, VSEmbed, then VidLink** (10 seconds per host, 40 seconds total). The player uses a different, broader order: **VSEmbed, VidCore, VidLink**, followed by every host registered in `EMBED_HOSTS`. Set `PLAYER_HOSTS=VidLink,VSEmbed,...` to override that order. Adding a new external embed host is therefore a registry entry plus an optional env change — the player needs no code change.

Example:

```
GET https://mfp.oplay.id/gateway/resolve/tv/94997/1/1
```

```json
{
  "host": "VidCore",
  "player_url": "https://mfp.oplay.id/_token_<encrypted>/player/tv/94997/1/1",
  "stream_url": "https://mfp.oplay.id/proxy/hls/manifest.m3u8?d=...&exp=...",
  "sources": [{"id": "auto", "label": "Auto", "url": "..."}],
  "subtitles": [
    {"id": "host-sub-id-0", "label": "Indonesian", "language": "id", "format": "vtt", "url": "...", "origin": "host"}
  ],
  "provider_subtitles": [
    {"id": "provider-sub-en-1", "label": "English", "language": "en", "format": "vtt", "url": "...", "origin": "provider"}
  ],
  "capabilities": {"host_subtitles": true, "provider_subtitles": true},
  "expires_in": 21600,
  "audit": {"blocked_domains": [], "blocked_urls": 0, "hls_filter_enabled": true},
  "media": {"type": "tv", "id": "94997", "season": "1", "episode": "1"}
}
```

`stream_url`, `sources`, and subtitle URLs remain in the response only for older clients. The browser never receives `api_password`: every upstream header and query parameter is encrypted into the token path (`/_token_...`). The player validates the token against the path and renders its own signed, same-origin URLs.

### Player simulation

Simulation starts when the iframe mounts, not on every play click. With a hard budget of `PLAYER_SIMULATION_BUDGET=10` seconds, the player resolves hosts concurrently (`RESOLVE_TIMEOUT=5.5`, `PROBE_TIMEOUT=2.5`, `PROBE_CONCURRENCY=4`), fetches a manifest plus one tiny segment range, detects HLS/DASH/MP4 and codecs, checks subtitle availability, then ranks candidates. HTTP 200 manifest +40, segment 200/206 +25, HLS +20, DASH +8, HEVC −25, latency bonus up to +10, Indonesian subtitle +5.

All playable candidates are kept in order. The browser can switch host or quality; if the active host fails, the player moves to the next candidate at the same `currentTime`. Native subtitles from every host are merged, deduplicated by language/label, and ordered `id` > `en` > others while retaining `host`/`origin` metadata. hls.js `1.6.13` and dash.js `4.7.4` are bundled locally, so no third-party CDN is contacted.

Streamflix only listens to `postMessage` events (`ready`, `timeupdate`, `playing`, `paused`, `ended`, `error`) and may send `mfp:seek`, `mfp:setSubtitle`, `mfp:pause`, `mfp:play`, or `mfp:host`. CSP `frame-ancestors` is `https://movie.oplay.id` by default; override with `PLAYER_FRAME_ANCESTORS` if needed.

### Caching and retry

The provider is contacted **once** per media identity. The extracted result is cached in MediaFlow for at most 6 hours, or for a shorter time when the upstream URL advertises its own expiry (`expires`, `exp`, or `e`). The simulated player session has a separate, shorter cache (`PLAYER_CACHE_TTL=600`, default 10 minutes) shared across workers via Redis when configured. `?retry=1` on the gateway clears the positive extractor cache; the player's **Cari ulang host** button uses `/player/.../retry` to bypass the player cache and re-run the simulation, e.g.:

```
GET https://mfp.oplay.id/gateway/resolve/tv/94997/1/1?retry=1
```

There are no `?mode=`, `?server=`, `?slug=`, or legacy `/embed/...` routes: server selection is internal and cannot be overridden by the browser.

### Ad filtering

Ad filtering runs at three layers and writes an audit trail:

1. **Fetch time** — provider HTML is sanitised (ad scripts, ad iframes, `meta refresh`, `window.open`, and `location` redirects are removed). Every candidate URL is checked against an advertising/tracker/wrapper denylist by domain, path, and query parameter; a resolved stream that is an ad/wrapper is rejected and the host is skipped.
2. **HLS manifest** — `CUE-OUT`/`CUE-IN` and `DATERANGE CLASS="ad"` ranges are removed from proxied playlists, with a `[ad-filter] removed N HLS ad segments ...` log line. Set `FILTER_HLS_ADS=false` to disable this layer.
3. **Audit** — the `audit` object in the gateway response reports blocked domains/URLs and sanitised scripts/iframes/redirects; the server additionally logs `[ad-filter] audit host=... media=... {...}` whenever something was blocked.

### Removed legacy surfaces

`/embed/relay/*`, `/embed/relay/hosts`, `/embed/servers`, `/embed/<slug>/*`, `?mode=standard`, `?mode=relay`, and the old `OPLAY_EMBED_*` postMessage contract have been removed entirely. They are replaced by the tokenized `/player/...` iframe described above. Streamflix keeps its navbar, Play button, `/watch/...` page, and progress/history; MediaFlow owns playback and host logic.

These endpoints are intentionally public: they resolve the upstream stream and sign a short-lived MediaFlow proxy URL internally, so `api_password` is never exposed to the browser. The TMDB API key used by trailer/banner endpoints also stays server-side.
