"""Listening data (MIXES.md 3.2): player events, «Не нравится», Navidrome's scrobble history.

The library has «Кино — Группа крови» (catalog dz:100, Navidrome song "ndK", 245 s) and a song known only to
Navidrome ("ndX"). Navidrome is a mock that keeps the user's scrobbles (`state["scrobbles"]`).
"""

from __future__ import annotations

import asyncio
import io
import uuid
import wave
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from tests.support import app_client

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def ev(kind: str, *, at: datetime | None = None, **kw) -> dict:
    return {"event_id": uuid.uuid4().hex, "ts": iso(at or datetime.now(UTC)), "kind": kind, "client": "app/1.5.0", **kw}


@pytest.fixture
async def env(hub_env):
    from app.main import create_app

    state: dict = {"scrobbles": [], "logins": 0}

    def nd_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            state["logins"] += 1
            return httpx.Response(200, json={"token": "jwt", "id": "nd-user-1", "username": "admin"})
        if request.url.path == "/api/scrobble":
            p = request.url.params
            assert request.headers["x-nd-authorization"] == "Bearer jwt" and p["user_id"] == "nd-user-1"
            assert (p["_sort"], p["_order"]) == ("id", "DESC")
            rows = sorted(state["scrobbles"], key=lambda x: -x["id"])
            return httpx.Response(200, json=rows[int(p["_start"]):int(p["_end"])])
        ok = {"status": "ok", "version": "1.16.1"}
        if request.url.path.endswith("getUser"):
            return httpx.Response(200, json={"subsonic-response": {**ok, "user": {"adminRole": True}}})
        if request.url.path.endswith("getStarred2"):
            return httpx.Response(200, json={"subsonic-response": {**ok, "starred2": {}}})
        if request.url.path.endswith("unstar"):
            state.setdefault("unstarred", []).append(request.url.params.get_list("id"))
        return httpx.Response(200, json={"subsonic-response": ok})

    app = create_app()
    async with app.router.lifespan_context(app):
        with respx.mock(assert_all_called=False) as mock:
            mock.route(host="navidrome.test").mock(side_effect=nd_handler)
            async with app_client(app) as client:
                await add_track("Кино", "Группа крови", "ndK", deezer_track_id="100")
                await add_track("Кто-то", "Своё", "ndX")
                yield {"client": client, "state": state}


async def add_track(artist: str, title: str, nd_song_id: str, duration_s: int = 245, **kw) -> int:
    from app.db import session_scope
    from app.matching.normalize import fingerprint
    from app.models import Track

    async with session_scope() as s:
        t = Track(uid=str(uuid.uuid4()), path=f"{artist}/{title}.mp3", nd_song_id=nd_song_id, artist=artist,
                  title=title, duration_s=duration_s, fingerprint=fingerprint(artist, title), **kw)
        s.add(t)
        await s.flush()
        return t.id


async def stored() -> list:
    from app.db import session_scope
    from app.models import PlayEvent

    async with session_scope() as s:
        return list((await s.execute(select(PlayEvent).order_by(PlayEvent.ts, PlayEvent.id))).scalars())


async def post(c: httpx.AsyncClient, *events: dict) -> dict:
    r = await c.post("/api/plays/events", json={"events": list(events)})
    assert r.status_code == 200, r.text
    return r.json()


async def test_checkpoints_are_cumulative_and_retries_are_idempotent(env):
    c = env["client"]
    attempt = dict(ref="dz:100", playback_id="attempt-1", device_id="device-1", play_trigger="manual")
    start = ev("start", listened_ms=0, **attempt)
    first = ev("progress", listened_ms=10000, **attempt)
    later = ev("progress", listened_ms=22000, **attempt)
    terminal = ev("interrupted", listened_ms=25000, **attempt)
    assert (await post(c, start, later, first, terminal, later))["accepted"] == 4
    assert (await post(c, {**first, "play_trigger": "auto"}, terminal))["duplicates"] == 2
    rows = [x for x in await stored() if x.playback_id == "attempt-1"]
    assert max(x.listened_ms for x in rows) == 25000
    assert {x.device_id for x in rows} == {"device-1"}
    assert {x.play_trigger for x in rows} == {"manual"}
    assert (await post(c, ev("progress", ref="dz:100", listened_ms=10)))["rejected"]
    assert (await post(c, ev("finish", ref="dz:100", position_s=100)))["accepted"] == 1
    legacy = next(x for x in await stored() if x.position_s == 100)
    assert legacy.listened_ms is None and legacy.playback_id is None and legacy.play_trigger is None
    response = await post(c, ev("finish", ref="dz:100", play_trigger="repeat"),
                          ev("finish", ref="dz:100", play_trigger="search"), ev("like", ref="dz:100", play_trigger="manual"))
    assert response["accepted"] == 1 and [r["index"] for r in response["rejected"]] == [1, 2]


async def test_listening_metadata_survives_library_deletion(env):
    from app.db import session_scope
    from app.models import Track

    c = env["client"]
    event = ev("start", nd_song_id="ndK", playback_id="play", device_id="device", listened_ms=0,
               track_metadata={"artists": [{"name": "Кино", "ref": "dz:7"}]})
    assert (await post(c, event))["accepted"] == 1
    async with session_scope() as s:
        track = (await s.execute(select(Track).where(Track.nd_song_id == "ndK"))).scalar_one()
        await s.delete(track)
    row = next(x for x in await stored() if x.event_id == event["event_id"])
    assert row.track_metadata["title"] == "Группа крови"
    assert row.track_metadata["artists"] == [{"name": "Кино", "ref": "dz:7"}]
    assert row.track_metadata["provenance"]["title"] == "library"
    assert row.track_metadata["provenance"]["artists"] == "client"


async def test_catalog_artists_are_snapshotted_before_cache_removal(env):
    from app.db import session_scope
    from app.models import CatalogCache, utcnow

    async with session_scope() as s:
        s.add(CatalogCache(key="dz:track:100", payload_json={"ref": "dz:100", "artist": "A",
            "artist_ref": "dz:1", "artists": ["A", "B"]}, fetched_at=utcnow(), ttl_s=3600))
    event = ev("start", ref="dz:100", track_metadata={"artists": [{"name": "A"}]})
    assert (await post(env["client"], event))["accepted"] == 1
    async with session_scope() as s:
        row = await s.get(CatalogCache, "dz:track:100")
        await s.delete(row)
    snapshot = next(x.track_metadata for x in await stored() if x.event_id == event["event_id"])
    assert snapshot["artists"] == [{"name": "A", "ref": "dz:1"}, {"name": "B"}]
    assert snapshot["provenance"]["artists"] == "catalog_cache+client"


async def test_summary_uses_cumulative_max_and_distinguishes_legacy(env):
    from zoneinfo import ZoneInfo

    c = env["client"]
    now = datetime.now(UTC)
    target = {"ref": "dz:100", "device_id": "device", "playback_id": "attempt"}
    now -= timedelta(seconds=5)
    await post(c, ev("start", at=now, listened_ms=0, **target),
               ev("progress", at=now + timedelta(seconds=1), listened_ms=32000, **target),
               ev("interrupted", at=now + timedelta(seconds=2), listened_ms=33000, **target),
               ev("finish", at=now, ref="dz:999", position_s=100))
    day = now.astimezone(ZoneInfo("Europe/Moscow")).date().isoformat()
    response = await c.get("/api/plays/summary", params={"start": day, "end": day, "timezone": "Europe/Moscow"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["measured_attempts"], body["measured_listens"], body["measured_ms"]) == (1, 1, 33000)
    assert body["legacy_finish_evidence"] == 1
    assert body["counted_listens"] == 1
    assert body["tracks"][0]["metadata"]["title"] == "Группа крови"
    assert (await c.get("/api/plays/summary", params={"start": day, "end": day, "timezone": "not/a-zone"})).status_code == 422


async def test_summary_excludes_future_checkpoints(env):
    c = env["client"]
    now = datetime.now(UTC)
    target = {"ref": "dz:100", "device_id": "dev", "playback_id": "future"}
    await post(c, ev("start", at=now, listened_ms=0, **target),
               ev("progress", at=now + timedelta(minutes=30), listened_ms=40000, **target))
    day = now.date().isoformat()
    response = await c.get("/api/plays/summary", params={"start": day, "end": day})
    assert response.status_code == 200, response.text
    assert response.json()["measured_ms"] == 0
    assert response.json()["measured_listens"] == 0


async def test_summary_unifies_verified_aliases_after_library_deletion(env):
    from app.db import session_scope
    from app.models import Track

    c = env["client"]
    now = datetime.now(UTC) - timedelta(seconds=10)
    async with session_scope() as s:
        track = (await s.execute(select(Track).where(Track.nd_song_id == "ndK"))).scalar_one()
        await s.delete(track)
    await post(c, ev("finish", at=now, ref="dz:100", device_id="dev", playback_id="a", listened_ms=30000))
    await post(c, ev("finish", at=now, ref="dz:100", nd_song_id="ndK", device_id="dev", playback_id="b", listened_ms=40000))
    day = now.date().isoformat()
    response = await c.get("/api/plays/summary", params={"start": day, "end": day})
    assert response.status_code == 200, response.text
    assert len(response.json()["tracks"]) == 1
    assert response.json()["tracks"][0]["measured_ms"] == 70000


async def test_summary_keeps_rich_metadata_when_later_checkpoint_is_sparse(env):
    c = env["client"]
    now = datetime.now(UTC) - timedelta(seconds=5)
    target = {"ref": "dz:100", "device_id": "dev", "playback_id": "rich"}
    await post(c, ev("start", at=now, listened_ms=0, track_metadata={"artists": [
        {"name": "A", "ref": "dz:1"}, {"name": "B", "ref": "dz:2"}]}, **target),
        ev("progress", at=now + timedelta(seconds=1), listened_ms=30000,
           track_metadata={"artists": [{"name": "A"}]}, **target))
    day = now.date().isoformat()
    response = await c.get("/api/plays/summary", params={"start": day, "end": day})
    metadata = response.json()["tracks"][0]["metadata"]
    assert [artist["ref"] for artist in metadata["artists"]] == ["dz:1", "dz:2"]
    assert metadata["provenance"]["artists"] == "client"


async def test_summary_assigns_checkpoint_deltas_across_midnight(env):
    c = env["client"]
    day = (datetime.now(UTC) - timedelta(days=2)).date()
    midnight = datetime.combine(day + timedelta(days=1), datetime.min.time(), UTC)
    target = {"ref": "dz:100", "device_id": "dev", "playback_id": "boundary"}
    await post(c, ev("start", at=midnight - timedelta(seconds=30), listened_ms=0, **target),
        ev("progress", at=midnight - timedelta(seconds=15), listened_ms=15000, **target),
        ev("progress", at=midnight + timedelta(seconds=15), listened_ms=45000, **target),
        ev("finish", at=midnight + timedelta(seconds=20), listened_ms=50000, **target))
    first = (await c.get("/api/plays/summary", params={"start": str(day), "end": str(day)})).json()
    second_day = day + timedelta(days=1)
    second = (await c.get("/api/plays/summary", params={"start": str(second_day), "end": str(second_day)})).json()
    assert (first["measured_ms"], second["measured_ms"]) == (15000, 35000)
    assert (first["measured_listens"], second["measured_listens"]) == (0, 1)


async def test_expected_account_rejects_cross_account_delivery(env):
    c = env["client"]
    r = await c.post("/api/plays/events", json={"expected_user_id": 999999, "events": [ev("start", ref="dz:100")]})
    assert r.status_code == 409
    assert not await stored()


async def test_typed_favorites_and_old_star_payload_share_one_state(env):
    from app.db import session_scope
    from app.favorites import snapshot
    from app.models import User

    c = env["client"]
    r = await c.post("/api/star", json={"song_ids": ["ndK"], "event_id": "decision"})
    assert r.status_code == 200, r.text
    assert r.json()["event_ids"] == ["decision"]
    assert (await post(c, ev("like", event_id="decision", nd_song_id="ndK")))["duplicates"] == 1
    out = await post(c, ev("like", target_type="album", ref="dz:100"),
                     ev("like", target_type="artist", ref="dz:100", target_data={"name": "Кино"}),
                     ev("start", target_type="artist", ref="dz:100"))
    assert out["accepted"] == 2 and len(out["rejected"]) == 1
    async with session_scope() as s:
        user = (await s.execute(select(User))).scalars().first()
        states = await snapshot(s, user.id)
    assert len(states) == 3 and all(x.liked for x in states.values())
    assert "typed_favorites_v1" in (await c.get("/healthz")).json()["capabilities"]


async def test_own_pending_unlike_is_in_starred_state(env):
    c = env["client"]
    r = await c.post("/api/star", json={"targets": [{"target_type": "album", "ref": "dz:500"}], "event_id": "album"})
    assert r.status_code == 200, r.text
    await post(c, ev("like", ref="hm:pending"))
    body = (await c.get("/api/starred")).json()
    assert body["refs"] == ["hm:pending"]
    assert {x["target_type"] for x in body["subjects"]} == {"track", "album"}
    assert (await c.post("/api/star", json={"refs": ["hm:pending"], "unstar": True})).status_code == 200
    assert (await c.get("/api/starred")).json()["refs"] == []


async def test_catalog_favorite_download_is_idempotent_and_unlike_cancels_pending(env):
    from app.db import session_scope
    from app.models import DownloadBatch, Job

    c = env["client"]
    like = ev("like", event_id="catalog-like", ref="dz:987")
    await post(c, like)  # Telemetry may win the race with the command.
    body = {"track_refs": ["dz:987"], "star": True, "favorite_event_id": like["event_id"],
            "favorite_ts": like["ts"], "favorite_client": like["client"]}
    first = await c.post("/api/downloads", json=body)
    assert first.status_code == 200, first.text
    again = await c.post("/api/downloads", json=body)
    assert again.status_code == 200 and again.json()["batch_id"] == first.json()["batch_id"]
    unlike = ev("unlike", event_id="catalog-unlike", ref="dz:987")
    await post(c, unlike)
    response = await c.post("/api/star", json={"refs": ["dz:987"], "unstar": True,
                                                "event_id": unlike["event_id"], "ts": unlike["ts"]})
    assert response.status_code == 200, response.text
    assert "dz:987" not in (await c.get("/api/starred")).json()["refs"]
    async with session_scope() as s:
        jobs = (await s.execute(select(Job).where(Job.kind == "expand_batch"))).scalars().all()
        batches = (await s.execute(select(DownloadBatch).where(DownloadBatch.origin == "favorite-app"))).scalars().all()
    assert len(jobs) == len(batches) == 1
    assert jobs[0].payload_json["favorite_events"] == {"dz:987": "catalog-like"}
    assert [e.event_id for e in await stored() if e.ref == "dz:987"] == ["catalog-like", "catalog-unlike"]


async def test_late_catalog_like_does_not_enqueue_after_newer_unlike(env):
    from app.db import session_scope
    from app.models import Job

    c = env["client"]
    now = datetime.now(UTC)
    await post(c, ev("unlike", at=now, event_id="newer", ref="dz:988"))
    r = await c.post("/api/downloads", json={"track_refs": ["dz:988"], "star": True,
        "favorite_event_id": "older", "favorite_ts": iso(now - timedelta(seconds=1))})
    assert r.status_code == 200, r.text
    assert "dz:988" not in (await c.get("/api/starred")).json()["refs"]
    async with session_scope() as s:
        assert not (await s.execute(select(Job).where(Job.kind == "expand_batch"))).scalars().all()


async def test_compensation_before_expansion_cancels_quiet_download(env):
    from app.db import session_scope
    from app.jobs.downloads import create_items
    from app.models import DownloadBatch, DownloadItem, Job
    from app.providers.base import TrackQuery

    c = env["client"]
    r = await c.post("/api/downloads", json={"track_refs": ["dz:990"], "star": True,
                                               "favorite_event_id": "original"})
    assert r.status_code == 200, r.text
    await post(c, ev("unlike", event_id="undo", ref="dz:990", reverts_event_id="original"))
    async with session_scope() as s:
        batch = await s.get(DownloadBatch, r.json()["batch_id"])
        await create_items(s, batch, [(TrackQuery(artist="A", title="T"),
                                      {"track_ref": "dz:990"}, None)], star=True,
                           favorite_events={"dz:990": "original"})
        item = (await s.execute(select(DownloadItem).where(DownloadItem.batch_id == batch.id))).scalar_one()
        assert item.status == "cancelled" and item.query_json["star_cancelled"]
        assert not (await s.execute(select(Job).where(Job.kind == "download_item"))).scalars().all()


async def test_catalog_like_joins_explicit_download_without_changing_its_visibility(env):
    from app.db import session_scope
    from app.models import DownloadBatch, DownloadItem, User

    c = env["client"]
    async with session_scope() as s:
        user = (await s.execute(select(User))).scalars().first()
        batch = DownloadBatch(user_id=user.id, origin="catalog")
        s.add(batch)
        await s.flush()
        item = DownloadItem(user_id=user.id, batch_id=batch.id, status="queued",
                            query_json={"query": {}, "meta": {"track_ref": "dz:991"}, "requested": True})
        s.add(item)
        await s.flush()
        item_id = item.id
    r = await c.post("/api/downloads", json={"track_refs": ["dz:991"], "star": True,
                                               "favorite_event_id": "joined"})
    assert r.status_code == 200, r.text
    unlike = ev("unlike", event_id="joined-unlike", ref="dz:991")
    await post(c, unlike)
    r = await c.post("/api/star", json={"refs": ["dz:991"], "unstar": True, "event_id": "joined-unlike"})
    assert r.status_code == 200, r.text
    async with session_scope() as s:
        item = await s.get(DownloadItem, item_id)
        assert item.status == "queued" and item.query_json["requested"]
        assert not item.query_json["star"] and item.query_json["star_cancelled"]
    assert [e.event_id for e in await stored() if e.ref == "dz:991"] == ["joined", "joined-unlike"]


async def test_compensation_cancels_active_quiet_download(env):
    from app.db import session_scope
    from app.models import DownloadBatch, DownloadItem, User

    c = env["client"]
    async with session_scope() as s:
        user = (await s.execute(select(User))).scalars().first()
        batch = DownloadBatch(user_id=user.id, origin="catalog")
        s.add(batch)
        await s.flush()
        item = DownloadItem(user_id=user.id, batch_id=batch.id, status="queued",
                            query_json={"query": {}, "meta": {"track_ref": "dz:992"}, "star": True})
        s.add(item)
        await s.flush()
        item_id = item.id
    r = await c.post("/api/downloads", json={"track_refs": ["dz:992"], "star": True,
                                               "favorite_event_id": "quiet"})
    assert r.status_code == 200, r.text
    await post(c, ev("unlike", event_id="quiet-undo", ref="dz:992", reverts_event_id="quiet"))
    async with session_scope() as s:
        item = await s.get(DownloadItem, item_id)
        assert item.status == "cancelled" and item.query_json["star_cancelled"]


async def test_compensation_arriving_before_catalog_command_does_not_enqueue(env):
    from app.db import session_scope
    from app.models import Job

    c = env["client"]
    await post(c, ev("unlike", event_id="early-undo", ref="dz:993", reverts_event_id="late-like"))
    r = await c.post("/api/downloads", json={"track_refs": ["dz:993"], "star": True,
                                               "favorite_event_id": "late-like"})
    assert r.status_code == 200, r.text
    async with session_scope() as s:
        assert not (await s.execute(select(Job).where(Job.kind == "expand_batch"))).scalars().all()


async def test_catalog_unlike_mirrors_to_navidrome_when_telemetry_arrived_first(env):
    c = env["client"]
    unlike = ev("unlike", event_id="owned-unlike", ref="dz:100")
    await post(c, unlike)
    r = await c.post("/api/star", json={"refs": ["dz:100"], "unstar": True,
                                       "event_id": "owned-unlike", "ts": unlike["ts"]})
    assert r.status_code == 200, r.text
    assert env["state"]["unstarred"] == [["ndK"]]


async def test_scrobble_dedup_preserves_synthetic_likes_and_bounds_future(env):
    from app.db import session_scope
    from app.models import PlayEvent, User, utcnow
    from app.plays import user_events

    now = utcnow()
    async with session_scope() as s:
        user = (await s.execute(select(User))).scalars().first()
        s.add(PlayEvent(user_id=user.id, event_id="ndstar", kind="like", target_type="track",
                        ref="dz:100", nd_song_id="ndK", ts=now, received_at=now, client="navidrome"))
    await post(env["client"], ev("finish", nd_song_id="ndK", at=now.replace(tzinfo=UTC)),
               ev("unlike", nd_song_id="ndK", at=(now + timedelta(minutes=30)).replace(tzinfo=UTC)))
    async with session_scope() as s:
        rows = await user_events(s, user.id, now=now + timedelta(seconds=1))
    assert {e.kind for e in rows} == {"finish", "like"}


async def test_favorites_sync_still_runs_when_scrobble_import_fails(env, monkeypatch):
    from unittest.mock import AsyncMock
    from app import favorites, plays
    monkeypatch.setattr(plays, "import_scrobbles", AsyncMock(side_effect=TimeoutError()))
    sync = AsyncMock(return_value=0)
    monkeypatch.setattr(favorites, "sync_user", sync)
    assert await plays.sync_navidrome() == 0
    sync.assert_awaited_once()


async def test_events_are_stored_once_with_both_ids(env):
    c = env["client"]
    batch = [
        ev("start", ref="dz:100", context="album"),
        ev("skip", ref="dz:100", position_s=12.5, duration_s=245, context="album"),
        ev("finish", nd_song_id="ndK", position_s=245, duration_s=245, context="mix:7"),
        ev("like", ref="hm:555"),  # not in the library: the ref alone
    ]
    assert await post(c, *batch) == {"accepted": 4, "duplicates": 0, "rejected": []}
    # the app sends the batch again (no answer reached it): nothing doubles
    assert await post(c, *batch, ev("finish", ref="dz:100")) == {"accepted": 1, "duplicates": 4, "rejected": []}

    rows = await stored()
    assert len(rows) == 5
    by_id = {r.event_id: r for r in rows}
    skip = by_id[batch[1]["event_id"]]
    assert (skip.kind, skip.ref, skip.nd_song_id, skip.position_s, skip.context, skip.client) == (
        "skip", "dz:100", "ndK", 12.5, "album", "app/1.5.0")
    assert (by_id[batch[2]["event_id"]].ref, by_id[batch[3]["event_id"]].nd_song_id) == ("dz:100", None)


async def test_malformed_event_is_rejected_alone(env):
    c = env["client"]
    good, trackless = ev("finish", ref="dz:100"), ev("start")
    out = await post(c, {"event_id": "x1", "ts": iso(NOW), "kind": "pause", "ref": "dz:100", "client": "pwa"},
                     trackless, good, "junk")
    assert out["accepted"] == 1 and out["duplicates"] == 0
    assert [(x["index"], x["event_id"]) for x in out["rejected"]] == [(0, "x1"), (1, trackless["event_id"]),
                                                                       (3, None)]
    assert "kind" in out["rejected"][0]["detail"] and "nd_song_id" in out["rejected"][1]["detail"]
    assert [r.event_id for r in await stored()] == [good["event_id"]]


async def test_wrong_client_clock_gets_the_time_the_event_came(env):
    c = env["client"]
    past = datetime.now(UTC) - timedelta(days=3)  # an offline queue: kept as it is
    await post(c, ev("finish", ref="dz:100", at=datetime.now(UTC) + timedelta(days=2)),
               ev("finish", ref="dz:100", at=past),
               ev("finish", ref="dz:100", at=datetime(2001, 1, 1, tzinfo=UTC)))
    now = datetime.now(UTC).replace(tzinfo=None)
    times = sorted(r.ts for r in await stored())
    assert times[0] == past.replace(tzinfo=None)
    assert all(abs(t - now) < timedelta(minutes=1) for t in times[1:])


async def test_dislike_event_marks_the_track_and_undislike_clears_it(env):
    c = env["client"]
    await post(c, ev("dislike", nd_song_id="ndK", context="queue"))
    rows = (await c.get("/api/dislikes")).json()
    assert [(x["ref"], x["nd_song_id"], x["source"]) for x in rows] == [("dz:100", "ndK", "app")]
    await post(c, ev("undislike", ref="dz:100"))
    assert (await c.get("/api/dislikes")).json() == []


@pytest.mark.parametrize(("action", "ref"), [("dislike", "file:hitmoz:123"), ("like-event", "file:hitmoz:123"),
                                             ("desktop-heart", "dz:987")])
async def test_feedback_uploads_the_middle_of_the_heard_copy_without_waiting_for_analysis(
        env, monkeypatch, tmp_path, action, ref):
    from app import listen
    from app.config import get_settings

    base = "http://embedder.test"
    monkeypatch.setattr(get_settings(), "embedder_url", base)
    audio = tmp_path / "heard.wav"
    rate = 24000
    middle = b"\x11\x22" * (30 * rate)
    with wave.open(str(audio), "wb") as f:
        f.setparams((1, 2, rate, 0, "NONE", ""))
        f.writeframes(b"\0\0" * rate + middle + b"\0\0" * rate)
    monkeypatch.setattr(listen, "open_heard_wave_recording",
                        lambda user, key: (audio.open("rb"), 32) if key == ref else None)
    release = asyncio.Event()

    async def status(_):
        await release.wait()
        return httpx.Response(404)

    uploaded = asyncio.get_running_loop().create_future()

    def accept(request):
        uploaded.set_result(request)
        return httpx.Response(200, json={"queued": 1, "known": 0, "rejected": []})

    with respx.mock() as remote:
        remote.get(host="embedder.test", path=f"/embed/{ref}").mock(side_effect=status)
        remote.post(f"{base}/embed/clip", params={"key": ref}).mock(side_effect=accept)
        async with asyncio.timeout(10):
            client = env["client"]
            if action == "like-event":
                result = await post(client, ev("like", ref=ref, context="wave:session"))
                assert result["accepted"] == 1
            elif action == "desktop-heart":
                r = await client.post("/api/downloads", json={"track_refs": [ref], "star": True})
                assert r.status_code == 200, r.text
            else:
                r = await client.post("/api/dislikes", json={"ref": ref, "client": "desktop"})
                assert r.status_code == 200 and r.json()["nd_song_id"] is None
            audio.unlink()  # eviction during the status lookup must not lose or change the recording
            release.set()
            request = await uploaded
        assert request.headers["content-type"] == "audio/wav"
        with wave.open(io.BytesIO(request.content)) as clip:
            assert clip.getframerate() == rate and clip.getnframes() == 30 * rate
            assert clip.readframes(clip.getnframes()) == middle


async def test_dislikes_api_tracks_and_artists(env):
    c = env["client"]
    r = await c.post("/api/dislikes", json={"ref": "dz:100", "artist": "Кино", "title": "Группа крови",
                                            "context": "search"})
    assert r.status_code == 200, r.text
    assert (r.json()["nd_song_id"], r.json()["source"]) == ("ndK", "pwa")
    # the same track by its other id is the same row
    r = await c.post("/api/dislikes", json={"nd_song_id": "ndK"})
    assert r.status_code == 200 and r.json()["title"] == "Группа крови"
    r = await c.post("/api/dislikes", json={"artist_ref": "dz:artist:5", "artist": "Кто-то"})
    assert r.status_code == 200, r.text
    artist_id = r.json()["id"]
    for bad in ({}, {"ref": "dz:100", "artist_ref": "dz:artist:5"}):
        assert (await c.post("/api/dislikes", json=bad)).status_code == 422

    rows = (await c.get("/api/dislikes")).json()
    assert [(x["ref"], x["artist_ref"]) for x in rows] == [(None, "dz:artist:5"), ("dz:100", None)]  # newest first
    assert [(e.kind, e.target_type, e.ref, e.nd_song_id, e.client, e.context) for e in await stored()] == [
        ("dislike", "track", "dz:100", "ndK", "pwa", "search"),
        ("dislike", "track", "dz:100", "ndK", "pwa", None),
        ("dislike", "artist", "dz:artist:5", None, "pwa", None)]

    r = await c.delete("/api/dislikes", params={"nd_song_id": "ndK"})
    assert r.json() == {"removed": 1}
    assert (await c.delete("/api/dislikes")).status_code == 400
    assert (await c.delete(f"/api/dislikes/{artist_id}")).json() == {"removed": 1}
    assert (await c.delete(f"/api/dislikes/{artist_id}")).status_code == 404
    assert (await c.get("/api/dislikes")).json() == []
    assert [e.kind for e in await stored()][-1] == "undislike"
    assert [(e.kind, e.target_type) for e in await stored()][-2:] == [("undislike", "track"), ("undislike", "artist")]


async def test_navidrome_scrobbles_become_finishes_without_doubles(env):
    from app.plays import sync_navidrome

    c, state = env["client"], env["state"]
    now = datetime.now(UTC)
    unix = lambda dt: int(dt.timestamp())  # noqa: E731
    # the app reported this listen itself: its scrobble (at half the track) is the same play
    await post(c, ev("finish", nd_song_id="ndK", at=now - timedelta(hours=2), position_s=245, duration_s=245))
    state["scrobbles"] = [
        {"id": 1, "mediaFileId": "ndK", "submissionTime": unix(now - timedelta(hours=2, minutes=2))},
        {"id": 2, "mediaFileId": "ndX", "submissionTime": unix(now - timedelta(hours=1))},  # the web UI
        {"id": 3, "mediaFileId": "ndK", "submissionTime": unix(now - timedelta(minutes=30))},
    ]
    assert await sync_navidrome() == 2
    nd = [e for e in await stored() if e.client == "navidrome" and not e.superseded_scrobble]
    assert [(e.event_id, e.kind, e.nd_song_id, e.ref, e.duration_s) for e in nd] == [
        ("nd:2", "finish", "ndX", None, 245), ("nd:3", "finish", "ndK", "dz:100", 245)]

    assert all(e.position_s is None for e in nd)  # a scrobble does not measure playback position

    # the next hour reads only what is new
    state["scrobbles"].append({"id": 4, "mediaFileId": "ndX", "submissionTime": unix(now)})
    assert await sync_navidrome() == 1
    assert state["logins"] == 1  # the token is kept

    # the app was offline and sends the listen that Navidrome already counted: the app's events win
    await post(c, ev("finish", nd_song_id="ndK", at=now - timedelta(minutes=28), position_s=245, duration_s=245))
    assert [e.event_id for e in await stored() if e.client == "navidrome" and not e.superseded_scrobble] == ["nd:2", "nd:4"]
    assert {e.event_id for e in await stored() if e.superseded_scrobble} == {"nd:1", "nd:3"}
    assert await sync_navidrome() == 0


async def test_import_likes_do_not_count_unless_liked_again(env):
    from app.db import session_scope
    from app.models import Import, User
    from app.plays import imported_likes, user_events

    c = env["client"]
    async with session_scope() as s:
        uid = (await s.execute(select(User.id))).scalar_one()
        s.add(Import(user_id=uid, source="yandex", method="token", options_json={"liked": ["ndK", "ndX", "ndZ"]}))
    await post(c, ev("like", nd_song_id="ndX"), ev("finish", ref="dz:100", at=datetime.now(UTC) - timedelta(days=40)),
               ev("skip", ref="dz:100", position_s=5))
    async with session_scope() as s:
        assert await imported_likes(s, uid) == {"ndK", "ndZ"}
        assert [e.kind for e in await user_events(s, uid, days=30)] == ["like", "skip"]
        assert [e.kind for e in await user_events(s, uid, days=60, kinds=["finish", "skip"])] == ["finish", "skip"]


async def test_plays_need_login(env):
    from app.main import create_app

    async with app_client(create_app(), login=None) as anon:
        assert (await anon.post("/api/plays/events", json={"events": []})).status_code == 401
        assert (await anon.get("/api/dislikes")).status_code == 401


def test_migration_carries_dislikes_of_past_imports(tmp_path):
    import sqlite3
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    import app.db

    db = tmp_path / "hub.sqlite3"
    root = Path(app.db.__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "a7c3e9f2b1d4")
    con = sqlite3.connect(db)
    con.execute("INSERT INTO users (id, nd_username, nd_password_enc, is_admin, created_at) "
                "VALUES (1, 'admin', 'x', 1, '2026-10-01 10:00:00')")
    con.execute("INSERT INTO tracks (id, uid, path, nd_song_id, artist, title, fingerprint, warnings, acquired_at, "
                "locked) VALUES (5, 'u5', 'a/b.mp3', 'ndA', 'А', 'Б', 'а|б', '[]', '2026-10-01 10:00:00', 0)")
    for imp in (1, 2):  # the same dislike in two imports is one row, from the first import
        con.execute("INSERT INTO imports (id, user_id, source, method, status, mode, keep_token, auth_json, "
                    "options_json, created_at, updated_at) VALUES (?, 1, 'yandex', 'token', 'done', 'economy', 0, "
                    "'{}', '{}', ?, ?)", (imp, f"2026-10-0{imp + 1} 10:00:00", f"2026-10-0{imp + 1} 10:00:00"))
        con.execute("INSERT INTO import_collections (id, import_id, kind, external_id, title, position, selected, "
                    "nd_song_ids) VALUES (?, ?, 'dislikes', 'dislikes', 'Не нравится', 0, 1, '[]')", (imp * 10, imp))
        con.execute("INSERT INTO import_collections (id, import_id, kind, external_id, title, position, selected, "
                    "nd_song_ids) VALUES (?, ?, 'likes', 'likes', 'Мне нравится', 1, 1, '[]')", (imp * 10 + 1, imp))
    rows = [(10, "dz:1", None, '{"artist": "Х", "title": "Раз"}'), (20, "dz:1", None, "{}"),
            (20, "dz:2", 5, '{"artist": "А", "title": "Б"}'), (20, None, None, "{}"),  # not found: no ref
            (21, "dz:3", None, "{}")]  # a like
    for i, (coll, ref, track_id, raw) in enumerate(rows):
        con.execute("INSERT INTO import_items (import_id, collection_id, position, external_id, raw_json, ref, "
                    "status, track_id) VALUES (?, ?, ?, ?, ?, ?, 'skipped', ?)",
                    (coll // 10, coll, i, str(i), raw, ref, track_id))
    con.commit()
    con.close()
    command.upgrade(cfg, "head")
    con = sqlite3.connect(db)
    got = con.execute("SELECT user_id, ref, nd_song_id, artist, title, source, created_at FROM dislikes "
                      "ORDER BY ref").fetchall()
    assert got == [(1, "dz:1", None, "Х", "Раз", "yandex_import", "2026-10-02 10:00:00"),
                   (1, "dz:2", "ndA", "А", "Б", "yandex_import", "2026-10-03 10:00:00")]
    con.close()
    command.downgrade(cfg, "a7c3e9f2b1d4")


async def test_late_accept_chain_has_separate_capability_and_preserves_original_date(env):
    c = env["client"]
    capabilities = (await c.get("/healthz")).json()["capabilities"]
    assert {"typed_favorites_v1", "favorite_compensation_chains_v1"} <= set(capabilities)
    at = datetime(2026, 10, 26, 12, tzinfo=UTC)
    original = ev("like", at=at, event_id="accept-a", target_type="album", ref="dz:500")
    compensation = ev("unlike", at=at + timedelta(seconds=1), event_id="accept-c",
                      target_type="album", ref="dz:500", reverts_event_id="accept-a")
    restoration = ev("like", at=at + timedelta(seconds=2), event_id="accept-r",
                    target_type="album", ref="dz:500", reverts_event_id="accept-c")
    # Every causal target may arrive after its retraction, including the original.
    assert (await post(c, restoration, compensation))["accepted"] == 2
    assert (await post(c, original))["accepted"] == 1
    assert (await post(c, compensation, restoration, original))["duplicates"] == 3
    body = (await c.get("/api/starred")).json()
    subject = next(x for x in body["subjects"] if x.get("ref") == "dz:500")
    assert subject["liked"] and subject["event_id"] == "accept-a"
    assert subject["liked_since"].startswith("2026-10-26T12:00:00")


async def test_launch_fields_are_stored_and_never_change_the_taste(env):
    from sqlalchemy import update

    from app.catalog.models import CatalogTrack
    from app.config import get_settings
    from app.db import session_scope
    from app.mixes import build
    from app.mixes.taste_math import calculate_profile
    from app.mixes.taste_snapshot import load_snapshot
    from app.models import PlayEvent, User

    c = env["client"]
    at = datetime.now(UTC) - timedelta(days=2)
    launch = dict(launch_id="launch-1", launch_origin="mix_page")
    events = [ev("start", at=at, ref="dz:100", context="mix:7", **launch),
              ev("finish", at=at + timedelta(minutes=4), ref="dz:100", position_s=245, duration_s=245,
                 context="mix:7", **launch),
              ev("skip", at=at + timedelta(minutes=5), nd_song_id="ndX", position_s=5, duration_s=200,
                 context="album", **launch),
              ev("like", at=at + timedelta(minutes=6), ref="dz:100")]
    bad = ev("start", ref="dz:100", launch_origin="Mix page")
    out = await post(c, *events, bad, ev("start", ref="dz:100", launch_id="x" * 65))
    assert out["accepted"] == 4 and [x["index"] for x in out["rejected"]] == [4, 5]
    assert [(x.launch_id, x.launch_origin) for x in await stored()] == [("launch-1", "mix_page")] * 3 + [(None, None)]

    now = datetime.now(UTC).replace(tzinfo=None)
    song = build.Cand(track=CatalogTrack(ref="dz:100", source="dz", id="100", title="Группа крови", artist="Кино",
                                         artists=["Кино"]), gens={"ytm_radio": 0}, nd_song_id="ndK")

    async def taste():
        async with session_scope() as s:
            me = (await s.execute(select(User.id).where(User.nd_username == "admin"))).scalar_one()
            p = calculate_profile(await load_snapshot(s, me, now=now), now=now, timezone="Europe/Moscow")
        return p, build.score(song, get_settings().cfg.mixes.weights, p)

    with_launch = await taste()
    async with session_scope() as s:
        await s.execute(update(PlayEvent).values(launch_id=None, launch_origin=None))
    without = await taste()
    assert with_launch[0].tracks and with_launch == without
