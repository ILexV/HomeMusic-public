"""Playable exposure is account-scoped, finite and separate from listening history."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.db import session_scope
from app.mixes import build, core, recent_offers, smart, wave
from app.mixes.taste import Profile
from app.models import Mix, MixItem, PlayEvent, SmartItem, SmartSession, User, WaveItem, WaveReoffer, WaveSession
from tests import test_smart, test_wave
from tests.test_mix_build import gens
from tests.test_mixes import add_track, add_user
from tests.test_wave import cand, fake_sources, tr, wave_items

NOW = datetime(2026, 11, 19, 12)
env = test_wave.env


async def test_daily_mix_uses_another_waves_offers_but_keeps_the_pool(env):
    me, other = env["admin"], await add_user("other")
    first, third = cand(1), cand(3)
    async with session_scope() as s:
        s.add(WaveSession(id="old", user_id=me))
        await s.flush()
        await wave.save_items(s, "old", me, [first, third], {}, now=NOW - timedelta(days=7))
        # A new handout (not an unchanged client queue) refreshes the clock, without stacking penalties.
        await wave.save_items(s, "old", me, [third], {}, now=NOW)
    await add_track(3)  # joined the library after its catalog-only offer
    assert await recent_offers.load(me, now=NOW) == {"dz:3": .12, "nd:nd3": .12}
    assert await recent_offers.load(me, now=NOW + timedelta(days=3.5)) == {"dz:3": .06, "nd:nd3": .06}
    assert not await recent_offers.load(me, now=NOW + timedelta(days=7))
    assert not await recent_offers.load(me, now=NOW, exclude_wave="old")

    for uid, order in [(other, [1, 2, 3, 4]), (me, [1, 2, 4, 3])]:
        profile = Profile(user_id=uid, now=NOW)
        ranked = await build.build(profile, "discover", gens(ytm_radio=[tr(i) for i in range(1, 5)]))
        assert [c.track.ref for c in ranked.active] == [f"dz:{i}" for i in order]
        for c in ranked.active:
            expected = recent_offers.PENALTY if uid == me and c.track.ref == "dz:3" else 0
            assert c.features()["score_parts"]["recent_offer_penalty"] == expected
        assert not profile.tracks and not profile.disliked_ids
    fallback = await build.build(Profile(user_id=me, now=NOW), "normal", gens(ytm_radio=[tr(1), tr(3)]))
    assert [c.track.ref for c in fallback.active] == ["dz:1", "dz:3"]


async def test_mix_remembers_only_once_ready_including_late_reserve_promotions(env, monkeypatch):
    me = env["admin"]
    one, two, three, pending = [cand(i) for i in range(1, 5)]
    for i, c in enumerate([one, two, three], start=1):
        c.nd_song_id, c.library_track_id = f"nd{i}", await add_track(i)
    monkeypatch.setattr(core, "utcnow", lambda: NOW)
    async with session_scope() as s:
        mix = await core.save(s, await s.get(User, me), build.Built([one, pending], [two, three], "Mix", "", {}))
        mid = mix.id
    assert await recent_offers.load(me, now=NOW) == {"dz:1": .12, "nd:nd1": .12}

    # A previously available active row loses its local file. Sync promotes an already-ready reserve;
    # neither the remaining reserve nor the queued download is an offer. Navidrome mirroring is independent.
    async with session_scope() as s:
        lost = (await s.execute(select(MixItem).where(MixItem.mix_id == mid, MixItem.position == 0))).scalar_one()
        lost.ref, lost.track_id, lost.nd_song_id = None, None, None
    promoted = NOW + timedelta(days=3)
    monkeypatch.setattr(core, "utcnow", lambda: promoted)
    await core.sync(mid)
    assert await recent_offers.load(me, now=promoted) == pytest.approx(
        {"dz:1": .12 * 4/7, "nd:nd1": .12 * 4/7, "dz:2": .12, "nd:nd2": .12})
    monkeypatch.setattr(core, "utcnow", lambda: NOW + timedelta(days=8))
    await core.sync(mid)
    assert await recent_offers.load(me, now=NOW + timedelta(days=8)) == pytest.approx(
        {"dz:2": .12 * 2/7, "nd:nd2": .12 * 2/7})
    assert not await recent_offers.load(me, now=promoted + timedelta(days=7, seconds=1))


async def test_wave_rotates_close_candidates_between_sessions_but_not_its_own_queue_or_seed(env, monkeypatch):
    monkeypatch.setattr(wave, "utcnow", lambda: NOW)
    fake_sources(monkeypatch, [tr(i) for i in range(5000, 5020)])
    client = env["client"]
    start = {"seed": {"ref": "dz:100"}, "mode": "discover"}
    first = await client.post("/api/wave/start", json=start)
    assert first.status_code == 200, first.text
    sid = first.json()["session_id"]
    refs = [t["ref"] for t in first.json()["tracks"]]
    assert refs == ["dz:100", "dz:5000", "dz:5001", "dz:5002", "dz:5003"]
    refresh = await client.post(f"/api/wave/{sid}/next", json={"queued": refs, "played": []})
    assert [t["ref"] for t in refresh.json()["tracks"]] == refs
    assert all(not row.decision_json["score_parts"]["recent_offer_penalty"] for row in await wave_items(sid))
    assert all(row.handed_count == 1 for row in await wave_items(sid))

    second = await client.post("/api/wave/start", json=start)
    assert second.status_code == 200, second.text
    items = await wave_items(second.json()["session_id"])
    assert [i.ref for i in items] == ["dz:100", "dz:5000", "dz:5001", "dz:5004", "dz:5002"]
    assert items[0].value is None and items[0].decision_json["score_parts"]["recent_offer_penalty"] == 0
    assert items[1].decision_json["score_parts"]["recent_offer_penalty"] == recent_offers.PENALTY
    assert items[3].decision_json["score_parts"]["recent_offer_penalty"] == 0
    async with session_scope() as s:
        events = await s.execute(select(PlayEvent.id).where(PlayEvent.user_id == env["admin"]))
        assert events.first() is None


async def test_smart_reads_both_other_surfaces_and_its_slots_affect_both(env, monkeypatch):
    from app import listen

    monkeypatch.setattr(listen, "prefetch", lambda uid, refs: len(refs))
    me = env["admin"]
    tracks = [tr(i) for i in (5000, 5001, 5002)]
    ids = await test_smart.library(20)
    test_smart.fake_gens(monkeypatch, tracks)
    fake_sources(monkeypatch, tracks)
    monkeypatch.setattr(smart, "utcnow", lambda: NOW)
    monkeypatch.setattr(wave, "utcnow", lambda: NOW)
    async with session_scope() as s:
        mix = Mix(user_id=me, title="old", mode="discover")
        s.add_all([mix, WaveSession(id="prior", user_id=me)])
        await s.flush()
        s.add(MixItem(mix_id=mix.id, ref="dz:5000", nd_song_id="old5000", position=0,
                      generator="favorites", offered_at=NOW - timedelta(days=3.5)))
        await wave.save_items(s, "prior", me, [cand(5001)], {}, now=NOW - timedelta(days=1.75))

    plan = await test_smart.start(env["client"], ids)
    assert set(plan["own"]) == set(ids)
    async with session_scope() as s:
        slots = (await s.scalars(select(SmartItem).where(SmartItem.session_id == plan["session_id"]))).all()
        assert {row.ref: row.decision_json["score_parts"]["recent_offer_penalty"] for row in slots} == \
               pytest.approx({"dz:5000": .06, "dz:5001": .09, "dz:5002": 0})
        for row in slots:
            parts = row.decision_json["score_parts"]
            assert row.value == pytest.approx(row.score + parts["playlist_sound"] - parts["recent_offer_penalty"])
    # A different catalog ref paired to the older Mix's ND id sees the newer Smart timestamp too.
    scores = await recent_offers.load(me, now=NOW)
    assert recent_offers.penalty(scores, "file:new", "old5000") == .12
    assert not any(f"nd:{nd}" in scores for nd in ids)
    ranked = await build.build(Profile(user_id=me, now=NOW), "discover", gens(ytm_radio=tracks))
    assert len(ranked.active) == 3 and all(c.recent_offer_penalty == .12 for c in ranked.active)
    response = await env["client"].post("/api/wave/start", json={"seed": {"ref": "dz:100"}, "mode": "discover"})
    assert response.status_code == 200, response.text
    rows = await wave_items(response.json()["session_id"])
    assert {r.ref for r in rows} == {"dz:100", "dz:5000", "dz:5001", "dz:5002"}
    assert all(r.decision_json["score_parts"]["recent_offer_penalty"] == .12 for r in rows if r.ref != "dz:100")


async def test_report_orders_known_offers_and_discloses_legacy_wave_gaps(env, db_file):
    from app.matching.replay_quality import observed_quality
    from app.mixes.evaluate import readonly_db

    me, other = env["admin"], await add_user("other")

    def ago(days):
        return NOW - timedelta(days=days)

    async with session_scope() as s:
        mix = Mix(user_id=me, title="prior", mode="normal")
        s.add_all([mix, WaveSession(id="w", user_id=me), SmartSession(id="s", user_id=me, context="liked"),
                   WaveSession(id="other", user_id=other)])
        await s.flush()
        s.add(MixItem(mix_id=mix.id, ref="dz:8", nd_song_id="eight", position=0, generator="tags", offered_at=ago(9)))
        # One real handout, stored twice under different aliases. The prior Mix is in lookback only.
        for i, ref in enumerate(("dz:8", "nd:eight")):
            s.add(SmartItem(session_id="s", user_id=me, slot=i, position=i, ref=ref, generator="tags", handed_at=ago(4)))
        # Offered exactly seven days apart: the new slot enters the denominator, but is not a repeat.
        s.add_all([MixItem(mix_id=mix.id, position=1, ref="dz:9", generator="tags", offered_at=ago(8)),
                   SmartItem(session_id="s", user_id=me, slot=2, position=2, ref="dz:9",
                             generator="tags", handed_at=ago(1))])
        for days in (8, 3, 2, 1, 1):  # an identical handout timestamp is idempotent
            await wave.save_items(s, "w", me, [cand(1)], {}, now=ago(days))
        await wave.save_items(s, "other", other, [cand(8)], {}, now=ago(2))
        legacy = (await s.scalars(select(WaveItem).where(WaveItem.session_id == "w"))).one()
        legacy.handed_count += 3  # pre-migration repeats, no timestamps
        await s.flush()
        snapshot = await recent_offers.report(s, me, days=7, now=NOW)
        assert snapshot["by_surface"] == {"mix": 0, "wave": 3, "smart": 2}
        assert (snapshot["offers"], snapshot["repeats"], snapshot["per_50"], snapshot["wave_undated_handouts"]) == (5, 4, 40, 3)
        assert snapshot["lookback_days"] == 7 and any("без точной даты: 3" in note for note in snapshot["limitations"])
        earlier = await recent_offers.report(s, me, days=7, now=ago(2.5))
        assert (earlier["offers"], earlier["repeats"]) == (5, 2)
        assert len((await s.scalars(select(WaveReoffer))).all()) == 2
        await s.commit()
        with readonly_db(db_file) as conn:
            assert observed_quality(conn, user_id=me, days=7, until=NOW)["offer_repeats"] == snapshot
        await s.delete(await s.get(WaveSession, "w"))
        await s.flush()
        assert not (await s.scalars(select(WaveReoffer))).all()
        empty = await recent_offers.report(s, other, days=7, now=ago(3))
        assert empty["offers"] == empty["repeats"] == 0 and empty["per_50"] is None
