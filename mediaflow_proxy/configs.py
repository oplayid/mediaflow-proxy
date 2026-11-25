from typing import Dict, Literal, Optional

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings


class RouteConfig(BaseModel):
    """Configuration for a specific route"""

    proxy: bool = True
    proxy_url: Optional[str] = None
    verify_ssl: bool = True


class TransportConfig(BaseSettings):
    """Main proxy configuration"""

    proxy_url: Optional[str] = Field(
        None, description="Primary proxy URL. Example: socks5://user:pass@proxy:1080 or http://proxy:8080"
    )
    disable_ssl_verification_globally: bool = Field(
        False, description="Disable SSL verification for all requests globally."
    )
    all_proxy: bool = Field(False, description="Enable proxy for all routes by default")
    transport_routes: Dict[str, RouteConfig] = Field(
        default_factory=dict, description="Pattern-based route configuration"
    )
    timeout: int = Field(60, description="Timeout for HTTP requests in seconds")

    class Config:
        env_file = ".env"
        extra = "ignore"


class Settings(BaseSettings):
    api_password: str | None = None  # The password for protecting the API endpoints.
    tmdb_api_key: str | None = None  # TMDB v3 API key used by the public trailer endpoints.
    log_level: str = "INFO"  # The logging level to use.
    transport_config: TransportConfig = Field(default_factory=TransportConfig)  # Configuration for HTTP transport.
    enable_streaming_progress: bool = False  # Whether to enable streaming progress tracking.
    disable_home_page: bool = False  # Whether to disable the home page UI.
    disable_docs: bool = False  # Whether to disable the API documentation (Swagger UI).
    disable_speedtest: bool = False  # Whether to disable the speedtest UI.
    clear_cache_on_startup: bool = (
        False  # Whether to clear all caches (extractor, MPD, etc.) on startup. Useful for development.
    )
    stremio_proxy_url: str | None = None  # The Stremio server URL for alternative content proxying.
    m3u8_content_routing: Literal["mediaflow", "stremio", "direct"] = (
        "mediaflow"  # Routing strategy for M3U8 content URLs: "mediaflow", "stremio", or "direct"
    )
    enable_hls_prebuffer: bool = True  # Whether to enable HLS pre-buffering for improved streaming performance.
    filter_hls_ads: bool = True  # Remove HLS ad ranges (CUE-OUT/CUE-IN, DATERANGE CLASS=ad) from proxied manifests.
    livestream_start_offset: (
        float | None
    ) = -18  # Default start offset for live streams (e.g., -18 to start 18 seconds behind live edge). Applies to HLS and MPD live playlists. Set to None to disable.
    hls_prebuffer_segments: int = 5  # Number of segments to pre-buffer ahead.
    hls_prebuffer_cache_size: int = 50  # Maximum number of segments to cache in memory.
    hls_prebuffer_max_memory_percent: int = 80  # Maximum percentage of system memory to use for HLS pre-buffer cache.
    hls_prebuffer_emergency_threshold: int = 90  # Emergency threshold percentage to trigger aggressive cache cleanup.
    hls_prebuffer_inactivity_timeout: int = 60  # Seconds of inactivity before stopping playlist refresh loop.
    hls_segment_cache_ttl: int = 300  # TTL (seconds) for cached HLS segments; 300s (5min) for VOD, lower for live.
    enable_dash_prebuffer: bool = True  # Whether to enable DASH pre-buffering for improved streaming performance.
    dash_prebuffer_segments: int = 5  # Number of segments to pre-buffer ahead.
    dash_prebuffer_cache_size: int = 50  # Maximum number of segments to cache in memory.
    dash_prebuffer_max_memory_percent: int = 80  # Maximum percentage of system memory to use for DASH pre-buffer cache.
    dash_prebuffer_emergency_threshold: int = 90  # Emergency threshold percentage to trigger aggressive cache cleanup.
    dash_prebuffer_inactivity_timeout: int = 60  # Seconds of inactivity before cleaning up stream state.
    dash_segment_cache_ttl: int = 60  # TTL (seconds) for cached media segments; longer = better for slow playback.
    dash_player_lock_timeout: float = 2.5  # Max wait (seconds) for player requests when a segment lock is busy.
    dash_prebuffer_lock_timeout: float = 0.25  # Max wait (seconds) for background prebuffer lock acquisition.
    dash_prefetch_max_concurrent: int = 1  # Max concurrent live DASH prefetch downloads to reduce lock contention.
    dash_live_initial_media_prebuffer: bool = (
        False  # Whether manifest-time prebuffer should fetch live media segments (init segments are still prewarmed).
    )
    mpd_live_init_cache_ttl: int = 60  # TTL (seconds) for live init segment cache; 0 disables caching.
    mpd_live_playlist_depth: int = 8  # Number of recent segments to expose per live playlist variant.
    remux_to_ts: bool = False  # Remux fMP4 segments to MPEG-TS for ExoPlayer/VLC compatibility.
    processed_segment_cache_ttl: int = 60  # TTL (seconds) for caching processed (decrypted/remuxed) segments.

    # Byparr settings — Firefox/Camoufox-based solver for Cloudflare bypass and chevy IP whitelist.
    # https://github.com/ThePhaseless/Byparr  (drop-in FlareSolverr-compatible API)
    byparr_url: str | None = None  # Byparr service URL. Example: http://localhost:8192
    byparr_timeout: int = 60  # Timeout (seconds) for Byparr requests.

    # Acestream settings
    enable_acestream: bool = False  # Whether to enable Acestream proxy support.
    acestream_host: str = "localhost"  # Acestream engine host.
    acestream_port: int = 6878  # Acestream engine port.
    acestream_buffer_size: int = 4 * 1024 * 1024  # Buffer size for MPEG-TS streaming (4MB default, like acexy).
    acestream_empty_timeout: int = 30  # Timeout (seconds) when no data is received from upstream.
    acestream_session_timeout: int = 60  # Session timeout (seconds) for cleanup of inactive sessions.
    acestream_keepalive_interval: int = 15  # Interval (seconds) for session keepalive polling.

    # Telegram MTProto settings
    enable_telegram: bool = False  # Whether to enable Telegram MTProto proxy support.
    telegram_api_id: int | None = None  # Telegram API ID from https://my.telegram.org/apps
    telegram_api_hash: SecretStr | None = None  # Telegram API hash from https://my.telegram.org/apps
    telegram_session_string: SecretStr | None = None  # Persistent session string (avoids re-authentication).
    telegram_max_connections: int = 8  # Max parallel DC connections for downloads (max 20, careful of floods).
    telegram_request_timeout: int = 30  # Request timeout in seconds.
    telegram_document_scan_limit: int = 500  # Max recent messages to scan when resolving chat_id+document_id.
    telegram_document_cache_ttl: int = 3600  # TTL (seconds) for cached document_id->message_id mappings.

    # Transcode settings
    enable_transcode: bool = True  # Whether to enable on-the-fly transcoding endpoints (MKV→fMP4, HLS VOD).
    transcode_prefer_gpu: bool = True  # Prefer GPU acceleration (NVENC/VideoToolbox/VAAPI) when available.
    transcode_video_bitrate: str = "4M"  # Target video bitrate for re-encoding (e.g. "4M", "2000k").
    transcode_audio_bitrate: int = 192000  # AAC audio bitrate in bits/s for the Python transcode pipeline.
    transcode_video_preset: str = "medium"  # Encoding speed/quality tradeoff (libx264: ultrafast..veryslow).

    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"  # The user agent to use for HTTP requests.

    # Upstream error resilience settings
    upstream_retry_on_disconnect: bool = True  # Enable/disable retry when upstream disconnects mid-stream.
    upstream_retry_attempts: int = 2  # Number of retry attempts when upstream disconnects during streaming.
    upstream_retry_delay: float = 1.0  # Delay (seconds) between retry attempts.
    graceful_stream_end: bool = True  # Return valid empty playlist instead of error when upstream fails.

    # Generic HTTP forward endpoint (/proxy/forward) settings
    forward_allowed_hosts: list[str] = []  # Allowlist of hostnames. Empty = allow all.
    forward_denied_hosts: list[str] = []  # Extra denied hostnames (in addition to automatic private-IP guard).
    forward_max_body_bytes: int = (
        10 * 1024 * 1024
    )  # Deprecated: use forward_max_request_body_bytes / forward_max_response_body_bytes.
    forward_max_request_body_bytes: int = (
        50 * 1024 * 1024
    )  # Max incoming request body size (50 MB — allows NZB/torrent file uploads).
    forward_max_response_body_bytes: int = (
        10 * 1024 * 1024
    )  # Max upstream response body size (10 MB — API JSON responses).
    public_ip: str | None = (
        None  # MediaFlow's own public IP. Used to substitute {mediaflow_ip} in forwarded requests. Auto-detected if not set.
    )

    # Smart player settings. The player is served by MediaFlow itself and
    # embedded by StreamFlix; StreamFlix never sees provider/proxy URLs.
    player_simulation_budget: float = 10.0  # Max seconds spent checking servers before playback.
    player_cache_ttl: int = 600  # Seconds a simulated session is reused across workers.
    # Comma-separated canonical names, slugs or domains of the servers the player
    # may use, in priority order. Empty = HOST_ORDER from embed_hosts.py.
    player_hosts: str = ""
    # How many servers may be resolved and signed into one session. The first
    # playable one stays the default; the rest feed the manual Server picker.
    player_max_servers: int = 4
    player_frame_ancestors: str = "https://movie.oplay.id"  # CSP frame-ancestors for the iframe.
    # HEVC browsers/TV boxes often cannot decode hvc1/hev1. Transcode to H.264.
    player_transcode_hevc: bool = True
    player_transcode_height: int = 720
    player_transcode_crf: int = 23
    player_transcode_preset: str = "veryfast"
    player_transcode_hls_time: int = 4
    player_transcode_hls_list_size: int = 0  # 0 = keep all VOD segments
    player_transcode_idle_ttl: int = 300
    player_transcode_max_jobs: int = 4
    player_transcode_startup_timeout: int = 30
    # SubDL is an optional external subtitle source inside the mfp player. Set
    # SUBDL_API_KEY in the deployment environment; it is never sent to browsers.
    subdl_api_key: str = ""

    # Optional EarnVids bridge. StreamFlix signs a small request with
    # MFP_EARNVIDS_SECRET; mfp resolves the source, signs a same-origin proxy URL,
    # remuxes it to one MP4 and uploads that file to EarnVids. Neither the
    # provider URL nor these secrets are ever sent to the browser.
    earnvids_api_key: SecretStr | None = None
    mfp_earnvids_secret: SecretStr | None = None
    earnvids_source_token_ttl: int = 6 * 60 * 60
    earnvids_fld_id: str = ""
    earnvids_cat_id: str = ""
    earnvids_tags: str = ""
    earnvids_request_timeout: float = 20.0
    # File-upload pipeline. The old URL-upload endpoint expects a direct video
    # file, while the embeds resolve to HLS manifests, so mfp converts first.
    earnvids_upload_height: int = 720
    earnvids_upload_crf: int = 23
    earnvids_upload_preset: str = "veryfast"
    earnvids_upload_max_seconds: int = 14400
    earnvids_upload_max_bytes: int = 2 * 1024 * 1024 * 1024
    # Remux (-c copy) is the primary path; transcode is only a fallback.
    earnvids_upload_copy_timeout: int = 900
    earnvids_upload_ffmpeg_timeout: int = 3600
    earnvids_job_ttl: int = 24 * 60 * 60
    # HLS is downloaded segment-by-segment in parallel before remuxing. Each
    # connection is still rate limited by most hosts, so concurrency is the
    # only way to use more of the VPS bandwidth. The stall timeout replaces a
    # hard total limit: a long film keeps running while bytes keep arriving.
    earnvids_download_concurrency: int = 8
    earnvids_stall_timeout: int = 600

    # EPG proxy settings
    epg_cache_ttl: int = 3600  # TTL (seconds) for cached EPG/XMLTV data. Default 1 hour.

    # Redis settings
    redis_url: str | None = None  # Redis URL for distributed locking and caching. None = disabled.
    cache_namespace: str | None = (
        None  # Optional namespace for instance-specific caches (e.g. pod name or hostname). When set, extractor results and other IP-bound data are stored under this namespace so multiple pods sharing one Redis don't serve each other's IP-specific URLs.
    )

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
