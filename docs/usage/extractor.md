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

## StreamFlix iframe embed endpoints

MediaFlow owns the iframe player, the embed-host registry, host selection, and failover. Streamflix only needs two URL templates and keeps its existing `OPLAY_EMBED_*` postMessage handling.

| Endpoint | Description |
|---|---|
| `/embed/auto/movie/<tmdb_id>` | Movie player with automatic host selection |
| `/embed/auto/tv/<tmdb_id>/<season>/<episode>` | TV episode player with automatic host selection |
| `/embed/<slug>/movie/<tmdb_id>` | Force one host (for debugging) |
| `/embed/<slug>/tv/<tmdb_id>/<season>/<episode>` | Force one host (for debugging) |
| `/embed/servers` | JSON list of available servers and priorities |

`auto` tries hosts in priority order — VidCore, VSEmbed, then the remaining historical hosts — and skips hosts that recently failed (5 minute negative cache). The generated player includes a small control panel inside the iframe, so Streamflix does not need a server picker of its own: **Server**, **Kualitas**, **Subtitle**, **Audio**, and **Speed**. Quality/audio/subtitle tracks come from the HLS manifest and, for VidCore, from the upstream `sources`/`subtitles` API metadata. Subtitle default is Indonesian, falling back to English when no Indonesian track exists; everything else starts as Off. Nested variant playlists inherit `force_playlist_proxy`, so AES keys, segments, and external `.vtt` subtitles stay on the gateway instead of leaking to the embed host (which often blocks cross-origin playback).

Examples:

```
https://mfp.oplay.id/embed/auto/movie/550
https://mfp.oplay.id/embed/auto/tv/1399/1/1?autoplay=true
```

Add `?autoplay=true` to start muted autoplay. `?server=<slug>` can force a host while still using the `auto` route.

In addition to the existing `OPLAY_EMBED_READY` / `OPLAY_EMBED_PROGRESS` messages, the player emits:

```js
{ type: "OPLAY_EMBED_SERVER", id: "vidcore", name: "VidCore" }
{ type: "OPLAY_EMBED_ERROR", message: "..." }
```

These endpoints are intentionally public: they resolve the upstream stream and sign a short-lived MediaFlow proxy URL internally, so `api_password` is never exposed to the browser.
