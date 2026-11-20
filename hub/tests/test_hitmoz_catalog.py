"""hitmoz catalog: artist/album pages -> catalog models, cached in a temp SQLite DB (respx, no network)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from app.catalog.hitmoz_catalog import HitmozCatalog
from app.config import HitmozCfg
from app.db import dispose, init_engine
from app.providers.hitmoz import HitmozProvider

FIX = Path(__file__).parent / "fixtures" / "hitmoz"
BASE = "https://ru.hitmoz.org"


def fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.fixture
async def catalog(db_file):
    init_engine(db_file)
    client = httpx.AsyncClient(follow_redirects=True)
    provider = HitmozProvider(HitmozCfg(rate_per_sec=1000), client=client)
    provider.base_delay = 0.01
    yield HitmozCatalog(provider)
    await client.aclose()
    await dispose()


@respx.mock
async def test_search_artists(catalog):
    respx.get(f"{BASE}/").mock(return_value=httpx.Response(200, text=""))
    route = respx.get(f"{BASE}/search").mock(return_value=httpx.Response(200, text=fx("search_artist_kino.html")))
    artists = await catalog.search_artists("Кино")
    assert artists[0].ref == "hm:1286" and artists[0].source == "hm" and artists[0].name == "Кино"
    assert artists[0].picture_url == "https://avatars.yandex.net/get-music-content/49707/703d86d2.p.41075/400x400"
    again = await catalog.search_artists("кино")
    assert [a.ref for a in again] == [a.ref for a in artists]
    assert route.call_count == 1  # cached


@respx.mock
async def test_search_artists_none(catalog):
    respx.get(f"{BASE}/").mock(return_value=httpx.Response(200, text=""))
    respx.get(f"{BASE}/search").mock(return_value=httpx.Response(200, text=fx("search_empty.html")))
    assert await catalog.search_artists("zzz") == []


@respx.mock
async def test_similar_seed_reads_only_exact_track_metadata(catalog):
    respx.get(f"{BASE}/").mock(return_value=httpx.Response(200, text=""))
    page = respx.get(f"{BASE}/song/79436285").mock(return_value=httpx.Response(200, text=fx("track_kino.html")))
    track = await catalog.get_track("79436285")
    assert track and (track.ref, track.title, track.artist_ref) == ("hm:79436285", "Группа крови", "hm:1286")
    assert await catalog.get_track("79436285") == track and page.call_count == 1
    assert await catalog.get_track("../artist/1286") is None


@respx.mock
async def test_get_artist(catalog):
    respx.get(f"{BASE}/").mock(return_value=httpx.Response(200, text=""))
    page1 = respx.get(f"{BASE}/artist/1286").mock(return_value=httpx.Response(200, text=fx("artist_kino.html")))
    more = respx.get(url__regex=rf"{BASE}/artist/1286/start/\d+").mock(
        return_value=httpx.Response(200, text=fx("artist_kino.html"))
    )
    respx.get(f"{BASE}/search").mock(return_value=httpx.Response(200, text=fx("search_artist_kino.html")))
    page = await catalog.get_artist("1286")
    assert page.artist.ref == "hm:1286" and page.artist.name == "Кино"
    assert page.artist.picture_url and "avatars.yandex.net" in page.artist.picture_url
    assert more.call_count == 2  # max 3 pages in total
    assert len(page.top_tracks) == 48  # duplicates across pages removed
    t = page.top_tracks[0]
    assert (t.ref, t.id, t.source, t.title) == ("hm:64291868", "64291868", "hm", "Хочу перемен")
    assert t.artist == "Кино" and t.artists == ["Кино", "Виктор Цой"] and t.artist_ref == "hm:1286"
    assert t.duration_s == 317
    assert page.albums == []
    await catalog.get_artist("1286")
    assert page1.call_count == 1  # cached


@respx.mock
async def test_get_album(catalog):
    respx.get(f"{BASE}/").mock(return_value=httpx.Response(200, text=""))
    route = respx.get(f"{BASE}/album/488389").mock(return_value=httpx.Response(200, text=fx("album.html")))
    album = await catalog.get_album("488389")
    assert (album.ref, album.title, album.year, album.artist) == (
        "hm:488389",
        "Акустический альбом",
        1998,
        "Король и Шут",
    )
    assert album.artist_ref == "hm:1701" and album.genre == "Панк" and album.nb_tracks == 17
    assert album.cover_url.endswith("/1000x1000") and album.cover_small_url.endswith("/200x200")
    assert album.tracks and album.tracks[0].track_no == 1 and album.tracks[0].album_ref == "hm:488389"
    assert album.tracks[0].title == "Ели мясо мужики" and album.tracks[0].duration_s == 134
    cached = await catalog.get_album("488389")
    assert cached == album and route.call_count == 1
