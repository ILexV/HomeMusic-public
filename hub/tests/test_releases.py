"""Release calendar boundaries, bounded public catalog reads and private reversible marks."""

from datetime import UTC, date, datetime

import pytest
import respx
from sqlalchemy import func, select

from app import releases
from app.catalog.deezer import CatalogNotFound
from app.catalog.models import CatalogAlbum, CatalogArtist, SearchResults
from app.db import session_scope
from app.models import DownloadBatch, DownloadItem, Job, MixSettings, PlayEvent, User
from app.release_models import ReleaseSeen
from tests.support import app_client
from tests.test_mixes import ND

NOW = datetime(2026, 11, 12, 21, 30)  # November 13 in Europe/Moscow.


def row(id_, released="2026-11-13", title="Новый альбом"):
    return {"id": id_, "title": title, "release_date": released, "nb_tracks": 8,
            "cover_medium": "https://cover.test/album.jpg", "record_type": "album"}


class Catalog:
    def __init__(self):
        self.pages = {"1": [row(10)]}
        self.calls = []
        self.down = set()
        self.mismatch = False

    async def get_artist(self, id_):
        self.calls.append(("artist", id_))
        if id_ in self.down:
            raise RuntimeError("https://private.test/?token=secret user-password")
        return CatalogArtist(ref=f"dz:{id_}", source="dz", id=id_, name=f"Artist {id_}")

    async def search(self, name, kinds, limit):
        self.calls.append(("search", name))
        if name == "Artist 1":
            return SearchResults(artists=[await self.get_artist("1")])
        return SearchResults()

    async def _get(self, path, params):
        self.calls.append((path, params))
        id_ = path.split("/")[1]
        if id_ in self.down:
            raise RuntimeError("secret catalog transport failure")
        return {"data": self.pages.get(id_, [])}

    async def get_album(self, id_):
        self.calls.append(("album", id_))
        if "album" in self.down:
            raise RuntimeError("secret catalog transport failure")
        if id_ not in {str(r["id"]) for rows in self.pages.values() for r in rows}:
            raise CatalogNotFound("secret upstream URL")
        return CatalogAlbum(ref=f"dz:{99 if self.mismatch else id_}", source="dz", id=id_,
                            title="Новый альбом", artist="Artist 1", release_date=date(2026, 11, 13))

    async def aclose(self):
        pass


@pytest.fixture
async def env(hub_env, monkeypatch):
    from app.main import create_app
    from app.services import svc

    monkeypatch.setattr(releases, "utcnow", lambda: NOW)
    nd, catalog = ND(), Catalog()
    app = create_app()
    async with app.router.lifespan_context(app):
        monkeypatch.setattr(svc, "deezer", catalog)
        with respx.mock(assert_all_called=False) as mock:
            mock.route(host="navidrome.test").mock(side_effect=nd.handler)
            async with app_client(app) as client:
                async with session_scope() as s:
                    uid = (await s.execute(select(User.id).where(User.nd_username == "admin"))).scalar_one()
                yield app, client, uid, catalog, nd


async def onboard(user_id, refs=("dz:1",)):
    async with session_scope() as s:
        settings = await s.get(MixSettings, user_id)
        if settings is None:
            settings = MixSettings(user_id=user_id)
            s.add(settings)
        settings.artists_json = [{"ref": r, "name": f"Artist {r.split(':')[1]}"} for r in refs]


@pytest.mark.parametrize(("period", "now", "zone", "expected"), [
    ("week", NOW, "Europe/Moscow", (date(2026, 11, 7), date(2026, 11, 13))),
    ("week", datetime(2026, 11, 12, 20, 59, tzinfo=UTC), "Europe/Moscow",
     (date(2026, 11, 6), date(2026, 11, 12))),
    ("month", NOW, "Europe/Moscow", (date(2026, 10, 15), date(2026, 11, 13))),
    ("week", datetime(2026, 3, 9, 3, 30, tzinfo=UTC), "America/New_York",
     (date(2026, 3, 2), date(2026, 3, 8))),
])
def test_local_calendar_boundaries(period, now, zone, expected):
    assert releases.date_window(period, now, zone) == expected


@pytest.mark.parametrize("bad", [None, "2026", "2026-11", "0000-00-00", "2026-02-30", "2026-11-13garbage",
                                 "2026-11-13T00:00:00Z", 20261113, datetime(2026, 11, 13)])
def test_invalid_dates(bad):
    assert releases.release_date(bad) is None


async def test_empty_profile_does_not_query_catalog(env):
    _, client, _, catalog, _ = env
    response = await client.get("/api/releases")
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []
    assert response.json()["partial"] is False
    assert response.json()["errors"] == []
    assert catalog.calls == []
    assert (await client.get("/api/releases?period=year")).status_code == 422


async def test_dates_identity_dedup_and_public_cache(env):
    _, client, uid, catalog, _ = env
    await onboard(uid)
    catalog.pages["1"] = [row(10), row(10), row(11), row(12, "2026-11-07"), row(13, "2026-11-06"),
                          row(14, "2026-11-14"), row(15, None), row(16, "2026-11-13junk"),
                          row(17, "2026-10-15"), row(18, "2026-10-14"), row(19, "0000-00-00")]
    response = await client.get("/api/releases")
    assert response.status_code == 200, response.text
    body = response.json()
    assert {a["ref"] for a in body["items"]} == {"dz:10", "dz:11", "dz:12"}
    assert len(body["items"]) == 3  # same title but different catalog identity stays distinct
    assert body["items"][0]["track_count"] == 8
    assert body["items"][0]["artist_ref"] == "dz:1"
    assert body["items"][0]["cover_url"] == "https://cover.test/album.jpg"
    assert body["generated_at"].endswith("Z")
    month = (await client.get("/api/releases?period=month")).json()
    assert {a["ref"] for a in month["items"]} == {"dz:10", "dz:11", "dz:12", "dz:13", "dz:17"}
    assert len([c for c in catalog.calls if c[0] == "artist/1/albums"]) == 1


async def test_outage_preserves_available_results_and_sanitizes(env):
    _, client, uid, catalog, _ = env
    await onboard(uid, ("dz:1", "dz:2"))
    catalog.down.add("2")
    response = await client.get("/api/releases")
    body = response.json()
    assert body["partial"] is True
    assert [a["ref"] for a in body["items"]] == ["dz:10"]
    assert body["errors"] == [releases.CATALOG_ERROR]
    assert "secret" not in response.text and "private.test" not in response.text
    catalog.down.add("1")
    body = (await client.get("/api/releases")).json()
    assert body["partial"] is True and body["items"] == []


async def test_private_seen_state_reversal_and_no_side_effects(env):
    app, client, uid, _, nd = env
    await onboard(uid)
    for _ in range(2):
        r = await client.put("/api/releases/seen", json={"ref": "dz:10", "seen": True})
        assert r.status_code == 200, r.text
        assert r.json() == {"ref": "dz:10", "seen": True}
    assert (await client.get("/api/releases")).json()["items"][0]["seen"] is True
    assert (await client.get("/api/releases?unseen=true")).json()["items"] == []
    async with app_client(app, login=("other", "pw")) as other:
        async with session_scope() as s:
            other_id = (await s.execute(select(User.id).where(User.nd_username == "other"))).scalar_one()
        # Public cache and album identity must not leak the other account's profile/read state.
        assert (await other.get("/api/releases")).json()["items"] == []
        await onboard(other_id)
        assert (await other.get("/api/releases")).json()["items"][0]["seen"] is False
        assert (await other.put("/api/releases/seen", json={"ref": "dz:10", "seen": False})).status_code == 200
        assert (await client.get("/api/releases")).json()["items"][0]["seen"] is True
    async with session_scope() as s:
        marks = list((await s.execute(select(ReleaseSeen))).scalars())
        assert [(m.user_id, m.ref) for m in marks] == [(uid, "dz:10")]
    assert (await client.put("/api/releases/seen", json={"ref": "dz:10", "seen": False})).status_code == 200
    assert (await client.get("/api/releases?unseen=true")).json()["items"][0]["seen"] is False
    async with session_scope() as s:
        for model in (ReleaseSeen, DownloadBatch, DownloadItem, Job, PlayEvent):
            assert (await s.execute(select(func.count()).select_from(model))).scalar_one() == 0
    assert nd.writes == 0


@pytest.mark.parametrize("ref", ["dz:track:10", "nd:10", "mb:10", "dz:0010", "dz:../10", "dz:0", "hm:http"])
async def test_rejects_invalid_album_refs_before_catalog(env, ref):
    _, client, _, catalog, _ = env
    response = await client.put("/api/releases/seen", json={"ref": ref, "seen": True})
    assert response.status_code == 422
    assert catalog.calls == []


async def test_mark_resolves_real_album_and_sanitizes_outages(env):
    _, client, _, catalog, _ = env
    assert (await client.put("/api/releases/seen", json={"ref": "dz:999", "seen": True})).status_code == 404
    catalog.mismatch = True
    assert (await client.put("/api/releases/seen", json={"ref": "dz:10", "seen": True})).status_code == 404
    catalog.mismatch = False
    catalog.down.add("album")
    response = await client.put("/api/releases/seen", json={"ref": "dz:10", "seen": True})
    assert response.status_code == 503 and "secret" not in response.text
    async with session_scope() as s:
        assert (await s.execute(select(func.count()).select_from(ReleaseSeen))).scalar_one() == 0


async def test_unmark_remains_available_during_catalog_outage(env):
    _, client, uid, catalog, _ = env
    assert (await client.put("/api/releases/seen", json={"ref": "dz:10", "seen": True})).status_code == 200
    catalog.down.add("album")
    response = await client.put("/api/releases/seen", json={"ref": "dz:10", "seen": False})
    assert response.status_code == 200 and response.json()["seen"] is False
    async with session_scope() as s:
        assert await s.get(ReleaseSeen, (uid, "dz:10")) is None


async def test_favorite_artist_ledger_is_current_and_account_scoped(env):
    from app.api.schemas import FavoriteTarget
    from app.favorites import record

    _, client, uid, _, _ = env
    target = FavoriteTarget(target_type="artist", ref="dz:1", target_data={"name": "Artist 1"})
    async with session_scope() as s:
        await record(s, uid, target, True, ts=NOW, normalize=False)
    assert len((await client.get("/api/releases")).json()["items"]) == 1
    async with session_scope() as s:
        await record(s, uid, target, False, ts=NOW, normalize=False)
    assert (await client.get("/api/releases")).json()["items"] == []


async def test_artist_and_page_caps_are_explicit(env, monkeypatch):
    _, client, uid, catalog, _ = env
    monkeypatch.setattr(releases, "MAX_ARTISTS", 1)
    monkeypatch.setattr(releases, "MAX_PAGES", 2)
    await onboard(uid, ("dz:1", "dz:2"))
    calls = []

    async def paged(path, params):
        calls.append(params["index"])
        return {"data": [row(10 + params["index"])], "next": "https://ignored.test/next"}

    catalog._get = paged
    body = (await client.get("/api/releases")).json()
    assert body["partial"] is True and releases.LIMIT_ERROR in body["errors"]
    assert calls == [0, releases.PAGE_SIZE]
    assert {a["ref"] for a in body["items"]} == {"dz:10", "dz:110"}
    assert ("artist", "2") not in catalog.calls


async def test_release_routes_require_authentication(env):
    app, _, _, _, _ = env
    async with app_client(app, login=None) as client:
        assert (await client.get("/api/releases")).status_code == 401
        assert (await client.put("/api/releases/seen", json={"ref": "dz:10", "seen": True})).status_code == 401


async def test_favorite_track_artist_without_onboarding(env):
    from app.api.schemas import FavoriteTarget
    from app.favorites import record

    _, client, uid, catalog, _ = env
    async with session_scope() as s:
        await record(s, uid, FavoriteTarget(ref="dz:100", target_data={"artist": "Artist 1", "title": "Song"}),
                     True, ts=NOW, normalize=False)
    body = (await client.get("/api/releases")).json()
    assert [a["ref"] for a in body["items"]] == ["dz:10"]
    assert body["partial"] is False
    assert ("search", "Artist 1") in catalog.calls


async def test_slow_catalog_is_bounded_and_keeps_completed_artists(env, monkeypatch):
    import asyncio

    _, client, uid, catalog, _ = env
    await onboard(uid, ("dz:1", "dz:2"))
    monkeypatch.setattr(releases, "ARTIST_TIMEOUT_S", 2.0)
    monkeypatch.setattr(releases, "FEED_TIMEOUT_S", 1.0)
    original = catalog.get_artist
    cancelled = asyncio.Event()

    async def slow(id_):
        if id_ == "2":
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        return await original(id_)

    catalog.get_artist = slow
    body = (await client.get("/api/releases")).json()
    assert body["partial"] is True and body["errors"] == [releases.CATALOG_ERROR]
    assert [a["ref"] for a in body["items"]] == ["dz:10"]
    assert cancelled.is_set()
