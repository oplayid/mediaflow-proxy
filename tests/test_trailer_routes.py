"""Offline tests for the public YouTube trailer endpoints."""

from __future__ import annotations

import pytest

from mediaflow_proxy.configs import settings
from mediaflow_proxy.routes import trailer as trailer_routes


def _video(key: str, language: str, **extra) -> dict:
    payload = {
        "site": "YouTube",
        "key": key,
        "type": "Trailer",
        "iso_639_1": language,
        "name": f"{language} trailer",
        "official": True,
    }
    payload.update(extra)
    return payload


def test_select_youtube_trailer_prefers_indonesian():
    selected = trailer_routes.select_youtube_trailer(
        [
            _video("en123456789", "en"),
            _video("xx123456789", "fr", site="Vimeo"),
            _video("id123456789", "id", official=False),
        ]
    )
    assert selected and selected["key"] == "id123456789"
    assert selected["language"] == "id"


def test_select_youtube_trailer_falls_back_to_english():
    selected = trailer_routes.select_youtube_trailer(
        [_video("xx123456789", "fr"), _video("en123456789", "en")]
    )
    assert selected and selected["key"] == "en123456789"


def test_select_youtube_trailer_rejects_invalid_keys():
    assert (
        trailer_routes.select_youtube_trailer([_video("short", "id")]) is None
    )


def test_embed_url_is_muted_and_loops_by_default():
    url = trailer_routes._embed_url(
        "id123456789", autoplay=True, mute=True, controls=False, loop=True
    )
    assert url.startswith("https://www.youtube-nocookie.com/embed/id123456789?")
    assert "autoplay=1" in url
    assert "mute=1" in url
    assert "controls=0" in url
    assert "playlist=id123456789" in url


@pytest.mark.asyncio
async def test_trailer_page_renders_selected_iframe(monkeypatch):
    monkeypatch.setattr(settings, "tmdb_api_key", "tmdb-secret")
    monkeypatch.setattr(
        trailer_routes,
        "_get_trailer",
        lambda media_type, tmdb_id: _async_result(
            {
                "key": "id123456789",
                "title": "Trailer Indonesia",
                "language": "id",
                "type": "Trailer",
                "official": True,
            }
        ),
    )

    response = await trailer_routes._render_trailer(
        "movie", "550", autoplay=True, mute=True, controls=False, loop=True
    )
    body = response.body.decode()

    assert "youtube-nocookie.com/embed/id123456789" in body
    assert "autoplay=1" in body
    assert "mute=1" in body
    assert "tmdb-secret" not in body


@pytest.mark.asyncio
async def test_trailer_page_without_trailer_shows_placeholder(monkeypatch):
    monkeypatch.setattr(settings, "tmdb_api_key", "tmdb-secret")
    monkeypatch.setattr(
        trailer_routes,
        "_get_trailer",
        lambda media_type, tmdb_id: _async_result(None),
    )

    response = await trailer_routes._render_trailer(
        "tv", "1399", autoplay=True, mute=True, controls=False, loop=True
    )
    body = response.body.decode()

    assert "Trailer tidak tersedia" in body
    assert "youtube-nocookie.com" not in body


async def _async_result(value):
    return value


def test_banner_html_shows_poster_then_delayed_trailer_without_controls():
    page = trailer_routes._banner_html(
        "https://image.tmdb.org/t/p/w1280/backdrop.jpg",
        "/trailer/movie/550?autoplay=1&mute=1&controls=0&loop=1",
        "Fight Club",
        3000,
    ).body.decode()

    assert "w1280/backdrop.jpg" in page
    assert "setTimeout" in page
    assert ">3000<" in page or ", 3000)" in page
    assert "/trailer/movie/550" in page
    assert "pointer-events:none" in page
    assert 'target="_top"' not in page
    assert 'id="play"' not in page
    assert "/embed/auto/" not in page
    assert "api_key" not in page


def test_banner_router_registers_path_params_without_query_conflict():
    """Regression: FastAPI rejects Query defaults for path params at import time."""
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(trailer_routes.banner_router, prefix="/banner")
    paths = {route.path for route in app.routes}

    assert "/banner/movie/{tmdb_id}" in paths
    assert "/banner/tv/{tmdb_id}" in paths
    assert "/banner/tv/{tmdb_id}/{season}/{episode}" in paths


def test_banner_html_without_trailer_shows_poster_only():
    page = trailer_routes._banner_html(
        "https://image.tmdb.org/t/p/w1280/backdrop.jpg",
        None,
        "Game of Thrones",
        3000,
    ).body.decode()

    assert "w1280/backdrop.jpg" in page
    assert "/trailer/" not in page
    assert "/embed/auto/" not in page
    assert "Klik" not in page


@pytest.mark.asyncio
async def test_render_banner_uses_trailer_and_tmdb_backdrop(monkeypatch):
    monkeypatch.setattr(settings, "tmdb_api_key", "tmdb-secret")
    monkeypatch.setattr(
        trailer_routes,
        "_get_trailer",
        lambda media_type, tmdb_id: _async_result({"key": "id123456789", "title": "Trailer"}),
    )
    monkeypatch.setattr(
        trailer_routes,
        "_get_detail",
        lambda media_type, tmdb_id: _async_result(
            {
                "backdrop": "https://image.tmdb.org/t/p/w1280/backdrop.jpg",
                "title": "Fight Club",
            }
        ),
    )

    response = await trailer_routes._render_banner(
        "movie",
        "550",
        delay=2500,
        autoplay_trailer=True,
        mute=True,
    )
    body = response.body.decode()

    assert "Fight Club" in body
    assert "backdrop.jpg" in body
    assert "/trailer/movie/550?autoplay=1" in body
    assert "mute=1" in body
    assert "tmdb-secret" not in body
