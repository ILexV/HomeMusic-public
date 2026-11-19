"""V2 consumer math and geometry operations: pure state/HTTP mocks, no external services."""
import json
from collections import Counter
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import get_settings
from app.mixes import build, embedder, taste_select, wave
from app.mixes.taste import Profile, TasteTrack, artist_key
from app.mixes.taste_math import V2Profile, saturation
from app.models import PlayEvent
from tests.test_embedder_geometry import DIAGNOSTICS, SPACE, profile_body
from tests.test_mix_build import NOW, cand, profile, tr


@pytest.fixture(autouse=True)
def fresh_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def vp(*tracks, maturity=1):
    base = profile(*tracks)
    return V2Profile(**{k: getattr(base, k) for k in Profile.__dataclass_fields__},
                     timezone="Europe/Moscow", maturity=maturity)


def signal(eid, kind, ref="dz:1", position=None, **kw):
    return PlayEvent(user_id=kw.pop("user_id", 1), event_id=eid, ts=NOW, received_at=NOW,
                     kind=kind, ref=ref, position_s=position, duration_s=200, client="pwa", target_type="track", **kw)


def seed_track(i, artist=None, score=2, age=1):
    track = tr(i, artist or f"A{i}")
    return TasteTrack(track.ref, ref=track.ref, artist=track.artist, title=track.title, score=score,
                      last_positive=NOW, positive_age=age, track=track)


def test_early_skip_is_penalty_not_filter_rest_or_familiarity_and_penalties_are_bounded():
    t = seed_track(1, score=-100)
    t.last_played = NOW
    p = vp(t, maturity=.25)
    p.artists[artist_key(t.artist)] = -100
    c = cand(1, t.artist)
    c.sim = .8
    assert build.keep(c, p, get_settings().cfg.mixes) and not c.familiar
    assert build.score(c, {"ytm_radio": 1}, p) == pytest.approx(1 + 1.5 * .25 * .8 - 2 - .9)
    p.explicit_negative_keys.add(t.key)
    assert not build.keep(c, p, get_settings().cfg.mixes)


def test_v2_calendar_rest_uses_last_heard_not_last_start_or_skip():
    t = seed_track(1)
    t.liked = t.heard30 = True
    t.last_played, t.last_heard = NOW, NOW - timedelta(days=8)
    p = vp(t)
    assert [x.ref for x in build.favorites(p)] == [t.ref]
    assert build.keep(cand(1), p, get_settings().cfg.mixes)
    t.last_heard = NOW - timedelta(days=2)
    assert not build.keep(cand(1), p, get_settings().cfg.mixes)
    assert not build.favorites(p)
    t.last_heard = NOW - timedelta(days=61)
    t.score, t.album_liked, t.liked = -1, True, False
    assert [x.ref for x in build.stash(p)] == [t.ref]
    t.last_heard = NOW - timedelta(days=59)
    assert not build.stash(p)


def test_stash_order_uses_profile_local_day_across_utc_midnight():
    tracks = [seed_track(i) for i in range(8)]
    for t in tracks:
        t.downloaded = True
    p = vp(*tracks)
    first = [t.ref for t in build.stash(p)]
    p.now -= timedelta(hours=3)  # UTC date changed; Moscow local date is still the same.
    assert [t.ref for t in build.stash(p)] == first


def test_facet_seeds_are_daily_deterministic_cover_minority_and_limit_artists():
    tracks = [seed_track(i, "Major" if i < 4 else None, score=20 - i) for i in range(16)]
    p = vp(*tracks)
    data = profile_body()
    data["facets"] = [{"id": "major", "weight": .8, "members": [t.key for t in tracks[:12]], "center": [1, 0]},
                      {"id": "minor", "weight": .2, "members": [t.key for t in tracks[12:]], "center": [0, 1]}]
    p.geometry = embedder.GeometryProfile(**data)
    selected = p.seeds(8)
    assert [t.key for t in selected] == [t.key for t in p.seeds(8)]
    assert len(selected) == 8 and any(t.key in {t.key for t in tracks[12:]} for t in selected)
    assert max(Counter(t.artist for t in selected).values()) <= 2
    p.tracks = dict(reversed(list(p.tracks.items())))
    assert [t.key for t in selected] == [t.key for t in p.seeds(8)]
    assert artist_key(tracks[12].artist) in p.top_artists(2)
    p.now += timedelta(days=1)
    assert [t.key for t in selected] != [t.key for t in p.seeds(8)]


def test_fallback_is_blocked_by_recent_positive_even_below_score_or_missing_names():
    weak = seed_track(1, score=.2)
    weak.artist = weak.title = ""
    saved = seed_track(2, score=0)
    saved.downloaded, saved.last_positive, saved.positive_age = True, None, None
    p = vp(weak, saved)
    assert p.has_recent_positive and not p.seeds(8)
    weak.positive_age = 31
    assert [t.key for t in p.seeds(8)] == [saved.key]
    saved.score = -3
    assert [t.key for t in p.seeds(8)] == [saved.key]


async def test_cold_start_reserves_onboarding_and_redistributes_empty_slots(monkeypatch):
    own = [seed_track(i) for i in range(8)]
    cold = [seed_track(100 + i) for i in range(8)]
    p = vp(*own, maturity=.5)
    p.picked_artists = [{"ref": "dz:artist", "name": "Cold"}]

    async def cold_seeds(p, n):
        return cold[:n]
    monkeypatch.setattr(build, "cold_seeds", cold_seeds)
    result = await taste_select.seeds(p, 8)
    assert len(result) == 8 and len([t for t in result if t.key.startswith("dz:10")]) == 4
    p.picked_artists = []
    assert len(await taste_select.seeds(p, 8)) == 8
    p = vp(maturity=0)
    p.picked_artists = [{"ref": "dz:artist", "name": "Cold"}]
    assert len(await taste_select.seeds(p, 8)) == 8


async def test_saved_album_and_own_artist_can_seed_without_fabricated_track_scores(monkeypatch):
    from app.favorites import fold

    album = seed_track(1, score=0)
    album.album_liked, album.last_positive, album.positive_age = True, None, None
    p = vp(album, maturity=0)
    assert [t.key for t in p.seeds(8)] == [album.key]
    p = vp(maturity=0)
    own = signal("artist", "like", ref="dz:artist", target_data={"name": "Own artist"})
    own.target_type = "artist"
    p.favorite_states = fold([own])

    async def cold(p, n):
        assert p.picked_artists == [{"ref": "dz:artist", "name": "Own artist"}]
        return [seed_track(2)]
    monkeypatch.setattr(build, "cold_seeds", cold)
    assert [t.key for t in await taste_select.seeds(p, 8)] == ["dz:2"]
    assert not p.tracks and not p.picked_artists


@pytest.mark.parametrize("fail_twice", [False, True], ids=["refresh", "bounded_fallback"])
async def test_stale_space_discards_partial_similarity_and_all_old_vectors(monkeypatch, respx_mock, fail_twice):
    monkeypatch.setenv("EMBEDDER_URL", "http://embedder.test")
    get_settings.cache_clear()
    p, c = vp(seed_track(1)), cand(2)
    profiles = respx_mock.post("http://embedder.test/v2/profile").mock(side_effect=[
        httpx.Response(200, json={**profile_body(), "space_id": "old"}),
        httpx.Response(200, json={**profile_body(), "space_id": "new"})])

    def similarity(request):
        space = json.loads(request.content)["space_id"]
        return httpx.Response(200, json={**SPACE, "space_id": space, "results": [
            {"key": c.key, "pos": .9 if space == "old" else .2, "neg": 0, "score": .9 if space == "old" else .2}],
            "missing": [], "diagnostics": DIAGNOSTICS})
    respx_mock.post("http://embedder.test/v2/similarity").mock(side_effect=similarity)
    response = httpx.Response(409, json={"detail": {"code": "stale_space", "space_id": "new"}})
    vectors = respx_mock.post("http://embedder.test/v2/vectors").mock(side_effect=[response,
        response if fail_twice else httpx.Response(200, json={**SPACE, "space_id": "new", "vectors": {c.key: [0, 1]},
                                                            "missing": [], "diagnostics": DIAGNOSTICS})])
    result = await taste_select.geometry_scores(p, [c])
    assert profiles.call_count == vectors.call_count == 2
    assert result == ({} if fail_twice else {c.key: [0, 1]})
    assert c.sim == (None if fail_twice else .2)
    assert (p.geometry.space_id if p.geometry else None) == (None if fail_twice else "new")


@pytest.mark.parametrize("consumer", ["mix", "wave"])
async def test_uncached_catalog_dislike_demotes_similar_tracks_outside_the_library(monkeypatch, respx_mock, consumer):
    base = "http://embedder.test"
    monkeypatch.setenv("EMBEDDER_URL", base)
    monkeypatch.setenv("MIX_POLL_S", "0")
    monkeypatch.setenv("MIX_EMBED_WAIT_S", "5")
    preview = "https://preview.example/disliked.mp3"
    monkeypatch.setattr(build.svc, "deezer", SimpleNamespace(preview_url=AsyncMock(return_value=preview)))
    monkeypatch.setattr(build, "library_items", AsyncMock(return_value=[]))
    p, similar, unrelated = vp(), cand(2), cand(3)
    p.explicit_negative_keys.add("dz:1")
    queued = respx_mock.post(base + "/embed", json={"items": [{"key": "dz:1", "url": preview}]})
    queued.respond(json={"queued": 1})
    missing = {**profile_body(), "facets": [], "negatives": [], "missing": ["dz:1"]}
    ready = {**missing, "negatives": ["dz:1"], "missing": []}
    # Candidates already have scores while the disliked preview is still being analyzed.
    respx_mock.post(base + "/v2/profile").mock(side_effect=[
        httpx.Response(200, json=row) for row in (missing, missing, ready)])

    def similarity(request):
        active = "dz:1" in json.loads(request.content)["negatives"]
        scores = [{"key": c.key, "pos": 0, "neg": neg, "score": -.5 * neg}
                  for c, neg in [(similar, int(active)), (unrelated, 0)]]
        return httpx.Response(200, json={**SPACE, "missing": [], "results": scores, "diagnostics": DIAGNOSTICS})

    respx_mock.post(base + "/v2/similarity").mock(side_effect=similarity)
    respx_mock.post(base + "/v2/vectors").respond(json={**SPACE, "missing": [], "diagnostics": DIAGNOSTICS,
                                                        "vectors": {similar.key: [1, 0], unrelated.key: [0, 1]}})
    if consumer == "mix":
        await build.sound(p, [similar, unrelated], {})
    else:
        rt = wave.Runtime(1, "normal", wave.State(taste_model="v2"), profile=p)
        rt.state.pool.update((c.key, c) for c in (similar, unrelated))
        for _ in range(2):
            await wave._vectors(rt)
            assert similar.score == unrelated.score  # keep handing out music until analysis completes
        await wave._vectors(rt)
        assert similar.score < unrelated.score
    assert queued.call_count == 1 and build.score(similar, {}, p) < build.score(unrelated, {}, p)


async def test_old_embedder_or_down_drops_sound_without_raw_fallback(monkeypatch, respx_mock):
    monkeypatch.setenv("EMBEDDER_URL", "http://embedder.test")
    get_settings.cache_clear()
    route = respx_mock.post("http://embedder.test/v2/profile").respond(404)
    c = cand(1)
    c.sim = 1
    p = vp(seed_track(1))
    assert await taste_select.geometry_scores(p, [c]) == {}
    assert c.sim is None and p.geometry is None and route.call_count == 1


async def test_wave_rebuilds_session_coordinates_and_drops_cached_raw_vectors(monkeypatch, respx_mock):
    monkeypatch.setenv("EMBEDDER_URL", "http://embedder.test")
    get_settings.cache_clear()
    p, candidate = vp(seed_track(1)), cand(2)
    rt = wave.Runtime(1, "normal", wave.State(taste_model="v2", pos=["dz:1"], positive_weights={"dz:1": 2}),
                      profile=p, vectors={"old-only": [1, 0], candidate.key: [1, 0]})
    rt.state.pool[candidate.track.ref] = candidate
    respx_mock.post("http://embedder.test/v2/profile").respond(json={**profile_body(), "space_id": "changed"})
    respx_mock.post("http://embedder.test/v2/similarity").respond(json={**SPACE, "space_id": "changed",
        "results": [{"key": candidate.key, "pos": .5, "neg": 0, "score": .5}], "missing": [],
        "diagnostics": DIAGNOSTICS})
    respx_mock.post("http://embedder.test/v2/vectors").respond(json={**SPACE, "space_id": "changed",
        "vectors": {candidate.key: [0, 1], "dz:1": [0, 1]}, "missing": [], "diagnostics": DIAGNOSTICS})
    vectors = await wave._vectors(rt)
    assert "old-only" not in vectors and vectors[candidate.key] == [0, 1]
    assert rt.state.space == {"model": "fake", "space_id": "changed", "dim": 2}
    assert wave._weighted_center(rt.state.positive_weights, vectors) == [0, 1]


async def test_v2_session_weights_like_unlike_late_skip_and_restart_idempotence(monkeypatch):
    async def no_io(*a, **kw):
        return None
    monkeypatch.setattr(wave, "_download", no_io)
    monkeypatch.setattr(wave, "_gather", no_io)
    rt = wave.Runtime(1, "normal", wave.State(taste_model="v2"), profile=vp())
    rt.state.out["dz:1"] = cand(1)
    rows = [signal("finish", "finish"), signal("like", "like"), signal("like-again", "like")]
    # Stable distinct timestamps model actual ordering, independent of DB ids.
    for i, e in enumerate(rows):
        e.ts = NOW + timedelta(seconds=i)
    await wave.learn("s", rt, rows)
    assert rt.state.positive_weights["dz:1"] == pytest.approx(saturation(3))
    rt.state = wave.State.load(rt.state.dump())
    before = rt.state.dump()
    await wave.learn("s", rt, rows)
    assert rt.state.dump() == before
    unlike = signal("unlike", "unlike")
    unlike.ts = NOW + timedelta(seconds=3)
    await wave.learn("s", rt, [unlike])
    assert rt.state.positive_weights == {"dz:1": 2}
    late = signal("late", "skip", position=115)
    late.ts = NOW + timedelta(seconds=4)
    await wave.learn("s", rt, [late])
    assert rt.state.positive_weights["dz:1"] == pytest.approx(saturation(2.4))
    assert not rt.state.neg and not rt.state.skips


async def test_v2_compensation_chains_and_other_users_do_not_add_session_weight(monkeypatch):
    async def no_io(*a, **kw):
        return None
    monkeypatch.setattr(wave, "_gather", no_io)
    rt = wave.Runtime(1, "normal", wave.State(taste_model="v2"), profile=vp())
    rt.state.out["dz:1"] = cand(1)
    original = signal("A", "like")
    cancel = signal("C", "unlike", reverts_event_id="A")
    restore = signal("R", "like", reverts_event_id="C")
    original.ts, cancel.ts, restore.ts = (NOW + timedelta(seconds=i) for i in range(3))
    await wave.learn("s", rt, [cancel, original, signal("other", "like", user_id=2)])
    assert not rt.state.positive_weights
    await wave.learn("s", rt, [restore])
    assert rt.state.positive_weights == {"dz:1": 1} and "other" not in rt.state.seen


async def test_external_unlike_removes_session_like_only_preserving_finish(monkeypatch):
    from app.favorites import fold

    async def no_io(*a, **kw):
        return None
    monkeypatch.setattr(wave, "_download", no_io)
    monkeypatch.setattr(wave, "_gather", no_io)
    p = vp(seed_track(1))
    p.favorite_states = fold([signal("external-unlike", "unlike")])
    rt = wave.Runtime(1, "normal", wave.State(taste_model="v2"), profile=p)
    rt.state.out["dz:1"] = cand(1)
    await wave.learn("s", rt, [signal("finish", "finish"), signal("like", "like")])
    assert rt.state.positive_weights == {"dz:1": 2} and not rt.state.neg


async def test_future_session_signal_is_not_consumed_before_its_time(monkeypatch):
    async def no_io(*a, **kw):
        return None
    now = [NOW]
    monkeypatch.setattr(wave, "utcnow", lambda: now[0])
    monkeypatch.setattr(wave, "_download", no_io)
    monkeypatch.setattr(wave, "_gather", no_io)
    rt = wave.Runtime(1, "normal", wave.State(taste_model="v2"), profile=vp())
    rt.state.out["dz:1"] = cand(1)
    e = signal("future", "finish")
    e.ts = NOW + timedelta(minutes=30)
    await wave.learn("s", rt, [e])
    assert not rt.state.seen and not rt.state.positive_weights
    now[0] += timedelta(minutes=30)
    await wave.learn("s", rt, [e])
    assert rt.state.positive_weights == {"dz:1": 2} and "future" in rt.state.seen


def test_nearest_session_negative_preserves_unrelated_cluster_and_m4_weighted_center():
    st = wave.State(taste_model="v2", neg=["n1", "n2"], positive_weights={"p1": 2, "p2": 1})
    vectors = {"n1": [1, 0], "n2": [-1, 0], "p1": [0, 1], "p2": [1, 0]}
    c = cand(1)
    vectors[c.key] = [1, 0]
    assert wave.value(c, st, vectors, None, None) == pytest.approx(c.score - .5)
    vectors[c.key] = [0, -1]
    assert wave.value(c, st, vectors, None, None) == c.score
    assert wave._weighted_center(st.positive_weights, vectors) == pytest.approx([1 / 5 ** .5, 2 / 5 ** .5])


def test_unversioned_session_is_always_v1_even_after_config_change():
    st = wave.State.load({"pos": ["old-raw"], "taste_model": "v2"})
    assert st.taste_model == "v1" and st.version == 1 and st.pos == ["old-raw"]


def score_case(model, *, s=None, artist_score=None, sim=None, rank=None):
    t = seed_track(1, "A1", score=s) if s is not None else None
    p = vp(*([t] if t else []), maturity=.4) if model == "v2" else profile(*([t] if t else []))
    if artist_score is not None:
        p.artists[artist_key("A1")] = artist_score
    c = cand(1, "A1")
    c.gens, c.sim = {"ytm_radio": 0, "lastfm_similar": 3}, sim
    c.track.rank = rank
    return c, p


@pytest.mark.parametrize("model,kw", [
    ("v1", dict(sim=.6, rank=10_000)), ("v1", dict()),
    ("v2", dict(s=-3, artist_score=-5, sim=.7, rank=500)), ("v2", dict(s=-9, artist_score=-1)),
    ("v2", dict(s=2, artist_score=4, sim=-.2)), ("v2", dict()),
], ids=["v1_sound", "v1_no_sim", "v2_negative_track_and_artist", "v2_no_sim", "v2_positive", "v2_unknown"])
def test_score_parts_add_up_to_the_same_score(model, kw):
    c, p = score_case(model, **kw)
    weights = get_settings().cfg.mixes.weights

    parts = build.score_parts(c, weights, p)

    assert parts["total"] == build.score(c, weights, p)
    assert parts["total"] == pytest.approx(sum(parts[k] for k in ("generators", "sound", "popularity", "track_penalty",
                                                                  "artist_penalty")))
    if model == "v1":
        assert parts["maturity"] is None and parts["sound"] == pytest.approx(1.5 * (kw.get("sim") or 0))
    else:
        assert parts["maturity"] == .4 and parts["sound"] == pytest.approx(1.5 * .4 * (kw.get("sim") or 0))
        assert parts["track_penalty"] == pytest.approx(.5 * max(kw["s"], -4) if kw.get("s", 0) < 0 else 0)
        assert parts["artist_penalty"] == pytest.approx(.3 * max(kw["artist_score"], -3)
                                                        if kw.get("artist_score", 0) < 0 else 0)


def test_the_known_track_snapshot_names_what_the_profile_knew():
    c, p = score_case("v2", s=-3, artist_score=-5)
    t = p.tracks["dz:1"]
    t.positive_raw, t.negative = 1.5, 4.5

    assert build.known_of(c, p) == {"key": "dz:1", "s": -3, "positive": 1.5, "negative": 4.5, "artist_score": -5,
                                    "familiar": False}
    assert build.known_of(cand(2, "A2"), p) is None
