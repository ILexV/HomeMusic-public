from datetime import datetime, timedelta
from itertools import count

import pytest

from app.discovery_adoption import summarize
from app.mixes.taste_snapshot import Subject
from app.models import PlayEvent
from app.recommendations import Decision

NOW = datetime(2026, 10, 30, 12)
DAY = timedelta(days=1)
TICK = timedelta(microseconds=1)
HEARD = NOW - 17 * DAY
IDS = count(1)


def event(ref, kind="finish", at=HEARD, context="mix:1", **kw):
    i = next(IDS)
    return PlayEvent(id=i, user_id=kw.pop("user_id", 1), event_id=str(i), ref=ref, ts=at,
                     kind=kind, target_type=kw.pop("target_type", "track"), context=context,
                     received_at=kw.pop("received_at", at), client=kw.pop("client", "pwa"),
                     playback_id=kw.pop("playback_id", str(i)), device_id="device", **kw)


def decision(ref, context="mix:1", at=HEARD - 100 * DAY, source="ytm_radio", **kw):
    return Decision(kw.pop("user_id", 1), context, ref, kw.pop("nd_song_id", None),
                    at, source, kw.pop("familiar", False), **kw)


def report(events, decisions, **kw):
    return summarize(1, events, decisions, now=NOW, days=kw.pop("days", 7), **kw)


@pytest.mark.parametrize("days", [7, 30, 90])
def test_right_closed_cohort_before_complete_followup_and_separate_pending(days):
    end = NOW - 14 * DAY
    start = end - days * DAY
    times = {"old": start, "first": start + TICK, "last": end, "pending": end + TICK, "future": NOW + TICK}
    events = [event(ref, at=at) for ref, at in times.items()]
    events += [event("last", "like", NOW), event("pending", "like", NOW)]
    out = report(events, [decision(ref, at=start - DAY) for ref in times], days=days)
    assert (out["window_days"], out["mature"], out["adopted"], out["pending"], out["rate"]) == (14, 2, 1, 1, .5)
    assert (out["cohort_since"], out["cohort_until"]) == (start.isoformat(), end.isoformat())
    empty = report([event("new", at=NOW)], [decision("new")], days=days)
    assert (empty["mature"], empty["pending"], empty["rate"], empty["sources"]) == (0, 1, None, [])


def test_first_heard_once_with_frozen_source_and_verified_aliases_across_offers():
    d = decision("dz:1", decision_json={"novelty": "new_track_known_artist", "score_parts": {
        "generator_contributions": {"a": 3, "b": 1}}})
    later = decision("dz:1", "smart:s", at=HEARD + DAY, source="c")
    events = [event(None, nd_song_id="song"), event("dz:1", at=HEARD + DAY, context="smart:s"),
              event("nd:song", at=HEARD + 2 * DAY, context="search", launch_id="search", play_trigger="manual"),
              event(None, at=HEARD + 3 * DAY, context="search", launch_id="search", nd_song_id="song", play_trigger="manual"),
              event("dz:2", context="wave:w"), event("old"), event("old", at=HEARD - 200 * DAY, context="artist")]
    ds = [later, d, decision("dz:2", "wave:w", source="b"), decision("old")]
    out = report(reversed(events), ds, subjects=[Subject("track", ("ref:dz:1", "nd:song"))])
    assert (out["mature"], out["adopted"]) == (2, 1)
    assert [(s["id"], s["mature"], s["adopted"], s["rate"]) for s in out["sources"]] == [
        ("a", .75, .75, 1.), ("b", 1.25, .25, .2)]


def test_only_owned_heard_new_handouts_not_seeds_starts_failures_or_unknown_classifications():
    ds = [decision("legacy"), decision("old", familiar=True), decision("unknown", decision_json={"novelty": None}),
          decision("known", decision_json={"novelty": "familiar_track"}), decision("seed", source="seed"),
          decision("prior_offer", at=HEARD + TICK), decision("someone", user_id=2),
          decision("othercontext", "smart:another")]
    rows = [event(d.ref) for d in ds] + [event("no_offer")]
    for ref, kw in (("start", {"kind": "start"}), ("progress", {"kind": "progress", "listened_ms": 60000}),
                    ("seek", {"kind": "skip", "listened_ms": 500, "position_s": 100.}),
                    ("short", {"kind": "finish", "listened_ms": 10000, "duration_s": 20.}),
                    ("late", {"received_at": NOW + TICK}), ("qa", {"client": "app-debug"}),
                    ("mirror", {"superseded_scrobble": True}), ("other", {"user_id": 2})):
        ds.append(decision(ref))
        rows.append(event(ref, **kw))
    for kind in ("error", "interrupted"):
        ds.append(decision(kind))
        rows.extend([event(kind, playback_id=kind, listened_ms=90000), event(kind, kind, at=HEARD + TICK, playback_id=kind)])
    out = report(rows, ds)
    assert (out["mature"], out["adopted"]) == (1, 0)
    assert any("Старый" in line for line in out["limitations"])


def test_causal_likes_frozen_at_each_deadline_with_provenance_and_real_state_changes():
    at = HEARD + DAY
    deadline = HEARD + 14 * DAY
    refs = ("kept", "undone", "restored", "unliked", "before", "same", "late", "import", "synced", "bootstrap", "retry", "alien", "album")
    rows = [event(ref) for ref in refs]
    for ref in ("undone", "restored"):
        like = event(ref, "like", at=at, context="search")
        undo = event(ref, "unlike", at=deadline, reverts_event_id=like.event_id, context="download:failed")
        rows.extend([like, undo])
        if ref == "restored":
            rows.append(event(ref, "like", at=deadline, reverts_event_id=undo.event_id))
    rows.extend([event("kept", "like", at=deadline), event("kept", "unlike", at=deadline + TICK),
                 event("unliked", "like", at=at), event("unliked", "unlike", at=deadline),
                 event("before", "like", at=HEARD - TICK), event("same", "like", at=HEARD),
                 event("late", "like", at=deadline + TICK), event("import", "like", at=at, target_data={"provenance": "import"}),
                 event("synced", "like", at=at, context="favorite:reconcile"),
                 event("bootstrap", "like", at=at, context="favorite:bootstrap"),
                 event("retry", "like", at=HEARD - 300 * DAY, client="import"), event("retry", "like", at=at),
                 event("alien", "like", at=at, user_id=2), event("album", "like", at=at, target_type="album")])
    result = report(rows, [decision(ref) for ref in refs])
    assert (result["mature"], result["adopted"]) == (len(refs), 2)


@pytest.mark.parametrize("other,flags", [
    ("queue", {}), (None, {}), ("continuous", {}), ("wave:w", {}), ("smart:s", {}), ("mix:1", {}),
    ("playlist", {}), ("playlist:generated", {}), ("search", {"launch_origin": "mix_page_track"}),
    ("library", {"launch_id": "wave:w"}), ("artist", {"kind": "skip", "listened_ms": 300}),
    ("search", {"received_at": NOW + TICK}), ("search", {"at": HEARD + 14 * DAY + TICK}),
    ("playlist:mine", {"play_trigger": "auto"}), ("liked", {"play_trigger": "repeat"}),
])
def test_one_heard_organic_plus_retry_and_nonqualifying_second_is_not_adoption(other, flags):
    first = event("x", at=HEARD + DAY, context="search", playback_id="own", listened_ms=31000, play_trigger="manual")
    retry = event("x", at=HEARD + DAY + TICK, context="search", playback_id="own", listened_ms=31000, play_trigger="manual")
    second = event("x", **{"at": HEARD + 2 * DAY, "context": other, "play_trigger": "manual", **flags})
    out = report([event("x"), first, retry, second], [decision("x")], mix_playlist_ids=["generated"])
    assert (out["mature"], out["adopted"], out["rate"]) == (1, 0, 0.)
    assert out["unknown_return_tracks"] == 0


def test_unknown_returns_count_distinct_mature_tracks_only_and_never_undo_explicit_likes():
    refs = ("legacy", "liked", "excluded", "pending")
    rows = [event(ref, at=NOW - 3 * DAY if ref == "pending" else HEARD) for ref in refs]
    for day in (1, 2):
        for ref in refs:
            at = (NOW - 3 * DAY if ref == "pending" else HEARD) + day * DAY
            rows.append(event(ref, at=at, context="smart:s" if ref == "excluded" else "search", playback_id=None))
    rows.append(event("liked", "like", at=HEARD + DAY))
    out = report(rows, [decision(ref) for ref in refs])
    assert (out["mature"], out["adopted"], out["pending"], out["unknown_return_tracks"]) == (3, 1, 1, 2)


@pytest.mark.parametrize("playback_id,last_trigger,adopted,unknown", [
    ("id", None, 1, 0),              # evidence belongs to one identified attempt
    ("id", "auto", 0, 1),            # conflict is not confirmed intent
    (None, None, 0, 1),              # no inference across unkeyed legacy events
    (None, "manual", 1, 0),
])
def test_return_uses_attempt_not_just_terminal_or_list(playback_id, last_trigger, adopted, unknown):
    rows = [event("x"), event("x", at=HEARD + DAY, context="liked", play_trigger="manual"),
            event("x", "start", HEARD + 2 * DAY, "playlist:own", playback_id=playback_id, play_trigger="manual"),
            event("x", at=HEARD + 2 * DAY + TICK, context="playlist:own", playback_id=playback_id, play_trigger=last_trigger)]
    out = report(rows, [decision("x")], mix_playlist_ids=["generated"])
    assert (out["adopted"], out["unknown_return_tracks"]) == (adopted, unknown)


async def test_quality_loads_first_listen_and_prior_marks_beyond_its_learning_window(hub_env, db_file):
    from app.db import dispose, init_engine, session_scope
    from app.models import Mix, MixItem, SmartItem, SmartSession, User, WaveItem, WaveSession
    from app.recommendations import quality

    init_engine(db_file)
    try:
        async with session_scope() as s:
            user = User(nd_username="adoption", nd_password_enc="x")
            s.add(user)
            await s.flush()
            uid = user.id
            s.add(Mix(id=1, user_id=uid, title="mix", mode="normal", created_at=HEARD - DAY))
            s.add(WaveSession(id="w", user_id=uid, created_at=HEARD - DAY))
            s.add(SmartSession(id="s", user_id=uid, context="liked", created_at=HEARD - DAY))
            await s.flush()
            for pos, ref in enumerate(("old", "repeat")):
                s.add(MixItem(mix_id=1, position=pos, ref=ref, familiar=False, generator="a", offered_at=HEARD))
            s.add(WaveItem(session_id="w", user_id=uid, position=0, ref="wave", key="wave", generator="b",
                familiar=False, first_handed_at=HEARD, last_handed_at=HEARD))
            s.add(SmartItem(session_id="s", user_id=uid, position=0, slot=0, ref="smart", generator="c",
                familiar=False, handed_at=HEARD))
            s.add_all([event("old", user_id=uid), event("old", user_id=uid, at=HEARD - 180 * DAY, context="library"),
                event("repeat", user_id=uid), event("repeat", "like", user_id=uid, at=HEARD - 180 * DAY),
                event("repeat", "like", user_id=uid, at=HEARD + DAY), event("smart", context="smart:s", user_id=uid),
                event("wave", context="wave:w", user_id=uid), event("smart", "like", user_id=uid, at=HEARD + DAY)])
            await s.flush()
            out = (await quality(s, uid, days=7, now=NOW))["discovery_adoption"]
            assert (out["mature"], out["adopted"], out["pending"]) == (3, 1, 0)
            assert {row["id"]: row["rate"] for row in out["sources"]} == {"a": 0., "b": 0., "c": 1.}
    finally:
        await dispose()


async def test_no_report_queries_for_offers_without_a_heard_candidate(hub_env, db_file):
    from sqlalchemy import event as sql_event

    from app.db import dispose, init_engine, session_scope
    from app.discovery_adoption import report as load_report

    init_engine(db_file)
    try:
        async with session_scope() as s:
            queries = []
            sql_event.listen(s.bind.sync_engine, "before_cursor_execute", lambda *args: queries.append(args[2]))
            ds = [decision("x")]
            journals = [[], [event("x", "start")], [event("x", at=HEARD - 30 * DAY)],
                        [event("x", context="library")], [event("x", "skip", listened_ms=200)],
                        [event("x", playback_id="broken"), event("x", "error", playback_id="broken")]]
            for journal in journals:
                out = await load_report(s, 1, journal, ds, now=NOW, days=7)
                assert (out["mature"], out["pending"], out["rate"]) == (0, 0, None)
            assert queries == []
    finally:
        await dispose()


KNOWN_ARTIST = "new_track_known_artist"
NEW_ARTIST = "new_artist"


def test_subtype_fractions_use_first_frozen_decision_and_keep_legacy_separate():
    kinds = {"a": KNOWN_ARTIST, "b": KNOWN_ARTIST, "c": NEW_ARTIST, "legacy": None}
    ds = [decision(ref, decision_json={"novelty": k} if k else {}) for ref, k in kinds.items()]
    ds.append(decision("legacy", context="wave:w", at=HEARD + DAY, decision_json={"novelty": NEW_ARTIST}))
    rows = [event(ref) for ref in kinds] + [event("legacy", at=HEARD + 2 * DAY, context="wave:w"),
        event("a", "like", at=HEARD + DAY), event("legacy", "like", at=HEARD + DAY)]
    out = report(rows, ds)
    assert [(r["id"], r["adopted"], r["mature"], r["rate"]) for r in out["subtypes"]] == [
        (KNOWN_ARTIST, 1, 2, .5), (NEW_ARTIST, 0, 1, 0.), ("unknown", 1, 1, 1.)]


@pytest.mark.parametrize("n_known,n_new,winner,expected", [
    (0, 0, None, None), (19, 100, NEW_ARTIST, None), (100, 19, KNOWN_ARTIST, None),
    (21, 45, None, None), (20, 20, KNOWN_ARTIST, .55), (20, 20, NEW_ARTIST, .45),
    (80, 100, KNOWN_ARTIST, .6), (100, 80, NEW_ARTIST, .4),
])
def test_balance_requires_both_mature_cohorts_and_shrinks_the_difference(n_known, n_new, winner, expected):
    rows, ds = [], []
    for kind, n in ((KNOWN_ARTIST, n_known), (NEW_ARTIST, n_new)):
        for i in range(n):
            ref = f"{kind}:{i}"
            rows.append(event(ref))
            ds.append(decision(ref, decision_json={"novelty": kind}))
            if kind == winner:
                rows.append(event(ref, "like", at=HEARD + DAY))
    balance = {}
    out = report(rows, ds, balance=balance)
    assert [r["mature"] for r in out["subtypes"]] == [n_known, n_new]
    assert all(r["rate"] is None for r in out["subtypes"] if not r["mature"])
    if expected is None:
        assert balance["known_artist_share"] is None
    else:
        assert balance["known_artist_share"] == pytest.approx(expected)
    assert balance["days"] == 90 and balance["min_per_subtype"] == 20 and balance["reason"]


async def test_fixed_window_balance_matches_offline_and_cached_selection(hub_env, db_file, monkeypatch):
    from app import recommendations
    from app.db import dispose, init_engine, session_scope
    from app.matching.replay_quality import observed_quality
    from app.mixes.evaluate import readonly_db
    from app.models import Mix, MixItem, User

    init_engine(db_file)
    monkeypatch.setattr(recommendations, "utcnow", lambda: NOW)
    # Inside the mature 90-day cohort, outside the last 90 wall-clock days and the 7-day report.
    at = NOW - 100 * DAY
    try:
        async with session_scope() as s:
            u = User(nd_username="balance", nd_password_enc="x")
            s.add(u)
            await s.flush()
            uid = u.id
            s.add(Mix(id=1, user_id=uid, title="mix", mode="discover", created_at=at - DAY))
            await s.flush()
            for i in range(40):
                ref = f"dz:{i}"
                subtype = KNOWN_ARTIST if i < 20 else NEW_ARTIST
                s.add(MixItem(mix_id=1, position=i, ref=ref, generator="ytm_radio",
                              features_json={"novelty": subtype}, offered_at=at))
                s.add(event(ref, user_id=uid, at=at))
                if i < 20:
                    s.add(event(ref, "like", user_id=uid, at=at + DAY))
            # This track has two eligible legacy returns: insufficient evidence for either a win or a loss.
            s.add_all([event("dz:39", user_id=uid, context="search", at=at + j * DAY) for j in (1, 2)])
            await s.flush()
            q = await recommendations.quality(s, uid, now=NOW, days=7)
            assert not q["discovery_adoption"]["mature"] and q["discovery_balance"]["known_artist_share"] is None
            s.add(event("dz:39", "like", user_id=uid, at=at + 3 * DAY))
        async with session_scope() as s:
            q = await recommendations.quality(s, uid, now=NOW, days=7)
        with readonly_db(db_file) as db:
            offline = observed_quality(db, user_id=uid, until=NOW, days=7)
        assert offline["discovery_balance"] == q["discovery_balance"]
        assert offline["discovery_adoption"] == q["discovery_adoption"]
        share = q["discovery_balance"]["known_artist_share"]
        assert share == pytest.approx(.5475)
        assert await recommendations.discovery_share(uid) == share
        assert await recommendations.discovery_share(uid + 1) is None
        async def unexpected(*args):
            pytest.fail("cached Discover selection reloaded account history")
        monkeypatch.setattr(recommendations, "load_data", unexpected)
        assert await recommendations.discovery_share(uid) == share
        assert await recommendations.discovery_share(uid + 1) is None
    finally:
        await dispose()
