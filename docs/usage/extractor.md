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

## StreamFlix gateway (output only)

MediaFlow is **not** a player and does not serve a provider list, a relay page, a server switcher, or a player iframe. Streamflix sends only media identity; MediaFlow visits the provider embed page server-side, scrubs ads, resolves the clean HLS/MP4 URL plus subtitle URLs, caches the result, and returns JSON containing MediaFlow-signed proxy URLs. Streamflix owns the UI, the player, watch progress, and history.

| Endpoint | Description |
|---|---|
| `/gateway/resolve/movie/<tmdb_id>` | Resolve a movie |
| `/gateway/resolve/tv/<tmdb_id>` | Show, defaults to season 1 episode 1 |
| `/gateway/resolve/tv/<tmdb_id>/<season>` | Show, defaults to episode 1 |
| `/gateway/resolve/tv/<tmdb_id>/<season>/<episode>` | Exact TV episode |
| `/gateway/cache` | Debug-only cache statistics (no signed URLs) |

TV IDs accept an optional `tv-` prefix; invalid or zero season/episode values fall back to `1`. The gateway resolves three hosts, in order: **VidCore, VSEmbed, then VidLink**. Add `?host=vidcore`, `?host=vsembed`, or `?host=vidlink` to force one host (useful for diagnostics). Each probe has a 10 second timeout and selection is capped at 40 seconds; failed hosts are skipped for 30 seconds. Historical hosts remain in the extractor registry for backwards-compatible `host=` lookups but are never probed by this gateway. If all three hosts fail the gateway returns `503` with a readable `detail`; it never returns a player shell, a provider page, or a raw proxy error.

Example:

```
GET https://mfp.oplay.id/gateway/resolve/tv/94997/1/1
```

```json
{
  "host": "VidCore",
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

`stream_url` and every URL in `sources`/`subtitles`/`provider_subtitles` is signed: upstream headers, query parameters, and `api_password` are encrypted into the token, so the browser never sees `api_password` in plaintext. `expires_in` is the token lifetime in seconds. Streamflix should re-fetch the gateway when the token approaches expiry (or when the user retries) instead of contacting the provider again.

`subtitles` are the host's own captions and are safe to auto-enable. `provider_subtitles` come from a separate subtitle API (for example VidCore's subtitle list) and must stay opt-in. Both lists are ordered Indonesian first, English second; Streamflix selects the Indonesian host track by default and falls back to English.

### Caching and retry

The provider is contacted **once** per media identity. The extracted result is cached in MediaFlow for at most 6 hours, or for a shorter time when the upstream URL advertises its own expiry (`expires`, `exp`, or `e`). Signed token lifetime matches the cache entry. Playback then goes only through `mfp.oplay.id/proxy/hls/...`; the embed host is not contacted again until the cache expires. `?retry=1` clears both positive and negative cache entries for that movie/episode and forces a fresh extraction, e.g.:

```
GET https://mfp.oplay.id/gateway/resolve/tv/94997/1/1?retry=1
```

There are no `?mode=`, `?server=`, `?slug=`, or `/embed/...` routes: server selection is internal and cannot be overridden by the browser.

### Ad filtering

Ad filtering runs at three layers and writes an audit trail:

1. **Fetch time** — provider HTML is sanitised (ad scripts, ad iframes, `meta refresh`, `window.open`, and `location` redirects are removed). Every candidate URL is checked against an advertising/tracker/wrapper denylist by domain, path, and query parameter; a resolved stream that is an ad/wrapper is rejected and the host is skipped.
2. **HLS manifest** — `CUE-OUT`/`CUE-IN` and `DATERANGE CLASS="ad"` ranges are removed from proxied playlists, with a `[ad-filter] removed N HLS ad segments ...` log line. Set `FILTER_HLS_ADS=false` to disable this layer.
3. **Audit** — the `audit` object in the gateway response reports blocked domains/URLs and sanitised scripts/iframes/redirects; the server additionally logs `[ad-filter] audit host=... media=... {...}` whenever something was blocked.

### Removed legacy surfaces

`/embed/relay/*`, `/embed/relay/hosts`, `/embed/servers`, `/embed/<slug>/*`, `?mode=standard`, `?mode=relay`, and the in-iframe player with its `OPLAY_EMBED_*` postMessage contract have been removed entirely. Streamflix keeps its navbar, Play button, `/watch/...` page, and progress/history; MediaFlow only outputs clean URLs.

These endpoints are intentionally public: they resolve the upstream stream and sign a short-lived MediaFlow proxy URL internally, so `api_password` is never exposed to the browser. The TMDB API key used by trailer/banner endpoints also stays server-side.
