"""«Микс дня» in the Hub (app/mixes/core.py, taste.py, api/mixes.py): the taste profile, the build with its quiet
downloads and the Navidrome playlist, the reserve, the replacement with its deletion rule, the API, the schedule.

Navidrome is a mock that keeps every user's ♥ and playlists (`ND.starred`, `ND.playlists`); the generators of the
build are fakes (`fake_generators`): the songs the library has and new catalog tracks dz:5000… .
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import func, select

from app.catalog.models import CatalogTrack
from tests.support import FakeDeezer, app_client

NOW = datetime(2026, 10, 22, 12, 0)


class ND:
    """Navidrome as several users see it: ♥ and playlists per user; `down` — it doesn't answer."""

    def __init__(self) -> None:
        self.starred: dict[str, set[str]] = {}
        self.playlists: dict[str, dict[str, Any]] = {}  # id -> {name, owner, songs, comment}
        self.artists: dict[str, dict[str, Any]] = {}  # id -> getArtist's artist (wave by an artist)
        self.albums: dict[str, dict[str, Any]] = {}  # id -> getAlbum's album
        self.songs: dict[str, dict[str, Any]] = {}
        self.down = False
        self.writes = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        p = request.url.params
        user = p.get("u", "")
        path = request.url.path
        if path == "/auth/login":
            return httpx.Response(200, json={"token": "jwt", "id": "nd-1"})
        if path == "/api/scrobble":
            return httpx.Response(200, json=[])
        if self.down:
            return httpx.Response(503)
        ok: dict[str, Any] = {"status": "ok", "version": "1.16.1"}
        name = path.rsplit("/", 1)[-1].removesuffix(".view")
        if name == "getUser":
            ok["user"] = {"adminRole": user == "admin"}
        elif name == "getStarred2":
            ok["starred2"] = {"song": [{"id": x, "starred": "2026-10-01T10:00:00Z"} for x in self.starred.get(user, ())]}
        elif name == "getPlaylists":
            ok["playlists"] = {"playlist": [{"id": k, "name": v["name"]} for k, v in self.playlists.items()
                                            if v["owner"] == user]}
        elif name == "getPlaylist":
            pl = self.playlists[p["id"]]
            ok["playlist"] = {"id": p["id"], "entry": [{"id": x} for x in pl["songs"]]}
        elif name == "createPlaylist":
            songs = p.get_list("songId")
            if pid := p.get("playlistId"):
                self.playlists[pid]["songs"] = songs
                self.writes += 1
            else:
                pid = f"pl{len(self.playlists) + 1}"
                self.playlists[pid] = {"name": p["name"], "owner": user, "songs": songs, "comment": ""}
            ok["playlist"] = {"id": pid}
        elif name in ("getArtist", "getAlbum", "getSong"):
            found = {"getArtist": self.artists, "getAlbum": self.albums, "getSong": self.songs}[name].get(p.get("id", ""))
            if found is None:
                return httpx.Response(200, json={"subsonic-response": {
                    "status": "failed", "error": {"code": 70, "message": "not found"}}})
            ok[name[3:].lower()] = found
        elif name == "updatePlaylist":
            pl = self.playlists[p["playlistId"]]
            pl["songs"] += p.get_list("songIdToAdd")
            if "comment" in p:
                pl["comment"] = p["comment"]
            if "name" in p:
                pl["name"] = p["name"]
        return httpx.Response(200, json={"subsonic-response": ok})


@pytest.fixture
async def env(hub_env, tmp_path: Path):
    from app.main import create_app
    from app.services import svc

    (tmp_path / "none.yaml").write_text("mixes:\n  size: 6\n  reserve: 2\n", encoding="utf-8")
    from app.config import get_settings
    get_settings.cache_clear()  # importing app.main creates the module's app and may cache defaults
    nd = ND()
    app = create_app()
    async with app.router.lifespan_context(app):
        svc.deezer = FakeDeezer()
        with respx.mock(assert_all_called=False) as mock:
            mock.route(host="navidrome.test").mock(side_effect=nd.handler)
            async with app_client(app) as client:
                yield {"app": app, "client": client, "nd": nd, "music": hub_env, "admin": await user_id("admin")}


async def user_id(name: str) -> int:
    from app.db import session_scope
    from app.models import User

    async with session_scope() as s:
        return (await s.execute(select(User.id).where(User.nd_username == name))).scalar_one()


async def add_user(name: str) -> int:
    from app.db import session_scope
    from app.models import User
    from app.security import encrypt

    async with session_scope() as s:
        u = User(nd_username=name, nd_password_enc=encrypt("pw"))
        s.add(u)
        await s.flush()
        return u.id


async def add_track(n: int, *, music: Path | None = None, deezer: bool = True) -> int:
    """A library song «Свой n — Песня n» (Navidrome nd<n>, Deezer dz:<n> unless `deezer` is false)."""
    from app.db import session_scope
    from app.matching.normalize import fingerprint
    from app.models import Track

    path = f"Свой {n}/Песня {n}.mp3"
    if music is not None:
        f = music / "library" / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"mp3")
    async with session_scope() as s:
        t = Track(uid=str(uuid.uuid4()), path=path, nd_song_id=f"nd{n}", artist=f"Свой {n}", title=f"Песня {n}",
                  duration_s=200, deezer_track_id=str(n) if deezer else None,
                  fingerprint=fingerprint(f"Свой {n}", f"Песня {n}"))
        s.add(t)
        await s.flush()
        return t.id


async def add_event(user: int, kind: str, *, nd: str | None = None, ref: str | None = None, at: datetime | None = None,
                    position: float | None = None) -> None:
    from app.db import session_scope
    from app.models import PlayEvent

    async with session_scope() as s:
        s.add(PlayEvent(user_id=user, event_id=uuid.uuid4().hex, ts=at or NOW, kind=kind, ref=ref, nd_song_id=nd,
                        position_s=position, client="app/1.5.0"))


def new_track(n: int) -> CatalogTrack:
    return CatalogTrack(ref=f"dz:{n}", source="dz", id=str(n), title=f"Новая {n}", artist=f"Новый {n}",
                        artists=[f"Новый {n}"], duration_s=200)


def fake_generators(monkeypatch: pytest.MonkeyPatch, new: list[CatalogTrack]) -> None:
    from app.mixes import build

    def gens(p, seeds):
        async def fav():
            return build.favorites(p)

        async def radio():
            return new

        return {"favorites": fav, "ytm_radio": radio}

    monkeypatch.setattr(build, "generators", gens)


async def history(user: int, n: int = 4) -> None:
    """The user listened to library songs 1..n ten days ago."""
    for i in range(1, n + 1):
        await add_track(i)
        await add_event(user, "finish", nd=f"nd{i}", at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=10))


# --- the profile ---------------------------------------------------------------------------------------------------


async def test_profile_weights_and_decay(env) -> None:
    """M4 (2026-10-23): ♥ +1, finish +2, skip before 30 s −1, a later skip up to +2 by how much was heard,
    «Не нравится» −5, an own download 0; half the weight in 30 days; a ♥ that an import set is not the user's."""
    from app.db import session_scope
    from app.mixes.taste import build_profile
    from app.models import Dislike, DownloadBatch, DownloadItem, Import, User

    me = env["admin"]
    for n in range(1, 10):
        await add_track(n)
    await add_event(me, "finish", nd="nd1", at=NOW)
    await add_event(me, "finish", nd="nd2", at=NOW - timedelta(days=30))
    await add_event(me, "skip", nd="nd3", at=NOW, position=10)
    await add_event(me, "skip", nd="nd3", at=NOW, position=100)  # after 30 s: 70 of the 170 s past them (file: 200 s)
    await add_event(me, "like", nd="nd7", at=NOW - timedelta(days=1))
    await add_event(me, "skip", nd="nd8", at=NOW, position=199)  # a second before the end
    async with session_scope() as s:
        batch = DownloadBatch(user_id=me, origin="catalog")
        s.add(batch)
        await s.flush()
        s.add(DownloadItem(batch_id=batch.id, user_id=me, status="done", track_id=4, created_at=NOW,
                           query_json={"query": {}, "meta": {"track_ref": "dz:4"}}))
        s.add(DownloadItem(batch_id=batch.id, user_id=me, status="done", track_id=6, created_at=NOW,
                           query_json={"query": {}, "meta": {"track_ref": "dz:6"}, "mix": {"id": 1}}))  # not own
        s.add(DownloadItem(batch_id=batch.id, user_id=me, status="done", track_id=9, created_at=NOW,
                           query_json={"query": {}, "meta": {"track_ref": "dz:9"}}))
        s.add(Dislike(user_id=me, nd_song_id="nd5", source="app", created_at=NOW))
        s.add(Import(user_id=me, source="yandex", method="token", options_json={"liked": ["nd6"]}))
        user = await s.get(User, me)
        p = await build_profile(s, user, now=NOW, starred=[{"id": "nd4", "starred": "2026-10-22T12:00:00Z"},
                                                            {"id": "nd6", "starred": "2026-10-22T12:00:00Z"},
                                                            {"id": "nd7", "starred": "2026-10-21T12:00:00Z"}])

    score = {t.nd_song_id: round(t.score, 3) for t in p.tracks.values()}
    assert score == {
        "nd1": 2.0, "nd2": 1.0,
        "nd3": -0.702,  # −1 + 2 · (70/170) / (1 + 3 · 100/170)
        "nd4": 1.0,  # ♥ without an event; its own download adds nothing
        "nd5": -5.0,
        "nd7": 0.977,  # the ♥ event a day ago
        "nd8": 1.954,  # a skip at the very end counts almost as a finish
        "nd9": 0.0,  # only downloaded
    }  # nd6: the import's ♥, a mix's download
    assert p.tracks["nd:nd4"].liked and p.tracks["nd:nd4"].downloaded and p.tracks["nd:nd7"].liked
    assert p.find(ref="dz:1") is p.tracks["nd:nd1"]  # known by its catalog id too
    # nd2 and nd4 tie at 1.0: the profile's order (events first)
    assert [t.nd_song_id for t in p.seeds(8)] == ["nd1", "nd8", "nd2", "nd4", "nd7"]
    assert p.disliked(ref=None, nd_song_id="nd5", artist="x", artist_ref=None)


async def test_own_download_alone_weighs_nothing_but_stays_in_the_profile(env) -> None:
    """A download without other events: score 0 — not a favorite and not in the sound profile, but not a negative
    either: the stash and the fallback seeds take it, and the profile is not empty."""
    from app.db import session_scope
    from app.mixes.build import favorites, stash
    from app.mixes.taste import build_profile
    from app.models import DownloadBatch, DownloadItem, User

    me = env["admin"]
    await add_track(1)
    async with session_scope() as s:
        batch = DownloadBatch(user_id=me, origin="catalog")
        s.add(batch)
        await s.flush()
        s.add(DownloadItem(batch_id=batch.id, user_id=me, status="done", track_id=1, created_at=NOW,
                           query_json={"query": {}, "meta": {"track_ref": "dz:1"}}))
        await s.flush()
        p = await build_profile(s, await s.get(User, me), now=NOW, starred=[])

    t = p.tracks["nd:nd1"]
    assert t.downloaded and t.score == 0.0
    assert favorites(p) == [] and p.vector_profile() == {}
    assert [x.ref for x in stash(p)] == ["dz:1"]
    assert [x.nd_song_id for x in p.seeds(8)] == ["nd1"] and not p.empty


def test_skip_weight_rises_slowly_then_steeply() -> None:
    """A skip after 30 s: 0 right after them, +2 at the very end, a hyperbola between (slow first, steep last)."""
    from app.mixes.taste import skip_weight

    w = [round(skip_weight(30 + 170 * x, 200), 3) for x in (0, 0.25, 0.5, 0.75, 0.9, 1)]
    assert w == [0.0, 0.154, 0.4, 0.857, 1.385, 2.0]
    assert skip_weight(250, 200) == 2.0  # a position past the length (rounding) is the end
    assert skip_weight(29, 200) == 0.0 and skip_weight(40, None) == 0.0 and skip_weight(25, 30) == 0.0


async def test_skip_after_30_s_takes_the_length_of_the_event(env) -> None:
    from app.db import session_scope
    from app.mixes.taste import build_profile
    from app.models import PlayEvent, User

    me = env["admin"]
    await add_track(1)  # the file says 200 s, the player said 130 s: the event wins
    async with session_scope() as s:
        s.add(PlayEvent(user_id=me, event_id="e1", ts=NOW, kind="skip", nd_song_id="nd1", position_s=80,
                        duration_s=130, client="app/1.6.0"))
        await s.flush()
        p = await build_profile(s, await s.get(User, me), now=NOW, starred=[])

    assert round(p.tracks["nd:nd1"].score, 3) == 0.4  # x = 50 / 100: 2 · 0.5 / (1 + 3 · 0.5)


async def test_cold_start_seeds_from_picked_artists(env) -> None:
    from app.db import session_scope
    from app.mixes.build import cold_seeds
    from app.mixes.taste import build_profile
    from app.models import MixSettings, User

    async with session_scope() as s:
        s.add(MixSettings(user_id=env["admin"], mode="normal", mix_hour=5,
                          artists_json=[{"ref": "dz:1", "name": "Кино"}]))
        await s.flush()
        p = await build_profile(s, await s.get(User, env["admin"]), starred=[])

    assert not p.empty and not p.seeds(8)
    assert [(t.artist, t.title) for t in await cold_seeds(p, 8)] == [("Кино", "Группа крови")]


# --- the build, downloads, playlist -------------------------------------------------------------------------------


async def mix_rows(mix_id: int) -> list:
    from app.db import session_scope
    from app.models import MixItem

    async with session_scope() as s:
        return list((await s.execute(select(MixItem).where(MixItem.mix_id == mix_id).order_by(MixItem.position)))
                    .scalars())


@pytest.mark.parametrize("shadow", ["off", "on", "error", "snapshot_error"])
async def test_build_queues_quiet_downloads_and_writes_the_playlist(env, monkeypatch, shadow) -> None:
    from app import listen
    from app.config import get_settings
    from app.db import session_scope
    from app.mixes import core, taste_snapshot
    from app.mixes import shadow as shadow_module
    from app.models import DownloadItem, Job, MixSettings

    monkeypatch.setattr(get_settings().cfg.mixes, "taste_shadow", shadow != "off")
    if shadow == "error":
        def fail(*args, **kwargs):
            raise RuntimeError("diagnostic failure")
        monkeypatch.setattr(shadow_module, "compare", fail)

    if shadow == "snapshot_error":
        async def fail_snapshot(*args, **kwargs):
            raise RuntimeError("snapshot failure")
        monkeypatch.setattr(taste_snapshot, "load_snapshot", fail_snapshot)

    warmed: list[list[str]] = []
    monkeypatch.setattr(listen, "warm_light", lambda refs: warmed.append(list(refs)))
    me = env["admin"]
    await history(me)
    fake_generators(monkeypatch, [new_track(5000 + i) for i in range(10)])

    mix = await core.build_for(me)

    if shadow == "off":
        assert "taste_shadow" not in mix.stats_json
    elif shadow in ("error", "snapshot_error"):
        assert mix.stats_json["taste_shadow"]["error"] == (
            "comparison_failed" if shadow == "error" else "snapshot_failed")
    else:
        assert mix.stats_json["taste_shadow"]["schema"] == 1
        assert mix.stats_json["taste_shadow"]["sound"] is None
    rows = await mix_rows(mix.id)
    active = [r for r in rows if r.state == "active"]
    # the light copies for a metered network are made ahead: the songs of the mix the library has
    assert warmed and warmed[-1] == [f"nd:{r.nd_song_id}" for r in active if r.nd_song_id]
    assert len(active) == 6 and len([r for r in rows if r.state == "reserve"]) == 2
    assert sum(r.familiar for r in active) == 2  # «Обычный»: 40 % of 6
    assert active[0].familiar and active[0].nd_song_id and active[0].generator == "favorites"
    async with session_scope() as s:
        dls = list((await s.execute(select(DownloadItem))).scalars())
        jobs = list((await s.execute(select(Job).where(Job.kind == "download_item"))).scalars())
        settings = await s.get(MixSettings, me)
    # the new songs are downloaded quietly, in the background; the library's are not
    assert {d.query_json["meta"]["track_ref"] for d in dls} == {r.ref for r in active if not r.familiar}
    assert all(d.query_json["mix"]["id"] == mix.id for d in dls) and {j.priority for j in jobs} == {-10}
    assert all(r.downloaded_for_mix for r in active if not r.familiar)
    r = await env["client"].get("/api/downloads")
    assert r.status_code == 200 and all(x["star"] for x in r.json())  # clients hide them
    # the playlist «Микс дня» has the songs the library has, in the mix's order
    pl = env["nd"].playlists[settings.nd_playlist_id]
    assert pl["name"] == "Микс дня" and pl["songs"] == [r.nd_song_id for r in active if r.nd_song_id]
    assert mix.title in pl["comment"] and mix.nd_playlist_id == settings.nd_playlist_id


async def test_a_song_that_did_not_download_gives_its_place_to_the_reserve(env, monkeypatch) -> None:
    from app.db import session_scope
    from app.jobs.downloads import update_item
    from app.mixes import core
    from app.models import DownloadItem, Job, MixSettings

    me = env["admin"]
    await history(me)
    fake_generators(monkeypatch, [new_track(5000 + i) for i in range(10)])
    mix = await core.build_for(me)
    rows = await mix_rows(mix.id)
    failed, done = [r for r in rows if r.state == "active" and r.download_item_id][:2]

    await update_item(failed.download_item_id, status="needs_review", error="Не найдено ни в одном источнике")
    async with session_scope() as s:
        assert (await s.execute(select(func.count()).select_from(Job).where(Job.kind == "mix_sync"))).scalar() == 1
    tid = await add_track(5999, music=None)
    async with session_scope() as s:
        dl = await s.get(DownloadItem, done.download_item_id)
        dl.status, dl.track_id = "done", tid
    await core.sync(mix.id)

    rows = {r.id: r for r in await mix_rows(mix.id)}
    sub = next(r for r in rows.values() if r.position == failed.position and r.id != failed.id)
    assert rows[failed.id].state == "failed" and sub.state == "active" and not sub.familiar
    assert sub.download_item_id  # the substitute is downloaded now
    async with session_scope() as s:
        pid = (await s.get(MixSettings, me)).nd_playlist_id
    assert "nd5999" in env["nd"].playlists[pid]["songs"]


async def test_next_mix_keeps_the_playlist_and_closes_the_old_one(env, monkeypatch) -> None:
    from app.db import session_scope
    from app.mixes import core
    from app.models import Mix

    me = env["admin"]
    await history(me)
    fake_generators(monkeypatch, [new_track(5000 + i) for i in range(10)])
    first = await core.build_for(me)
    second = await core.build_for(me)

    async with session_scope() as s:
        old = await s.get(Mix, first.id)
    assert old.status == "closed" and second.status == "active"
    assert second.nd_playlist_id == first.nd_playlist_id and len(env["nd"].playlists) == 1


async def test_no_history_no_mix(env) -> None:
    from app.mixes import core

    assert await core.build_for(env["admin"]) is None


# --- the replacement: what may be deleted (6.3) --------------------------------------------------------------------


async def old_mix_with(user: int, track_id: int, *, for_mix: bool = True, ref: str = "dz:7") -> int:
    from app.db import session_scope
    from app.models import Mix, MixItem

    async with session_scope() as s:
        m = Mix(user_id=user, title="Микс", description="", mode="normal", status="closed")
        s.add(m)
        await s.flush()
        s.add(MixItem(mix_id=m.id, position=0, state="active", ref=ref, nd_song_id="nd7", track_id=track_id,
                      generator="ytm_radio", downloaded_for_mix=for_mix))
        return m.id


async def keep_by(case: str, env, me: int, other: int, track_id: int) -> None:
    from app.db import session_scope
    from app.models import DownloadBatch, DownloadItem, Import, ImportCollection, ImportItem, Mix, MixItem

    if case == "listened":
        await add_event(other, "finish", nd="nd7")
    elif case == "listened 30 s":
        await add_event(other, "skip", ref="dz:7", position=31)
    elif case == "starred":
        env["nd"].starred["other"] = {"nd7"}
    elif case == "in a playlist":
        env["nd"].playlists["plX"] = {"name": "Моё", "owner": "other", "songs": ["nd7"], "comment": ""}
    elif case == "asked to download":
        async with session_scope() as s:
            b = DownloadBatch(user_id=other, origin="catalog")
            s.add(b)
            await s.flush()
            s.add(DownloadItem(batch_id=b.id, user_id=other, status="done", track_id=track_id,
                               query_json={"query": {}, "meta": {"track_ref": "dz:7"}}))
    elif case == "another mix":
        async with session_scope() as s:
            m = Mix(user_id=other, title="Микс", description="", mode="normal", status="active")
            s.add(m)
            await s.flush()
            s.add(MixItem(mix_id=m.id, position=3, state="active", ref="dz:7", generator="favorites"))
    elif case == "an import":
        async with session_scope() as s:
            imp = Import(user_id=other, source="yandex", method="token")
            s.add(imp)
            await s.flush()
            c = ImportCollection(import_id=imp.id, kind="likes", external_id="3", title="Мне нравится")
            s.add(c)
            await s.flush()
            s.add(ImportItem(import_id=imp.id, collection_id=c.id, external_id="y7", ref="dz:7", status="waiting"))
    elif case == "Navidrome down":
        env["nd"].down = True


@pytest.mark.parametrize("case", ["listened", "listened 30 s", "starred", "in a playlist", "asked to download",
                                  "another mix", "an import", "Navidrome down"])
async def test_a_song_of_the_old_mix_stays(env, case: str) -> None:
    from app.db import session_scope
    from app.mixes import core
    from app.models import Track

    me, other = env["admin"], await add_user("other")
    tid = await add_track(7, music=env["music"])
    mix_id = await old_mix_with(me, tid)
    await keep_by(case, env, me, other, tid)

    assert await core.remove_unheard([mix_id]) == 0
    async with session_scope() as s:
        assert await s.get(Track, tid) is not None
    assert (env["music"] / "library" / "Свой 7" / "Песня 7.mp3").is_file()


async def test_a_song_another_mix_holds_goes_when_that_mix_closes(env) -> None:
    from app.db import session_scope
    from app.mixes import core
    from app.models import Mix

    me, other = env["admin"], await add_user("other")
    tid = await add_track(7, music=env["music"])
    mix_id = await old_mix_with(me, tid)
    await keep_by("another mix", env, me, other, tid)
    assert await core.remove_unheard([mix_id]) == 0
    async with session_scope() as s:
        for m in (await s.execute(select(Mix).where(Mix.user_id == other))).scalars():
            m.status = "closed"

    assert await core.pending_cleanup(me) == [mix_id]
    assert await core.remove_unheard([mix_id]) == 1
    assert await core.pending_cleanup(me) == []


async def test_closing_cancels_queued_downloads_and_waits_for_running_ones(env, monkeypatch) -> None:
    from app.db import session_scope
    from app.mixes import core
    from app.models import DownloadItem

    me = env["admin"]
    await history(me)
    fake_generators(monkeypatch, [new_track(5000 + i) for i in range(10)])
    first = await core.build_for(me)
    running, queued = [r for r in await mix_rows(first.id) if r.download_item_id][:2]
    async with session_scope() as s:
        (await s.get(DownloadItem, running.download_item_id)).status = "downloading"
    fake_generators(monkeypatch, [new_track(6000 + i) for i in range(10)])  # other new songs today

    await core.build_for(me)

    async with session_scope() as s:
        assert (await s.get(DownloadItem, queued.download_item_id)).status == "cancelled"
        dl = await s.get(DownloadItem, running.download_item_id)
        assert dl.status == "downloading"
        dl.status, dl.track_id = "done", await add_track(5998)  # it finished after the mix closed
    assert await core.pending_cleanup(me) == [first.id]
    assert await core.remove_unheard([first.id]) == 1  # decided at the next closing
    assert await core.pending_cleanup(me) == []


async def test_a_song_that_was_in_the_library_before_stays(env) -> None:
    from app.mixes import core

    tid = await add_track(7, music=env["music"])
    mix_id = await old_mix_with(env["admin"], tid, for_mix=False)

    assert await core.remove_unheard([mix_id]) == 0


async def test_an_unheard_song_goes_to_the_trash(env) -> None:
    from app.db import session_scope
    from app.mixes import core
    from app.models import Track

    me, other = env["admin"], await add_user("other")
    tid = await add_track(7, music=env["music"])
    mix_id = await old_mix_with(me, tid)
    await add_event(other, "skip", nd="nd7", position=12)  # skipped early: not listened
    await add_event(me, "start", nd="nd7")

    assert await core.remove_unheard([mix_id]) == 1
    async with session_scope() as s:
        assert await s.get(Track, tid) is None
    assert not (env["music"] / "library" / "Свой 7" / "Песня 7.mp3").exists()
    assert list((env["music"] / ".trash").rglob("Песня 7.mp3"))


# --- the API ---------------------------------------------------------------------------------------------------------


async def test_today_settings_and_rebuild(env, monkeypatch) -> None:
    from app.mixes import core

    client = env["client"]
    r = await client.get("/api/mixes/today")
    assert r.status_code == 200, r.text
    assert r.json()["mix"] is None and r.json()["onboarding"] is True
    assert r.json()["settings"]["mode"] == "normal" and r.json()["settings"]["mix_hour"] == 5

    await history(env["admin"])
    fake_generators(monkeypatch, [new_track(5000 + i) for i in range(10)])
    mix = await core.build_for(env["admin"])
    r = await client.get("/api/mixes/today")
    body = r.json()["mix"]
    assert body["id"] == mix.id and body["total"] == 6 and body["mode_title"] == "Обычный"
    first = body["tracks"][0]
    assert first["owned"] and first["nd_song_id"] and first["generator_title"] == "Любимое" and first["familiar"]
    assert {t["generator"] for t in body["tracks"]} == {"favorites", "ytm_radio"}
    assert body["ready"] == sum(1 for t in body["tracks"] if t["nd_song_id"])
    assert (await client.get(f"/api/mixes/{mix.id}")).json()["tracks"] == body["tracks"]

    r = await client.put("/api/mixes/settings", json={"mode": "discover", "mix_hour": 7})
    assert r.status_code == 200 and (r.json()["mode"], r.json()["mix_hour"]) == ("discover", 7)
    assert (await client.put("/api/mixes/settings", json={"mode": "loud"})).status_code == 422
    assert (await client.put("/api/mixes/settings", json={"mix_hour": 24})).status_code == 422

    r = await client.post("/api/mixes/rebuild", json={})
    assert r.status_code == 200 and r.json() == {"queued": True}
    assert (await client.post("/api/mixes/rebuild", json={})).json() == {"queued": False}  # one at a time
    assert (await client.get("/api/mixes/today")).json()["building"] is True


async def test_rebuild_is_for_the_admin_and_mixes_are_private(env, monkeypatch) -> None:
    from app.mixes import core

    other = await add_user("other")
    await history(other)
    fake_generators(monkeypatch, [new_track(5000 + i) for i in range(10)])
    mix = await core.build_for(other)

    async with app_client(env["app"], login=("other", "pw")) as c:
        assert (await c.post("/api/mixes/rebuild", json={})).status_code == 403
        assert (await c.get(f"/api/mixes/{mix.id}")).status_code == 200
    r = await env["client"].post("/api/mixes/rebuild", json={"user_id": other})  # the admin may
    assert r.json() == {"queued": True}


async def test_onboarding_picks_artists_and_starts_a_build(env) -> None:
    from app.db import session_scope
    from app.models import Job

    client = env["client"]
    r = await client.get("/api/taste/onboarding")
    assert r.status_code == 200, r.text
    assert r.json()["needed"] is True and [a["name"] for a in r.json()["suggestions"]] == ["Земфира", "Кино"]

    r = await client.post("/api/taste/artists", json={"artist_refs": ["dz:1"]})
    assert r.status_code == 200, r.text
    assert r.json()["artists"] == [{"ref": "dz:1", "name": "Кино"}]
    assert (await client.post("/api/taste/artists", json={"artist_refs": ["dz:999"]})).status_code == 404
    assert (await client.post("/api/taste/artists", json={"artist_refs": ["hm:1"]})).status_code == 400
    async with session_scope() as s:
        jobs = list((await s.execute(select(Job).where(Job.kind == "mix_build"))).scalars())
    assert [(j.payload_json, j.priority) for j in jobs] == [({"user_id": env["admin"]}, 0)]
    r = await client.get("/api/taste/onboarding")
    assert r.json()["needed"] is False and r.json()["selected"] == [{"ref": "dz:1", "name": "Кино"}]


# --- the schedule ------------------------------------------------------------------------------------------------------


async def test_schedule_builds_once_a_day_after_the_hour(env) -> None:
    from app.mixes import core

    me = env["admin"]
    quiet = await add_user("quiet")  # no history, no picked artists: no mix
    await history(me, 1)
    before = datetime(2026, 10, 23, 1, 30)  # 04:30 in Moscow
    after = datetime(2026, 10, 23, 2, 30)  # 05:30

    assert await core.due_users(before) == []
    assert await core.due_users(after) == [me]
    assert await core.due_users(after + timedelta(hours=1)) == []  # once a day
    assert await core.due_users(after + timedelta(days=1)) == []  # still queued: one build at a time
    assert quiet not in await core.due_users(after + timedelta(days=2))


@pytest.mark.parametrize("shadow", [False, True])
async def test_v2_shadow_does_not_repeat_build_services(env, monkeypatch, shadow):
    from app.config import get_settings
    from app.db import session_scope
    from app.mixes import build, core
    from app.models import DownloadItem, Job

    cfg = get_settings().cfg.mixes
    monkeypatch.setattr(cfg, "taste_model", "v2")
    monkeypatch.setattr(cfg, "taste_shadow", shadow)
    await history(env["admin"])
    fake_generators(monkeypatch, [new_track(5000 + i) for i in range(10)])
    calls = {name: 0 for name in ("run_generators", "sound", "name_mix")}
    for name in calls:
        original = getattr(build, name)

        async def counted(*args, _name=name, _original=original, **kwargs):
            calls[_name] += 1
            return await _original(*args, **kwargs)

        monkeypatch.setattr(build, name, counted)
    mix = await core.build_for(env["admin"])
    assert mix is not None and calls == {"run_generators": 1, "sound": 1, "name_mix": 1}
    if shadow:
        assert mix.stats_json["taste_shadow"]["active_seed_keys"] == mix.stats_json["seed_keys"]
    else:
        assert "taste_shadow" not in mix.stats_json
    rows = await mix_rows(mix.id)
    async with session_scope() as s:
        downloads = list((await s.execute(select(DownloadItem))).scalars())
        jobs = list((await s.execute(select(Job).where(Job.kind == "download_item"))).scalars())
    active_downloads = [row for row in rows if row.state == "active" and row.download_item_id]
    assert len(jobs) == len(downloads) == len(active_downloads)


async def test_publication_failure_keeps_local_mix_and_retries_the_sync_job(env, monkeypatch):
    from app.db import session_scope
    from app.jobs.queue import run_job
    from app.mixes import core
    from app.models import Job, Mix

    await history(env["admin"])
    fake_generators(monkeypatch, [new_track(5000 + i) for i in range(10)])
    with monkeypatch.context() as m:
        async def unavailable(*args):
            raise httpx.ReadTimeout("temporary Navidrome outage")

        m.setattr(core, "write_playlist", unavailable)
        mix = await core.build_for(env["admin"])
        assert mix.stats_json["playlist_status"] == "error"
        assert len([row for row in await mix_rows(mix.id) if row.state == "active"]) == 6
        async with session_scope() as s:
            job = (await s.execute(select(Job).where(Job.kind == "mix_sync"))).scalar_one()
            job.attempts = 1
        await run_job(job)
        async with session_scope() as s:
            retried = await s.get(Job, job.id)
            assert retried.status == "queued" and "temporary Navidrome outage" in retried.last_error
            assert len(list((await s.execute(select(Mix))).scalars())) == 1
        out = (await env["client"].get(f"/api/mixes/{mix.id}")).json()
        assert out["playlist_status"] == "error" and out["playlist_error"]
    await run_job(job)
    async with session_scope() as s:
        current = await s.get(Mix, mix.id)
        assert current.stats_json["playlist_status"] == "published"
        assert current.stats_json["playlist_error"] is None
        assert (await s.get(Job, job.id)).status == "done"
    assert len(env["nd"].playlists) == 1


@pytest.mark.parametrize("failure", ["lost_create_response", "songs", "comment"])
async def test_publication_reconciles_remote_identity_without_duplicates(env, monkeypatch, failure):
    import sqlite3

    from app import auth, db, listen
    from app.mixes import core
    from app.models import Mix

    await history(env["admin"])
    # Light-copy warming has its own transactions; this test isolates the publisher's write lock.
    monkeypatch.setattr(listen, "warm_light", lambda refs: None)
    fake_generators(monkeypatch, [new_track(5000 + i) for i in range(10)])
    original = auth.user_navidrome
    armed = True

    def assert_committed():
        with sqlite3.connect(db.engine().url.database, timeout=0) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT json_extract(stats_json, '$.playlist_status') FROM mixes "
                "WHERE user_id=? ORDER BY id DESC LIMIT 1", (env["admin"],),
            ).fetchone()
            assert row == ("pending",)

    def client(user):
        nd = original(user)
        create, songs, update = nd.create_playlist, nd.set_playlist_songs, nd.update_playlist

        async def create_once(*args, **kwargs):
            nonlocal armed
            assert_committed()
            result = await create(*args, **kwargs)
            if failure == "lost_create_response" and armed:
                armed = False
                raise httpx.ReadTimeout("response lost after create")
            return result

        async def songs_once(*args, **kwargs):
            nonlocal armed
            assert_committed()
            if failure == "songs" and armed:
                armed = False
                raise httpx.ReadTimeout("songs unavailable")
            return await songs(*args, **kwargs)

        async def comment_once(*args, **kwargs):
            nonlocal armed
            assert_committed()
            if failure == "comment" and armed:
                armed = False
                raise httpx.ReadTimeout("comment unavailable")
            return await update(*args, **kwargs)

        nd.create_playlist, nd.set_playlist_songs, nd.update_playlist = create_once, songs_once, comment_once
        return nd

    monkeypatch.setattr(auth, "user_navidrome", client)
    mix = await core.build_for(env["admin"])
    assert mix.stats_json["playlist_status"] == "error"
    assert len(env["nd"].playlists) == 1
    await core.sync(mix.id)
    await core.sync(mix.id)
    async with db.session_scope() as s:
        current = await s.get(Mix, mix.id)
    assert current.stats_json["playlist_status"] == "published"
    assert len(env["nd"].playlists) == 1
    playlist = env["nd"].playlists[current.nd_playlist_id]
    assert playlist["name"] == core.PLAYLIST_NAME
    assert playlist["songs"] == current.stats_json["playlist_songs"]


async def test_ambiguous_create_without_visible_remote_identity_is_not_reissued(env, monkeypatch):
    from app import auth
    from app.mixes import core

    await history(env["admin"])
    fake_generators(monkeypatch, [new_track(5000 + i) for i in range(10)])
    original = auth.user_navidrome
    creates = 0

    def client(user):
        nd = original(user)

        async def lost(*args, **kwargs):
            nonlocal creates
            creates += 1
            raise httpx.ReadTimeout("unknown create outcome")

        nd.create_playlist = lost
        return nd

    monkeypatch.setattr(auth, "user_navidrome", client)
    mix = await core.build_for(env["admin"])
    with pytest.raises(RuntimeError, match="не подтверждено"):
        await core.sync(mix.id)
    assert creates == 1 and mix.stats_json["playlist_status"] == "error"
