"""Building a «Микс дня» (app/mixes/build.py): filters, score, mode shares, ≤ 2 songs of an artist, order, name.
Generators are fakes here (their sources: test_mix_sources.py); the profile is made by hand (its making: test_mixes.py).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path

import httpx
import pytest
import respx

from app.catalog.models import CatalogTrack
from app.mixes import build as b
from app.mixes.taste import Profile, TasteTrack, artist_key

NOW = datetime(2026, 10, 23, 2, 0)  # Friday 05:00 in Moscow
EMBEDDER = "http://embedder.test"


def tr(n: int | str, artist: str = "", title: str = "", *, duration: int | None = 200, rank: int | None = None,
       artist_ref: str | None = None) -> CatalogTrack:
    return CatalogTrack(ref=f"dz:{n}", source="dz", id=str(n), title=title or f"Песня {n}",
                        artist=artist or f"Артист {n}", artists=[artist or f"Артист {n}"], duration_s=duration,
                        rank=rank, artist_ref=artist_ref)


def cand(n: int, artist: str = "", *, familiar: bool = False, score: float = 1.0) -> b.Cand:
    return b.Cand(track=tr(n, artist), gens={"ytm_radio": 0}, familiar=familiar, score=score)


def profile(*tracks: TasteTrack, listens: dict[str, int] | None = None) -> Profile:
    p = Profile(user_id=1, now=NOW)
    for t in tracks:
        p.tracks[t.key] = t
        for k in (t.ref, f"nd:{t.nd_song_id}" if t.nd_song_id else None, t.fp or None):
            if k:
                p.by_id[k] = t.key
        p.artists[artist_key(t.artist)] = p.artists.get(artist_key(t.artist), 0) + t.score
        p.artist_names[artist_key(t.artist)] = t.artist
    p.artist_listens = {artist_key(a): n for a, n in (listens or {}).items()}
    return p


def known(n: int, artist: str = "", *, score: float = 2.0, played_days_ago: float | None = None, **kw) -> TasteTrack:
    t = tr(n, artist)
    played = NOW - timedelta(days=played_days_ago) if played_days_ago is not None else None
    return TasteTrack(key=t.ref, ref=t.ref, artist=t.artist, title=t.title, score=score, last_played=played,
                      last_event=played, last_heard=played, heard30=played is not None, track=t, **kw)


# --- filters and score -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("track", "why"), [
    (tr(1, "Нелюбимый"), "disliked track"),
    (tr(2, "Нелюбимый артист", artist_ref="dz:666"), "disliked artist by ref"),
    (tr(3, "Тоже нелюбимый"), "disliked artist by name"),
    (tr(4, title="Песня (Live)"), "live"),
    (tr(5, title="Песня (Remix)"), "remix"),
    (tr(6, title="Песня (Караоке версия)"), "karaoke"),
    (tr(7, title="Песня - минусовка"), "instrumental"),
    (tr(8, duration=80), "shorter than 1:30"),
    (tr(9, duration=660), "longer than 10 min"),
], ids=lambda x: x if isinstance(x, str) else "")
def test_filters_drop(track: CatalogTrack, why: str) -> None:
    p = profile()
    p.disliked_ids.add("dz:1")
    p.disliked_artist_refs.add("dz:666")
    p.disliked_artists.add(artist_key("Тоже нелюбимый"))

    assert not b.keep(b.Cand(track=track, gens={"ytm_radio": 0}), p, b.get_settings().cfg.mixes), why


def test_familiar_and_new() -> None:
    p = profile(known(1, "Кино", played_days_ago=7), known(2, "Кино", played_days_ago=1), known(3, "Кино", score=-1),
                known(4, "Скачанный", score=0, downloaded=True), listens={"Кино": 6, "Сплин": 5})
    cfg = b.get_settings().cfg.mixes
    heard, recent, skipped = (b.Cand(track=tr(n, "Кино")) for n in (1, 2, 3))
    downloaded = b.Cand(track=tr(4, "Скачанный"))
    unheard_known_artist, unknown_artist = b.Cand(track=tr(10, "Кино")), b.Cand(track=tr(11, "Сплин"))

    assert b.keep(heard, p, cfg) and heard.familiar
    assert not b.keep(recent, p, cfg)  # heard in the last 3 days
    assert not b.keep(skipped, p, cfg)  # the user skips it
    assert b.keep(downloaded, p, cfg) and downloaded.familiar  # only downloaded: score 0 is not a skip
    assert b.keep(unheard_known_artist, p, cfg) and not unheard_known_artist.familiar
    assert b.keep(unknown_artist, p, cfg) and not unknown_artist.familiar
    assert [c.features()["novelty"] for c in (heard, downloaded, unheard_known_artist, unknown_artist)] == [
        "familiar_track", "familiar_track", "new_track_known_artist", "new_artist"]


@pytest.mark.parametrize("model", ["v1", "v2"])
def test_novelty_uses_playback_and_current_preferences_not_profile_row_presence(model):
    from app.mixes.taste_math import calculate_baseline, calculate_profile
    from app.mixes.taste_snapshot import TasteEvent, TasteSnapshot, TrackMetadata
    from app.recommendations import explain_candidate

    at = NOW - timedelta(days=8)
    events = (
        TasteEvent(1, "heard", at, "skip", "dz:1", nd_song_id="a", position_s=5, listened_ms=30_000),
        TasteEvent(1, "started", at, "start", "dz:2"),
        TasteEvent(1, "skip", at, "skip", "dz:3", position_s=195, listened_ms=5_000),
        TasteEvent(1, "failed", at, "error", "dz:4", listened_ms=50_000),
        TasteEvent(1, "like", at, "like", "dz:5", target_data={"artist": "Favourite"}),
        TasteEvent(1, "artist-like", at, "like", "dz:artist", target_type="artist"),
        TasteEvent(2, "foreign", at, "finish", "dz:6"),
        TasteEvent(1, "debug", at, "finish", "dz:7", client="web-debug"),
    )
    snapshot = TasteSnapshot(1, events, metadata=(TrackMetadata("dz:1", nd_song_id="a", artist="Heard", title="Song"),),
                             picked_artists=({"name": "Onboarding"},))
    p = (calculate_baseline if model == "v1" else calculate_profile)(snapshot, now=NOW, timezone="UTC")
    for ref in range(1, 8):
        c = b.Cand(track=tr(ref), nd_song_id="a" if ref == 1 else None)
        b.snapshot_profile(c, p)
        assert c.familiar == (ref in (1, 5))
        assert c.novelty == ("familiar_track" if c.familiar else "new_artist")
    for track in (tr(8, "FAVOURITE"), tr(9, "Onboarding"), tr(10, artist_ref="dz:artist")):
        c = b.Cand(track=track)
        assert b.keep(c, p, b.get_settings().cfg.mixes) and not c.familiar
        assert c.novelty == "new_track_known_artist"
        assert any("Новый трек знакомого исполнителя" in reason for reason in explain_candidate(c))


def test_score_adds_generators_sound_and_popularity() -> None:
    w = b.get_settings().cfg.mixes.weights
    one = b.Cand(track=tr(1), gens={"ytm_radio": 0})
    two = b.Cand(track=tr(1), gens={"ytm_radio": 0, "lastfm_similar": 2})
    far = b.Cand(track=tr(1), gens={"ytm_radio": 30})
    sound = b.Cand(track=tr(1, rank=1_000_000), gens={"ytm_radio": 0}, sim=0.5)

    assert b.score(one, w) == pytest.approx(1.0)
    assert b.score(two, w) == pytest.approx(1.0 + 0.8 / 2)
    assert b.score(far, w) < 0.25
    assert b.score(sound, w) == pytest.approx(1.0 + 1.5 * 0.5 + 0.2 * 1.0)


def test_merge_joins_the_same_song_from_two_sources() -> None:
    r1 = b.GenResult("ytm_radio", [tr(1, "Кино", "Война"), tr(2)])
    r2 = b.GenResult("lastfm_similar", [tr(3), tr(77, "КИНО", "Война")])  # another Deezer id of the same song

    cands = b.merge([r1, r2])

    assert cands["dz:1"].gens == {"ytm_radio": 0, "lastfm_similar": 1} and len(cands) == 3


# --- assembling ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("mode", "familiar"), [("normal", 20), ("favorites", 40), ("discover", 5)])
def test_mode_shares(mode: str, familiar: int) -> None:
    pool = [cand(i, familiar=True, score=10 - i / 100) for i in range(60)]
    pool += [cand(100 + i, score=9 - i / 100) for i in range(100)]

    active, reserve = b.select_songs(pool, mode, 50, 20, 2)

    assert len(active) == 50 and sum(c.familiar for c in active) == familiar
    assert len(reserve) == 20 and not {c.track.ref for c in reserve} & {c.track.ref for c in active}


@pytest.mark.parametrize("rare", ["new_track_known_artist", "new_artist"])
@pytest.mark.parametrize("share", [None, .4, .6])
def test_discover_keeps_both_kinds_in_active_and_reserve_despite_score_gap(rare, share):
    common = next(kind for kind in b.DISCOVERIES if kind != rare)
    pool = [cand(i, familiar=True) for i in range(6)]
    for i in range(60):
        c = cand(100 + i, score=1 - i / 100)
        c.novelty = rare if i >= 58 else common
        pool.append(c)

    active, reserve = b.select_songs(pool, "discover", 30, 10, 2, share)
    for group, familiar in ((active, 3), (reserve, 1)):
        assert sum(c.familiar for c in group) == familiar
        assert {c.novelty for c in group if not c.familiar} == b.DISCOVERIES
        assert sum(c.novelty == rare for c in group) == 1


@pytest.mark.parametrize("share", [.4, .6])
def test_subtype_quota_stays_inside_artist_cap_and_does_not_reduce_the_familiar_quota(share):
    pool = [cand(i, familiar=True) for i in range(10)]
    for i in range(60):
        # Increasing this low-ranked subtype's share may not exhaust its single artist's cap in active.
        c = cand(i + 100, artist="Repeated" if i < 30 else f"Other {i}", score=i)
        c.novelty = b.NEW_TRACK_KNOWN_ARTIST if i < 30 else b.NEW_ARTIST
        pool.append(c)
    active, reserve = b.select_songs(pool, "discover", 20, 10, 2, share)
    assert [sum(c.familiar for c in group) for group in (active, reserve)] == [2, 1]
    assert [sum(c.novelty == b.NEW_TRACK_KNOWN_ARTIST for c in group) for group in (active, reserve)] == [1, 1]
    assert (len(active), len(reserve)) == (20, 10)


def test_at_most_two_songs_of_an_artist() -> None:
    pool = [cand(i, "Кино", familiar=True, score=100 - i) for i in range(30)] + [cand(100 + i) for i in range(80)]

    active, reserve = b.select_songs(pool, "normal", 50, 20, 2)

    assert sum(c.track.artist == "Кино" for c in active + reserve) == 2


def test_a_short_pool_is_made_up_by_the_other() -> None:
    pool = [cand(i, familiar=True) for i in range(10)] + [cand(100 + i) for i in range(100)]

    active, _ = b.select_songs(pool, "normal", 50, 20, 2)

    assert len(active) == 50 and sum(c.familiar for c in active) == 10


def test_order_familiar_first_shares_spread_no_artist_twice_in_a_row() -> None:
    songs = [cand(i, f"F{i // 2}", familiar=True, score=50 - i) for i in range(20)]
    songs += [cand(100 + i, f"N{i // 2}", score=40 - i) for i in range(30)]

    out = b.order(songs, {})

    assert out[0].familiar and out[0].score == 50
    assert all(a.artist != c.artist for a, c in pairwise(out))
    assert sum(c.familiar for c in out[:10]) in (4, 5)  # 40 % of every stretch, not all at once


def test_order_puts_the_closest_by_sound_next() -> None:
    first = cand(1, familiar=True, score=10)
    near, far = cand(2, familiar=True, score=5), cand(3, familiar=True, score=6)

    out = b.order([first, far, near], {"dz:1": [1.0, 0.0], "dz:2": [0.9, 0.1], "dz:3": [0.0, 1.0]})

    assert [c.track.ref for c in out] == ["dz:1", "dz:2", "dz:3"]


def test_stash_and_favorites() -> None:
    p = profile(known(1, score=5, played_days_ago=3), known(2, score=4, played_days_ago=10),
                known(3, score=1, played_days_ago=90, liked=True), known(4, score=0, downloaded=True),
                known(5, score=3, played_days_ago=70, liked=True))

    # not the one heard 3 days ago, nor the one only downloaded (a download weighs nothing, M4)
    assert [t.ref for t in b.favorites(p)] == ["dz:2", "dz:5", "dz:3"]
    assert sorted(t.ref for t in b.stash(p)) == ["dz:3", "dz:4", "dz:5"]


# --- the whole build ----------------------------------------------------------------------------------------------


@pytest.fixture
async def db(hub_env, db_file: Path):
    from app.db import dispose, init_engine

    init_engine(db_file)
    yield
    await dispose()


def gens(**lists: list[CatalogTrack] | Exception) -> dict[str, b.Gen]:
    def make(x):
        async def fn():
            if isinstance(x, Exception):
                raise x
            return x
        return fn

    return {k: make(v) for k, v in lists.items()}


async def test_build_without_sound_and_ai(db) -> None:
    p = profile(*(known(i, f"Свой {i}", played_days_ago=20) for i in range(40)))
    radio = [tr(1000 + i, f"Новый {i}") for i in range(80)]

    built = await b.build(p, "normal", gens(favorites=b.favorites(p), ytm_radio=radio,
                                            lastfm_similar=b.Off("нет LASTFM_API_KEY"),
                                            yandex_similar=RuntimeError("yandex is down")))

    assert len(built.active) == 50 and sum(c.familiar for c in built.active) == 20 and len(built.reserve) == 20
    assert built.title == "Микс дня · пятница" and "знакомых" in built.description
    g = built.stats["generators"]
    assert g["favorites"]["used"] == 20 and g["ytm_radio"]["used"] == 30
    assert g["lastfm_similar"] == {"tracks": 0, "used": 0, "seconds": g["lastfm_similar"]["seconds"],
                                   "error": "нет LASTFM_API_KEY", "off": True}
    assert "yandex is down" in g["yandex_similar"]["error"] and built.stats["embedder"] == "off"


async def test_build_with_sound(db, monkeypatch: pytest.MonkeyPatch, respx_mock: respx.MockRouter) -> None:
    """New candidates are embedded from fresh Deezer previews; the build waits for their vectors and scores by them."""
    from app.config import get_settings
    from app.services import svc
    from tests.support import FakeDeezer

    monkeypatch.setenv("EMBEDDER_URL", EMBEDDER)
    monkeypatch.setenv("MIX_POLL_S", "0")
    get_settings.cache_clear()
    monkeypatch.setattr(svc, "deezer", FakeDeezer())
    p = profile(known(1, "Свой", played_days_ago=20))
    calls = {"similarity": 0}

    def similarity(request):
        calls["similarity"] += 1
        body = json.loads(request.content)
        if calls["similarity"] == 1:
            return httpx.Response(200, json={"model": "m", "results": [], "missing": body["candidates"]})
        return httpx.Response(200, json={"model": "m", "missing": [], "results": [
            {"key": k, "pos": 0, "neg": 0, "score": 0.9 if k == "dz:2002" else 0.0, "nearest": [], "nearest_neg": None}
            for k in body["candidates"]]})

    embed = respx_mock.post(f"{EMBEDDER}/embed").respond(json={"queued": 1, "known": 0, "rejected": []})
    respx_mock.post(f"{EMBEDDER}/similarity").mock(side_effect=similarity)
    respx_mock.post(f"{EMBEDDER}/vectors").respond(json={"model": "m", "dim": 1, "vectors": {}, "missing": []})

    built = await b.build(p, "discover", gens(ytm_radio=[tr(2001, "А"), tr(2002, "Б"), tr(2003, "В")]))

    sent = [x for c in embed.calls for x in json.loads(c.request.content)["items"]]
    assert {"key": "dz:2002", "url": "https://preview.test/p.mp3"} in sent
    assert calls["similarity"] == 2 and built.stats["embedder"]["scored"] == 3
    assert built.active[0].track.ref == "dz:2002" and built.active[0].sim == 0.9


async def test_build_survives_the_embedder_down(db, monkeypatch: pytest.MonkeyPatch,
                                                respx_mock: respx.MockRouter) -> None:
    from app.config import get_settings

    monkeypatch.setenv("EMBEDDER_URL", EMBEDDER)
    get_settings.cache_clear()
    respx_mock.post(url__startswith=EMBEDDER).respond(503)

    built = await b.build(profile(known(1)), "normal", gens(ytm_radio=[tr(2001), tr(2002)]))

    assert len(built.active) == 2 and str(built.stats["embedder"]).startswith("down")


@pytest.mark.parametrize("online", [False, True], ids=["off", "down"])
async def test_v2_build_keeps_early_skip_and_album_stash_when_sound_is_missing(db, monkeypatch, respx_mock, online):
    from app.config import get_settings
    from tests.test_taste_consumers import seed_track, vp

    if online:
        monkeypatch.setenv("EMBEDDER_URL", EMBEDDER)
        get_settings.cache_clear()
        respx_mock.post(url__startswith=EMBEDDER).respond(503)
    skipped = seed_track(1, score=-1)
    skipped.last_played = NOW
    album = seed_track(2, score=-3)
    album.album_liked = True
    liked = seed_track(3)
    liked.liked, liked.heard30, liked.last_heard = True, True, NOW - timedelta(days=8)
    p = vp(skipped, album, liked, maturity=.2)
    built = await b.build(p, "normal", gens(ytm_radio=[tr(1, skipped.artist)], stash=b.stash(p),
                                          favorites=b.favorites(p)))
    assert {c.track.ref for c in built.active} == {"dz:1", "dz:2", "dz:3"}
    assert not next(c for c in built.active if c.track.ref == "dz:1").familiar
    assert built.stats["taste_model"] == "v2" and built.stats["maturity"] == .2
    assert all(c.sim is None for c in built.active)
    assert all(call.request.url.path not in ("/similarity", "/vectors") for call in respx_mock.calls)


async def test_name_by_ai(db, monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy import select

    from app.ai.llm import LLMClient, LLMUsage
    from app.config import get_settings
    from app.db import session_scope
    from app.models import AiUsage, User

    monkeypatch.setenv("LLM_API_KEY", "k")
    get_settings.cache_clear()
    asked: dict = {}

    async def call_tool(self, **kw):
        asked.update(kw)
        return {"title": "Пост-панк на пятничное утро", "description": "Кино и соседи."}, LLMUsage(cost_usd=0.0004)

    monkeypatch.setattr(LLMClient, "call_tool", call_tool)
    async with session_scope() as s:
        s.add(User(id=1, nd_username="u", nd_password_enc="x"))
    songs = [cand(1, "Кино", familiar=True), cand(2, "Сплин")]

    title, desc = await b.name_mix(1, songs, b.local_now(NOW), {})

    assert (title, desc) == ("Пост-панк на пятничное утро", "Кино и соседи.")
    assert "пятница" in asked["user_content"] and "утро" in asked["user_content"]
    async with session_scope() as s:
        assert [(u.kind, u.cost_usd) for u in (await s.execute(select(AiUsage))).scalars()] == [("mix", 0.0004)]


async def test_build_counts_why_candidates_were_dropped_and_keeps_score_parts(db) -> None:
    p = profile(*(known(i, f"Свой {i}", played_days_ago=20) for i in range(30)), known(500, "Недавний", played_days_ago=1))
    p.disliked_ids.add("dz:501")
    radio = [tr(1000 + i, f"Новый {i}", rank=1000 * i) for i in range(60)]
    dropped = [tr(500, "Недавний"), tr(501, "Нелюбимый"), tr(502, title="Песня (Live)"), tr(503, title="Песня (Remix)"),
               tr(504, duration=80)]

    built = await b.build(p, "normal", gens(favorites=b.favorites(p), ytm_radio=radio + dropped))

    assert built.stats["rejected"] == {"disliked": 1, "version": 2, "duration": 1, "recent": 1, "negative_score": 0}
    assert built.stats["kept"] == built.stats["candidates"] - 5
    for c in built.active + built.reserve:
        f = c.features()
        assert f["score_parts"]["total"] == c.score and f["score_parts"]["maturity"] is None
        assert (f["profile"] is not None) == c.track.ref.startswith("dz:") and int(c.track.ref[3:]) < 30 or \
            f["profile"] is None
    own = next(c for c in built.active if c.track.ref == "dz:0").features()["profile"]
    assert own == {"key": "dz:0", "s": 2.0, "positive": None, "negative": None, "artist_score": 2.0, "familiar": True}


@pytest.mark.parametrize("model", ["v1", "v2"])
async def test_shadow_replays_live_active_and_reserve_with_adapted_policy(db, monkeypatch, model):
    from app import recommendation_policy, recommendations
    from app.mixes.shadow import compare, profiles_for
    from app.mixes.taste_math import V2Profile
    from app.mixes.taste_snapshot import TasteSnapshot

    cfg = b.get_settings().cfg.mixes
    monkeypatch.setattr(cfg, "size", 2)
    monkeypatch.setattr(cfg, "reserve", 4)
    p = Profile(1, NOW) if model == "v1" else V2Profile(1, NOW, maturity=1)
    # Only the active (online) profile knows this track; do not silently rebuild it from the local snapshot.
    p.disliked_ids.add("dz:9")
    p.artist_listens[artist_key("Muted")] = 6  # the low-ranked candidate is the only new song of a familiar artist
    snapshot = TasteSnapshot(1)
    weights = {"favorites": 1.25, "stash": .6}
    calls = []

    async def adapted(user_id, base):
        calls.append("weights")
        return weights

    async def policy(user_id):
        calls.append("policy")
        return recommendation_policy.Policy({"ref:dz:1"}, {artist_key("Muted")})

    async def sound(profile, candidates, stats):
        calls.append("sound")
        for c in candidates:
            c.sim = {"dz:6": .95, "dz:3": -.5}.get(c.key, 0.0)
        return {c.key: [float(i % 2), float(1 - i % 2)] for i, c in enumerate(candidates)}

    monkeypatch.setattr(recommendations, "adapted_weights", adapted)
    monkeypatch.setattr(recommendation_policy, "load_policy", policy)
    monkeypatch.setattr(b, "sound", sound)
    built = await b.build(p, "discover", gens(favorites=[tr(1), tr(2, "Muted"), tr(3), tr(4), tr(9)],
                                               stash=[tr(n) for n in range(5, 9)]), capture_replay=True)
    report = compare(snapshot, built.replay, profiles_for(snapshot, p, "UTC"), active_model=model,
                     mode="discover", timezone="UTC", cfg=cfg)
    assert calls == ["weights", "policy", "sound"]
    assert report["source_weights"] == weights != cfg.weights and report["pool_complete"]
    replay = report["selection"]["models"][model]
    assert replay["active"] == [c.key for c in built.active]
    assert replay["reserve"] == [c.key for c in built.reserve] and len(replay["reserve"]) == 4
    assert "dz:6" in replay["active"]
    assert {c.novelty for c in built.active} == b.DISCOVERIES
    assert all(c.familiar is False and c.features()["novelty"] == c.novelty for c in built.active)
    for name, summary in report["profiles"].items():
        rows = {r["key"]: r for r in summary["candidate_decisions"]}
        assert rows["dz:1"]["rejection_reason"] == "temporary_pause"
        assert rows["dz:9"]["kept"] == (name != model)
        assert rows["dz:2"]["novelty"] == ("new_track_known_artist" if name == model else "new_artist")
        assert rows["dz:2"]["contributions"]["temporary_multiplier"] == .25
        assert rows["dz:2"]["contributions"]["generator_weights"] == {"favorites": 1.25}
        selection = report["selection"]["models"][name]
        assert "dz:1" not in selection["active"] + selection["reserve"]


@pytest.mark.parametrize("share", [.4, .6])
@pytest.mark.parametrize("known_first", [False, True])
async def test_live_and_shadow_selection_use_the_same_frozen_discover_target(db, monkeypatch, share, known_first):
    from app import recommendations
    from app.mixes.shadow import compare, profiles_for
    from app.mixes.taste_snapshot import TasteSnapshot

    calls = []
    async def target(uid):
        calls.append(uid)
        return share
    monkeypatch.setattr(recommendations, "discovery_share", target)
    cfg = b.get_settings().cfg.mixes
    monkeypatch.setattr(cfg, "size", 20)
    monkeypatch.setattr(cfg, "reserve", 10)
    familiar_ids = range(40) if known_first else range(40, 80)
    p = profile(known(999, played_days_ago=8), listens={f"Артист {i}": 6 for i in familiar_ids})
    sources = gens(ytm_radio=[tr(i) for i in range(80)])
    snapshot = TasteSnapshot(1)
    built = await b.build(p, "discover", sources, capture_replay=True)
    report = compare(snapshot, built.replay, profiles_for(snapshot, p, "UTC"), active_model="v1",
                     mode="discover", timezone="UTC", cfg=cfg)
    assert report["known_artist_share"] == built.stats["known_artist_share"] == share
    replay = report["selection"]["models"]["v1"]
    for attr, size in (("active", 20), ("reserve", 10)):
        tracks = getattr(built, attr)
        # With ample supply raw ranking heavily favours just one kind. An account signal may shift at most
        # two active picks and one reserve pick vs neutral; it must not undo an already-favoured native ranking.
        neutral = size - 1 if known_first else 1
        delta = 0 if (share > .5) == known_first else round(size * .1) * (1 if share > .5 else -1)
        assert sum(c.novelty == b.NEW_TRACK_KNOWN_ARTIST for c in tracks) == neutral + delta
        assert replay[attr] == [c.key for c in tracks]
    normal = await b.build(p, "normal", sources)
    assert all((c.novelty == b.NEW_TRACK_KNOWN_ARTIST) == known_first for c in normal.active)
    assert calls == [p.user_id]  # neither alternate profiles nor non-Discover builds re-read account history
