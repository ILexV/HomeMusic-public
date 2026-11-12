"""Lyrics parser, version guards and authenticated provider boundary (no live network)."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.api import lyrics as api
from app.auth import current_user
from app.catalog.models import CatalogTrack
from app.db import get_session


@pytest.fixture(autouse=True)
def clear_lyrics_cache():
    api._cache.clear()
    yield
    api._cache.clear()


def test_multiple_timestamps_fractional_seconds_and_stable_multiline():
    parsed = api.parse_lrc("[ar:Example]\n[00:12.5][00:22.050]Refrain\n[00:12.500]Translation\n[01:02:25]End")
    assert [(line.time_s, line.text) for line in parsed] == [
        (12.5, "Refrain\nTranslation"), (22.05, "Refrain"), (62.25, "End")]


def test_global_offset_even_at_end_and_empty_gap():
    parsed = api.parse_lrc("[00:01.25]First\n[00:03.00]\n[00:04]Last\n[offset:+500]")
    assert [(line.time_s, line.text) for line in parsed] == [(0.75, "First"), (2.5, ""), (3.5, "Last")]
    assert api.parse_lrc("[offset:-250]\n[00:01]Late")[0].time_s == 1.25
    assert api.parse_lrc("[00:00.100]Start\n[offset:500]")[0].time_s == 0.0


def test_ordering_invalid_stamps_and_plain_text_do_not_fabricate_timing():
    lines = api.parse_lrc("Plain text\n[00:99]Invalid\n[00:12.250]Later\n[00:02.5]Earlier")
    assert [line.time_s for line in lines] == [2.5, 12.25]
    assert api.parse_lrc("[ti:Title]\nA plain line\nAnother line") == []
    assert api.parse_lrc("[00:01.1234]Invalid fraction") == []
    assert api.parse_lrc("x" * (api.MAX_TEXT + 1)) == []


META = api.Metadata("Song", "Artist", "Album", 200.0)
RECORD = {"trackName": "Song", "artistName": "Artist", "albumName": "Album", "duration": 200.5,
          "syncedLyrics": "[00:01.50]First\n[00:03]Second", "plainLyrics": "First\nSecond", "instrumental": False}


@pytest.mark.parametrize("field,value", [
    ("trackName", "Song (Live)"), ("trackName", "Song - Radio Edit"),
    ("trackName", "Song (Remastered)"), ("artistName", "Cover Artist"),
    ("albumName", "Live Album"), ("duration", 203), ("duration", "NaN"), ("duration", None),
])
def test_wrong_versions_are_rejected(field, value):
    assert not api.version_matches(META, {**RECORD, field: value})


def test_matching_preserves_qualifiers_but_tolerates_case_and_punctuation():
    assert api.version_matches(META, {**RECORD, "trackName": "SONG!", "artistName": "artist"})
    assert api.version_matches(META, {**RECORD, "duration": 202})
    assert not api.version_matches(replace(META, album="", duration_s=None), RECORD)
    assert api.version_matches(replace(META, duration_s=None), RECORD)
    assert not api.version_matches(replace(META, title="Song (Live)"), RECORD)


@pytest.mark.parametrize("body", [
    {}, {"title": "Song"}, {"title": " ", "artist": "Artist"},
    {"ref": "https://attacker.invalid"}, {"ref": "dz:../1"},
    {"ref": "dz:1", "nd_song_id": "1"},
    {"title": "Song", "artist": "Artist", "duration_s": -1},
    {"title": "Song", "artist": "Artist", "duration_s": float("inf")},
])
def test_request_validation(body):
    with pytest.raises(ValidationError):
        api.LyricsIn.model_validate(body)


async def test_real_provider_shape_is_parsed_and_cached(respx_mock):
    route = respx_mock.get(api.PROVIDER_URL).respond(json=RECORD)
    result = await api.fetch_lyrics(META)
    assert result.status == "synced"
    assert [line.time_s for line in result.lines] == [1.5, 3.0]
    assert route.calls[0].request.url.params["duration"] == "200.0"
    assert route.calls[0].request.url.params["album_name"] == "Album"
    assert await api.fetch_lyrics(META) == result
    assert route.call_count == 1


@pytest.mark.parametrize("record,status,times", [
    ({**RECORD, "instrumental": True}, "instrumental", []),
    ({**RECORD, "syncedLyrics": None}, "plain", [None, None]),
    ({**RECORD, "syncedLyrics": "Not LRC"}, "plain", [None, None]),
    ({**RECORD, "duration": 300}, "unavailable", []),
    ({**RECORD, "syncedLyrics": None, "plainLyrics": None}, "unavailable", []),
])
async def test_unavailable_instrumental_and_plain_are_explicit(respx_mock, record, status, times):
    respx_mock.get(api.PROVIDER_URL).respond(json=record)
    result = await api.fetch_lyrics(META)
    assert result.status == status
    assert [line.time_s for line in result.lines] == times


async def test_not_found_is_negative_cached(respx_mock):
    route = respx_mock.get(api.PROVIDER_URL).respond(404)
    assert (await api.fetch_lyrics(META)).status == "unavailable"
    assert (await api.fetch_lyrics(META)).status == "unavailable"
    assert route.call_count == 1


async def test_cache_is_bounded(respx_mock):
    respx_mock.get(api.PROVIDER_URL).respond(404)
    for index in range(130):
        await api.fetch_lyrics(replace(META, title=f"Song {index}"))
    assert len(api._cache) == 128
    assert replace(META, title="Song 0") not in api._cache


async def test_canonical_metadata_overrides_client_titles(monkeypatch):
    async def catalog(ref):
        assert ref == "dz:123"
        return CatalogTrack(ref=ref, source="dz", id="123", title="Canonical", artist="Artist",
                            album_title="Album", duration_s=200)
    monkeypatch.setattr("app.mixes.resolve.catalog_track", catalog)
    result = await api.resolve_metadata(api.LyricsIn(ref="dz:123", title="Spoofed", artist="Wrong"), SimpleNamespace())
    assert result == api.Metadata("Canonical", "Artist", "Album", 200)


async def test_library_metadata_uses_account_client_and_closes(monkeypatch):
    calls = []
    user = SimpleNamespace(id=7)

    class Client:
        async def get_song(self, song_id):
            calls.append(song_id)
            return {"id": song_id, "artist": "Artist", "title": "Canonical", "duration": 200}

        async def aclose(self):
            calls.append("closed")

    def client(account):
        assert account is user
        return Client()

    monkeypatch.setattr(api, "user_navidrome", client)
    result = await api.resolve_metadata(api.LyricsIn(nd_song_id="song1", title="Wrong"), user)
    assert result.title == "Canonical"
    assert calls == ["song1", "closed"]


async def test_unauthenticated_endpoint_never_calls_provider(monkeypatch):
    app = FastAPI()
    app.include_router(api.router)

    async def no_database():
        yield None

    async def forbidden(*args):
        pytest.fail("unauthenticated request reached lyrics lookup")

    app.dependency_overrides[get_session] = no_database
    monkeypatch.setattr(api, "resolve_metadata", forbidden)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/lyrics", json={"ref": "dz:123"})
    assert response.status_code == 401


async def test_provider_timeout_is_graceful_authenticated_response(monkeypatch, respx_mock):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=7)
    respx_mock.get(api.PROVIDER_URL).mock(side_effect=httpx.ReadTimeout("timed out"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/lyrics", json={"title": "Song", "artist": "Artist", "album": "Album",
                                                        "duration_s": 200})
    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert response.json()["lines"] == []
