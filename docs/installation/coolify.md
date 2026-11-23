# Deploy with Coolify and GitHub Actions

This guide deploys MediaFlow Proxy on [Coolify](https://coolify.io/) using a multi-arch Docker image built by GitHub Actions and published to GitHub Container Registry (GHCR).

The image already runs Gunicorn with Uvicorn workers on port `8888`, as a non-root user, with `ffmpeg` and a bundled headless Chromium installed. Chromium is only launched if you opt into the gated-player fallback below. No Dockerfile changes are needed.

## 1. Prepare your fork

The bundled workflow in upstream pushes to Docker Hub under `mhdzumair`. The version in this repository pushes to **GHCR under your own account** instead.

1. Fork this repository.
2. In the fork, go to **Settings → Actions → General → Workflow permissions** and select **Read and write permissions**. This is required for `packages: write` and for the webhook step.
3. Make sure a version tag exists in the fork (for example `v2.4.1`). The workflow accepts either `2.4.1` or `v2.4.1` and normalizes it to `v2.4.1`.
4. Run **Actions → MediaFlow Proxy CI/CD → Run workflow** and enter the tag. The job builds `linux/amd64` and `linux/arm64` images and pushes:
   - `ghcr.io/<your-user>/<your-repo>:v2.4.1`
   - `ghcr.io/<your-user>/<your-repo>:latest`

Prefer pinning the version tag (`:v2.4.1`) in production rather than `:latest`, so a rebuild cannot change a running deployment unexpectedly.

!!! note "Image visibility"
    If the GHCR package is private, create a personal access token with `read:packages` and add it as a registry credential in Coolify (username = your GitHub username, password = the token). Public packages need no credentials.

## 2. Deploy in Coolify

1. Create a new resource: **Docker Compose → Repository → select `docker-compose.coolify.yml`** (or paste its contents).
2. In the resource's **Environment Variables**, set at minimum:

   | Variable | Example | Notes |
   |----------|---------|-------|
   | `API_PASSWORD` | `my-secret` | Required. Without it authentication and encrypted URLs are disabled. |
   | `MFP_IMAGE` | `ghcr.io/oplayid/mediaflow-proxy:v2.4.5` | Optional; defaults to `:latest`. Use an immutable version tag in production. |
   | `TMDB_API_KEY` | `your-tmdb-v3-key` | Optional. Enables `/trailer/...` YouTube trailer pages; the key is never sent to the browser. |

    | `REDIS_URL` | `redis://redis:6379` | Keep enabled. Shares the transcode registry and caches across workers. |
    | `SUBDL_API_KEY` | `your-subdl-key` | Optional. Adds SubDL alternatives only when the winning host has no Indonesian subtitle. |
    | `ENABLE_HEADLESS_FALLBACK` | `true` | Optional. Last-resort Chromium fallback for Turnstile-gated VSEmbed/VidSrcTo pages. Off by default. |
    | `HEADLESS_FALLBACK_TIMEOUT` | `15` | Optional. Seconds allowed per gated player page. |

3. Set your domain and enable HTTPS in Coolify as usual; Traefik routes to port `8888`.
4. The compose file defines a healthcheck on `GET /health` and a Redis 7 service with a persistent volume. **Keep Redis enabled.** It shares the transcode job registry, player/extractor caches, rate limits and HLS/DASH prebuffer locks across Gunicorn workers. Without `REDIS_URL`, job metadata lives only in each worker's memory and a transcode URL created by one worker can return 404 when another worker serves it. Redis failures are fail-open, but a missing Redis is not safe for multi-worker playback.
5. Deploy and check the container logs for `Uvicorn running on http://0.0.0.0:8888`.

## 3. Optional: automatic deployment after an image build

1. In Coolify, open the service and copy its deployment webhook URL.
2. In the fork, add a repository secret named `COOLIFY_WEBHOOK_URL` with that URL.
3. The workflow then calls the webhook after a successful image push, so Coolify pulls the new image and redeploys.

## 4. Important runtime notes

- **`FORWARDED_ALLOW_IPS`** is set to `*` in the compose file. This is needed behind Coolify's Traefik reverse proxy: otherwise client IPs are read as `127.0.0.1` and IP-bound encrypted URLs will not work. Traefik overwrites `X-Forwarded-For`, but be aware that this setting trusts the header completely.
- **Worker count**: with Redis connected, the default 4 Gunicorn workers are safe for playback and transcoding. If CPU is limited, set `WEB_CONCURRENCY: "2"` in Coolify. Change this value only by redeploying — Gunicorn reads it once at startup.
- **Telegram interactive login** (`/proxy/telegram/session/*`) still keeps pending sessions in process memory. Set `WEB_CONCURRENCY: "1"` for that flow, or skip interactive login by supplying `TELEGRAM_SESSION_STRING`.
- **VSEmbed / VidSrcTo**: the upstream player can render before minting its short-lived `apiToken`. The extractor mirrors the browser's single refresh fetch when `apiTokenRefresh` is present; if the upstream still refuses to mint a token, that host fails cleanly and the next host in `HOST_ORDER` is selected.
- **Gated playback fallback**: when a page still has no `apiToken` and carries `turnstile` / `cstCaptcha`, set `ENABLE_HEADLESS_FALLBACK=true`. The image bundles Chromium; mfp opens the player page once while creating the session, waits up to `HEADLESS_FALLBACK_TIMEOUT` (15s by default) for the token, then continues through the normal stream API. It is serialized, briefly cached, and never used as a failover after playback starts. Managed challenges may pass automatically; interactive image challenges usually will not — for those, use a clean egress IP via `TRANSPORT_CONFIG` instead. Look for `playback gated` and either `gated config resolved via headless fallback` or a clean `playback gated (human verification)` error in the logs.
- **Docker Image resource (not Compose)**: the `redis` service in `docker-compose.coolify.yml` is not created in that deployment type. Create a separate Coolify Redis database in the same project and set `REDIS_URL` to its internal URL (for example `redis://mfp-redis:6379`). Keep `WEB_CONCURRENCY=1` until the startup log shows `Redis connected`, then raise it to 2.
- **`docker-compose.yml` at the repository root is not for forks**: it builds from `https://github.com/mhdzumair/mediaflow-proxy.git#main`. Use `docker-compose.coolify.yml` instead.
- The PyPI publishing and Gemini release-notes jobs are disabled by default in this fork. To enable them, set the repository variables `PUBLISH_TO_PYPI` / `GENERATE_RELEASE_NOTES` to `true` and add the corresponding secrets (`PYPI_API_TOKEN`, `GEMINI_API_KEY`).
- If you prefer Docker Hub over GHCR, change `REGISTRY`, `IMAGE_NAME`, and the login step in `.github/workflows/main.yml`, and add `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN` secrets.

## 5. Quick verification

```bash
curl https://your-domain.example/health
# {"status":"healthy"}

curl -I "https://your-domain.example/generate_url"
# endpoint exists; URL generation itself is a POST
```

Generate a stream URL from `/url-generator` with your `API_PASSWORD` and open the resulting `/proxy/...` link to confirm the proxy path works end to end.
