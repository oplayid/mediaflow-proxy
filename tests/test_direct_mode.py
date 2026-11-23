"""Offline tests for the identity-only JSON gateway."""

from __future__ import annotations

import importlib.util
import json

import pytest
from starlette.requests import Request

from mediaflow_proxy.extractors.embed_hosts import EMBED_HOSTS, HOST_ORDER


def _request(path: str = "/gateway/resolve/tv/94997/1/1", query: str = "") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "server": ("mfp.example", 443),
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.encode(),
            "headers": [(b"host", b"mfp.example")],
        }
    )


async def _resolve(request: Request, media_type="tv", media_id="94997", season="1", episode="1"):
    from mediaflow_proxy.routes import embed as embed_routes

    response = await embed_routes._gateway_response(
        request, media_type, media_id, season, episode
    )
    return response, json.loads(response.body)


@pytest.mark.asyncio
async def test_gateway_returns_only_player_url_and_media():
    from mediaflow_proxy.configs import settings

    before = settings.api_password
    settings.api_password = "super-secret"
    try:
        response, body = await _resolve(_request())
    finally:
        settings.api_password = before

    assert response.status_code == 200
    assert set(body) == {"player_url", "media"}
    assert body["media"] == {"type": "tv", "id": "94997", "season": "1", "episode": "1"}
    assert body["player_url"].startswith("https://mfp.example/_token_")
    assert "/player/tv/94997/1/1" in body["player_url"]
    assert "api_password" not in body
    assert "super-secret" not in response.body.decode()
    assert "cdn.example" not in response.body.decode()


@pytest.mark.asyncio
async def test_gateway_never_contacts_a_host(monkeypatch):
    import mediaflow_proxy.player.simulator as simulator

    def forbidden(*_args, **_kwargs):
        raise AssertionError("gateway must not resolve hosts")

    monkeypatch.setattr(simulator, "_resolve_host", forbidden)
    _, body = await _resolve(_request())
    assert set(body) == {"player_url", "media"}


@pytest.mark.asyncio
async def test_gateway_rejects_host_query_parameter():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await _resolve(_request(query="host=vidlink"))
    assert exc.value.status_code == 400
    assert "host selection" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_retry_flag_is_signed_into_player_token(monkeypatch):
    from mediaflow_proxy.configs import settings
    from mediaflow_proxy.utils.crypto_utils import EncryptionHandler

    monkeypatch.setattr(settings, "api_password", "unit-secret")
    _, body = await _resolve(_request(query="retry=1"))
    token = body["player_url"].split("/_token_", 1)[1].split("/player/", 1)[0]
    payload = EncryptionHandler("unit-secret").decrypt_data(token, None)
    assert payload["refresh"] is True
    assert payload["type"] == "tv"
    assert payload["id"] == "94997"


def test_gateway_routes_are_registered_and_cache_endpoint_is_gone():
    from mediaflow_proxy.main import app
    from mediaflow_proxy.routes import embed as embed_routes

    paths = [getattr(route, "path", "") for route in app.routes]
    for path in (
        "/gateway/resolve/movie/{media_id}",
        "/gateway/resolve/tv/{media_id}",
        "/gateway/resolve/tv/{media_id}/{season}",
        "/gateway/resolve/tv/{media_id}/{season}/{episode}",
    ):
        assert path in paths
    assert "/gateway/cache" not in paths
    for removed in ("_probe_gateway", "_resolve_gateway", "_resolved_cache", "_negative_cache"):
        assert not hasattr(embed_routes, removed)
    assert importlib.util.find_spec("mediaflow_proxy.routes.relay") is None
    assert EMBED_HOSTS["VidCore"].slug == "vidcore"
    assert HOST_ORDER[0] == "VidCore"


def test_tv_params_are_normalised_for_streamflix_ids():
    from mediaflow_proxy.routes.embed import _normalise_media

    assert _normalise_media("tv", "tv-1399", "0", "oops") == ("1399", "1", "1")
    assert _normalise_media("tv", "94997", "2", "5") == ("94997", "2", "5")
    assert _normalise_media("movie", "550", None, None) == ("550", None, None)
