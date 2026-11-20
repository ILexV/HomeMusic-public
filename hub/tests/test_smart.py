"""The smart shuffle in the Hub (app/mixes/smart.py, api/smart_shuffle.py): the order of the own songs and the
recommendations between them over the API — the ratio, the filters, the events and their context, «−» and «+», the next
recommendations as the player moves on, a fresh set on every run, the restart.

The generators are fakes (`fake_gens`): new catalog tracks dz:5000…; the playlist's songs are library songs
«Свой n — Песня n» (nd<n>, dz:<n>); Navidrome is the mock of test_mixes.py.
"""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import respx
from sqlalchemy import select

from app.catalog.models import CatalogTrack
from app.mixes import smart
from tests.support import FakeDeezer, app_client
from tests.test_mixes import ND, add_event, add_track, user_id


def new(n: int, artist: str | None = None) -> CatalogTrack:
    a = artist or f"Новый {n}"
    return CatalogTrack(ref=f"dz:{5000 + n}", source="dz", id=str(5000 + n), title=f"Новая {n}", artist=a, artists=[a],
                        artist_ref=f"dz:a-{a}", duration_s=200)


@pytest.fixture
async def env(hub_env, monkeypatch: pytest.MonkeyPatch):
    from app import listen
    from app.main import create_app
    from app.services import svc

    prefetched: list[list[str]] = []
    monkeypatch.setattr(listen, "prefetch", lambda uid, refs: prefetched.append(list(refs)) or len(refs))
    monkeypatch.setattr(listen, "warm_light", lambda refs: None)
    nd = ND()
    app = create_app()
    async with app.router.lifespan_context(app):
        svc.deezer = FakeDeezer()
        with respx.mock(assert_all_called=False) as mock:
            mock.route(host="navidrome.test").mock(side_effect=nd.handler)
            async with app_client(app) as client:
                yield {"client": client, "nd": nd, "admin": await user_id("admin"), "prefetched": prefetched}


def fake_gens(monkeypatch: pytest.MonkeyPatch, tracks: list[CatalogTrack]) -> list[list[str]]:
    """The pool is made of `tracks` (every generator call returns them); returns the seeds each start was asked by."""
    seeds_seen: list[list[str]] = []

    def gens(p, seeds):
        seeds_seen.append([t.title for t in seeds])

        async def g():
            return list(tracks)

        return {"ytm_radio": g}

    monkeypatch.setattr(smart, "generators", gens)
    return seeds_seen


async def library(n: int, artists: int | None = None) -> list[str]:
    """Library songs 1..n (Navidrome ids nd1…); `artists` — they belong to that many artists."""
    from app.db import session_scope
    from app.models import Track

    for i in range(1, n + 1):
        await add_track(i)
    if artists:
        async with session_scope() as s:
            for t in (await s.execute(select(Track))).scalars():
                t.artist = f"Исп {int(t.nd_song_id[2:]) % artists}"
    return [f"nd{i}" for i in range(1, n + 1)]


def ev(kind: str, ref: str, position: float | None = None, *, context: str | None = None,
       nd: str | None = None) -> dict[str, Any]:
    e: dict[str, Any] = {"event_id": uuid.uuid4().hex, "ts": datetime.now(UTC).isoformat(), "kind": kind,
                         "position_s": position, "duration_s": 200, "client": "pwa"}
    e.update({"ref": ref} if ref else {}, **({"nd_song_id": nd} if nd else {}), **({"context": context} if context else {}))
    return e


async def start(c, songs: list[str], context: str = "playlist:pl1", current: str | None = None) -> dict[str, Any]:
    r = await c.post("/api/smart-shuffle/start", json={"context": context, "songs": songs, "current": current,
                                                       "title": "Мой плейлист"})
    assert r.status_code == 200, r.text
    return r.json()


def sequence(plan: dict[str, Any]) -> list[str]:
    """What a client plays: the own songs in order, a recommendation right after the own song it names."""
    out: list[str] = []
    for i in plan["own"]:
        out.append(i)
        out += [r["ref"] for r in plan["recs"] if r["after"] == i]
    return out


# --- start ----------------------------------------------------------------------------------------------------------


async def test_a_long_playlist_gets_a_recommendation_after_every_third_own_song(env, monkeypatch) -> None:
    ids = await library(20)
    fake_gens(monkeypatch, [new(i) for i in range(30)])

    plan = await start(env["client"], ids, current="nd7")

    assert plan["own"][0] == "nd7" and sorted(plan["own"]) == sorted(ids) and plan["own_per_rec"] == 3
    assert [r["slot"] for r in plan["recs"]] == [0, 1, 2]  # the first three places: three recommendations ahead
    assert [r["after"] for r in plan["recs"]] == [plan["own"][2], plan["own"][5], plan["own"][8]]
    seq = sequence(plan)
    assert [i for i, x in enumerate(seq) if x.startswith("dz:")] == [3, 7, 11]  # own, own, own, rec
    assert all(r["generator"] and "after" in r for r in plan["recs"])
    assert env["prefetched"][-1] == [r["ref"] for r in plan["recs"]]  # their files are picked ahead


@pytest.mark.parametrize("model", ["v1", "v2"])
async def test_smart_slots_record_track_novelty_from_account_history(env, monkeypatch, model):
    from app.config import get_settings
    from app.db import session_scope
    from app.models import SmartItem

    monkeypatch.setattr(get_settings().cfg.mixes, "taste_model", model)
    ids = await library(13)
    at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=10)
    for i in range(6):
        await add_event(env["admin"], "finish", nd=ids[-1], at=at - timedelta(hours=i))
    familiar = CatalogTrack(ref="dz:13", source="dz", id="13", title="Песня 13", artist="Свой 13", duration_s=200)
    fake_gens(monkeypatch, [new(0, "Свой 13"), new(1), familiar])
    plan = await start(env["client"], ids[:-1], current=ids[0])
    assert [r["slot"] for r in plan["recs"]] == [0, 1, 2]
    async with session_scope() as s:
        rows = (await s.scalars(select(SmartItem).where(SmartItem.session_id == plan["session_id"]))).all()
        assert {r.ref: (r.familiar, r.decision_json["novelty"]) for r in rows} == {
            "dz:5000": (False, "new_track_known_artist"), "dz:5001": (False, "new_artist"),
            "dz:13": (True, "familiar_track")}
    assert any("Новый трек знакомого исполнителя" in why for r in plan["recs"] if r["ref"] == "dz:5000"
               for why in r["explanation"])


async def test_a_short_playlist_gets_one_after_every_second_and_a_tiny_one_gets_one(env, monkeypatch) -> None:
    ids = await library(8)
    fake_gens(monkeypatch, [new(i) for i in range(30)])

    plan = await start(env["client"], ids[:8])
    one = await start(env["client"], ids[:1])
    two = await start(env["client"], ids[:2])

    assert plan["own_per_rec"] == 2 and [r["after"] for r in plan["recs"]] == [plan["own"][1], plan["own"][3],
                                                                              plan["own"][5]]
    assert len(one["recs"]) == 1 and one["recs"][0]["after"] == "nd1"  # a single song: followed by a recommendation
    assert len(two["recs"]) == 1 and two["recs"][0]["after"] == two["own"][1]


async def test_the_playlist_songs_and_the_disliked_are_never_recommended(env, monkeypatch) -> None:
    from app.db import session_scope
    from app.models import Dislike

    ids = await library(10)
    me = env["admin"]
    pool = [new(i) for i in range(12)]
    pool += [CatalogTrack(ref="dz:3", source="dz", id="3", title="Песня 3", artist="Свой 3", artists=["Свой 3"]),  # in it
             CatalogTrack(ref="dz:9999", source="dz", id="9999", title="Песня 4", artist="Свой 4", artists=["Свой 4"])]
    fake_gens(monkeypatch, pool)
    async with session_scope() as s:
        s.add(Dislike(user_id=me, ref="dz:5000", source="app"))
        s.add(Dislike(user_id=me, artist_ref="dz:a-Новый 1", artist="Новый 1", source="app"))
    banned = {"dz:3", "dz:9999", "dz:5000", "dz:5001"}  # the playlist's songs (by id and by name), the disliked

    refs = set()
    for _ in range(3):
        refs |= {r["ref"] for r in (await start(env["client"], ids))["recs"]}

    assert refs and not refs & banned


async def test_recently_heard_songs_stand_later_in_the_order(env, monkeypatch) -> None:
    ids = await library(40)
    me = env["admin"]
    fake_gens(monkeypatch, [new(i) for i in range(30)])
    for i in range(1, 21):
        await add_event(me, "finish", nd=f"nd{i}", at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=i))
    from app.auth import current_user  # noqa: F401  (the user object below is the stored admin)
    from app.db import session_scope
    from app.models import User

    async with session_scope() as s:
        user = await s.get(User, me)

    heard_pos, fresh_pos = [], []
    for seed in range(15):
        plan = await smart.start(user, "playlist:pl1", ids, "nd40", rng=random.Random(seed))
        heard_pos += [plan.own.index(f"nd{i}") for i in range(1, 21)]
        fresh_pos += [plan.own.index(f"nd{i}") for i in range(21, 40)]

    assert sum(heard_pos) / len(heard_pos) > sum(fresh_pos) / len(fresh_pos) + 4


async def test_a_catalog_only_like_is_an_own_song_by_its_ref(env, monkeypatch) -> None:
    ids = await library(5)
    fake_gens(monkeypatch, [new(i) for i in range(12)])

    plan = await start(env["client"], ["dz:777", *ids], context="liked")

    assert sorted(plan["own"]) == sorted(["dz:777", *ids]) and "dz:777" not in [r["ref"] for r in plan["recs"]]


async def test_start_validates_the_context_and_the_list(env) -> None:
    c = env["client"]

    assert (await c.post("/api/smart-shuffle/start", json={"context": "album:1", "songs": ["a"]})).status_code == 422
    assert (await c.post("/api/smart-shuffle/start", json={"context": "liked", "songs": []})).status_code == 422
    assert (await c.post("/api/smart-shuffle/start", json={"context": "playlist:", "songs": ["a"]})).status_code == 422


# --- next -----------------------------------------------------------------------------------------------------------


async def test_next_fills_the_places_ahead_as_the_player_moves(env, monkeypatch) -> None:
    ids = await library(30)
    fake_gens(monkeypatch, [new(i) for i in range(40)])
    c = env["client"]
    plan = await start(c, ids)
    sid = plan["session_id"]

    same = (await c.post(f"/api/smart-shuffle/{sid}/next", json={"at": plan["own"][0]})).json()
    later = (await c.post(f"/api/smart-shuffle/{sid}/next", json={"at": plan["own"][8]})).json()

    assert same["recs"] == [] and same["own"] == []  # nothing missing yet
    assert [r["slot"] for r in later["recs"]] == [3, 4]  # places 0..2 are filled; at song 9 the next three are 2..4
    assert [r["after"] for r in later["recs"]] == [plan["own"][11], plan["own"][14]]
    assert not {r["ref"] for r in later["recs"]} & {r["ref"] for r in plan["recs"]}  # never the same track twice


async def test_next_after_a_restart_of_the_hub_goes_on_from_the_stored_session(env, monkeypatch) -> None:
    ids = await library(30)
    fake_gens(monkeypatch, [new(i) for i in range(40)])
    c = env["client"]
    plan = await start(c, ids)
    await smart.reset()

    r = await c.post(f"/api/smart-shuffle/{plan['session_id']}/next", json={"at": plan["own"][5]})

    assert r.status_code == 200, r.text
    assert [x["slot"] for x in r.json()["recs"]] == [3]


async def test_a_session_of_another_user_or_unknown_is_404(env, monkeypatch) -> None:
    ids = await library(5)
    fake_gens(monkeypatch, [new(i) for i in range(12)])
    c = env["client"]
    plan = await start(c, ids)

    assert (await c.post("/api/smart-shuffle/nope/next", json={})).status_code == 404
    assert (await c.post("/api/smart-shuffle/nope/hide", json={"ref": "dz:1"})).status_code == 404
    assert (await c.post(f"/api/smart-shuffle/{plan['session_id']}/hide", json={"ref": "dz:404"})).status_code == 404


async def test_events_of_a_recommendation_get_the_smart_context_and_teach_the_session(env, monkeypatch) -> None:
    from app.db import session_scope
    from app.models import PlayEvent

    ids = await library(30, artists=30)
    fake_gens(monkeypatch, [new(i) for i in range(40)])
    c = env["client"]
    plan = await start(c, ids)
    sid = plan["session_id"]
    first, second = plan["recs"][0], plan["recs"][1]
    own_event = {**ev("finish", "", nd=plan["own"][0], context="playlist"), "play_trigger": "manual"}
    early = {**ev("start", first["ref"]), "play_trigger": "auto"}
    stored_first = {**ev("start", second["ref"]), "context": "queue", "play_trigger": "repeat"}
    assert (await c.post("/api/plays/events", json={"events": [stored_first]})).status_code == 200

    r = await c.post(f"/api/smart-shuffle/{sid}/next", json={
        "played": [own_event, early, ev("skip", first["ref"], 8), stored_first], "at": plan["own"][3]})

    assert r.status_code == 200, r.text
    assert r.json()["recorded"]["accepted"] == 3 and r.json()["recorded"]["duplicates"] == 1
    async with session_scope() as s:
        rows = list((await s.execute(select(PlayEvent).where(PlayEvent.user_id == env["admin"]))).scalars())
    contexts = {e.ref: e.context for e in rows if e.ref}
    assert contexts[first["ref"]] == contexts[second["ref"]] == f"smart:{sid}"  # also the one stored under "queue"
    assert {e.context for e in rows if e.nd_song_id == plan["own"][0]} == {"playlist"}  # an own song keeps its list
    triggers = {e.event_id: e.play_trigger for e in rows}
    assert [triggers[e["event_id"]] for e in (own_event, early, stored_first)] == ["manual", "auto", "repeat"]
    # the early skip counted against the artist in this session
    assert smart._rt[sid].state.skips.get(smart.artist_key(first["artist"])) == 1


# --- «−» and «+» -----------------------------------------------------------------------------------------------------


async def test_hide_removes_the_track_from_this_context_for_good(env, monkeypatch) -> None:
    from app.db import session_scope
    from app.models import Dislike, SmartHidden, SmartItem

    ids = await library(20)
    fake_gens(monkeypatch, [new(i) for i in range(30)])
    c = env["client"]
    plan = await start(c, ids)
    hidden = plan["recs"][0]

    r = await c.post(f"/api/smart-shuffle/{plan['session_id']}/hide", json={"ref": hidden["ref"]})

    assert r.status_code == 200, r.text
    async with session_scope() as s:
        row = (await s.execute(select(SmartHidden))).scalar_one()
        item = (await s.execute(select(SmartItem).where(SmartItem.ref == hidden["ref"]))).scalar_one()
        assert (row.context, row.ref, item.state) == ("playlist:pl1", hidden["ref"], "hidden")
        assert not (await s.execute(select(Dislike))).scalars().all()  # not the global «Не нравится»
    for _ in range(4):  # runs of the same context never offer it again ...
        assert hidden["ref"] not in [x["ref"] for x in (await start(c, ids))["recs"]]
    other = {x["ref"] for x in (await start(c, ids, context="playlist:other"))["recs"]}  # ... another context may
    assert len(other) == 3


async def test_a_hidden_track_is_gone_from_the_rest_of_the_session_too(env, monkeypatch) -> None:
    ids = await library(30)
    fake_gens(monkeypatch, [new(i) for i in range(40)])
    c = env["client"]
    plan = await start(c, ids)
    sid = plan["session_id"]
    pool_refs = set(smart._rt[sid].state.pool)
    gone = next(iter(pool_refs))

    await c.post(f"/api/smart-shuffle/{sid}/hide", json={"ref": plan["recs"][0]["ref"]})
    smart._rt[sid].state.pool.pop(gone)  # (any later arrival of the hidden one is filtered by its fingerprint)
    later = (await c.post(f"/api/smart-shuffle/{sid}/next", json={"at": plan["own"][14]})).json()

    assert plan["recs"][0]["ref"] not in [r["ref"] for r in later["recs"]]


async def test_add_puts_a_library_recommendation_in_the_playlist(env, monkeypatch) -> None:
    ids = await library(20)
    await add_track(101)  # the library has dz:101 as well
    env["nd"].playlists["pl1"] = {"name": "Мой", "owner": "admin", "songs": list(ids), "comment": ""}
    fake_gens(monkeypatch, [CatalogTrack(ref="dz:101", source="dz", id="101", title="Песня 101", artist="Свой 101",
                                        artists=["Свой 101"], duration_s=200)])
    c = env["client"]
    plan = await start(c, ids)
    rec = plan["recs"][0]
    assert rec["ref"] == "dz:101" and rec["owned"] and rec["nd_song_id"] == "nd101"

    r = await c.post(f"/api/smart-shuffle/{plan['session_id']}/add", json={"ref": rec["ref"]})

    assert r.status_code == 200 and r.json() == {"ok": True, "status": "added", "playlist_id": "pl1"}
    assert env["nd"].playlists["pl1"]["songs"][-1] == "nd101"


async def test_add_downloads_a_catalog_track_into_the_playlist(env, monkeypatch) -> None:
    from app.db import session_scope
    from app.models import DownloadBatch, Job

    ids = await library(20)
    fake_gens(monkeypatch, [new(i) for i in range(30)])
    c = env["client"]
    plan = await start(c, ids)
    rec = plan["recs"][0]
    assert not rec["owned"]

    r = await c.post(f"/api/smart-shuffle/{plan['session_id']}/add", json={"ref": rec["ref"]})

    assert r.status_code == 200 and r.json()["status"] == "downloading", r.text
    async with session_scope() as s:
        batch = (await s.execute(select(DownloadBatch).where(DownloadBatch.origin == "smart-shuffle"))).scalar_one()
        jobs = (await s.execute(select(Job).where(Job.kind == "expand_batch"))).scalars().all()
    assert batch.target_playlist_id == "pl1" and any(rec["ref"] in j.payload_json["track_refs"] for j in jobs)


async def test_add_in_liked_only_notes_it_and_the_next_picks_lean_to_it(env, monkeypatch) -> None:
    ids = await library(30)
    fake_gens(monkeypatch, [new(i) for i in range(40)])
    c = env["client"]
    plan = await start(c, ids, context="liked")
    rec = plan["recs"][0]

    r = await c.post(f"/api/smart-shuffle/{plan['session_id']}/add", json={"ref": rec["ref"]})

    assert r.json() == {"ok": True, "status": "liked", "playlist_id": None}
    assert smart._rt[plan["session_id"]].accepted == [rec["ref"]]


# --- a fresh set, no pool ---------------------------------------------------------------------------------------------


async def test_every_run_brings_a_fresh_set(env, monkeypatch) -> None:
    ids = await library(20)
    fake_gens(monkeypatch, [new(i) for i in range(30)])
    c = env["client"]

    first = {r["ref"] for r in (await start(c, ids))["recs"]}
    second = {r["ref"] for r in (await start(c, ids))["recs"]}
    elsewhere = {r["ref"] for r in (await start(c, ids, context="liked"))["recs"]}

    assert len(first) == len(second) == 3 and not first & second
    assert elsewhere == first  # only the small shared spacing term applies outside the same context


async def test_a_scarce_pool_repeats_rather_than_leave_a_place_empty(env, monkeypatch) -> None:
    ids = await library(20)
    fake_gens(monkeypatch, [new(i) for i in range(3)])
    c = env["client"]

    first = await start(c, ids)
    second = await start(c, ids)

    assert len(first["recs"]) == len(second["recs"]) == 3


async def test_the_recommendation_artists_do_not_repeat_and_do_not_touch_their_neighbours(env, monkeypatch) -> None:
    ids = await library(30, artists=30)
    pool = [new(i, "Один и тот же") for i in range(6)] + [new(10 + i) for i in range(20)]
    fake_gens(monkeypatch, pool)
    c = env["client"]

    plan = await start(c, ids)
    nxt = (await c.post(f"/api/smart-shuffle/{plan['session_id']}/next", json={"at": plan["own"][20]})).json()

    artists = [r["artist"] for r in [*plan["recs"], *nxt["recs"]]]
    assert artists.count("Один и тот же") == 1 and len(artists) == len(set(artists))


async def test_seeds_are_ten_songs_of_the_playlist(env, monkeypatch) -> None:
    ids = await library(40, artists=40)
    seen = fake_gens(monkeypatch, [new(i) for i in range(12)])

    await start(env["client"], ids)

    assert len(seen[0]) == 10 and set(seen[0]) <= {f"Песня {i}" for i in range(1, 41)}
    assert {"Песня 40", "Песня 39", "Песня 38"} <= set(seen[0])  # the newest added ones are among them


async def test_sessions_idle_for_a_week_lose_their_state_but_keep_their_rows(env, monkeypatch) -> None:
    from app.db import session_scope
    from app.models import SmartItem, SmartSession

    ids = await library(20)
    fake_gens(monkeypatch, [new(i) for i in range(30)])
    plan = await start(env["client"], ids)
    async with session_scope() as s:
        (await s.get(SmartSession, plan["session_id"])).updated_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8)

    assert await smart.clear_old() == 1

    async with session_scope() as s:
        assert (await s.get(SmartSession, plan["session_id"])).state_json == {"cleared": True}
        assert len((await s.execute(select(SmartItem))).scalars().all()) == 3
    assert (await env["client"].post(f"/api/smart-shuffle/{plan['session_id']}/next", json={})).status_code == 404


def test_the_smart_context_is_a_recommended_one_for_the_taste_and_a_source_for_the_insights() -> None:
    from app.insights import launches
    from app.mixes.taste_math import recommended

    assert recommended("smart:abc") and not recommended("playlist") and launches.source_of("smart:abc") == "smart"




async def test_the_v2_taste_model_learns_from_the_session_and_survives_a_restart(env, monkeypatch) -> None:
    from app.config import get_settings
    from app.db import session_scope
    from app.models import SmartSession

    monkeypatch.setattr(get_settings().cfg.mixes, "taste_model", "v2")
    ids = await library(30, artists=30)
    fake_gens(monkeypatch, [new(i) for i in range(40)])
    c = env["client"]
    plan = await start(c, ids)
    sid, first = plan["session_id"], plan["recs"][0]["ref"]
    assert (await c.post(f"/api/smart-shuffle/{sid}/add", json={"ref": plan["recs"][1]["ref"]})).status_code == 200

    r = await c.post(f"/api/smart-shuffle/{sid}/next", json={
        "played": [ev("start", first), ev("finish", first, 200)], "at": plan["own"][5]})

    assert r.status_code == 200, r.text
    await smart.settle(sid)
    weights = smart._rt[sid].state.positive_weights
    assert weights[first] == 2 and weights[plan["recs"][1]["ref"]] == 1  # played to its end, and added with «+»
    async with session_scope() as s:
        assert (await s.get(SmartSession, sid)).state_json["wave"]["taste_model"] == "v2"
    monkeypatch.setattr(get_settings().cfg.mixes, "taste_model", "v1")
    await smart.reset()
    again = await c.post(f"/api/smart-shuffle/{sid}/next", json={"at": plan["own"][14]})
    assert again.status_code == 200 and smart._rt[sid].state.taste_model == "v2"
