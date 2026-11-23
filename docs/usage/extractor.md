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
| `VidSrcTo` | `vidsrcto` | `vidsrc.to`, `www.vidsrc.to`, `vidsrc.sh`, `www.vidsrc.sh` |
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

`/gateway/resolve/...` is an identity-only signer. Streamflix sends the media ID and receives exactly two fields: `player_url` and `media`. The gateway does not contact an embed host, does not cache extraction results, does not accept `?host=` or `?server=`, and never returns `host`, `stream_url`, `sources`, `subtitles`, `provider_subtitles`, `expires_in`, or `audit`. Servers are checked once, when the iframe mounts; the first playable one becomes the default and the rest feed the player's manual Server picker.

| Endpoint | Description |
|---|---|
| `/gateway/resolve/movie/<tmdb_id>` | Movie; returns `player_url` + `media` |
| `/gateway/resolve/tv/<tmdb_id>` | Show; defaults to season 1 episode 1 |
| `/gateway/resolve/tv/<tmdb_id>/<season>` | Show; defaults to episode 1 |
| `/gateway/resolve/tv/<tmdb_id>/<season>/<episode>` | Exact TV episode |
| `/_token_<token>/player/<type>/<id>[/<season>/<episode>]` | Opaque mfp player iframe |
| `/_token_<token>/player/subtitle/<key>.vtt` | Signed, server-converted WebVTT track |
| `/_token_<token>/player/subtitle/upload` | POST raw `.srt`/`.vtt` body; returns a signed `origin: "custom"` track |
| `/_token_<token>/player/transcode/<job>/master.m3u8` | Tokenized HLS output of a server-side HEVC → H.264 job |

TV IDs accept an optional `tv-` prefix; invalid or zero season/episode values fall back to `1`.

Example:

```
GET https://mfp.oplay.id/gateway/resolve/tv/94997/1/1
```

```json
{
  "player_url": "https://mfp.oplay.id/_token_<encrypted>/player/tv/94997/1/1",
  "media": {"type": "tv", "id": "94997", "season": "1", "episode": "1"}
}
```

`?retry=1` signs `refresh: true` into the `player_url` token; the player then ignores the positive session cache and performs the host check again. It is a token field rather than a free query parameter because `EncryptionMiddleware` merges the decrypted payload over the query string.

### Player host check

The iframe mount is the only server check. Active servers come from the adapter registry in `mediaflow_proxy/player/adapters.py`: each declarative `EmbedHostConfig` in `embed_hosts.py` is registered as a `ServerAdapter`, and `PLAYER_HOSTS` selects the active names and their priority order (comma-separated canonical names, slugs or domains). When `PLAYER_HOSTS` is unset, the default is `HOST_ORDER`: **VidCore → VSEmbed → VidSrcTo → VidLink**. `PLAYER_MAX_SERVERS=4` caps how many playable candidates are signed into one session.

The player resolves servers in that order within `PLAYER_SIMULATION_BUDGET=10` seconds (`RESOLVE_TIMEOUT=7.5`, `PROBE_TIMEOUT=2.5`), fetching the manifest plus one tiny segment range. A server that fails is skipped; each is checked at most once. Every server that passes is signed as a candidate, while the first one stays the default. The viewer can switch between candidates with the **Server** picker in the player, which reuses the already-signed, same-origin URLs and resumes at the current time. There is no automatic failover: if the current source later fails, the player shows a clean error and the viewer chooses another server manually.

`VidSrcTo` follows the `vidsrc.to` wrapper to `vidsrc.sh`, reads its short-lived `/vs_src.php` gate, then reuses the VSEmbed player chain. A server with a bespoke API can be added by registering a `ServerAdapter` with its own `resolver`, without touching the player or gateway. `?host=` and `?server=` remain browser-unsettable.

The probe detects HLS, DASH, and MP4 plus HEVC codecs. DASH sources are routed through the `/proxy/mpd/...` conversion pipeline, including padded `$Number%05d$` / `$Time%05d$` templates that previously stayed literal and made every segment request 404. Relative segments resolve correctly in both dash.js and the FFmpeg transcode prewarm; the browser receives an HLS URL even when the probe saw an MPD. Ad filtering and the SSRF allowlist still run at fetch time; `/pl/` requests still use `curl_cffi`.

All quality and subtitle settings live in the mfp player. Every host resolver returns the video URL together with the full native subtitle package exposed by that host (including nested `captions`, `textTracks`, and `default_subs` shapes). Tracks from every playable server are normalised, deduplicated by language/label, ordered `id` > `en` > others, and selected as the default; none are dropped by a short list cap. Upstream `.srt` files are converted to WebVTT by the signed `/hls/segment.vtt` proxy so browser `<track>` elements can render them. All sources are merged into one manually selectable menu grouped as **Host / SubDL / Custom**. Native host tracks stay first and are the default; `SUBDL_API_KEY` is consulted whenever a winner exists, even if the host already has an Indonesian track, so viewers can still choose a SubDL alternative. SubDL search/download and SRT → WebVTT conversion run server-side, and those tracks appear as signed, same-origin alternatives that never expose the API key. Viewer-uploaded `.srt`/`.vtt` files are posted as a raw body to the tokenized `/player/subtitle/upload` endpoint (5 MB cap), converted to WebVTT, and returned as a signed `origin: "custom"` track; the upload URL carries its own 6-hour token so it keeps working after the 10-minute iframe token expires, and custom tracks are merged client-side so cached sessions are unaffected. hls.js and dash.js are bundled locally, so no third-party CDN is contacted. The session is injected as a non-executable `<script type="application/json" id="mfp-session">` data block: the player's CSP uses `script-src 'self'`, which forbids inline JavaScript, so an inline `window.__MFP_SESSION__ = ...` assignment would be blocked and leave the player with no candidates.

### HEVC → H.264 transcoding

Some hosts (notably VidLink DASH) only expose `hev1`/`hvc1`/`h265`, which most HP browsers and TV boxes cannot decode. When the probe detects HEVC in the manifest codecs or the upstream URL, the player registers a lazy server-side FFmpeg job and gives the browser only a tokenized HLS URL:

- `PLAYER_TRANSCODE_HEVC=true` (default) enables the fallback; the candidate is marked `needs_transcode` and `playback_type="hls"`.
- The source URL and signed headers stay server-side in the Redis job registry. FFmpeg re-encodes video to H.264 (`libx264`, `yuv420p`) and copies AAC audio when present, capped by `PLAYER_TRANSCODE_HEIGHT=720`, `PLAYER_TRANSCODE_CRF=23`, `PLAYER_TRANSCODE_PRESET=veryfast`, `PLAYER_TRANSCODE_HLS_TIME=4`.
- `PLAYER_TRANSCODE_HLS_LIST_SIZE=0` keeps every VOD segment so playback starts at the beginning; idle jobs are stopped after `PLAYER_TRANSCODE_IDLE_TTL=300` seconds. At most `PLAYER_TRANSCODE_MAX_JOBS=4` run per worker.
- The winner is prewarmed in the background so the host-check budget is unchanged. The player polls the tokenized manifest until it returns 200, showing **Menyiapkan transcode HEVC → H.264…**, then forces hls.js. If the job does not become ready within 25 seconds, the player tries the native proxied source once before showing an error. FFmpeg stderr is logged with the job id when a process exits unexpectedly.
- Native quality options are hidden for transcoded candidates; subtitles still work. Nothing switches automatically after playback starts; the viewer may pick another server manually.

StreamFlix does not control playback: its watch page contains only the iframe and a back button. The mfp player may emit `postMessage` events (`ready`, `timeupdate`, `playing`, `paused`, `ended`, `error`) and accept `mfp:seek`, `mfp:setSubtitle` (by merged track `id`, with language fallback), `mfp:pause`, or `mfp:play`, but Streamflix sends no host command and renders no retry, quality, or subtitle UI. The Server, quality and subtitle controls stay inside the mfp iframe. CSP `frame-ancestors` is `https://movie.oplay.id` by default; override with `PLAYER_FRAME_ANCESTORS` if needed.

### Caching

Extraction results are cached in MediaFlow for at most 6 hours, or for a shorter time when the upstream URL advertises its own expiry (`expires`, `exp`, or `e`). The player session has a separate, shorter cache (`PLAYER_CACHE_TTL=600`, default 10 minutes) shared across workers via Redis when configured. `?retry=1` on the gateway clears the positive session cache so the server check runs again; failures are not cached. There are no `?exclude=`, `?t=`, `/retry`, or `/gateway/cache` routes.

`api_password` is never sent to the browser as plaintext. Every sensitive upstream URL, header, and subtitle lookup is wrapped in a `/_token_...` path. Media and subtitle URLs carried by a session use a 6-hour token so the manual Server picker keeps working during a long playback; headers and `api_password` still never reach the browser. There are no `?mode=`, `?server=`, `?slug=`, or legacy `/embed/...` routes: the Server picker only chooses among candidates already signed into that session, so the browser cannot inject a new host.

### Ad filtering

Ad filtering runs at three layers and writes an audit trail:

1. **Fetch time** — provider HTML is sanitised (ad scripts, ad iframes, `meta refresh`, `window.open`, and `location` redirects are removed). Every candidate URL is checked against an advertising/tracker/wrapper denylist by domain, path, and query parameter; a resolved stream that is an ad/wrapper is rejected and the host is skipped.
2. **HLS manifest** — `CUE-OUT`/`CUE-IN` and `DATERANGE CLASS="ad"` ranges are removed from proxied playlists, with a `[ad-filter] removed N HLS ad segments ...` log line. Set `FILTER_HLS_ADS=false` to disable this layer.
3. **Audit** — the `audit` object in the gateway response reports blocked domains/URLs and sanitised scripts/iframes/redirects; the server additionally logs `[ad-filter] audit host=... media=... {...}` whenever something was blocked.

### Removed legacy surfaces

`/embed/relay/*`, `/embed/relay/hosts`, `/embed/servers`, `/embed/<slug>/*`, `?mode=standard`, `?mode=relay`, `/gateway/cache`, `/player/.../retry`, `?exclude=`, `?t=`, and the old `OPLAY_EMBED_*` postMessage contract have been removed entirely. They are replaced by the tokenized `/player/...` iframe described above. Streamflix's `/watch/...` page resolves only the media ID, embeds one opaque `player_url`, and shows a back button; MediaFlow owns playback, host selection, quality, and subtitles.

These endpoints are intentionally public: the gateway signs only the player identity, and the player resolves the stream and signs MediaFlow proxy URLs internally, so `api_password` is never exposed to the browser. The TMDB API key used by trailer/banner endpoints and the SubDL API key also stay server-side.
