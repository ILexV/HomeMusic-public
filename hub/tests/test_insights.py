"""Admin insights (INSIGHTS.md 7): weight breakdowns, launches, period statistics and the read-only admin API.

All data is synthetic, with an explicit `NOW` (naive UTC) and the Hub's timezone (Europe/Moscow, UTC+3). The library
has «Кино — Группа крови» (dz:100 / ndK, 245 s) and «Би-2 — Полковнику» (dz:200 / ndM, 200 s); «Ария — Беспечный
ангел» (dz:300) is a catalog track only.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import event as sa_event

from app.mixes.taste_math import Trace, calculate_profile, saturation
from app.mixes.taste_snapshot import TasteEvent, TasteSnapshot

NOW = datetime(2026, 3, 15, 12, 0)
TZ = "Europe/Moscow"


# --- the breakdown of a weight: pure, no DB --------------------------------------------------------------------


def tev(eid, kind="finish", at=NOW, ref="dz:1", **kw):
    return TasteEvent(1, eid, at, kind, ref, received_at=kw.pop("received_at", at), client=kw.pop("client", "pwa"),
                      **kw)


def trace_snapshot() -> TasteSnapshot:
    rows = [tev("f1", at=NOW - timedelta(days=2), position_s=245, duration_s=245),
            tev("s1", "skip", at=NOW - timedelta(days=1), position_s=10, duration_s=245),
            tev("s2", "skip", at=NOW - timedelta(hours=5), position_s=200, duration_s=245),
            tev("st", "start", at=NOW - timedelta(hours=4)),
            tev("lk", "like", at=NOW - timedelta(hours=3)),
            tev("dz2", "dislike", at=NOW - timedelta(hours=2), ref="dz:2"),
            tev("f2", at=NOW - timedelta(days=3), ref="dz:2"),
            # a ♥ from four years ago: in the state, out of the window
            tev("old", "like", at=NOW - timedelta(days=4 * 366), ref="dz:3"),
            tev("f3", at=NOW - timedelta(days=1), ref="dz:3"),
            # an import's ♥ is not the user's own
            tev("imp", "like", at=NOW - timedelta(days=1), ref="dz:4", target_data={"provenance": "import"}),
            tev("f4", at=NOW - timedelta(hours=1), ref="dz:4")]
    # a listen long ago, followed by 250 active days (5 tracks a day): its weight drops below 0.01
    start = NOW - timedelta(days=300)
    rows.append(tev("ancient", at=start, ref="dz:5"))
    rows += [tev(f"d{d}-{n}", at=start + timedelta(days=d), ref=f"dz:fill{n}") for d in range(1, 251) for n in range(5)]
    return TasteSnapshot(1, tuple(rows))


def test_trace_observes_without_changing_the_profile_and_adds_up():
    snap = trace_snapshot()
    plain = calculate_profile(snap, now=NOW, timezone=TZ)
    trace = Trace()
    traced = calculate_profile(snap, now=NOW, timezone=TZ, trace=trace)
    assert traced == plain
    for key, t in traced.tracks.items():
        rows = trace.tracks.get(key, [])
        assert sum(c.value for c in rows if c.side == "P") == pytest.approx(t.positive_raw), key
        assert sum(c.value for c in rows if c.side == "N") == pytest.approx(t.negative), key
        assert t.score == pytest.approx(saturation(sum(c.value for c in rows if c.side == "P")) -
                                        sum(c.value for c in rows if c.side == "N"))
    one = {c.event_id: c for c in trace.tracks["dz:1"]}
    assert (one["f1"].side, one["f1"].base) == ("P", 2.0)
    assert (one["s1"].side, one["s1"].base, one["s1"].value) == ("N", -1.0, pytest.approx(one["s1"].w))
    assert one["s2"].side == "P" and 0 < one["s2"].base < 2
    assert (one["st"].side, one["st"].note) == ("0", "start — вес 0")
    assert (one["lk"].side, one["lk"].base) == ("P", 1.0)
    assert {c.kind: c.side for c in trace.tracks["dz:2"]}["dislike"] == "N"
    assert [(c.side, c.note) for c in trace.tracks["dz:3"] if c.kind == "like"] == [("0", "старше 3 лет")]
    assert [(c.side, c.note) for c in trace.tracks["dz:4"] if c.kind == "like"] == [("0", "импорт — не свой")]
    assert [(c.w, c.note) for c in trace.tracks["dz:5"]] == [(0.0, "w < 0.01")]


# --- the Hub: users, sessions, rows ----------------------------------------------------------------------------


def write_config(tmp_path, text: str) -> None:
    from app import config

    (tmp_path / "none.yaml").write_text(text, encoding="utf-8")
    config.get_settings.cache_clear()


@pytest.fixture
async def hub(hub_env, db_file, tmp_path, monkeypatch):
    """The app, its users (admin, anna, boris, qa — the QA account left out of the totals) and clients."""
    from app.api import insights
    from app.db import dispose, init_engine, session_scope
    from app.main import create_app
    from app.matching.normalize import fingerprint
    from app.models import Session, Track, User
    from app.security import new_token

    write_config(tmp_path, "mixes:\n  taste_model: v2\nanalytics:\n  exclude_users: [qa]\n")
    monkeypatch.setattr(insights, "utcnow", lambda: NOW)
    init_engine(db_file)  # no lifespan here: this test's own database
    tokens, ids = {}, {}
    async with session_scope() as s:
        for name in ("admin", "anna", "boris", "qa"):
            # seen «in the future»: the auth check has nothing to refresh, so a request writes nothing
            u = User(nd_username=name, nd_password_enc="x", is_admin=name == "admin", last_seen_at=datetime(2100, 1, 1))
            s.add(u)
            await s.flush()
            ids[name] = u.id
            tokens[name], digest = new_token()
            s.add(Session(id=digest, user_id=u.id, expires_at=datetime(2100, 1, 1)))
        for artist, title, nd, dz, dur in (("Кино", "Группа крови", "ndK", "100", 245),
                                           ("Би-2", "Полковнику", "ndM", "200", 200)):
            s.add(Track(uid=str(uuid.uuid4()), path=f"{artist}/{title}.mp3", nd_song_id=nd, artist=artist, title=title,
                        duration_s=dur, deezer_track_id=dz, fingerprint=fingerprint(artist, title)))
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    clients = {}
    for name in ("admin", "anna"):
        clients[name] = httpx.AsyncClient(transport=transport, base_url="http://hub.test",
                                          cookies={"hm_session": tokens[name]})
    yield {"ids": ids, "admin": clients["admin"], "user": clients["anna"], "tmp": tmp_path}
    for c in clients.values():
        await c.aclose()
    await dispose()


def pe(uid, kind, at, ref=None, nd=None, **kw):
    from app.models import PlayEvent

    return PlayEvent(user_id=uid, event_id=uuid.uuid4().hex, ts=at, kind=kind, ref=ref, nd_song_id=nd,
                     client=kw.pop("client", "app/1.12.0"), received_at=kw.pop("received_at", at), **kw)


def attempt(uid, at, ref=None, nd=None, *, end="finish", heard_ms=40_000, position=None, duration=245,
            device="dev1", **kw):
    """One measured attempt: start, a checkpoint, then `end` (finish/skip) — all with one playback id."""
    pb = uuid.uuid4().hex
    common = {"playback_id": pb, "device_id": device, "duration_s": duration, **kw}
    return [pe(uid, "start", at, ref, nd, listened_ms=0, **common),
            pe(uid, "progress", at + timedelta(seconds=heard_ms / 1000), ref, nd, listened_ms=heard_ms, **common),
            pe(uid, end, at + timedelta(seconds=heard_ms / 1000 + 1), ref, nd, listened_ms=heard_ms,
               position_s=position if position is not None else (duration if end == "finish" else heard_ms / 1000),
               **common)]


def test_launch_skips_use_heard_seconds_and_bad_playback_is_neutral():
    from app.insights import launches

    skipped = attempt(1, NOW, "dz:1", end="skip", heard_ms=10_000, position=170)
    failed = attempt(1, NOW + timedelta(hours=1), "dz:2", end="skip", heard_ms=2_000, position=2)
    failed.append(pe(1, "error", failed[-1].ts, "dz:2", device_id="dev1", playback_id=failed[0].playback_id))
    rows = skipped + failed
    first, second = launches.attempts_of(rows, launches.Aliases(rows))
    assert first.early_skip_at == 10 and not first.late_skip
    assert second.early_skip_at is None and not second.late_skip and not second.finished
    assert second.listened_ms == 2_000  # Heard time remains real, even when there is no taste outcome.


async def add(*rows) -> None:
    from app.db import session_scope

    async with session_scope() as s:
        for r in rows:
            if isinstance(r, list):
                s.add_all(r)
            else:
                s.add(r)


def mix_row(uid, mix_id, created, *, closed=None, old=False, playlist="pl1"):
    from app.models import Mix

    stats = {"mode": "normal", "taste_model": "v2", "seeds": ["Кино — Группа крови"], "seed_keys": ["nd:ndK"],
             "generators": {"ytm_radio": {"tracks": 40, "used": 2, "seconds": 1.2},
                            "lastfm_similar": {"tracks": 0, "used": 0, "seconds": 0, "off": True}},
             "candidates": 60, "kept": 50, "familiar": 1, "maturity": .4,
             "facets": [{"id": "f0", "weight": 1.0, "artists": ["Кино"], "members": ["nd:ndK"]}],
             "embedder": {"space_id": "sp", "model": "m"}, "build_seconds": 3.1}
    if not old:
        stats["rejected"] = {"disliked": 1, "version": 2, "duration": 0, "recent": 7, "negative_score": 0}
    return Mix(id=mix_id, user_id=uid, created_at=created, title="Микс дня", description="", mode="normal",
               nd_playlist_id=playlist, status="closed" if closed else "active", stats_json=stats, closed_at=closed)


def mix_item(mix_id, position, *, ref=None, nd=None, state="active", generator="ytm_radio", familiar=False,
             parts=True, artist="Кино", title="Группа крови"):
    from app.models import MixItem

    features = {"track": {"ref": ref, "artist": artist, "title": title}, "generators": {generator: 0}, "sim": .5,
                "rank": 10, "familiar": familiar}
    if parts:
        features |= {"score_parts": {"generators": 1.0, "sound": .3, "total": 1.3}, "profile": None}
    return MixItem(mix_id=mix_id, position=position, state=state, familiar=familiar, ref=ref, nd_song_id=nd,
                   generator=generator, score=1.3, features_json=features)


def wave_row(uid, sid, created, *, origin=None, seed=None, request=None, cleared=False, restart_of=None):
    from app.models import WaveSession

    state = {"cleared": True, "gens": {"favorites": {"tracks": 3}}, "downloads": ["dz:9"]} if cleared else \
        {"version": 2, "taste_model": "v2", "gens": {"favorites": {"tracks": 3, "seconds": .1}}, "downloads": []}
    return WaveSession(id=sid, user_id=uid, created_at=created, updated_at=created + timedelta(minutes=30),
                       mode="normal", seed_json=seed, state_json=state, origin=origin, client="app/1.12.0",
                       restart_of=restart_of, taste_model="v2", request_json=request, next_calls=2,
                       cleared_at=created + timedelta(days=8) if cleared else None)


def wave_item(uid, sid, position, *, ref, nd=None, generator="favorites", artist="Би-2", title="Полковнику"):
    from app.models import WaveItem

    return WaveItem(session_id=sid, user_id=uid, first_handed_at=NOW - timedelta(days=1),
                    last_handed_at=NOW - timedelta(days=1), position=position, ref=ref, nd_song_id=nd,
                    key=f"nd:{nd}" if nd else ref, artist=artist, title=title, generator=generator,
                    gens={generator: 0}, familiar=True, score=1.0)


async def listeners(ids) -> None:
    """anna: a launched mix, a wave from the home card, its restart and a session known only by events."""
    m = ids["anna"]
    mix_at = NOW - timedelta(days=2)
    await add(mix_row(m, 1, mix_at), mix_item(1, 0, ref="dz:100", nd="ndK"),
              mix_item(1, 1, ref="dz:300", artist="Ария", title="Беспечный ангел", generator="lastfm_similar"),
              mix_item(1, 50, ref="dz:200", nd="ndM", state="reserve"))
    played = mix_at + timedelta(hours=2)
    await add(attempt(m, played, "dz:100", "ndK", context="mix:1", launch_id="L1", launch_origin="mix_page"),
              attempt(m, played + timedelta(minutes=5), "dz:300", end="skip", heard_ms=10_000, context="mix:1",
                      launch_id="L1", launch_origin="mix_page"),
              pe(m, "like", played + timedelta(minutes=2), "dz:100", "ndK", context="mix:1"))
    w_at = NOW - timedelta(days=1)
    await add(wave_row(m, "w1", w_at, origin="home_card", request={"seed": None, "origin": "home_card"}),
              wave_row(m, "w2", w_at + timedelta(hours=1), origin="restart", restart_of="w1",
                       request={"seed": None, "origin": "restart"}))
    await add(wave_item(m, "w1", 0, ref="dz:200", nd="ndM"),
              wave_item(m, "w1", 1, ref="dz:300", generator="ytm_radio", artist="Ария", title="Беспечный ангел"))
    await add(attempt(m, w_at + timedelta(minutes=1), "dz:200", "ndM", duration=200, context="wave:w1",
                      launch_id="wave:w1", launch_origin="wave"),
              attempt(m, w_at - timedelta(days=3), "dz:100", "ndK", context="wave:gone"))


# --- launches ---------------------------------------------------------------------------------------------------


async def history(uid):
    from app.db import session_scope
    from app.insights import launches

    async with session_scope() as s:
        return await launches.load(s, uid, NOW)


async def test_launches_by_id_wave_session_summary_reconstruction_and_restart(hub):
    m = hub["ids"]["anna"]
    await listeners(hub["ids"])
    await add(wave_row(m, "w0", NOW - timedelta(days=12), origin=None, seed={"ref": "dz:100", "artist": "Кино",
                                                                            "title": "Группа крови"}, cleared=True))
    h = await history(m)
    by = {x.id: x for x in h.launches}
    assert by["L1"].attribution == "launch_id" and by["L1"].origin == "mix_page" and by["L1"].mix_id == 1
    assert len(by["L1"].attempts) == 2 and by["L1"].stats()["likes"] == 1
    assert by["wave:w1"].attribution == "wave_session" and by["wave:w1"].origin == "home_card"
    assert len(by["wave:w1"].attempts) == 1 and not by["wave:w1"].restart
    assert by["wave:w2"].restart and not by["wave:w2"].attempts
    assert by["wave:w0"].attribution == "wave_session" and h.sessions["w0"].cleared_at is not None
    assert by["wave:gone"].attribution == "reconstructed" and by["wave:gone"].origin is None


async def test_old_history_splits_on_context_gap_and_device(hub):
    m = hub["ids"]["anna"]
    t0 = NOW - timedelta(days=1)

    def legacy(at, ref, ctx, client="app/1.0.0", end="finish"):
        return [pe(m, "start", at, ref, context=ctx, client=client),
                pe(m, end, at + timedelta(minutes=3), ref, context=ctx, client=client, position_s=180)]

    await add(legacy(t0, "dz:1", "album"), legacy(t0 + timedelta(minutes=4), "dz:2", "album"),
              legacy(t0 + timedelta(minutes=8), "dz:3", "search"),  # another context
              legacy(t0 + timedelta(minutes=50), "dz:4", "search"),  # a 39-minute gap
              legacy(t0 + timedelta(minutes=9), "dz:5", "album", client="pwa"))  # another device, in parallel
    h = await history(m)
    shape = [(x.attribution, x.context, x.client, len(x.attempts)) for x in h.launches]
    assert shape == [("inferred", "album", "app/1.0.0", 2), ("inferred", "search", "app/1.0.0", 1),
                     ("inferred", "album", "pwa", 1), ("inferred", "search", "app/1.0.0", 1)]


async def test_playlists_are_attributed_to_the_mix_in_effect(hub):
    m = hub["ids"]["anna"]
    await add(mix_row(m, 1, NOW - timedelta(days=3), closed=NOW - timedelta(days=2)), mix_row(m, 2, NOW - timedelta(days=2)),
              mix_item(2, 0, ref="dz:100", nd="ndK"), mix_item(2, 1, ref="dz:200", nd="ndM"))
    at = NOW - timedelta(hours=5)
    await add(attempt(m, at, "dz:100", "ndK", context="playlist:pl1", launch_id="P1"),
              # the app's old `playlist` without an id: 2 of 3 tracks (67 %) are the mix's
              attempt(m, at + timedelta(hours=1), "dz:100", "ndK", context="playlist", device="dev2"),
              attempt(m, at + timedelta(hours=1, minutes=5), "dz:200", "ndM", context="playlist", device="dev2"),
              attempt(m, at + timedelta(hours=1, minutes=9), "dz:777", context="playlist", device="dev2"),
              # an unrelated playlist
              attempt(m, at + timedelta(hours=3), "dz:777", context="playlist", device="dev3"))
    h = await history(m)
    got = [(x.id.split(":")[0], x.mix_id, x.mix_attribution) for x in h.launches]
    assert got == [("P1", 2, "playlist_id"), ("inferred", 2, "inferred_playlist"), ("inferred", None, None)]


# --- listening ---------------------------------------------------------------------------------------------------


async def test_counted_listens_match_the_summary_and_hours_are_local(hub):
    from app.db import session_scope
    from app.insights import analytics
    from app.listening_summary import summary
    from app.models import User

    m = hub["ids"]["anna"]
    # 21:30 UTC on March 13 is 00:30 on Saturday March 14 in Moscow
    late = datetime(2026, 3, 13, 21, 30)
    await add(attempt(m, late, "dz:100", "ndK", context="album"),
              attempt(m, late + timedelta(minutes=5), "dz:300", end="skip", heard_ms=12_000, context="album"),
              attempt(m, datetime(2026, 3, 1, 9), "dz:200", "ndM", context="search", duration=200),
              pe(m, "finish", datetime(2026, 3, 14, 8), None, "ndM", client="navidrome"),  # a scrobble
              pe(m, "finish", datetime(2026, 3, 14, 9), "dz:300", client="app/1.0.0", position_s=200),  # legacy
              attempt(m, datetime(2026, 2, 1, 9), "dz:100", "ndK", context="album"))  # before the period
    start, end = date(2026, 3, 1), date(2026, 3, 15)
    async with session_scope() as s:
        user = await s.get(User, m)
        got = await analytics.listening(s, user, start, end, TZ, NOW)
        want = await summary(s, m, start, end, TZ)
    assert got["totals"]["counted_listens"] == want["counted_listens"] == 3
    assert got["totals"]["listened_ms"] == want["measured_ms"]
    assert got["heatmap"][5][0] == 1  # Saturday, 00:xx local
    assert got["quality"]["scrobbles"] == 1 and got["quality"]["legacy_finishes"] == 1
    assert got["totals"]["heard_events"] == 4  # two finishes, the scrobble, the legacy finish
    by_day = {d["date"]: d for d in got["by_day"]}
    assert by_day["2026-03-14"]["heard"] == 3 and by_day["2026-03-14"]["by_source"]["scrobble"] == 1
    # ndK was heard in February already: only ndM (and the legacy dz:300) are new
    assert {d["key"] for d in got["discoveries"]} == {"nd:ndM", "dz:300"}
    assert got["totals"]["new_artists"] == 1  # Би-2; dz:300 has no local name (no network): «Неизвестный трек»
    assert next(d for d in got["discoveries"] if d["key"] == "dz:300")["title"] == "Неизвестный трек"


async def test_listening_counts_mix_and_wave_recommendations_and_adoption(hub):
    from app.db import session_scope
    from app.insights import analytics
    from app.models import User

    m = hub["ids"]["anna"]
    await listeners(hub["ids"])
    # the mix's new song (dz:300, skipped) later played twice from an album: adopted
    await add(attempt(m, NOW - timedelta(hours=6), "dz:300", context="album", device="dev2"),
              attempt(m, NOW - timedelta(hours=5), "dz:300", context="album", device="dev2"))
    async with session_scope() as s:
        got = await analytics.listening(s, await s.get(User, m), date(2026, 3, 1), date(2026, 3, 15), TZ, NOW)
    mix, wave = got["recommendations"]["mix"], got["recommendations"]["wave"]
    assert (mix["built"], mix["launched_mixes"], mix["launches"], mix["started"], mix["heard"]) == (1, 1, 1, 2, 1)
    assert mix["likes"] == 1 and mix["familiar"] == {"started": 0, "heard": 0} and mix["new"]["started"] == 2
    gens = {g["generator"]: g for g in mix["by_generator"]}
    assert (gens["ytm_radio"]["offered"], gens["ytm_radio"]["finished"], gens["ytm_radio"]["liked"]) == (1, 1, 1)
    assert gens["lastfm_similar"]["early_skips"] == 1
    assert (wave["launches"], wave["restarts"], wave["by_origin"], wave["by_seed_kind"]) == (
        2, 1, {"home_card": 1, "not_recorded": 1}, {"own": 1, "unknown": 1})
    assert {g["generator"]: g["offered"] for g in wave["by_generator"]} == {"favorites": 1, "ytm_radio": 1}
    assert mix["adopted"] == 0  # dz:300 was skipped in the mix: first heard from an album
    assert got["quality"]["reconstructed_waves"] == 1


# --- the API ------------------------------------------------------------------------------------------------------


PATHS = ["/api/admin/insights/overview", "/api/admin/users/{u}/listening", "/api/admin/users/{u}/taste",
         "/api/admin/users/{u}/taste/tracks", "/api/admin/users/{u}/taste/track?key=nd:ndK",
         "/api/admin/taste/matrix", "/api/admin/users/{u}/mixes", "/api/admin/mixes/1",
         "/api/admin/users/{u}/waves", "/api/admin/waves/w1"]


async def test_every_path_is_admin_only_uncached_read_only_and_offline(hub):
    from app.db import engine

    await listeners(hub["ids"])
    u = hub["ids"]["anna"]
    writes: list[str] = []

    def spy(_conn, _cursor, statement, *_args):
        if statement.lstrip().split(" ", 1)[0].upper() in ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP"):
            writes.append(statement)

    sa_event.listen(engine().sync_engine, "before_cursor_execute", spy)
    try:
        with respx.mock() as net:  # nothing mocked: any request fails the test
            for path in PATHS:
                url = path.format(u=u)
                assert (await hub["user"].get(url)).status_code == 403, url
                r = await hub["admin"].get(url)
                assert r.status_code == 200, (url, r.text)
                assert r.headers["cache-control"] == "no-store", url
            assert not net.calls
    finally:
        sa_event.remove(engine().sync_engine, "before_cursor_execute", spy)
    assert writes == []


@pytest.mark.parametrize("url,status", [
    ("/api/admin/users/999/listening", 404), ("/api/admin/users/999/taste", 404), ("/api/admin/mixes/999", 404),
    ("/api/admin/waves/nope", 404), ("/api/admin/users/{u}/taste/track?key=dz:nope", 404),
    ("/api/admin/users/{u}/listening?from=2026-03-10&to=2026-03-01", 422),
    ("/api/admin/users/{u}/listening?from=2020-01-01&to=2026-03-01", 422),
    ("/api/admin/users/{u}/listening?from=yesterday", 422), ("/api/admin/taste/matrix?sort=user:x", 422),
    ("/api/admin/users/{u}/taste/tracks?limit=500", 422),
], ids=["user", "taste_user", "mix", "wave", "track", "reversed", "too_long", "bad_date", "bad_sort", "limit"])
async def test_unknown_ids_and_bad_parameters(hub, url, status):
    await listeners(hub["ids"])
    r = await hub["admin"].get(url.format(u=hub["ids"]["anna"]))
    assert r.status_code == status, r.text


async def test_admin_profile_is_the_mix_build_profile(hub):
    from app.db import session_scope
    from app.insights.taste_view import admin_profile
    from app.mixes.taste import build_profile
    from app.models import User

    await listeners(hub["ids"])
    async with session_scope() as s:
        user = await s.get(User, hub["ids"]["anna"])
        mine = await admin_profile(s, user.id, model=None, now=NOW)
        built = await build_profile(s, user, now=NOW, model="v2")
    assert mine == built and mine.tracks


async def test_taste_endpoints_explain_each_weight(hub):
    await listeners(hub["ids"])
    u = hub["ids"]["anna"]
    r = await hub["admin"].get(f"/api/admin/users/{u}/taste")
    assert r.status_code == 200, r.text
    taste = r.json()
    assert taste["model"] == "v2" and taste["facets"] is None and taste["embedder"] == "off"
    assert taste["counts"]["liked"] == 1 and taste["counts"]["positive"] >= 2
    r = await hub["admin"].get(f"/api/admin/users/{u}/taste/tracks", params={"filter": "liked"})
    rows = r.json()["rows"]
    assert [(x["key"], x["artist"], x["title"], x["owned"], x["liked"]) for x in rows] == [
        ("nd:ndK", "Кино", "Группа крови", True, True)]
    r = await hub["admin"].get(f"/api/admin/users/{u}/taste/tracks", params={"sort": "title", "q": "АНГЕЛ"})
    assert [x["title"] for x in r.json()["rows"]] == ["Беспечный ангел"]
    r = await hub["admin"].get(f"/api/admin/users/{u}/taste/track", params={"key": "dz:100"})  # an alias
    track = r.json()
    assert track["key"] == "nd:ndK" and track["formula"].startswith("s = sat(P) − N")
    p = sum(c["value"] for c in track["contributions"] if c["side"] == "P")
    assert p == pytest.approx(track["p"]) and track["s"] == pytest.approx(saturation(p) - track["n"])
    assert [c["kind"] for c in track["contributions"]][:2] == ["like", "finish"] or \
        {c["kind"] for c in track["contributions"]} >= {"like", "finish", "start"}
    assert [(x["mix_id"], x["state"], x["outcome"]) for x in track["mixes"]] == [(1, "active", "liked")]
    assert track["artist"]["name"] == "Кино" and track["facet_cos"] is None
    assert track["rest"]["favorites_after"] is not None


async def test_matrix_joins_verified_aliases_only_and_leaves_out_the_qa_user(hub):
    ids = hub["ids"]
    at = NOW - timedelta(days=1)
    # anna knows the song only by the catalog ref, boris by a verified pair (an event with both ids)
    await add(attempt(ids["anna"], at, "dz:500", context="search"),
              attempt(ids["boris"], at, "dz:500", "ndQ", context="search"),
              # the same «artist|title» on another ref stays a separate row
              attempt(ids["boris"], at + timedelta(hours=1), "dz:501", context="search",
                      track_metadata={"title": "Песня", "artist_credit": "Группа"}),
              attempt(ids["anna"], at + timedelta(hours=2), "dz:502", end="skip", heard_ms=5_000, position=5,
                      context="search"),
              attempt(ids["boris"], at + timedelta(hours=2), "dz:502", context="search"),
              attempt(ids["qa"], at, "dz:500", context="search"))
    r = await hub["admin"].get("/api/admin/taste/matrix")
    assert r.status_code == 200, r.text
    m = r.json()
    assert [u["username"] for u in m["users"]] == ["admin", "anna", "boris"]
    rows = {x["key"]: x for x in m["rows"]}
    assert set(rows["nd:ndQ"]["cells"]) == {str(ids["anna"]), str(ids["boris"])}
    assert "dz:501" in rows and rows["dz:501"]["title"] == "Песня"
    shared = (await hub["admin"].get("/api/admin/taste/matrix", params={"filter": "shared_liked"})).json()
    assert [x["key"] for x in shared["rows"]] == ["nd:ndQ"]
    single = (await hub["admin"].get("/api/admin/taste/matrix", params={"filter": "single"})).json()
    assert {x["key"] for x in single["rows"]} == {"dz:501"}
    by_boris = (await hub["admin"].get("/api/admin/taste/matrix", params={"sort": f"user:{ids['boris']}"})).json()
    boris = [x["cells"][str(ids["boris"])]["s"] for x in by_boris["rows"] if str(ids["boris"]) in x["cells"]]
    assert boris == sorted(boris, reverse=True) and str(ids["boris"]) in by_boris["rows"][0]["cells"]
    with_qa = (await hub["admin"].get("/api/admin/taste/matrix", params={"include_excluded": "true"})).json()
    assert [u["username"] for u in with_qa["users"]] == ["admin", "anna", "boris", "qa"]
    artists = (await hub["admin"].get("/api/admin/taste/matrix", params={"by": "artist"})).json()
    assert artists["by"] == "artist"


async def test_mix_detail_funnel_items_and_old_mixes(hub):
    m = hub["ids"]["anna"]
    await listeners(hub["ids"])
    await add(mix_row(m, 2, NOW - timedelta(days=10), closed=NOW - timedelta(days=9), old=True),
              mix_item(2, 0, ref="dz:100", nd="ndK", parts=False))
    r = await hub["admin"].get("/api/admin/mixes/1")
    assert r.status_code == 200, r.text
    mix = r.json()
    f = mix["funnel"]
    assert (f["candidates"], f["kept"], f["rejected"]["recent"], f["active"], f["reserve"]) == (60, 50, 7, 2, 1)
    assert (f["launches"], f["started"], f["heard"], f["liked"]) == (1, 2, 1, 1)
    assert [g["generator"] for g in f["generators"]] == ["ytm_radio", "lastfm_similar"]
    assert [(i["state"], i["outcome"]) for i in mix["items"]] == [("active", "liked"), ("active", "early_skip"),
                                                                  ("reserve", "not_played")]
    assert mix["items"][1]["early_skip_at"] == 10 and mix["items"][1]["title"] == "Беспечный ангел"
    assert mix["items"][0]["score_parts"]["total"] == 1.3
    assert mix["seeds"][0]["title"] == "Группа крови" and mix["seeds"][0]["facet_id"] == "f0"
    old = (await hub["admin"].get("/api/admin/mixes/2")).json()
    assert old["funnel"]["rejected"] is None and old["items"][0]["score_parts"] is None
    listing = (await hub["admin"].get(f"/api/admin/users/{m}/mixes")).json()
    assert [(x["id"], x["launches"], x["likes"]) for x in listing] == [(1, 1, 1), (2, 0, 0)]


async def test_wave_list_and_detail(hub):
    m = hub["ids"]["anna"]
    await listeners(hub["ids"])
    r = await hub["admin"].get(f"/api/admin/users/{m}/waves")
    assert r.status_code == 200, r.text
    waves = r.json()
    assert [(x["session_id"], x["seed_kind"], x["restart"], x["reconstructed"]) for x in waves["rows"]] == [
        ("w2", "own", True, False), ("w1", "own", False, False), ("gone", "unknown", False, True)]
    assert (waves["launches"], waves["restarts"]) == (2, 1)
    only = (await hub["admin"].get(f"/api/admin/users/{m}/waves", params={"origin": "home_card"})).json()
    assert [x["session_id"] for x in only["rows"]] == ["w1"]
    w = (await hub["admin"].get("/api/admin/waves/w1")).json()
    assert [(i["title"], i["generator"], i["outcome"]) for i in w["items"]] == [
        ("Полковнику", "favorites", "finished"), ("Беспечный ангел", "ytm_radio", "not_played")]
    assert w["gens"]["favorites"]["title"] and [e["kind"] for e in w["events"]] == ["start", "progress", "finish"]
    assert w["origin"] == "home_card" and w["next_calls"] == 2 and w["heard"] == 1


async def test_overview_rows_and_family_totals(hub):
    await listeners(hub["ids"])
    await add(attempt(hub["ids"]["qa"], NOW - timedelta(hours=3), "dz:100", "ndK", context="album"))
    r = await hub["admin"].get("/api/admin/insights/overview", params={"from": "2026-03-01", "to": "2026-03-15"})
    assert r.status_code == 200, r.text
    o = r.json()
    rows = {x["user"]["username"]: x for x in o["users"]}
    assert set(rows) == {"admin", "anna", "boris"}
    anna = rows["anna"]
    assert (anna["mix_launches"], anna["wave_launches"], anna["wave_restarts"], anna["likes"]) == (1, 2, 1, 1)
    assert anna["app_version"] == "1.12.0" and o["totals"]["counted_listens"] == anna["counted_listens"]
    with_qa = (await hub["admin"].get("/api/admin/insights/overview",
                                      params={"from": "2026-03-01", "include_excluded": "true"})).json()
    qa = next(x for x in with_qa["users"] if x["user"]["username"] == "qa")
    assert qa["user"]["excluded"] and qa["counted_listens"] == 1
    assert with_qa["totals"]["counted_listens"] == anna["counted_listens"]


async def test_admin_insights_do_not_change_what_the_admin_reads_twice(hub):
    """Two reads in a row answer the same: nothing a read does feeds back into the next one."""
    await listeners(hub["ids"])
    u = hub["ids"]["anna"]
    first = (await hub["admin"].get(f"/api/admin/users/{u}/listening")).json()
    second = (await hub["admin"].get(f"/api/admin/users/{u}/listening")).json()
    assert first == second

