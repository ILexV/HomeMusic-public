"""Readonly replay contracts: no application startup and no current-state leakage."""
import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from app.mixes.evaluate import auc, bootstrap_days, evaluate, main, outcomes, read_snapshot, readonly_db
from app.mixes.taste_math import calculate_baseline, calculate_profile
from app.mixes.taste_snapshot import TasteEvent, TasteSnapshot

NOW = datetime(2026, 10, 27, 12)


def ev(eid, kind="finish", ref="dz:1", at=NOW, **kwargs):
    return TasteEvent(1, eid, at, kind, ref, received_at=kwargs.pop("received_at", at), **kwargs)


def seed_db(path, events):
    with sqlite3.connect(path) as c:
        for e in events:
            c.execute("INSERT INTO play_events (user_id,event_id,ts,kind,ref,client,received_at,target_data,context,position_s) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)", (e.user_id, e.event_id, e.ts.isoformat(" "), e.kind, e.ref,
                        e.client, (e.received_at or e.ts).isoformat(" "), json.dumps(e.target_data), e.context, e.position_s))


def test_readonly_connection_rejects_write_and_cli_does_not_change_database(db_file, tmp_path, monkeypatch):
    seed_db(db_file, [ev("finish")])
    before = hashlib.sha256(db_file.read_bytes()).hexdigest()
    with readonly_db(db_file) as c:
        assert c.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            c.execute("DELETE FROM play_events")
    # Any accidental lifecycle/network path fails immediately.
    import app.db
    import app.mixes.taste
    monkeypatch.setattr(app.db, "init_engine", lambda *a, **kw: pytest.fail("engine startup"))
    monkeypatch.setattr(app.db, "run_migrations", lambda *a, **kw: pytest.fail("migration startup"))
    monkeypatch.setattr(app.mixes.taste, "_catalog_info", lambda *a, **kw: pytest.fail("network enrichment"))
    output = tmp_path / "result.json"
    args = ["--db", str(db_file), "--until", NOW.isoformat(), "--user-id", "1", "--model", "v2", "--seed", "42",
            "--timezone", "Europe/Moscow", "--output", str(output)]
    assert main(args) == 0
    first = output.read_bytes()
    assert main(args) == 0 and output.read_bytes() == first
    assert hashlib.sha256(db_file.read_bytes()).hexdigest() == before
    result = json.loads(first)
    assert result["vectors"]["status"] == "missing_historical_snapshot"
    assert result["quality"]["selected"]["auc"] is None
    assert result["quality"]["selected"]["reason"] == "need_finish_and_earlyskip_classes"


def test_history_ignores_current_mutable_state_and_limits_occurred_available(db_file):
    seed_db(db_file, [ev("own", "like", at=NOW - timedelta(days=1)),
        ev("future", ref="dz:future", at=NOW + timedelta(seconds=1)),
        ev("delayed", ref="dz:delayed", at=NOW - timedelta(days=1), received_at=NOW + timedelta(days=1)),
        ev("debug", ref="dz:debug", client="app/2-debug"), replace(ev("other"), user_id=2)])
    with sqlite3.connect(db_file) as c:
        c.execute("INSERT INTO dislikes (user_id,ref,source,created_at) VALUES (1,'dz:future-state','pwa',?)",
                  ((NOW - timedelta(days=1)).isoformat(" "),))
        c.execute("INSERT INTO mix_settings (user_id,mode,mix_hour,artists_json,updated_at) VALUES (1,'normal',5,?,?)",
                  (json.dumps([{"ref": "dz:future-artist", "name": "Future"}]), NOW.isoformat(" ")))
    with readonly_db(db_file) as c:
        snapshot = read_snapshot(c, user_id=1, until=NOW)
    available = calculate_profile(snapshot, now=NOW, timezone="UTC", mode="available-at")
    occurred = calculate_profile(snapshot, now=NOW, timezone="UTC", mode="occurred-at")
    assert set(available.tracks) == {"dz:1"}
    assert set(occurred.tracks) == {"dz:1", "dz:delayed"}
    assert not available.disliked_ids and not available.picked_artists
    assert snapshot.limitations


def test_pre_outcome_prediction_never_sees_own_label_future_likes_or_metadata():
    rows = (ev("warm", at=NOW - timedelta(days=2)),
        ev("good", context="wave:1", at=NOW - timedelta(days=1)),
        ev("bad", "skip", "dz:bad", context="mix:1", position_s=5),
        ev("future", "like", "dz:bad", at=NOW + timedelta(seconds=1)),
        ev("own-context", "skip", context="album:1", position_s=5))
    snapshot = TasteSnapshot(1, rows)
    result = outcomes(snapshot, until=NOW, timezone="UTC", model="v2", mode="available-at")
    assert [r["event_id"] for r in result] == ["good", "bad"]
    assert result[1]["score"] == 0
    assert auc([r["label"] for r in result], [r["score"] for r in result]) == 1


@pytest.mark.parametrize("labels,scores,expected", [
    ([1, 0], [2, 1], 1), ([1, 0], [0, 1], 0), ([1, 0], [1, 1], .5),
    ([1, 0, 1, 0], [4, 3, 2, 1], .75), ([1, 1], [2, 3], None), ([], [], None),
])
def test_auc_known_values_and_missing_classes(labels, scores, expected):
    assert auc(labels, scores) == expected


def test_seeded_bootstrap_uses_whole_days_and_is_deterministic():
    rows = [{"day": "a", "label": 1, "score": 2}, {"day": "a", "label": 0, "score": 0},
            {"day": "b", "label": 1, "score": 3}, {"day": "b", "label": 0, "score": 1}]
    first = bootstrap_days(rows, seed=42)
    assert first == bootstrap_days(rows, seed=42)
    assert first["auc"] == 1 and first["ci95"] == [1, 1] and first["days"] == 2


def test_baseline_is_calendar_v1_without_saturation_or_experiments():
    rows = tuple(ev(str(i), at=NOW - timedelta(days=30)) for i in range(30))
    p = calculate_baseline(TasteSnapshot(1, rows), now=NOW, timezone="UTC")
    assert p.tracks["dz:1"].score == 30
    selected = evaluate(TasteSnapshot(1, rows), until=NOW, timezone="UTC", model="P5", seed=7)
    assert selected["model"] == "P5" and selected["v1_baseline"]["tracks"]["dz:1"]["score"] == 30


def test_online_configuration_rejects_offline_hypotheses():
    from pydantic import ValidationError

    from app.config import MixesCfg
    assert MixesCfg().taste_model == "v1" and not MixesCfg().taste_shadow
    with pytest.raises(ValidationError):
        MixesCfg(taste_model="P1")


async def test_online_v2_adapter_loads_batches_without_network_or_writes(db_file, monkeypatch):
    from sqlalchemy import event as sql_event

    from app.db import dispose, init_engine, session_scope
    from app.mixes.taste import build_profile
    from app.models import Album, FavoriteSubject, PlayEvent, Track, User

    engine = init_engine(db_file)
    try:
        async with session_scope() as s:
            user = User(nd_username="taste-test", nd_password_enc="unused")
            s.add(user)
            album = Album(uid="hub-album-id", title="Album", album_artist="Artist", deezer_album_id="album")
            s.add(album)
            await s.flush()
            s.add_all([Track(uid="one", path="one.mp3", nd_song_id="current", artist="Artist", title="One",
                             deezer_track_id="1", fingerprint="artist|one", duration_s=200),
                       Track(uid="two", path="two.mp3", nd_song_id="album-song", artist="Artist", title="Two",
                             deezer_track_id="2", fingerprint="artist|two", duration_s=200, album_id=album.id),
                       Track(uid="other", path="other.mp3", nd_song_id="other-song", artist="Other", title="Other",
                             deezer_track_id="other", fingerprint="other|other", duration_s=200)])
            s.add(FavoriteSubject(target_type="track", canonical_key="ref:dz:1", ref="dz:1", nd_id="current",
                aliases=["ref:dz:1", "nd:old", "nd:current"], data={}))
            s.add_all(PlayEvent(user_id=user.id, event_id=f"{i:03}", ts=NOW, kind="finish", ref=None,
                                nd_song_id="old", client="pwa", received_at=NOW) for i in range(100))
            s.add(PlayEvent(user_id=user.id, event_id="album", ts=NOW, kind="like", target_type="album",
                           ref="dz:album", target_data={"artist": "Artist"}, client="pwa", received_at=NOW))
            await s.flush()
            uid = user.id
        import app.mixes.taste
        monkeypatch.setattr(app.mixes.taste, "_catalog_info", lambda *a: pytest.fail("profile network"))
        statements = []
        sql_event.listen(engine.sync_engine, "before_cursor_execute", lambda c, u, statement, *a: statements.append(statement))
        async with session_scope() as s:
            user = await s.get(User, uid)
            p = await build_profile(s, user, now=NOW, model="v2", timezone="Europe/Moscow")
            assert not s.new and not s.dirty and not s.deleted
        assert len(statements) < 20  # 100 events do not produce 100 metadata SQL queries.
        assert set(p.tracks) == {"nd:current", "nd:album-song"}
        assert p.find(nd_song_id="old") is p.find(nd_song_id="current")
        assert p.tracks["nd:current"].positive_raw == 200
        assert p.tracks["nd:album-song"].album_liked and p.tracks["nd:album-song"].score == 0
    finally:
        await dispose()


def test_observed_replay_keeps_unknown_policy_audio_and_adjacent_sample_sizes(db_file):
    from app.matching.replay_quality import observed_quality
    seed_db(db_file, [
        ev("complete", context="wave:missing", at=NOW - timedelta(days=1)),
        ev("previous", "skip", context="wave:missing", at=NOW - timedelta(days=35), position_s=4),
        ev("not-yet-received", context="wave:missing", at=NOW - timedelta(days=2),
           received_at=NOW + timedelta(days=1)),
        replace(ev("other-account", context="wave:missing"), user_id=2),
    ])
    with readonly_db(db_file) as connection:
        report = observed_quality(connection, user_id=1, until=NOW)
    assert report["eligible_plays"] == 1 and report["completion_rate"] == 1
    assert report["comparison"]["previous"]["eligible_plays"] == 1
    assert report["comparison"]["previous"]["early_skip_rate"] == 1
    assert report["policy_cohorts"][0]["policy_version"] == "unknown"
    assert report["recordings"]["observations"] == 0 and report["recordings"]["rate"] is None
    assert report["unavailable_events"] == 1
    assert report["comparison"]["current"]["since"] == report["comparison"]["previous"]["until"]
