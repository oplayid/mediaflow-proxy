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
