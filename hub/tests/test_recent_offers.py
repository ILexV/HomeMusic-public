"""Playable exposure is account-scoped, finite and separate from listening history."""
from datetime import datetime, timedelta

from sqlalchemy import select

from app.db import session_scope
from app.mixes import build, core, recent_offers, wave
from app.mixes.taste import Profile
from app.models import MixItem, PlayEvent, User, WaveSession
from tests import test_wave
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
        # Coming back in another queue refresh does not add a second deduction or restart the seven days.
        await wave.save_items(s, "old", me, [third], {}, now=NOW)
    await add_track(3)  # joined the library after its catalog-only offer
    assert await recent_offers.load(me, now=NOW) == {"dz:1", "dz:3", "nd:nd3"}
    assert not await recent_offers.load(me, now=NOW + timedelta(microseconds=1))
    assert not await recent_offers.load(me, now=NOW, exclude_session="old")

    for uid, order in [(other, [1, 2, 3, 4]), (me, [1, 2, 4, 3])]:
        profile = Profile(user_id=uid, now=NOW)
        ranked = await build.build(profile, "discover", gens(ytm_radio=[tr(i) for i in range(1, 5)]))
        assert [c.track.ref for c in ranked.active] == [f"dz:{i}" for i in order]
        for c in ranked.active:
            expected = recent_offers.PENALTY if uid == me and c.track.ref in ("dz:1", "dz:3") else 0
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
    assert await recent_offers.load(me, now=NOW) == {"dz:1", "nd:nd1"}

    # A previously available active row loses its local file. Sync promotes an already-ready reserve;
    # neither the remaining reserve nor the queued download is an offer. Navidrome mirroring is independent.
    async with session_scope() as s:
        lost = (await s.execute(select(MixItem).where(MixItem.mix_id == mid, MixItem.position == 0))).scalar_one()
        lost.ref, lost.track_id, lost.nd_song_id = None, None, None
    promoted = NOW + timedelta(days=3)
    monkeypatch.setattr(core, "utcnow", lambda: promoted)
    await core.sync(mid)
    assert await recent_offers.load(me, now=promoted) == {"dz:1", "nd:nd1", "dz:2", "nd:nd2"}
    monkeypatch.setattr(core, "utcnow", lambda: NOW + timedelta(days=8))
    await core.sync(mid)
    assert await recent_offers.load(me, now=NOW + timedelta(days=8)) == {"dz:2", "nd:nd2"}
    assert not await recent_offers.load(me, now=promoted + timedelta(days=7, seconds=1))


async def test_wave_rotates_close_candidates_between_sessions_but_not_its_own_queue_or_seed(env, monkeypatch):
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
