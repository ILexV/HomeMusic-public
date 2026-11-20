"""The Navidrome library for the PWA in a browser (app/api/medialib.py, playlist pages): artists, albums,
«Понравившиеся», «Главная», covers.

Navidrome is a mock: artists «Кино» (♥, 2 albums) and «Кто-то» (1 album); album "alK" holds «Группа крови» (song
"ndK", known to the Hub as Deezer dz:100) and «Звезда» ("ndZ", only Navidrome knows it). Playlist "pl1" holds both.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import respx

from tests.support import app_client

OK = {"status": "ok", "version": "1.16.1"}
SONGS = [
    {"id": "ndK", "title": "Группа крови", "artist": "Кино", "artistId": "arK", "album": "Группа крови",
     "albumId": "alK", "coverArt": "al-alK", "duration": 245, "track": 1, "starred": "2026-10-01T10:00:00Z"},
    {"id": "ndZ", "title": "Звезда", "artist": "Кино", "artistId": "arK", "album": "Группа крови",
     "albumId": "alK", "coverArt": "al-alK", "duration": 220, "track": 2, "starred": "2026-10-20T10:00:00Z"},
]
ALBUMS = [
    {"id": "alK", "name": "Группа крови", "artist": "Кино", "artistId": "arK", "year": 1988, "songCount": 2,
     "duration": 465, "coverArt": "al-alK", "starred": "2026-10-02T00:00:00Z"},
    {"id": "alK2", "name": "Звезда по имени Солнце", "artist": "Кино", "artistId": "arK", "year": 1989, "songCount": 10},
]
ARTISTS = [
    {"id": "arX", "name": "Кто-то", "albumCount": 1},
    {"id": "arK", "name": "Кино", "albumCount": 2, "coverArt": "ar-arK", "starred": "2026-10-01T00:00:00Z"},
]


@pytest.fixture
async def env(hub_env):
    from app.main import create_app

    # a test sets state["down"] to make Navidrome unreachable, state["fail"][<method>] = <Subsonic error code>
    state: dict = {"calls": [], "fail": {}, "down": False}

    def nd(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        p = request.url.params
        state["calls"].append((name, dict(p)))
        if state["down"]:
            raise httpx.ConnectError("connection refused", request=request)
        if name in state["fail"]:
            error = {"code": state["fail"][name], "message": "failed"}
            return httpx.Response(200, json={"subsonic-response": {"status": "failed", "error": error}})
        body: dict = {}
        if name == "getUser":
            body = {"user": {"adminRole": True}}
        elif name == "getArtists":
            body = {"artists": {"index": [{"name": "К", "artist": ARTISTS}]}}
        elif name == "getAlbumList2":
            body = {"albumList2": {"album": ALBUMS[:1] if p["type"] == "starred" else ALBUMS}}
        elif name == "getAlbum":
            if p["id"] != "alK":
                return httpx.Response(200, json={"subsonic-response": {"status": "failed", "error": {"code": 70, "message": "not found"}}})
            body = {"album": {**ALBUMS[0], "song": SONGS, **state.get("album", {})}}
        elif name == "getArtist":
            body = {"artist": {**ARTISTS[1], "album": state.get("artist_albums", ALBUMS)}}
        elif name == "getStarred2":
            body = {"starred2": {"song": SONGS, "album": ALBUMS[:1], "artist": ARTISTS[1:]}}
        elif name == "getPlaylist":
            body = {"playlist": {"id": "pl1", "name": "Дорога", "songCount": 2, "duration": 465, "entry": SONGS}}
        elif name == "getCoverArt":
            return httpx.Response(200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"})
        return httpx.Response(200, json={"subsonic-response": {**OK, **body}})

    app = create_app()
    async with app.router.lifespan_context(app):
        with respx.mock(assert_all_called=False) as mock:
            mock.route(host="navidrome.test").mock(side_effect=nd)
            async with app_client(app) as client:
                await add_track("Кино", "Группа крови", "ndK", deezer_track_id="100")
                yield {"client": client, "state": state, "mock": mock}


async def add_track(artist: str, title: str, nd_song_id: str, **kw) -> int:
    from app.db import session_scope
    from app.matching.normalize import fingerprint
    from app.models import Track

    async with session_scope() as s:
        t = Track(uid=str(uuid.uuid4()), path=f"{artist}/{title}.mp3", nd_song_id=nd_song_id, artist=artist,
                  title=title, duration_s=245, fingerprint=fingerprint(artist, title), **kw)
        s.add(t)
        await s.flush()
        return t.id


async def test_library_search_finds_substrings_regardless_of_unicode_case(env, monkeypatch):
    from app.services import svc

    monkeypatch.setattr(svc, "deezer", None)
    monkeypatch.setattr(svc, "hitmoz_catalog", None)
    await add_track("Дуэт", "Про кино", "title")
    await add_track("Оркестр", "Тема", "album", album="КИНОфильмы")
    await add_track("Трио", "100%_live", "punctuation")

    for query, expected in [
        ("кино", {"ndK", "title", "album"}), ("КИНО", {"ndK", "title", "album"}),
        ("%", {"punctuation"}), ("_", {"punctuation"}),
        ("Кино крови", {"ndK"}),  # no literal phrase; still match words across artist and title
    ]:
        response = await env["client"].get("/api/search", params={"q": query})
        assert response.status_code == 200, response.text
        assert {t["nd_song_id"] for t in response.json()["library"]} == expected, query


@pytest.mark.parametrize("failed", ["artist", "album", "track"])
async def test_search_keeps_available_sections_on_partial_catalog_failure(env, monkeypatch, failed):
    from app.catalog.deezer import BASE_URL
    from app.catalog.models import CatalogArtist
    from app.services import svc

    artist = {"id": 1, "name": "Кино"}
    for kind, row in {"artist": artist, "album": {"id": 10, "title": "Ночь", "artist": artist},
                      "track": {"id": 100, "title": "Группа крови", "artist": artist}}.items():
        route = env["mock"].get(f"{BASE_URL}/search/{kind}")
        if kind == failed:
            route.respond(400)
        else:
            route.respond(json={"data": [row], "total": 2})

    async def fallback(q):
        return [CatalogArtist(id="9", ref="hm:9", source="hm", name="Кино")]

    monkeypatch.setattr(svc.hitmoz_catalog, "search_artists", fallback)
    response = await env["client"].get("/api/search", params={"q": "Кино"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert [t["nd_song_id"] for t in data["library"]] == ["ndK"]
    assert [a["ref"] for a in data["artists"]] == (["hm:9"] if failed == "artist" else ["dz:1"])
    assert [a["ref"] for a in data["albums"]] == ([] if failed == "album" else ["dz:10"])
    assert [(t["ref"], t["owned"]) for t in data["tracks"]] == ([] if failed == "track" else [("dz:100", True)])
    assert data["tracks_next"] == (None if failed == "track" else 1)
    assert data["errors"]


@pytest.mark.parametrize("params", [{"limit": -1}, {"limit": 0}, {"offset": -1}])
async def test_managed_album_list_rejects_invalid_page_bounds(env, params):
    response = await env["client"].get("/api/library/albums", params=params)
    assert response.status_code == 422


async def test_managed_album_pages_order_ties_and_skip_only_matching_albums(env):
    from datetime import datetime

    from app.db import session_scope
    from app.models import Album

    async with session_scope() as s:
        for title in ("Ночь первая", "Ночь вторая", "День", "Ночь третья"):
            album = Album(uid=str(uuid.uuid4()), title=title, album_artist="Кино", updated_at=datetime(2020, 1, 1))
            s.add(album)
        await s.flush()
        newest_id = album.id
    await add_track("Кино", "Дорога", "ndRoad", album_id=newest_id)
    for params, expected in [
        ({}, [("Ночь третья", 1), ("День", 0)]),
        ({"q": "ночь КИНО", "offset": 1}, [("Ночь вторая", 0), ("Ночь первая", 0)]),
        ({"q": "ночь КИНО", "offset": 3}, []),
    ]:
        response = await env["client"].get("/api/library/albums", params={"limit": 2, **params})
        assert response.status_code == 200, response.text
        assert [(a["title"], a["track_count"]) for a in response.json()] == expected


async def test_album_songs_are_owned_tracks_with_the_hubs_refs(env):
    r = await env["client"].get("/api/nd/albums/alK")
    assert r.status_code == 200, r.text
    a = r.json()
    assert (a["title"], a["year"], a["cover_url"]) == ("Группа крови", 1988, "/api/nd/cover/al-alK?size=300")
    assert a["catalog_status"] is None
    k, z = a["tracks"]
    # the Hub's record gives the Deezer ref; a song only Navidrome knows plays by nd:<song>
    assert (k["ref"], k["nd_song_id"], k["owned"], k["library_track_id"] is not None) == ("dz:100", "ndK", True, True)
    assert (z["ref"], z["source"], z["nd_song_id"], z["library_track_id"]) == ("nd:ndZ", "nd", "ndZ", None)
    assert (z["nd_album_id"], z["nd_artist_id"], z["cover_url"]) == ("alK", "arK", "/api/nd/cover/al-alK?size=1000")

    assert (await env["client"].get("/api/nd/albums/nope")).status_code == 404


async def test_partial_album_catalog_preview_is_metadata_only(env, monkeypatch):
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import Album, DownloadItem, Track
    from app.services import svc
    from tests.support import FakeDeezer

    catalog = FakeDeezer()
    missing = catalog.track.model_copy(update={"id": "101", "ref": "dz:101", "title": "Новый трек",
                                               "track_no": 3, "isrc": None})
    catalog.album.tracks = [catalog.track, missing]
    monkeypatch.setattr(svc, "deezer", catalog)
    async with session_scope() as s:
        album = Album(uid=str(uuid.uuid4()), title=catalog.album.title, album_artist=catalog.album.artist,
                      deezer_album_id=catalog.album.id)
        s.add(album)
        await s.flush()
        track = await s.scalar(select(Track).where(Track.nd_song_id == "ndK"))
        track.album_id = album_id = album.id

    client = env["client"]
    nd = await client.get("/api/nd/albums/alK?catalog=true")
    assert nd.status_code == 200, nd.text
    assert nd.json()["catalog_status"] == "matched"
    assert [(t["ref"], t["owned"], t["nd_song_id"]) for t in nd.json()["tracks"]] == [
        ("dz:100", True, "ndK"), ("dz:101", False, None), ("nd:ndZ", True, "ndZ")]
    managed = await client.get(f"/api/library/albums/{album_id}?catalog=true")
    assert managed.status_code == 200, managed.text
    assert [t["ref"] for t in managed.json()["tracks"]] == ["dz:100"]
    assert [(t["ref"], t["owned"]) for t in managed.json()["other_tracks"]] == [("dz:101", False)]
    async with session_scope() as s:
        assert await s.scalar(select(func.count()).select_from(DownloadItem)) == 0
    assert not catalog.previews


@pytest.mark.parametrize("down", [False, True])
async def test_album_falls_back_to_second_catalog(env, monkeypatch, down):
    from app.services import svc
    from tests.support import FakeDeezer

    catalog = FakeDeezer()
    catalog.track.album_ref = None
    catalog.track.album_title = None

    async def no_match(artist, title):
        if down:
            raise httpx.ConnectError("unavailable")
        return [catalog.album.model_copy(update={"title": "Другой релиз"})]

    class Hitmoz:
        async def search_albums(self, query):
            assert query == catalog.album.title
            return [catalog.album.model_copy(update={"ref": "hm:99", "source": "hm", "id": "99", "artist": ""})]

        async def get_album(self, id_):
            assert id_ == "99"
            missing = catalog.track.model_copy(update={"ref": "hm:2", "source": "hm", "id": "2",
                                                       "title": "Недостающий трек", "isrc": None})
            local = catalog.track.model_copy(update={"ref": "hm:1", "source": "hm", "id": "1", "isrc": None})
            return catalog.album.model_copy(update={"id": "99", "ref": "hm:99", "source": "hm", "tracks": [missing, local]})

    monkeypatch.setattr(catalog, "search_albums", no_match)
    monkeypatch.setattr(svc, "deezer", catalog)
    monkeypatch.setattr(svc, "hitmoz_catalog", Hitmoz())
    response = await env["client"].get("/api/nd/albums/alK?catalog=true")
    assert response.status_code == 200, response.text
    assert response.json()["catalog_status"] == "matched"
    assert [(t["ref"], t["owned"]) for t in response.json()["tracks"]] == [
        ("hm:2", False), ("dz:100", True), ("nd:ndZ", True)]


async def test_unidentified_album_requires_unambiguous_release(env, monkeypatch):
    from app.services import svc
    from tests.support import FakeDeezer

    catalog = FakeDeezer()
    catalog.track.album_ref = None
    catalog.track.album_title = None

    async def ambiguous(artist, title):
        return [catalog.album, catalog.album.model_copy(update={"ref": "dz:11", "id": "11"})]

    async def detail(id_):
        return catalog.album.model_copy(update={"ref": f"dz:{id_}", "id": id_})

    monkeypatch.setattr(catalog, "get_album", detail)
    monkeypatch.setattr(catalog, "search_albums", ambiguous)
    monkeypatch.setattr(svc, "deezer", catalog)
    response = await env["client"].get("/api/nd/albums/alK?catalog=true")
    assert response.status_code == 200, response.text
    assert response.json()["catalog_status"] == "ambiguous"
    assert [t["ref"] for t in response.json()["tracks"]] == ["dz:100", "nd:ndZ"]


@pytest.mark.parametrize("with_album_id", [True, False])
async def test_recording_identifies_renamed_and_misdated_release(env, monkeypatch, with_album_id):
    from app.services import svc
    from tests.support import FakeDeezer

    catalog = FakeDeezer()
    env["state"]["album"] = {"name": "Имя на диске", "artist": "Локальный тег", "year": 2000, "song": SONGS[:1]}
    if not with_album_id:
        catalog.track.album_ref = None  # the recording only has the original album name
    extra = catalog.track.model_copy(update={"ref": "dz:101", "id": "101", "title": "Новый трек", "isrc": None})
    catalog.album.tracks = [catalog.track, extra]
    queries = []

    async def search(artist, title):
        queries.append((artist, title))
        return [catalog.album] if (artist, title) == (catalog.album.artist, catalog.album.title) else []

    monkeypatch.setattr(catalog, "search_albums", search)
    monkeypatch.setattr(svc, "deezer", catalog)
    response = await env["client"].get("/api/nd/albums/alK?catalog=true")
    album = response.json()
    assert album["catalog_status"] == "matched"
    assert [(t["ref"], t["owned"]) for t in album["tracks"]] == [("dz:100", True), ("dz:101", False)]
    assert queries == ([] if with_album_id else [(catalog.album.artist, catalog.album.title)])


async def test_single_stays_a_single_instead_of_searching_for_more_music(env, monkeypatch):
    from app.services import svc
    from tests.support import FakeDeezer

    catalog = FakeDeezer()
    env["state"]["album"] = {"song": SONGS[:1]}
    monkeypatch.setattr(svc, "deezer", catalog)
    album = (await env["client"].get("/api/nd/albums/alK?catalog=true")).json()
    assert album["catalog_status"] == "matched" and album["song_count"] == 1
    assert [(t["ref"], t["owned"]) for t in album["tracks"]] == [("dz:100", True)]


@pytest.mark.parametrize(("failure", "expected"), [("error", "unavailable"), ("missing", "not_found"),
                                                  ("wrong_year", "not_found"), ("no_overlap", "not_found")])
async def test_missing_and_unavailable_editions_are_distinct(env, monkeypatch, failure, expected):
    from app.catalog.deezer import CatalogNotFound
    from app.services import svc
    from tests.support import FakeDeezer

    catalog = FakeDeezer()

    async def track(id_):
        raise CatalogNotFound(id_)

    async def search(artist, title):
        if failure == "error":
            raise httpx.ConnectError("unavailable")
        if failure == "missing":
            return []
        return [catalog.album.model_copy(update={"year": None, "release_date": None})]

    if failure == "wrong_year":
        catalog.album.year = 2000  # absent on the search card, visible on the album itself
    if failure == "no_overlap":
        catalog.album.tracks = [catalog.track.model_copy(update={"ref": "dz:200", "id": "200", "title": "Не из альбома"})]
    monkeypatch.setattr(catalog, "get_track", track)
    monkeypatch.setattr(catalog, "search_albums", search)
    monkeypatch.setattr(svc, "deezer", catalog)
    monkeypatch.setattr(svc, "hitmoz_catalog", None)
    data = (await env["client"].get("/api/nd/albums/alK?catalog=true")).json()
    assert data["catalog_status"] == expected
    assert [t["ref"] for t in data["tracks"]] == ["dz:100", "nd:ndZ"]


async def test_album_search_without_any_catalog_ids_preserves_navidrome_song(env, monkeypatch):
    from app.services import svc
    from tests.support import FakeDeezer

    env["state"]["album"] = {"song": SONGS[1:]}
    catalog = FakeDeezer()
    missing = catalog.track.model_copy(update={"id": "101", "ref": "dz:101", "title": "Первая", "isrc": None})
    owned = catalog.track.model_copy(update={"id": "102", "ref": "dz:102", "title": "Звезда", "track_no": 2,
                                             "duration_s": 221, "isrc": None})
    catalog.album.tracks = [missing, owned]
    monkeypatch.setattr(svc, "deezer", catalog)
    data = (await env["client"].get("/api/nd/albums/alK?catalog=true")).json()
    assert data["catalog_status"] == "matched"
    assert [(t["ref"], t["owned"], t["nd_song_id"]) for t in data["tracks"]] == [
        ("dz:101", False, None), ("dz:102", True, "ndZ")]


async def test_stored_release_on_second_library_row_wins_over_recording_lookup(env, monkeypatch):
    from app.db import session_scope
    from app.models import Album
    from app.services import svc
    from tests.support import FakeDeezer

    catalog = FakeDeezer()
    async with session_scope() as s:
        a = Album(uid=str(uuid.uuid4()), title="Группа крови", album_artist="Кино", deezer_album_id="10")
        s.add(a)
        await s.flush()
        album_id = a.id
    await add_track("Кино", "Звезда", "ndZ", album_id=album_id)
    lookups = []

    async def record(id_):
        lookups.append(id_)
        return catalog.track

    monkeypatch.setattr(catalog, "get_track", record)
    monkeypatch.setattr(svc, "deezer", catalog)
    data = (await env["client"].get("/api/nd/albums/alK?catalog=true")).json()
    assert data["catalog_status"] == "matched"
    assert not lookups


@pytest.mark.parametrize("failure", ["empty", "timeout", "different_edition"])
async def test_empty_or_slow_exact_album_tries_backup_catalog(env, monkeypatch, failure):
    import asyncio

    from app.api import catalog as catalog_api
    from app.services import svc
    from tests.support import FakeDeezer

    catalog = FakeDeezer()
    # The primary catalog knows the exact album, but its tracklist is unavailable.
    async def broken_album(id_):
        if failure == "timeout":
            await asyncio.Event().wait()
        return catalog.album.model_copy(update={"tracks": [], "nb_tracks": 1 if failure == "different_edition" else 2})

    class Hitmoz:
        album = catalog.album.model_copy(update={"id": "99", "source": "hm", "ref": "hm:99"})

        async def search_albums(self, query):
            return [self.album]

        async def get_album(self, id_):
            track = catalog.track.model_copy(update={"id": "8", "ref": "hm:8", "source": "hm", "album_ref": "hm:99"})
            other = track.model_copy(update={"id": "9", "ref": "hm:9", "title": "Бонус", "isrc": None})
            return self.album.model_copy(update={"tracks": [track, other]})

    monkeypatch.setattr(catalog_api, "_PROVIDER_SECONDS", 0.1)
    monkeypatch.setattr(catalog, "get_album", broken_album)
    monkeypatch.setattr(svc, "deezer", catalog)
    monkeypatch.setattr(svc, "hitmoz_catalog", Hitmoz())
    data = (await env["client"].get("/api/nd/albums/alK?catalog=true")).json()
    if failure == "different_edition":
        assert data["catalog_status"] == "unavailable"
        assert [t["ref"] for t in data["tracks"]] == ["dz:100", "nd:ndZ"]
        return
    assert data["catalog_status"] == "matched"
    assert [(t["ref"], t["owned"]) for t in data["tracks"]] == [("dz:100", True), ("hm:9", False), ("nd:ndZ", True)]


@pytest.mark.parametrize("stored_album", [False, True])
async def test_conflicting_recording_releases_are_not_combined(env, monkeypatch, stored_album):
    from app.db import session_scope
    from app.models import Album
    from app.services import svc
    from tests.support import FakeDeezer

    catalog = FakeDeezer()
    album_id = None
    if stored_album:
        async with session_scope() as s:
            album = Album(uid=str(uuid.uuid4()), title=catalog.album.title, album_artist=catalog.album.artist,
                          deezer_album_id=catalog.album.id)
            s.add(album)
            await s.flush()
            album_id = album.id
    await add_track("Кино", "Звезда", "ndZ", deezer_track_id="300", album_id=album_id)
    monkeypatch.setattr(svc, "deezer", catalog)
    data = (await env["client"].get("/api/nd/albums/alK?catalog=true")).json()
    assert data["catalog_status"] == "ambiguous"
    assert [t["ref"] for t in data["tracks"]] == ["dz:100", "dz:300"]


def test_album_merge_reserves_exact_refs_and_isrcs_before_name_matching():
    from app.api.medialib import complete_tracklist
    from app.api.schemas import NdSongOut, TrackOut

    local = NdSongOut(ref="nd:1", id="1", source="nd", nd_song_id="1", owned=True, artist="Кино", title="Песня",
                      duration_s=200, isrc="RU-A1D-88-00001")
    # Library-wide ownership falls back to a fingerprint without edition qualifiers. It is not
    # proof that either version is the local file.
    remix = TrackOut(ref="dz:2", id="2", source="dz", owned=True, nd_song_id="1", artist="Кино",
                     title="Песня (Remix)", duration_s=200, isrc="RUA1D8800002")
    original = remix.model_copy(update={"ref": "dz:3", "id": "3", "title": "Song", "isrc": "rua1d8800001"})
    rows = complete_tracklist([local], [remix, original])
    assert [(t.ref, t.owned, t.nd_song_id) for t in rows] == [("dz:2", False, None), ("dz:3", True, "1")]


async def test_liked_lists_the_newest_like_first(env):
    r = await env["client"].get("/api/nd/liked")
    assert r.status_code == 200, r.text
    d = r.json()
    assert [t["nd_song_id"] for t in d["songs"]] == ["ndZ", "ndK"]
    assert [a["id"] for a in d["albums"]] == ["alK"] and [a["name"] for a in d["artists"]] == ["Кино"]


async def test_catalog_like_is_listed_before_download_and_unlike_removes_it(env):
    from sqlalchemy import select

    from app.db import session_scope
    from app.events import bus
    from app.models import DownloadItem, User

    c = env["client"]
    async with session_scope() as s:
        uid = (await s.execute(select(User.id).where(User.nd_username == "admin"))).scalar_one()
    queue = bus.subscribe(uid)
    try:
        target = {"ref": "dz:999", "target_data": {"title": "WHAT TO DO?", "artist": "Jackboys",
                  "duration_s": 250, "cover_url": "/cover.jpg"}}
        r = await c.post("/api/star", json={"targets": [target], "event_id": "catalog-like"})
        assert r.status_code == 200, r.text
        assert '"event": "library"' in queue.get_nowait()
        liked = (await c.get("/api/nd/liked")).json()["songs"]
        assert liked[0]["ref"] == "dz:999"
        assert (liked[0]["title"], liked[0]["artist"], liked[0]["cover_url"]) == ("WHAT TO DO?", "Jackboys", "/cover.jpg")
        assert not liked[0]["owned"] and liked[0]["nd_song_id"] is None
        async with session_scope() as s:
            assert not (await s.execute(select(DownloadItem))).first()
        r = await c.post("/api/star", json={"targets": [target], "unstar": True, "event_id": "catalog-unlike"})
        assert r.status_code == 200, r.text
        assert "dz:999" not in [t["ref"] for t in (await c.get("/api/nd/liked")).json()["songs"]]
    finally:
        bus.unsubscribe(uid, queue)


@pytest.mark.parametrize("cache_key", ["listen:dz:999", "mix:track:dz:999", "dz:track:999", None])
async def test_old_catalog_like_uses_stored_metadata_without_network(env, cache_key):
    from app.catalog.cache import cache_set
    if cache_key:
        await cache_set(cache_key, {"title": "WHAT TO DO?", "artist": "Jackboys", "duration_s": 250}, 3600)
    c = env["client"]
    assert (await c.post("/api/star", json={"targets": [{"ref": "dz:999"}]})).status_code == 200
    song = (await c.get("/api/nd/liked")).json()["songs"][0]
    assert song["ref"] == "dz:999"
    assert (song["title"], song["artist"]) == (("WHAT TO DO?", "Jackboys") if cache_key else ("dz:999", ""))


async def test_own_likes_deduplicate_library_aliases_and_override_stale_nd_unlikes(env):
    c = env["client"]
    target = {"ref": "dz:100"}
    assert (await c.post("/api/star", json={"targets": [target]})).status_code == 200
    songs = (await c.get("/api/nd/liked")).json()["songs"]
    assert [t["ref"] for t in songs] == ["dz:100", "nd:ndZ"]
    assert (await c.post("/api/star", json={"targets": [target], "unstar": True})).status_code == 200
    # This fake ND still returns ndK: the explicit decision must win immediately.
    assert [t["ref"] for t in (await c.get("/api/nd/liked")).json()["songs"]] == ["nd:ndZ"]


async def test_pending_import_likes_are_visible_only_to_their_user_and_own_unlike_wins(env):
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Import, ImportCollection, ImportItem, User

    async with session_scope() as s:
        uid = (await s.execute(select(User.id).where(User.nd_username == "admin"))).scalar_one()
        other = User(nd_username="other", nd_password_enc="unused")
        s.add(other)
        await s.flush()
        for user_id, ref in ((uid, "dz:998"), (other.id, "dz:997")):
            imp = Import(user_id=user_id, source="yandex", method="profile", status="done")
            s.add(imp)
            await s.flush()
            coll = ImportCollection(import_id=imp.id, kind="likes", external_id="likes", title="Likes")
            s.add(coll)
            await s.flush()
            s.add(ImportItem(import_id=imp.id, collection_id=coll.id, external_id=ref, ref=ref, status="waiting",
                             raw_json={"title": "Imported song", "artist": "Artist"}))
    c = env["client"]
    songs = (await c.get("/api/nd/liked")).json()["songs"]
    assert "dz:998" in [t["ref"] for t in songs] and "dz:997" not in [t["ref"] for t in songs]
    assert (await c.post("/api/star", json={"targets": [{"ref": "dz:998"}], "unstar": True})).status_code == 200
    assert "dz:998" not in [t["ref"] for t in (await c.get("/api/nd/liked")).json()["songs"]]


async def test_home_rows_and_radio_with_liked_artists_first(env):
    r = await env["client"].get("/api/nd/home")
    assert r.status_code == 200, r.text
    d = r.json()
    assert [a["id"] for a in d["recent"]] == ["alK", "alK2"] and [a["id"] for a in d["starred"]] == ["alK"]
    assert [(a["name"], a["starred"]) for a in d["radio"]] == [("Кино", True), ("Кто-то", False)]
    types = sorted(p["type"] for name, p in env["state"]["calls"] if name == "getAlbumList2")
    assert types == ["frequent", "newest", "recent", "starred"]


async def test_artists_artist_page_and_album_lists(env):
    c = env["client"]
    assert [a["id"] for a in (await c.get("/api/nd/artists")).json()] == ["arX", "arK"]
    page = (await c.get("/api/nd/artists/arK")).json()
    assert page["artist"]["name"] == "Кино" and [a["year"] for a in page["albums"]] == [1989, 1988]
    r = await c.get("/api/nd/albums", params={"type": "newest", "offset": 50, "size": 25})
    assert r.status_code == 200 and len(r.json()) == 2
    assert ("getAlbumList2", {"type": "newest", "size": "25", "offset": "50"}) in [
        (n, {k: p[k] for k in ("type", "size", "offset")}) for n, p in env["state"]["calls"] if n == "getAlbumList2"]
    assert (await c.get("/api/nd/albums", params={"type": "bogus"})).status_code == 422


async def test_artist_albums_newest_year_first_then_without_a_year(env):
    env["state"]["artist_albums"] = [
        {"id": "a1", "name": "Без года"}, {"id": "a2", "name": "Б", "year": 1988}, {"id": "a3", "name": "а", "year": 1988},
        {"id": "a4", "name": "Новый", "year": 1990}, {"id": "a5", "name": "Ещё без года", "year": 0},
    ]
    page = (await env["client"].get("/api/nd/artists/arK")).json()
    assert [a["id"] for a in page["albums"]] == ["a4", "a3", "a2", "a1", "a5"]


async def test_playlist_page_remove_and_delete(env):
    c = env["client"]
    r = await c.get("/api/playlists/pl1")
    assert r.status_code == 200, r.text
    p = r.json()
    assert (p["name"], p["song_count"]) == ("Дорога", 2) and [t["ref"] for t in p["tracks"]] == ["dz:100", "nd:ndZ"]

    assert (await c.post("/api/playlists/pl1/remove", json={"indexes": [1, 1]})).json() == {"ok": True}
    upd = [p for n, p in env["state"]["calls"] if n == "updatePlaylist"]
    assert upd[-1]["playlistId"] == "pl1" and upd[-1]["songIndexToRemove"] == "1"
    assert (await c.post("/api/playlists/pl1/remove", json={"indexes": []})).status_code == 422

    assert (await c.delete("/api/playlists/pl1")).json() == {"ok": True}
    assert ("deletePlaylist", "pl1") in [(n, p.get("id")) for n, p in env["state"]["calls"]]


async def test_playlist_errors_map_navidromes_codes(env):
    c, fail = env["client"], env["state"]["fail"]
    fail["getPlaylist"] = 70
    assert (await c.get("/api/playlists/gone")).status_code == 404
    fail["updatePlaylist"] = 50  # not the owner
    assert (await c.post("/api/playlists/pl1/remove", json={"indexes": [0]})).status_code == 403
    fail["deletePlaylist"] = 0  # anything else Navidrome says
    r = await c.delete("/api/playlists/pl1")
    assert r.status_code == 502 and r.json()["detail"].startswith("Navidrome")


async def test_navidrome_unreachable_is_502(env):
    env["state"]["down"] = True
    c = env["client"]
    assert (await c.get("/api/nd/home")).status_code == 502
    assert (await c.get("/api/nd/cover/al-alK")).status_code == 502
    assert (await c.get("/api/playlists/pl1")).status_code == 502


async def test_cover_is_proxied_as_the_user(env):
    r = await env["client"].get("/api/nd/cover/al-alK", params={"size": 300})
    assert r.status_code == 200 and r.content == b"\xff\xd8jpeg" and r.headers["content-type"] == "image/jpeg"
    p = [p for n, p in env["state"]["calls"] if n == "getCoverArt"][-1]
    assert (p["id"], p["size"], p["u"]) == ("al-alK", "300", "admin")


async def test_library_needs_a_session(hub_env):
    from app.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        async with app_client(app, login=None) as client:
            assert (await client.get("/api/nd/home")).status_code == 401
