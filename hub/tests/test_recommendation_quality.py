"""Recommendation quality: real attempts, conservative learning, frozen explanations."""
from datetime import datetime, timedelta
from itertools import count

import httpx
import pytest

from app import recommendations as rec
from app.models import PlayEvent

NOW = datetime(2026, 11, 12, 12)
IDS = count(1)


def event(kind, *, attempt="a", ref="dz:1", at=None, user=1, context="mix:1", **kw):
    n = next(IDS)
    return PlayEvent(id=n, user_id=user, event_id=f"e{n}", ts=at or NOW - timedelta(hours=1),
                     kind=kind, ref=ref, client="pwa", target_type="track", context=context,
                     received_at=kw.pop("received_at", at or NOW - timedelta(hours=1)),
                     playback_id=attempt, device_id="dev" if attempt else None,
                     superseded_scrobble=False, **kw)


def decision(ref="dz:1", *, user=1, context="mix:1", generator="ytm_radio", familiar=False):
    return rec.Decision(user, context, ref, None, NOW - timedelta(days=2), generator, familiar)


def report(events, decisions=None, days=7):
    return rec.summarize(1, events, decisions if decisions is not None else [decision()], days=days, now=NOW)


def test_denominators_exclude_errors_interruptions_and_unfinished_starts():
    rows = [event("start"), event("finish", listened_ms=180000),
            event("start", attempt="b"), event("skip", attempt="b", listened_ms=10000, position_s=100),
            event("start", attempt="c"), event("error", attempt="c"),
            event("skip", attempt="c", listened_ms=0),
            event("start", attempt="d"), event("interrupted", attempt="d"),
            event("start", attempt="e"), event("progress", attempt="e", listened_ms=40000),
            event("like", at=NOW - timedelta(minutes=30))]
    out = report(rows)
    assert (out["attempts"], out["starts"], out["eligible_plays"]) == (5, 5, 2)
    assert (out["completed"], out["early_skips"], out["playback_errors"], out["interrupted"]) == (1, 1, 1, 1)
    assert out["completion_rate"] == out["early_skip_rate"] == out["repeat_rate"] == .5
    assert (out["new_track_plays"], out["new_track_likes"], out["new_track_like_rate"]) == (2, 1, .5)
    assert out["sources"][0]["learning_samples"] == 2


def test_duplicate_and_legacy_events_do_not_create_extra_attempts_or_error_skips():
    at = NOW - timedelta(hours=2)
    start = event("start", attempt=None, at=at)
    finish = event("finish", attempt=None, at=at + timedelta(seconds=180), position_s=180)
    resent = event("finish", attempt=None, at=finish.ts, position_s=180)
    rows = [start, start, finish, finish, resent,
            event("start", attempt=None, at=at + timedelta(minutes=10)),
            event("error", attempt=None, at=at + timedelta(minutes=11)),
            event("skip", attempt=None, at=at + timedelta(minutes=11, seconds=1), position_s=0)]
    out = report(rows)
    assert (out["attempts"], out["eligible_plays"], out["playback_errors"], out["early_skips"]) == (2, 1, 1, 0)
    assert any("playback_id" in note for note in out["limitations"])


def test_isolation_dates_aliases_and_verified_provenance():
    rows = [event("start", ref="dz:1", nd_song_id="lib"),
            event("finish", ref=None, nd_song_id="lib"),
            event("finish", user=2, attempt="other"),
            event("finish", attempt="future", at=NOW + timedelta(seconds=1)),
            event("finish", attempt="old", at=NOW - timedelta(days=8)),
            event("finish", attempt="unknown", context="wave:missing"),
            event("finish", attempt="manual", context="album"),
            event("like", ref="dz:offered")]
    out = report(rows, [decision(), decision(user=2, context="wave:missing"), decision("dz:offered")])
    assert (out["attempts"], out["eligible_plays"], out["completed"]) == (2, 2, 2)
    assert out["sources"][0]["learning_samples"] == 1
    assert out["new_track_plays"] == 1 and out["new_track_likes"] == 0
    assert any("Без подтверждённого" in note for note in out["limitations"])


@pytest.mark.parametrize("kind,extra", [
    ("like", {"context": "favorite:bootstrap"}),
    ("like", {"target_data": {"provenance": "import"}}),
    ("like", {"reverts_event_id": "old"}),
    ("unlike", {}),
])
def test_non_user_likes_do_not_reward_recommendations(kind, extra):
    out = report([event("finish"), event(kind, at=NOW, **extra)])
    assert out["new_track_likes"] == 0


def test_sparse_learning_is_neutral_and_factors_are_bounded():
    for n in range(20):
        assert rec.learned_factor(n, n, 0) == rec.learned_factor(n, 0, n) == 1
    assert 1 < rec.learned_factor(20, 20, 0) < 1.1
    assert .9 < rec.learned_factor(20, 0, 20) < 1
    assert .75 <= rec.learned_factor(1000000, 0, 1000000) < 1
    assert 1 < rec.learned_factor(1000000, 1000000, 0) <= 1.25
    assert rec.learned_factor(100, 50, 50) == 1
    out = report([])
    assert out["attempts"] == 0 and out["completion_rate"] is None and out["new_track_like_rate"] is None


def test_shares_only_use_recorded_positive_contributions_and_conserve_each_outcome():
    from dataclasses import replace

    raw = {"radio": 6, "tags": 3., "charts": 1., "zero": 0, "negative": -2, "invalid": "9",
           "bool": True, "inf": float("inf"), "nan": float("nan")}
    snapshot = {"generators": {"unrelated": 0}, "score_parts": {
        "generator_contributions": raw, "generator_weights": {"unrelated": 100}}}
    d = replace(decision(generator="radio"), decision_json=snapshot)
    assert rec.source_shares(d) == pytest.approx({"radio": .6, "tags": .3, "charts": .1})
    no_parts = replace(d, decision_json={"generators": {"unrelated": 0}})
    assert rec.source_shares(no_parts) == {"radio": 1.}
    assert rec.source_shares(replace(no_parts, generator="")) == {}
    assert rec.source_shares(replace(d, decision_json={"score_parts": {
        "generator_contributions": {"a": 1e308, "b": 1e308}}})) == {"a": .5, "b": .5}

    rows = [event("finish"), event("like", at=NOW), event("skip", attempt="b", listened_ms=5000),
            event("error", attempt="c"), event("interrupted", attempt="d"), event("start", attempt="unfinished")]
    out = report(rows, [d])
    for source in out["sources"]:
        share = rec.source_shares(d)[source["id"]]
        for metric in ("attempts", "eligible_plays", "early_skips", "completed", "playback_errors", "new_track_likes"):
            assert source["effective_" + metric] == pytest.approx(share * out[metric])
        assert source["fallback_attempts"] == 0
        assert source["attempts"] == (out["attempts"] if source["id"] == "radio" else 0)
    assert (out["attempts"], out["eligible_plays"], out["completed"], out["early_skips"],
            out["playback_errors"], out["new_track_likes"]) == (5, 2, 1, 1, 1, 1)


def test_mixed_track_attempt_id_cannot_assign_another_tracks_outcome():
    events = [event("start"), event("finish", ref="dz:changed")]
    out = report(events, [decision(), decision("dz:changed", generator="other")])
    assert out["eligible_plays"] == 1
    assert out["sources"] == [] and out["new_track_plays"] == 0


def test_threshold_uses_effective_outcomes_and_winner_only_comparison_is_observed():
    from dataclasses import replace

    d = replace(decision(generator="radio"), decision_json={"score_parts": {
        "generator_contributions": {"radio": 1, "tags": 1}}})
    rows = [event("finish", attempt=f"play{i}") for i in range(40)]
    before = {s["id"]: s for s in report(rows[:-1], [d])["sources"]}
    assert before["radio"]["winner_only_multiplier"] > 1
    for source in before.values():
        assert source["effective_learning_samples"] == 19.5 and source["multiplier"] == 1
    at = {s["id"]: s for s in report(rows, [d])["sources"]}
    for source in at.values():
        assert source["multiplier"] == pytest.approx(1 + .25 * 20 / 60)
        assert source["effective_learning_samples"] == 20
    assert at["tags"]["winner_only_multiplier"] == 1 and at["tags"]["learning_samples"] == 0


def test_explanation_uses_actual_weights_and_known_sound_only():
    from app.mixes import build
    from tests.test_mix_build import tr

    c = build.Cand(tr(1), gens={"favorites": 0, "ytm_radio": 0}, sim=None)
    build.apply_score(c, {"favorites": .2, "ytm_radio": 1.1}, None)
    c.explanation = rec.explain_candidate(c)
    saved = c.features()
    assert c.generator == "ytm_radio"
    assert saved["score_parts"]["generator_weights"] == {"favorites": .2, "ytm_radio": 1.1}
    original_reasons = tuple(c.explanation)
    c.known = {"familiar": True}
    c.sim = .7
    build.apply_score(c, {"favorites": 1, "ytm_radio": .1}, None)
    assert c.generator == "favorites"
    c.explanation[:] = rec.explain_candidate(c)
    assert tuple(rec.stored_reasons(saved)) == original_reasons
    assert rec.stored_reasons({"score_parts": c.parts}) == []  # old decisions aren't reinterpreted


@pytest.fixture
async def quality_db(hub_env, db_file):
    from app.db import dispose, init_engine, session_scope
    from app.models import Session, User
    from app.security import new_token

    init_engine(db_file)
    tokens, users = [], []
    async with session_scope() as s:
        for name in ("first", "second"):
            user = User(nd_username=name, nd_password_enc="x", is_admin=False, last_seen_at=datetime(2100, 1, 1))
            s.add(user)
            await s.flush()
            token, digest = new_token()
            s.add(Session(id=digest, user_id=user.id, expires_at=datetime(2100, 1, 1)))
            users.append(user.id)
            tokens.append(token)
    yield users, tokens
    await dispose()


async def test_api_owned_frozen_decisions_and_learning_isolation(quality_db, monkeypatch):
    from app.db import session_scope
    from app.main import create_app
    from app.models import Mix, MixItem, SmartItem, SmartSession, WaveItem, WaveSession

    users, tokens = quality_db
    monkeypatch.setattr(rec, "utcnow", lambda: NOW)
    async with session_scope() as s:
        s.add(Mix(id=1, user_id=users[0], created_at=NOW - timedelta(days=2), title="mix", mode="normal"))
        s.add(WaveSession(id="w", user_id=users[0], created_at=NOW - timedelta(days=2)))
        s.add(SmartSession(id="s", user_id=users[0], context="liked", created_at=NOW - timedelta(days=2)))
        await s.flush()
        s.add(MixItem(mix_id=1, position=0, ref="dz:1", generator="ytm_radio", familiar=False,
                      features_json={"explanation": ["Сохранённое решение"]}))
        s.add(MixItem(mix_id=1, position=1, ref="dz:old", generator="favorites", features_json={}))
        s.add(MixItem(mix_id=1, position=2, nd_song_id="library", generator="favorites",
                      features_json={"explanation": ["Сохранённый выбор из библиотеки"]}))
        s.add(WaveItem(session_id="w", user_id=users[0], position=0, ref="dz:2", key="dz:2",
                       generator="ytm_radio", decision_json={"explanation": ["Решение волны"]}))
        s.add(SmartItem(session_id="s", user_id=users[0], position=0, slot=0, ref="dz:3",
                        generator="ytm_radio", decision_json={"explanation": ["Решение списка"]}))
        s.add_all([event("finish", attempt=f"p{i}", user=users[0],
                         at=NOW - timedelta(hours=3) + timedelta(minutes=i)) for i in range(25)])
        s.add_all([event("skip", attempt=f"q{i}", user=users[1], listened_ms=0) for i in range(30)])
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://hub.test", cookies={"hm_session": tokens[0]}) as c:
        out = await c.get("/api/recommendations/quality", params={"days": 7})
        assert out.status_code == 200, out.text
        data = out.json()
        assert data["eligible_plays"] == 25
        source = next(x for x in data["sources"] if x["id"] == "ytm_radio")
        assert source["learning_samples"] == 25 and 1 < source["multiplier"] <= 1.25
        assert (await c.get("/api/recommendations/quality?days=8")).status_code == 422
        for context, ref, reason in (("mix:1", "dz:1", "Сохранённое решение"),
                                     ("wave:w", "dz:2", "Решение волны"), ("smart:s", "dz:3", "Решение списка")):
            response = await c.get("/api/recommendations/explain", params={"context": context, "ref": ref})
            assert response.status_code == 200 and response.json() == {"reasons": [reason]}
        assert (await c.get("/api/recommendations/explain?context=mix:1&ref=dz:old")).json() == {"reasons": []}
        for identity in ({"ref": "nd:library"}, {"nd_song_id": "library"},
                         {"ref": "nd:library", "nd_song_id": "library"}):
            response = await c.get("/api/recommendations/explain", params={"context": "mix:1", **identity})
            assert response.status_code == 200
            assert response.json() == {"reasons": ["Сохранённый выбор из библиотеки"]}
        mismatch = await c.get("/api/recommendations/explain",
                               params={"context": "mix:1", "ref": "nd:library", "nd_song_id": "another"})
        assert mismatch.status_code == 404
        assert (await c.get("/api/recommendations/explain?context=mix:1&ref=dz:missing")).status_code == 404
    async with httpx.AsyncClient(transport=transport, base_url="http://hub.test", cookies={"hm_session": tokens[1]}) as c:
        for context, ref in (("mix:1", "dz:1"), ("mix:1", "nd:library"), ("wave:w", "dz:2"), ("smart:s", "dz:3")):
            assert (await c.get("/api/recommendations/explain", params={"context": context, "ref": ref})).status_code == 404
        data = (await c.get("/api/recommendations/quality")).json()
        assert all(x["learning_samples"] == 0 for x in data["sources"])
    base = {"ytm_radio": 1., "favorites": .5, "disabled": 0.}
    learned = await rec.adapted_weights(users[0], base)
    assert 1 < learned["ytm_radio"] <= 1.25 and learned["favorites"] == .5 and learned["disabled"] == 0
    assert await rec.adapted_weights(users[1], base) == base


async def test_recorded_credit_is_the_same_in_live_weights_api_window_and_offline_replay(quality_db, db_file, monkeypatch):
    from app.db import session_scope
    from app.matching.replay_quality import observed_quality
    from app.mixes.evaluate import readonly_db
    from app.models import Mix, MixItem, SmartItem, SmartSession, WaveItem, WaveSession

    (uid, other), _ = quality_db
    monkeypatch.setattr(rec, "utcnow", lambda: NOW)
    offered = NOW - timedelta(days=75)
    recent = NOW - timedelta(days=1)

    def snapshot(weights):
        return {"novelty": "new_artist", "score_parts": {"generator_contributions": weights}}

    async with session_scope() as s:
        s.add_all([Mix(id=42, user_id=uid, created_at=offered - timedelta(days=2), title="mix", mode="normal"),
                   WaveSession(id="w42", user_id=uid, created_at=offered),
                   SmartSession(id="s42", user_id=uid, context="liked", created_at=offered)])
        await s.flush()
        s.add_all([
            MixItem(mix_id=42, position=0, ref="dz:1", generator="radio", offered_at=offered,
                    features_json=snapshot({"radio": 3, "charts": 1})),
            MixItem(mix_id=42, position=1, ref="dz:failed", generator="not-offered", state="failed"),
            WaveItem(session_id="w42", user_id=uid, position=0, ref="dz:2", key="dz:2", first_handed_at=offered,
                     last_handed_at=NOW, handed_count=2, generator="tags", decision_json=snapshot({"charts": 1, "tags": 3})),
            # Old waves overwrote their snapshot on repeat: no credit before its last known handout.
            WaveItem(session_id="w42", user_id=uid, position=1, ref="dz:5", key="dz:5", first_handed_at=offered,
                     last_handed_at=recent, handed_count=2, generator="too-late", decision_json={}),
            SmartItem(session_id="s42", user_id=uid, position=0, slot=0, ref="dz:3", handed_at=offered,
                      generator="charts", decision_json=snapshot({"charts": 2, "tags": 2})),
            SmartItem(session_id="s42", user_id=uid, position=1, slot=1, ref="dz:4", handed_at=offered,
                      generator="old", decision_json={"generators": {"wrong": 0}})])
        s.add_all([event("finish", user=uid, context="mix:42", attempt=f"past{i}",
                         at=NOW - timedelta(days=65) + timedelta(minutes=i)) for i in range(80)])
        s.add_all([event("finish", user=uid, context="mix:42", attempt="pre-offer", at=offered - timedelta(hours=1)),
                   event("finish", user=uid, context="wave:w42", ref="dz:5", attempt="pre-rehand",
                         at=NOW - timedelta(days=65)),
                   event("finish", user=uid, ref="dz:failed", context="mix:42", attempt="never-offered",
                         at=NOW - timedelta(days=65)),
                   event("finish", user=uid, ref="dz:3", context="smart:s42", attempt="discovery",
                         at=NOW - timedelta(days=18)),
                   event("like", user=uid, ref="dz:3", context="search", at=NOW - timedelta(days=17)),
                   event("finish", user=uid, context="mix:42", at=recent),
                   event("skip", user=uid, ref="dz:2", context="wave:w42", attempt="b", at=recent, listened_ms=1000),
                   event("finish", user=uid, ref="dz:3", context="smart:s42", attempt="c", at=recent),
                   event("finish", user=uid, ref="dz:4", context="smart:s42", attempt="d", at=recent),
                   event("finish", user=other, context="mix:42", attempt="not-owner", at=recent)])

    async with session_scope() as s:
        live = await rec.quality(s, uid, days=7, now=NOW)
    with readonly_db(db_file) as c:
        replay = observed_quality(c, user_id=uid, until=NOW, days=7)
        assert not observed_quality(c, user_id=other, until=NOW, days=7)["sources"]
    assert live["discovery_adoption"] == replay["discovery_adoption"]
    assert live["discovery_adoption"]["mature"] == live["discovery_adoption"]["adopted"] == 1
    assert {r["id"]: r["adopted"] for r in live["discovery_adoption"]["sources"]} == {"charts": .5, "tags": .5}
    active = {s["id"]: s for s in live["sources"] if s["effective_learning_samples"]}
    assert active == {s["id"]: s for s in replay["sources"]}
    assert set(active) == {"radio", "charts", "tags", "old"}
    assert sum(s["effective_attempts"] for s in active.values()) == live["attempts"] == 4
    assert sum(s["effective_learning_samples"] for s in active.values()) == 85
    assert active["old"]["fallback_attempts"] == active["old"]["effective_eligible_plays"] == 1
    assert active["radio"]["learning_samples"] == 81
    assert type(active["charts"]["learning_samples"]) is int
    assert active["charts"]["effective_learning_samples"] == 21.5
    assert (active["charts"]["effective_completed"], active["charts"]["effective_early_skips"]) == (.75, .25)
    assert active["tags"]["effective_eligible_plays"] == 1.25
    assert active["charts"]["winner_only_multiplier"] == 1.
    assert active["charts"]["multiplier"] == pytest.approx(1 + .25 * 21 / 61.5)
    base = {"radio": .5, "charts": 2., "tags": 1.}
    assert await rec.adapted_weights(uid, base) == {
        name: weight * active[name]["multiplier"] for name, weight in base.items()}
    assert await rec.adapted_weights(other, base) == base


async def test_mix_and_smart_apply_blocked_and_soft_penalized_candidates(quality_db, monkeypatch):
    from app import recommendation_policy
    from app.mixes import build, smart, wave
    from app.recommendation_policy import Policy
    from tests.test_mix_build import gens, profile, tr

    calls = {"weights": 0, "policy": 0}
    policy = Policy(blocked_aliases={"ref:dz:1"}, quiet_artists={"quiet"})

    async def load(uid):
        calls["policy"] += 1
        return policy

    async def weights(uid, base):
        calls["weights"] += 1
        return {**base, "ytm_radio": 1.0}

    async def vectors(rt):
        return {}

    async def sound(rt):
        pass

    monkeypatch.setattr(recommendation_policy, "load_policy", load)
    monkeypatch.setattr(rec, "adapted_weights", weights)
    tracks = [tr(1), tr(2, "Quiet"), tr(3, "Loud")]
    built = await build.build(profile(), "normal", gens(ytm_radio=tracks))
    assert {c.track.ref for c in built.active} == {"dz:2", "dz:3"}
    by_ref = {c.track.ref: c for c in built.active}
    assert by_ref["dz:2"].score < by_ref["dz:3"].score
    assert by_ref["dz:2"].parts["temporary_multiplier"] == .25
    assert built.stats["rejected"]["temporary_pause"] == 1
    assert calls == {"weights": 1, "policy": 1}
    monkeypatch.setattr(wave, "_vectors", vectors)
    monkeypatch.setattr(smart, "_sound", sound)
    rt = smart.SmartRuntime(user_id=quality_db[0][0], mode="normal", state=wave.State(), profile=profile(),
                            own=["own1", "own2", "own3"])
    rt.state.pool = {t.ref: build.Cand(t, gens={"ytm_radio": i}) for i, t in enumerate(tracks)}
    selected = await smart._fill(rt, [0])
    assert selected[0][1].track.ref == "dz:3"
    assert calls == {"weights": 2, "policy": 2}
    from sqlalchemy import select
    from app.db import session_scope
    from app.models import SmartItem, SmartSession

    rt.sid = "stored"
    async with session_scope() as s:
        s.add(SmartSession(id=rt.sid, user_id=rt.user_id, context="liked"))
    await smart._save_items(rt, selected)
    async with session_scope() as s:
        item = (await s.execute(select(SmartItem).where(SmartItem.session_id == rt.sid))).scalar_one()
        assert item.generator == selected[0][1].generator
        assert item.decision_json["explanation"] == selected[0][1].explanation
        assert item.decision_json["score_parts"]["selection_value"] == selected[0][2]


async def test_adaptation_cache_is_bounded_scoped_and_keeps_callers_base_weights(quality_db, monkeypatch):
    calls = []
    clock = [0.]

    async def load(s, user_id, now, days):
        calls.append(user_id)
        return [], []

    rec.clear_adaptation_cache()
    monkeypatch.setattr(rec, "load_data", load)
    monkeypatch.setattr(rec, "monotonic", lambda: clock[0])
    monkeypatch.setattr(rec, "ADAPTATION_CACHE_MAX", 2)
    try:
        assert await rec.adapted_weights(1, {"a": 1.}) == {"a": 1.}
        assert await rec.adapted_weights(1, {"a": 2., "b": .1}) == {"a": 2., "b": .1}
        assert calls == [1]
        await rec.adapted_weights(2, {"a": 1.})
        assert calls == [1, 2]
        clock[0] = 61.
        await rec.adapted_weights(1, {"a": 1.})
        assert calls == [1, 2, 1]
        await rec.adapted_weights(3, {"a": 1.})
        assert len(rec._adaptation_cache) == 2
        await rec.adapted_weights(2, {"a": 1.})
        assert calls == [1, 2, 1, 3, 2]
    finally:
        rec.clear_adaptation_cache()


def test_policy_cohorts_keep_unknown_and_use_eligible_attempt_denominator():
    decisions = [decision(), rec.Decision(1, "wave:w", "dz:2", None, NOW - timedelta(days=2),
                                          "favorites", False, "v2")]
    events = [event("finish"), event("skip", attempt="b", ref="dz:2", context="wave:w", listened_ms=1000),
              event("error", attempt="c", ref="dz:2", context="wave:w")]
    out = report(events, decisions)
    cohorts = {r["policy_version"]: r for r in out["policy_cohorts"]}
    assert cohorts["unknown"]["eligible_plays"] == 1
    assert cohorts["unknown"]["completion_rate"] == 1
    assert cohorts["v2"]["attempts"] == 2 and cohorts["v2"]["eligible_plays"] == 1
    assert cohorts["v2"]["early_skip_rate"] == 1
    assert out["metric_version"] == "recommendation-outcomes-v2"
