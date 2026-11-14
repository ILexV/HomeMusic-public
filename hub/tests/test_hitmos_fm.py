"""hitmos.fm provider: RSC payload parsing on a real saved page + provider with respx mocks."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from app.config import HitmosFmCfg
from app.providers.base import ProviderError, ProviderUnavailable, TrackQuery
from app.providers.hitmos_fm import (
    HitmosFmProvider,
    decode_file_path,
    is_empty_search,
    parse_rsc_tracks,
    parse_search,
)

FIX = Path(__file__).parent / "fixtures" / "hitmos_fm"
BASE = "https://rus.hitmos.fm"
DL = "https://dwc1.hitmos.fm/L21wMy8yMzEyOTAyL9Ca0LjQvdC+IC0g0JPRgNGD0L/Qv9CwINC60YDQvtCy0LggKGhpdG1vcy5mbSkubXAz.mp3"
MP3 = b"ID3\x04\x00" + b"\x00" * (150 * 1024)


def fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def test_rsc_tracks():
    tracks = parse_rsc_tracks(fx("search_kino.html"))
    assert len(tracks) == 26
    t = tracks["9355210"]
    assert t["download"] == DL and t["duration"] == 287
    assert t["play"].startswith("https://plc1.hitmos.fm/")


def test_decode_file_path():
    assert decode_file_path(DL) == "/mp3/2312902/Кино - Группа крови (hitmos.fm).mp3"
    assert decode_file_path("https://plc1.hitmos.fm/L21wMy8yMzEyOTAyL3RyYWNrLm1wMw==.mp3") == "/mp3/2312902/track.mp3"


def test_parse_search():
    res = parse_search(fx("search_kino.html"), BASE)
    assert len(res) == 26
    c = res[0]
    assert (c.provider, c.provider_id, c.artist, c.title, c.duration_s) == (
        "hitmos_fm",
        "9355210",
        "Кино",
        "Группа крови",
        287,
    )
    assert c.page_url == f"{BASE}/song/9355210"
    assert c.download_url == DL
    assert c.cover_url.startswith("https://static-c1.hitmos.fm/")
    assert c.extra["file_id"] == "2312902" and c.extra["artist_id"] == "7276247"
    assert all(x.download_url and x.download_url.startswith("https://dwc1.hitmos.fm/") for x in res)


def test_empty_search():
    html = fx("search_empty.html")
    assert is_empty_search(html)
    assert parse_search(html, BASE) == []


@pytest.fixture
async def provider():
    client = httpx.AsyncClient(follow_redirects=True)
    p = HitmosFmProvider(HitmosFmCfg(rate_per_sec=1000), client=client)
    p.base_delay = 0.01
    yield p
    await client.aclose()


@respx.mock
async def test_search_and_fetch(provider, tmp_path):
    respx.get(f"{BASE}/").mock(return_value=httpx.Response(200, text="<html></html>"))
    search = respx.get(f"{BASE}/search").mock(return_value=httpx.Response(200, text=fx("search_kino.html")))
    respx.get(DL).mock(return_value=httpx.Response(200, content=MP3, headers={"Content-Type": "audio/mpeg"}))
    res = await provider.search(TrackQuery(artist="Кино", title="Группа крови"), limit=3)
    assert len(res) == 3 and search.calls.last.request.url.params["q"] == "Кино Группа крови"
    c = await provider.enrich(res[0])
    assert c.enriched and not res[0].enriched
    progress: list[float] = []
    path = await provider.fetch(c, tmp_path, on_progress=progress.append)
    assert path.parent == tmp_path and path.name.startswith("audio_") and path.suffix == ".mp3"
    assert path.read_bytes()[:3] == b"ID3"
    assert progress[-1] == 1.0 and not list(tmp_path.glob("*.part"))


@respx.mock
async def test_fetch_rejects_non_audio(provider, tmp_path):
    respx.get(DL).mock(return_value=httpx.Response(200, text="<html>nope</html>"))
    c = parse_search(fx("search_kino.html"), BASE)[0]
    with pytest.raises(ProviderError):
        await provider.fetch(c, tmp_path)
    assert list(tmp_path.iterdir()) == []


@respx.mock
async def test_unavailable(provider):
    respx.get(f"{BASE}/").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(ProviderUnavailable):
        await provider.search(TrackQuery(artist="a", title="b"))
    assert (await provider.health())["ok"] is False


@respx.mock
async def test_provider_id_cannot_choose_download_path(provider, tmp_path):
    respx.get(DL).mock(return_value=httpx.Response(200, content=MP3, headers={"Content-Type": "audio/mpeg"}))
    candidate = parse_search(fx("search_kino.html"), BASE)[0].model_copy(update={"provider_id": "../../outside"})
    first = await provider.fetch(candidate, tmp_path)
    second = await provider.fetch(candidate, tmp_path)
    assert first.parent == second.parent == tmp_path and first != second
    assert first.name.startswith("audio_") and second.name.startswith("audio_")
    assert first.read_bytes() == second.read_bytes() == MP3
