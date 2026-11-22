"""DeezerClient tests on fixtures recorded from the live API (respx, no network)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.catalog.deezer import BASE_URL, CatalogNotFound, DeezerClient, DeezerError, dedupe_and_sort, map_album
from app.config import CatalogCfg
from app.db import dispose, init_engine

pytestmark = pytest.mark.respx(assert_all_called=False)

FIX = Path(__file__).parent / "fixtures" / "deezer"
ARTIST_ID = 73922432
ALBUM_ID = 1117245222
TRACK_ID = 4337103002


def load(name: str) -> Any:
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
async def db(db_file: Path):
    init_engine(db_file)
    yield
    await dispose()


@pytest.fixture
async def dz(db):
    client = DeezerClient(CatalogCfg(), client=httpx.AsyncClient())
    client.quota_delay_s = 0
    yield client
    await client.aclose()
    await client._client.aclose()


def albums_side_effect(request: httpx.Request) -> httpx.Response:
    index = int(request.url.params.get("index", "0"))
    return httpx.Response(200, json=load("artist_albums_p2" if index >= 25 else "artist_albums_p1"))


def mock_artist(router: respx.Router) -> dict[str, respx.Route]:
    return {
        "artist": router.get(f"{BASE_URL}/artist/{ARTIST_ID}").respond(json=load("artist")),
        "top": router.get(f"{BASE_URL}/artist/{ARTIST_ID}/top").respond(json=load("artist_top")),
        "albums": router.get(f"{BASE_URL}/artist/{ARTIST_ID}/albums").mock(side_effect=albums_side_effect),
    }


def mock_album(router: respx.Router) -> dict[str, respx.Route]:
    return {
        "album": router.get(f"{BASE_URL}/album/{ALBUM_ID}").respond(json=load("album")),
        "tracks": router.get(f"{BASE_URL}/album/{ALBUM_ID}/tracks").respond(json=load("album_tracks")),
    }


async def test_search_maps_all_kinds(dz: DeezerClient, respx_mock: respx.Router):
    respx_mock.get(f"{BASE_URL}/search/artist").respond(json=load("search_artist"))
    respx_mock.get(f"{BASE_URL}/search/album").respond(json=load("search_album"))
    respx_mock.get(f"{BASE_URL}/search/track").respond(json=load("search_track"))

    res = await dz.search("Три дня дождя")

    a = res.artists[0]
    assert (a.ref, a.name, a.nb_albums) == (f"dz:{ARTIST_ID}", "Три дня дождя", 47)
    assert a.picture_url and "1000x1000" in a.picture_url
    assert a.picture_small_url and "250x250" in a.picture_small_url
    assert res.albums and all(al.artist_ref for al in res.albums)
    assert res.albums[0].cover_url and "1000x1000" in res.albums[0].cover_url
    assert res.albums[0].cover_small_url and "250x250" in res.albums[0].cover_small_url
    assert res.tracks and all(t.source == "dz" and t.duration_s for t in res.tracks)
    assert all(t.album_ref and t.album_title for t in res.tracks)


async def test_search_selected_kinds_only(dz: DeezerClient, respx_mock: respx.Router):
    route = respx_mock.get(f"{BASE_URL}/search/artist").respond(json=load("search_artist"))
    res = await dz.search("Три дня дождя", kinds=("artist",))
    assert route.call_count == 1 and res.artists and not res.albums and not res.tracks


async def test_search_track_page_follows_index_until_the_end(dz: DeezerClient, respx_mock: respx.Router):
    items = load("search_track")["data"]  # 10 tracks: served 4 per page
    seen: list[tuple[int, int]] = []

    def side_effect(request: httpx.Request) -> httpx.Response:
        index, limit = int(request.url.params["index"]), int(request.url.params["limit"])
        seen.append((index, limit))
        page = items[index:index + limit]
        body: dict[str, Any] = {"data": page, "total": len(items)}
        if index + limit < len(items):
            body["next"] = f"{BASE_URL}/search/track?index={index + limit}"
        return httpx.Response(200, json=body)

    route = respx_mock.get(f"{BASE_URL}/search/track").mock(side_effect=side_effect)
    refs: list[str] = []
    offset: int | None = 0
    while offset is not None:
        tracks, offset = await dz.search_track_page("Три дня дождя", offset, 4)
        refs += [t.ref for t in tracks]
    assert seen == [(0, 4), (4, 4), (8, 4)]
    assert refs == [f"dz:{x['id']}" for x in items]

    # pages are cached by offset
    assert (await dz.search_track_page("Три дня дождя", 4, 4))[1] == 8 and route.call_count == 3


@pytest.mark.parametrize(("count", "total", "expected"), [(5, 10, None), (2, 10, 7), (5, None, 10)])
async def test_search_page_without_next_uses_reported_total(dz, respx_mock, count, total, expected):
    items = load("search_track")["data"][:count]
    body = {"data": items}
    if total is not None:
        body["total"] = total
    respx_mock.get(f"{BASE_URL}/search/track").respond(json=body)
    tracks, next_offset = await dz.search_track_page("Кино", offset=5, limit=5)
    assert [t.id for t in tracks] == [str(t["id"]) for t in items]
    assert next_offset == expected


async def test_genre_wave_uses_only_radios_of_requested_genre(dz, respx_mock, monkeypatch):
    from app.mixes.genre_wave import genre_tracks
    from app.services import svc

    items = load("search_track")["data"]
    respx_mock.get(f"{BASE_URL}/genre/98/radios").respond(json={"data": [
        {"id": 10, "title": "Opera"}, {"id": 20, "title": "Classical"}]})
    respx_mock.get(f"{BASE_URL}/radio/10/tracks").respond(json={"data": items[1:2]})
    respx_mock.get(f"{BASE_URL}/radio/20/tracks").respond(json={"data": items[:1]})
    monkeypatch.setattr(svc, "deezer", dz)
    assert [t.id for t in await genre_tracks("classical", 0)] == [str(items[0]["id"])]
    assert [t.id for t in await genre_tracks("classical", 1)] == [str(items[1]["id"])]
    assert await genre_tracks("classical", 2) == []


async def test_genre_wave_library_song_overrides_album_even_when_catalog_is_down(dz, respx_mock, monkeypatch):
    from app.db import session_scope
    from app.mixes.genre_wave import genre_tracks
    from app.models import Album, Track
    from app.services import svc

    async with session_scope() as s:
        album = Album(uid="compilation", title="Сборник", album_artist="Разные", genre="КЛАССИКА")
        s.add(album)
        await s.flush()
        for name, tag in (("sonata", None), ("pop-song", "Pop"), ("untagged", None)):
            s.add(Track(uid=name, path=f"{name}.flac", nd_song_id=name, artist="Artist", title=name, genre=tag,
                        album_id=None if name == "untagged" else album.id, fingerprint=f"artist|{name}"))
    monkeypatch.setattr(svc, "deezer", dz)
    for genre_id in (98, 132):
        respx_mock.get(f"{BASE_URL}/genre/{genre_id}/radios").respond(json={"error": {"code": 800}})
    assert [t.ref for t in await genre_tracks("classical", 0)] == ["nd:sonata"]
    assert [t.ref for t in await genre_tracks("pop", 0)] == ["nd:pop-song"]


async def test_search_tracks_falls_back_to_plain_query(dz: DeezerClient, respx_mock: respx.Router):
    queries: list[str] = []

    def side_effect(request: httpx.Request) -> httpx.Response:
        q = request.url.params["q"]
        queries.append(q)
        return httpx.Response(200, json=load("search_track_adv_empty" if "artist:" in q else "search_track_plain"))

    respx_mock.get(f"{BASE_URL}/search/track").mock(side_effect=side_effect)
    tracks = await dz.search_tracks("Три дня дождя", "Вина")

    assert queries == ['artist:"Три дня дождя" track:"Вина"', "Три дня дождя Вина"]
    assert tracks[0].title == "Вина" and tracks[0].artist == "Три дня дождя"


async def test_search_albums_advanced(dz: DeezerClient, respx_mock: respx.Router):
    route = respx_mock.get(f"{BASE_URL}/search/album").respond(json=load("search_album_adv"))
    albums = await dz.search_albums("Три дня дождя", "melancholia")
    assert route.call_count == 1
    assert route.calls[0].request.url.params["q"] == 'artist:"Три дня дождя" album:"melancholia"'
    assert [(a.title, a.record_type, a.explicit) for a in albums] == [("melancholia", "album", True)]


async def test_artist_page_paginates_sorts_and_maps(dz: DeezerClient, respx_mock: respx.Router):
    routes = mock_artist(respx_mock)

    page = await dz.get_artist_page(ARTIST_ID)

    assert routes["albums"].call_count == 2  # 25 + 22 items, followed `next`
    assert page.artist.name == "Три дня дождя"
    assert len(page.albums) == 47
    dates = [a.release_date for a in page.albums]
    assert dates == sorted(dates, reverse=True)
    kinds = {k: sum(a.record_type == k for a in page.albums) for k in ("album", "single", "ep")}
    assert kinds == {"album": 7, "single": 37, "ep": 3}
    assert all(a.artist == "Три дня дождя" and a.artist_ref == f"dz:{ARTIST_ID}" for a in page.albums)
    assert all(a.year == a.release_date.year for a in page.albums if a.release_date)

    assert len(page.top_tracks) == 10
    feat = next(t for t in page.top_tracks if t.artist == "Tritia")
    assert feat.artists == ["Tritia", "Три дня дождя"]
    assert page.top_tracks[0].artists == ["Три дня дождя", "Лали"]
    assert page.top_tracks[0].cover_url and "1000x1000" in page.top_tracks[0].cover_url
    assert page.top_tracks[0].cover_small_url and "250x250" in page.top_tracks[0].cover_small_url


async def test_dedupe_keeps_explicit_and_sorts_newest_first():
    art = {"id": 1, "name": "X"}
    raw = [
        {"id": 10, "title": "Same", "record_type": "single", "release_date": "2024-01-01", "explicit_lyrics": False},
        {"id": 11, "title": "same ", "record_type": "single", "release_date": "2024-05-01", "explicit_lyrics": True},
        {"id": 12, "title": "Same", "record_type": "album", "release_date": "2024-02-01", "explicit_lyrics": False},
        {"id": 13, "title": "Same", "record_type": "single", "release_date": "2023-01-01", "explicit_lyrics": False},
        {"id": 14, "title": "Odd", "record_type": "compile", "release_date": "2025-01-01"},
        {"id": 15, "title": "Undated", "record_type": "weird", "release_date": "0000-00-00"},
    ]
    out = dedupe_and_sort([map_album(x, art) for x in raw])
    assert [a.id for a in out] == ["14", "11", "12", "13", "15"]
    assert out[0].record_type == "compile" and out[-1].record_type == "album" and out[-1].year is None


async def test_get_album_full_tracklist(dz: DeezerClient, respx_mock: respx.Router):
    routes = mock_album(respx_mock)

    album = await dz.get_album(ALBUM_ID)

    assert routes["album"].call_count == 1 and routes["tracks"].call_count == 1
    assert (album.title, album.artist, album.record_type, album.year) == (
        "У ЧАСТЬ ВТОРАЯ",
        "Три дня дождя",
        "album",
        2026,
    )
    assert album.upc == "4660275341657" and album.genre == "Alternative" and album.explicit is True
    assert album.nb_tracks == 12 and album.tracks is not None and len(album.tracks) == 12
    assert [t.track_no for t in album.tracks] == list(range(1, 13))
    t1, t2 = album.tracks[0], album.tracks[1]
    assert (t1.title, t1.isrc, t1.disc_no, t1.explicit) == ("Кальций", "RUAGV2635740", 1, False)
    assert t2.explicit is True
    assert (
        t1.album_ref == f"dz:{ALBUM_ID}" and t1.album_title == "У ЧАСТЬ ВТОРАЯ" and t1.album_artist == "Три дня дождя"
    )
    assert t1.release_date == date(2026, 10, 9) and t1.cover_url == album.cover_url
    assert t1.cover_small_url == album.cover_small_url and "250x250" in t1.cover_small_url  # lists load this one
    assert t1.artist_ref == f"dz:{ARTIST_ID}" and t1.preview_url


async def test_album_tracks_follow_next_page(dz: DeezerClient, respx_mock: respx.Router):
    respx_mock.get(f"{BASE_URL}/album/{ALBUM_ID}").respond(json=load("album"))
    full = load("album_tracks")
    p1 = {"data": full["data"][:5], "total": 12, "next": f"{BASE_URL}/album/{ALBUM_ID}/tracks?limit=5&index=5"}
    p2 = {"data": full["data"][5:], "total": 12, "prev": f"{BASE_URL}/album/{ALBUM_ID}/tracks?limit=5&index=0"}

    def side_effect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=p2 if request.url.params.get("index") == "5" else p1)

    route = respx_mock.get(f"{BASE_URL}/album/{ALBUM_ID}/tracks").mock(side_effect=side_effect)
    album = await dz.get_album(ALBUM_ID)
    assert route.call_count == 2 and album.tracks is not None and len(album.tracks) == 12


@pytest.mark.parametrize("failure", ["short", "empty", "cycle"])
async def test_incomplete_album_pages_do_not_poison_cache(dz: DeezerClient, respx_mock: respx.Router, failure):
    url = f"{BASE_URL}/album/{ALBUM_ID}"
    respx_mock.get(url).respond(json=load("album"))
    full = load("album_tracks")
    next_url = f"{url}/tracks?index=5"
    first = {"data": full["data"][:5], "next": next_url}
    if failure == "short":
        first = {"data": first["data"], "total": full["total"]}
    last = {"data": []} if failure == "empty" else {"data": full["data"][5:6], "next": next_url}
    route = respx_mock.get(f"{url}/tracks").mock(side_effect=lambda req: httpx.Response(
        200, json=last if "index" in req.url.params else first))

    with pytest.raises(DeezerError):
        await dz.get_album(ALBUM_ID)

    route.respond(json=full)
    album = await dz.get_album(ALBUM_ID)
    assert [t.id for t in album.tracks] == [str(t["id"]) for t in full["data"]]


async def test_get_track_maps_isrc_and_contributors(dz: DeezerClient, respx_mock: respx.Router):
    respx_mock.get(f"{BASE_URL}/track/{TRACK_ID}").respond(json=load("track"))
    t = await dz.get_track(TRACK_ID)
    assert (t.ref, t.title, t.artist, t.artists) == (f"dz:{TRACK_ID}", "Кальций", "Три дня дождя", ["Три дня дождя"])
    assert (t.isrc, t.track_no, t.disc_no, t.explicit, t.duration_s) == ("RUAGV2635740", 1, 1, False, 168)
    assert t.release_date == date(2026, 10, 9)
    assert (t.album_ref, t.album_title) == (f"dz:{ALBUM_ID}", "У ЧАСТЬ ВТОРАЯ")


async def test_quota_error_is_retried(dz: DeezerClient, respx_mock: respx.Router):
    route = respx_mock.get(f"{BASE_URL}/track/{TRACK_ID}").mock(
        side_effect=[
            httpx.Response(200, json=load("error_quota")),
            httpx.Response(200, json=load("error_quota")),
            httpx.Response(200, json=load("track")),
        ]
    )
    t = await dz.get_track(TRACK_ID)
    assert route.call_count == 3 and t.isrc == "RUAGV2635740"


async def test_quota_error_gives_up(dz: DeezerClient, respx_mock: respx.Router):
    dz.quota_retries = 2
    route = respx_mock.get(f"{BASE_URL}/track/{TRACK_ID}").respond(json=load("error_quota"))
    with pytest.raises(DeezerError):
        await dz.get_track(TRACK_ID)
    assert route.call_count == 3


async def test_not_found(dz: DeezerClient, respx_mock: respx.Router):
    respx_mock.get(f"{BASE_URL}/album/1").respond(json=load("error_nodata"))
    respx_mock.get(f"{BASE_URL}/album/1/tracks").respond(json=load("error_nodata"))
    with pytest.raises(CatalogNotFound):
        await dz.get_album(1)


async def test_second_call_is_served_from_cache(dz: DeezerClient, respx_mock: respx.Router):
    artist_routes = mock_artist(respx_mock)
    album_routes = mock_album(respx_mock)
    search = respx_mock.get(f"{BASE_URL}/search/artist").respond(json=load("search_artist"))
    track = respx_mock.get(f"{BASE_URL}/track/{TRACK_ID}").respond(json=load("track"))

    async def run_all():
        return (
            await dz.get_artist_page(ARTIST_ID),
            await dz.get_album(ALBUM_ID),
            await dz.search("Три дня дождя", kinds=("artist",)),
            await dz.get_track(TRACK_ID),
        )

    first = await run_all()
    calls = [r.call_count for r in (*artist_routes.values(), *album_routes.values(), search, track)]
    second = await run_all()
    assert [r.call_count for r in (*artist_routes.values(), *album_routes.values(), search, track)] == calls
    assert first == second
    assert second[1].tracks and second[1].tracks[0].release_date == date(2026, 10, 9)  # re-validated from JSON

    # a fresh client over the same DB also hits the cache
    other = DeezerClient(CatalogCfg(), client=httpx.AsyncClient())
    try:
        assert (await other.get_album(ALBUM_ID)) == first[1]
    finally:
        await other._client.aclose()
    assert album_routes["album"].call_count == 1
