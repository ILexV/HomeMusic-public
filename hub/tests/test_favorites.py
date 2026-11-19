"""Permanent typed favorite state and reconciliation, with explicit clocks and no network."""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.api.schemas import FavoriteTarget, PlayEventIn
from app.favorites import command, fold, reconcile, record, snapshot
from app.models import FavoriteSubject, Import, NavidromeSync, PlayEvent, User

NOW = datetime(2026, 10, 26, 12)


def event(eid, kind, at=NOW, *, received=None, typ="track", ref="dz:1", nd=None, reverts=None, user=1):
    fields = {"track": "nd_song_id", "album": "nd_album_id", "artist": "nd_artist_id"}
    return PlayEvent(id=int(eid) if str(eid).isdigit() else 0, user_id=user, event_id=str(eid), kind=kind,
                     target_type=typ, ref=ref, **{fields[typ]: nd}, ts=at, received_at=received or at,
                     client="app/1", reverts_event_id=reverts, mirror_pending=False)


@pytest.mark.parametrize("typ", ["track", "album", "artist"])
def test_continuous_like_is_one_contribution_and_unlike_is_neutral(typ):
    rows = [event(1, "like", NOW - timedelta(days=900), typ=typ),
            event(2, "like", NOW - timedelta(days=1), typ=typ)]
    st = next(iter(fold(rows).values()))
    assert st.liked and st.since == rows[0].ts and st.event.event_id == "1"
    rows.append(event(3, "unlike", typ=typ))
    st = next(iter(fold(rows).values()))
    assert not st.liked and st.since is None
    rows.append(event(4, "like", NOW + timedelta(seconds=1), typ=typ))
    assert next(iter(fold(rows).values())).since == rows[-1].ts


@pytest.mark.parametrize("order", [(0, 1, 2), (2, 1, 0)])
def test_compensation_arriving_before_decision_cancels_legacy_duplicate(order):
    rows = [event("promise", "like"), event("legacy", "like", NOW + timedelta(seconds=1)),
            event("rollback", "unlike", NOW + timedelta(seconds=2), reverts="promise")]
    st = next(iter(fold([rows[i] for i in order]).values()))
    assert not st.liked


def test_stale_rollback_cannot_undo_new_independent_like_or_other_target():
    rows = [event(1, "like"), event(2, "unlike", NOW + timedelta(seconds=1)),
            event(3, "like", NOW + timedelta(seconds=2)),
            event(4, "unlike", NOW + timedelta(seconds=3), reverts="1"),
            event(5, "unlike", NOW + timedelta(seconds=4), ref="dz:2", reverts="3")]
    states = fold(rows)
    assert states[("track", "ref:dz:1")].liked
    assert states[("track", "ref:dz:1")].since == rows[2].ts


def test_successful_retry_after_compensation_is_a_new_series():
    rows = [event(1, "like"), event(2, "like", NOW + timedelta(seconds=1)),
            event(3, "unlike", NOW + timedelta(seconds=2), reverts="1"),
            event(4, "like", NOW + timedelta(seconds=3))]
    state = next(iter(fold(rows).values()))
    assert state.liked and state.since == rows[-1].ts


def test_aliases_merge_replaced_songs_and_keep_types_separate():
    subject = FavoriteSubject(target_type="track", canonical_key="ref:dz:1", ref="dz:1", nd_id="new",
                              aliases=["ref:dz:1", "nd:old", "nd:new"], data={})
    states = fold([event(1, "like", ref=None, nd="old"), event(2, "unlike", ref=None, nd="new"),
                   event(3, "like", typ="album", ref="dz:1")], [subject])
    assert len(states) == 2
    assert not states[("track", "nd:new")].liked
    assert states[("album", "ref:dz:1")].liked


def test_future_events_do_not_change_current_state():
    rows = [event(1, "like"), event(2, "unlike", NOW + timedelta(seconds=1))]
    assert next(iter(fold(rows, now=NOW).values())).liked


def test_event_intake_uses_three_calendar_years_including_leap_day():
    from app.plays import event_time
    now = datetime(2024, 2, 29, 12)
    assert event_time(datetime(2021, 2, 28, 12), now) == datetime(2021, 2, 28, 12)
    assert event_time(datetime(2021, 2, 28, 11), now) == now


@pytest.mark.parametrize("payload", [
    {"target_type": "album", "nd_song_id": "s"},
    {"target_type": "artist", "nd_album_id": "a", "ref": "dz:1"},
    {"target_type": "album", "nd_album_id": "a", "kind": "start"},
    {"target_type": "album", "nd_album_id": "a", "kind": "dislike"},
    {"ref": "dz:1", "reverts_event_id": "self"},
    {"ref": "dz:1", "target_data": {"name": "x" * 8193}},
])
def test_invalid_typed_target_is_rejected(payload):
    with pytest.raises(ValueError):
        PlayEventIn.model_validate(dict(event_id="self", ts=NOW, kind="like", client="pwa") | payload)


@pytest.fixture
async def ledger(db_file):
    from app.db import dispose, init_engine, session_scope
    init_engine(db_file)
    async with session_scope() as s:
        user = User(nd_username="qa", nd_password_enc="unused")
        other = User(nd_username="other", nd_password_enc="unused")
        s.add_all([user, other])
        await s.flush()
    yield user, other
    await dispose()


async def test_reconcile_bootstrap_import_provenance_restart_empty_and_grace(ledger):
    from app.db import session_scope
    user, other = ledger
    body = {"song": [{"id": "own", "starred": "2020-01-01T00:00:00Z"}, {"id": "imported"}],
            "album": [{"id": "ia"}], "artist": [{"id": "ir"}]}
    async with session_scope() as s:
        s.add(Import(user_id=user.id, source="yandex", method="token", options_json={
            "liked": ["imported"], "liked_album_ids": ["ia"], "liked_artist_ids": ["ir"]}))
        await s.flush()
        assert await reconcile(s, user, body, now=NOW) == 1
        assert await reconcile(s, user, body, now=NOW) == 0
        states = await snapshot(s, user.id)
        assert len(states) == 1
        st = next(iter(states.values()))
        assert st.liked and st.since == datetime(2020, 1, 1)
        assert await snapshot(s, other.id) == {}
        # received_at, not the 2020 timestamp, protects freshly delivered old history.
        assert await reconcile(s, user, {}, now=st.received_at + timedelta(hours=23)) == 0
        assert await reconcile(s, user, {}, now=st.received_at + timedelta(hours=25)) == 1
        assert not next(iter((await snapshot(s, user.id, st.received_at + timedelta(hours=25))).values())).liked
        assert (await s.get(NavidromeSync, user.id)).favorites_bootstrapped_at == NOW


async def test_old_star_cannot_resurrect_own_unlike_and_pending_has_no_nd(ledger):
    from app.db import session_scope
    user, _ = ledger
    async with session_scope() as s:
        ev = await record(s, user.id, FavoriteTarget(nd_song_id="s"), False, ts=NOW)
        await record(s, user.id, FavoriteTarget(ref="dz:pending"), True, ts=NOW)
        later = ev.received_at + timedelta(hours=25)
        assert await reconcile(s, user, {"song": [{"id": "s", "starred": "2020-01-01T00:00:00Z"}]}, now=later) == 0
        states = list((await snapshot(s, user.id)).values())
        assert next(x for x in states if x.ref == "dz:pending").liked
        assert not next(x for x in states if x.nd_id == "s").liked


async def test_failed_mirror_retries_instead_of_synthesizing_unlike(ledger):
    from app.db import session_scope
    user, _ = ledger
    async with session_scope() as s:
        ev = await record(s, user.id, FavoriteTarget(nd_song_id="s"), True, ts=NOW, mirror_pending=True)
        nd = AsyncMock()
        assert await reconcile(s, user, {}, now=ev.received_at + timedelta(days=2), nd=nd) == 0
        nd.star.assert_awaited_once_with(song_ids=["s"])
        assert not ev.mirror_pending
        assert next(iter((await snapshot(s, user.id)).values())).liked


async def test_command_repeat_and_stale_request_do_not_overwrite_newer_decision(ledger):
    from app.db import session_scope
    user, _ = ledger
    async with session_scope() as s:
        nd = AsyncMock()
        target = FavoriteTarget(nd_song_id="s")
        await command(s, user, [target], liked=True, operation="one", ts=NOW, nd=nd)
        await command(s, user, [target], liked=False, operation="two", ts=NOW + timedelta(seconds=2), nd=nd)
        await command(s, user, [target], liked=True, operation="one", ts=NOW, nd=nd)
        await command(s, user, [target], liked=True, operation="late", ts=NOW - timedelta(seconds=2), nd=nd)
        nd.star.assert_awaited_once()
        assert not next(iter((await snapshot(s, user.id)).values())).liked


async def test_assistant_partial_failure_journals_only_successful_types(ledger):
    from app.db import session_scope
    user, _ = ledger
    async with session_scope() as s:
        nd = AsyncMock()
        nd.star.side_effect = [None, RuntimeError("ND down")]
        with pytest.raises(RuntimeError):
            await command(s, user, [FavoriteTarget(nd_song_id="s"),
                                   FavoriteTarget(target_type="album", nd_album_id="a")],
                          liked=True, client="assistant", context="assistant:bulk", nd=nd)
        rows = list((await s.execute(select(PlayEvent))).scalars())
        assert len(rows) == 1 and rows[0].target_type == "track"
        assert rows[0].client == "assistant" and rows[0].context == "assistant:bulk"


async def test_incomplete_snapshot_does_not_remove_state(ledger):
    from app.db import session_scope
    user, _ = ledger
    async with session_scope() as s:
        await record(s, user.id, FavoriteTarget(nd_song_id="s"), True, ts=NOW)
        with pytest.raises(ValueError):
            await reconcile(s, user, {"song": None}, now=NOW + timedelta(days=2))
        assert next(iter((await snapshot(s, user.id)).values())).liked


async def test_v1_profile_counts_one_server_and_legacy_like_and_neutral_unlike(ledger):
    from app.db import session_scope
    from app.mixes.taste import build_profile
    from app.plays import record_events
    from app.models import utcnow

    user, _ = ledger
    async with session_scope() as s:
        at = utcnow() - timedelta(minutes=1)
        await record(s, user.id, FavoriteTarget(ref="dz:1", target_data={"artist": "A", "title": "T"}),
                     True, ts=at, received_at=at, event_id="server")
        await record_events(s, user.id, [dict(event_id="legacy", ts=at + timedelta(seconds=1),
                                            kind="like", ref="dz:1", client="pwa")], now=at + timedelta(seconds=1))
        p = await build_profile(s, user, now=at + timedelta(seconds=2))
        assert p.tracks["dz:1"].liked and p.tracks["dz:1"].score == pytest.approx(1, abs=.001)
        await record(s, user.id, FavoriteTarget(ref="dz:1"), False, ts=at + timedelta(seconds=3),
                     received_at=at + timedelta(seconds=3))
        p = await build_profile(s, user, now=at + timedelta(seconds=4))
        assert "dz:1" not in p.tracks
        await record(s, user.id, FavoriteTarget(ref="dz:1"), True, ts=at + timedelta(seconds=5),
                     received_at=at + timedelta(seconds=5))
        p = await build_profile(s, user, now=at + timedelta(seconds=6))
        assert p.tracks["dz:1"].score == pytest.approx(1, abs=.001)


async def test_sync_timeout_preserves_state(ledger, monkeypatch):
    from app import auth
    from app.db import session_scope
    from app.favorites import sync_user
    user, _ = ledger
    nd = AsyncMock()
    nd.get_starred2.side_effect = TimeoutError("offline")
    monkeypatch.setattr(auth, "user_navidrome", lambda user: nd)
    async with session_scope() as s:
        await record(s, user.id, FavoriteTarget(nd_song_id="s"), True, ts=NOW)
    with pytest.raises(TimeoutError):
        await sync_user(user)
    nd.aclose.assert_awaited_once()
    async with session_scope() as s:
        assert next(iter((await snapshot(s, user.id)).values())).liked


async def test_missing_starred2_envelope_is_not_an_empty_snapshot():
    from app.library.navidrome import NavidromeClient, SubsonicError
    nd = NavidromeClient("qa", "unused", client=AsyncMock())
    nd.call = AsyncMock(return_value={"status": "ok"})
    with pytest.raises(SubsonicError):
        await nd.get_starred2()
    nd.call.return_value = {"status": "ok", "starred2": {}}
    assert await nd.get_starred2() == {}


@pytest.mark.parametrize("stage", ["queued", "searching", "downloading", "tagging", "importing", "needs_review"])
async def test_pending_unlike_preserves_explicit_download_and_survives_stale_job(ledger, stage):
    from app.db import session_scope
    from app.favorites import download_requested
    from app.jobs.downloads import update_item
    from app.models import DownloadBatch, DownloadItem
    user, other = ledger
    async with session_scope() as s:
        batches = [DownloadBatch(user_id=u.id, origin="catalog") for u in (user, other)]
        s.add_all(batches)
        await s.flush()
        items = [DownloadItem(user_id=u.id, batch_id=b.id, status=stage,
            query_json={"query": {}, "meta": {"track_ref": "dz:pending"}, "star": True, "requested": True})
            for u, b in zip((user, other), batches)]
        s.add_all(items)
        await s.flush()
        for item in items:
            await download_requested(s, item)
        stale = dict(items[0].query_json)
        ids = [i.id for i in items]
    async with session_scope() as s:
        await command(s, user, [FavoriteTarget(ref="dz:pending")], liked=False, nd=AsyncMock())
    await update_item(ids[0], query_json=stale)
    async with session_scope() as s:
        mine, theirs = [await s.get(DownloadItem, id_) for id_ in ids]
        assert mine.status == stage and not mine.query_json["star"] and mine.query_json["star_cancelled"]
        assert theirs.status == stage and theirs.query_json["star"]
        assert not next(iter((await snapshot(s, user.id)).values())).liked
        assert next(iter((await snapshot(s, other.id)).values())).liked


async def test_pending_album_is_delivered_after_verified_library_binding(ledger):
    from app.db import session_scope
    from app.favorites import remember
    from app.models import utcnow
    user, _ = ledger
    async with session_scope() as s:
        nd = AsyncMock()
        await command(s, user, [FavoriteTarget(target_type="album", ref="dz:9")], liked=True, nd=nd)
        nd.star.assert_not_awaited()
        await remember(s, FavoriteTarget(target_type="album", ref="dz:9", nd_album_id="album9"))
        await reconcile(s, user, {}, now=utcnow(), nd=nd)
        nd.star.assert_awaited_once_with(album_ids=["album9"])
        state = next(iter((await snapshot(s, user.id)).values()))
        assert state.liked and not state.event.mirror_pending


async def test_legacy_import_album_artist_provenance_is_recovered_without_restarring(ledger):
    from app.db import session_scope
    from app.favorites import recover_import_subjects
    from app.models import Album, ImportCollection, ImportItem, Track
    user, _ = ledger
    async with session_scope() as s:
        album = Album(uid="a9", title="Album", album_artist="A", deezer_album_id="9")
        imp = Import(user_id=user.id, source="yandex", method="token")
        s.add_all([album, imp])
        await s.flush()
        s.add(Track(uid="t9", path="a/t.mp3", nd_song_id="song9", album_id=album.id,
                    artist="A", title="T", fingerprint="a|t"))
        collections = [ImportCollection(import_id=imp.id, kind=k, external_id=k, title=k) for k in ("albums", "artists")]
        s.add_all(collections)
        await s.flush()
        for coll in collections:
            s.add(ImportItem(import_id=imp.id, collection_id=coll.id, external_id="9", ref="dz:9",
                             raw_json={"name": "A"}, status="done"))
        await s.flush()
        nd = AsyncMock()
        nd.get_song.return_value = {"id": "song9", "albumId": "album9"}
        nd.search3.return_value = {"artist": [{"id": "artist9", "name": "A"}]}
        body = {"album": [{"id": "album9"}], "artist": [{"id": "artist9"}, {"id": "outside"}]}
        await recover_import_subjects(s, user.id, body, nd)
        assert imp.options_json == {"liked_album_ids": ["album9"], "liked_artist_ids": ["artist9"]}
        assert await reconcile(s, user, body, now=NOW) == 1
        states = list((await snapshot(s, user.id)).values())
        assert len(states) == 1 and states[0].target_type == "artist" and states[0].nd_id == "outside"
        nd.star.assert_not_awaited()
        nd.unstar.assert_not_awaited()


@pytest.mark.parametrize("typ", ["track", "album", "artist"])
@pytest.mark.parametrize("reverse", [False, True])
def test_retracting_compensation_restores_original_date_and_ignores_legacy_duplicate(typ, reverse):
    rows = [event("a", "like", typ=typ),
            event("legacy", "like", NOW + timedelta(seconds=1), typ=typ),
            event("c", "unlike", NOW + timedelta(seconds=2), typ=typ, reverts="a"),
            event("r", "like", NOW + timedelta(seconds=3), typ=typ, reverts="c")]
    state = next(iter(fold(list(reversed(rows)) if reverse else rows + [rows[-1]]).values()))
    assert state.liked and state.since == NOW and state.event.event_id == "a"


@pytest.mark.parametrize("kind", ["like", "unlike"])
def test_late_retraction_preserves_newer_independent_decision(kind):
    rows = [event("a", "like"), event("c", "unlike", NOW + timedelta(seconds=1), reverts="a"),
            event("new", kind, NOW + timedelta(seconds=2)),
            event("r", "like", NOW + timedelta(seconds=3), reverts="c")]
    state = next(iter(fold(rows).values()))
    assert state.event.event_id == "new" and state.liked == (kind == "like")
    assert state.since == (rows[2].ts if kind == "like" else None)


def test_parallel_compensations_require_both_retractions():
    rows = [event("a", "like"), event("c1", "unlike", reverts="a"),
            event("c2", "unlike", reverts="a"), event("r1", "like", reverts="c1")]
    assert not next(iter(fold(rows).values())).liked
    rows.append(event("r2", "like", reverts="c2"))
    assert next(iter(fold(rows).values())).since == NOW


@pytest.mark.parametrize("bad", ["unknown", "target", "type", "user", "cycle"])
def test_inert_compensation_does_not_renew_grace_or_undo_unlike(bad):
    rows = [event("a", "unlike")]
    kwargs = {"reverts": "a", "received": NOW + timedelta(days=2)}
    if bad == "unknown":
        kwargs["reverts"] = "missing"
    elif bad == "target":
        kwargs["ref"] = "dz:2"
    elif bad == "type":
        kwargs["typ"] = "album"
    elif bad == "user":
        kwargs["user"] = 2
    else:
        kwargs["reverts"] = "other"
        rows.append(event("other", "unlike", reverts="bad"))
    rows.append(event("bad", "like", **kwargs))
    state = fold(rows)[("track", "ref:dz:1")]
    assert not state.liked and state.received_at == NOW


def test_long_compensation_chain_is_iterative_and_preserves_original_date():
    rows = [event("a", "like")]
    for i in range(2000):
        rows.append(event(f"c{i}", "unlike", reverts=rows[-1].event_id))
    assert next(iter(fold(rows).values())).since == NOW


async def test_restoration_requeues_delivered_mirror_and_preserves_original_date(ledger):
    from app.db import session_scope
    from app.plays import record_events
    user, _ = ledger
    async with session_scope() as s:
        original = await record(s, user.id, FavoriteTarget(nd_song_id="s"), True,
                                ts=NOW, event_id="a")
        def payload(eid, kind, target):
            return dict(event_id=eid, kind=kind, ts=NOW, nd_song_id="s",
                        client="app/1", reverts_event_id=target)
        nd = AsyncMock()
        assert (await record_events(s, user.id, [payload("c", "unlike", "a")])).accepted == 1
        assert not next(iter((await snapshot(s, user.id)).values())).liked
        await reconcile(s, user, {"song": [{"id": "s"}]}, nd=nd)
        nd.unstar.assert_awaited_once_with(song_ids=["s"])
        assert not original.mirror_pending
        assert (await record_events(s, user.id, [payload("r", "like", "c")])).accepted == 1
        state = next(iter((await snapshot(s, user.id)).values()))
        assert state.liked and state.since == NOW and original.mirror_pending
        await reconcile(s, user, {}, nd=nd)
        nd.star.assert_awaited_once_with(song_ids=["s"])
        assert not original.mirror_pending
        original_received = state.received_at
        assert (await record_events(s, user.id, [payload("r", "like", "c")])).duplicates == 1
        state = next(iter((await snapshot(s, user.id)).values()))
        assert state.received_at == original_received and not state.event.mirror_pending


async def test_compensation_before_original_requeues_neutral_mirror(ledger):
    from app.db import session_scope
    from app.plays import record_events
    user, _ = ledger
    async with session_scope() as s:
        def payload(eid, kind, target=None):
            return dict(event_id=eid, kind=kind, ts=NOW, nd_song_id="s",
                        client="app/1", reverts_event_id=target)
        await record_events(s, user.id, [payload("c", "unlike", "a")])
        assert await snapshot(s, user.id) == {}
        await record_events(s, user.id, [payload("a", "like")])
        state = next(iter((await snapshot(s, user.id)).values()))
        assert not state.liked and state.event.mirror_pending
        nd = AsyncMock()
        await reconcile(s, user, {"song": [{"id": "s"}]}, nd=nd)
        nd.unstar.assert_awaited_once_with(song_ids=["s"])
