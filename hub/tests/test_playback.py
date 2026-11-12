"""Shared playback's observable authorization, persistence and acknowledged handoff contract."""

from __future__ import annotations

import asyncio
import copy
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import playback
from app.api.playback import router
from app.models import Session, User, utcnow
from app.playback_schema import Claim, Heartbeat
from app.security import hash_token


def snapshot(**changes):
    track = {"ref": "nd:song1", "nd_song_id": "song1", "title": "Песня", "artist": "Группа",
             "album": "Альбом", "duration_s": 200}
    return {"queue": [track, copy.deepcopy(track)], "index": 1, "occurrence": "duplicate-2",
            "position": 12.5, "playing": True, "volume": 0.7, "repeat": "all", "shuffle": True, **changes}


@pytest.fixture
async def env(hub_env, db_file, monkeypatch):
    from app.db import dispose, init_engine, session_scope

    init_engine(db_file)
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(playback, "time", SimpleNamespace(time=lambda: clock.now))
    async with session_scope() as session:
        users = [User(nd_username=name, nd_password_enc="unused") for name in ("alice", "bob")]
        session.add_all(users)
        await session.flush()
        ids = [user.id for user in users]
        for user, token in zip(users, ("alice-token", "bob-token"), strict=True):
            session.add(Session(id=hash_token(token), user_id=user.id, expires_at=utcnow() + timedelta(days=1)))
    app = FastAPI()
    app.include_router(router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                cookies={"hm_session": "alice-token"}) as alice:
        async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                    cookies={"hm_session": "bob-token"}) as bob:
            try:
                yield SimpleNamespace(alice=alice, bob=bob, clock=clock, ids=ids, db_file=db_file, app=app)
            finally:
                await dispose()


async def post(client, route, **body):
    response = await client.post(f"/api/playback/{route}", json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def heartbeat(client, device_id, **changes):
    return await post(client, "heartbeat", device_id=device_id, name="Устройство", kind="web",
                      **{"state": None, "revision": None, **changes})


async def owned(env):
    for device in ("source", "target", "remote"):
        await heartbeat(env.alice, device)
    result = await post(env.alice, "claim", device_id="source", state=snapshot(), revision=0)
    assert result["owner"] == "source" and result["revision"] == 1
    return result


async def prepare(env):
    state = await owned(env)
    result = await post(env.alice, "handoff", device_id="remote", target="target", revision=state["revision"])
    assert result["handoff"]["phase"] == "preparing"
    assert result["commands"] == []
    return (await heartbeat(env.alice, "target"))["commands"][0]


async def acknowledge(client, device_id, command, **changes):
    return await post(client, "ack", device_id=device_id, command_id=command["id"], **{"ok": True, **changes})


@pytest.mark.parametrize("route,body", [
    ("heartbeat", {"device_id": "x", "name": "Плеер", "kind": "web"}),
    ("claim", {"device_id": "x", "state": snapshot(), "revision": 0}),
    ("handoff", {"device_id": "x", "target": "y", "revision": 0}),
    ("command", {"device_id": "x", "target": "y", "revision": 0, "kind": "pause"}),
    ("ack", {"device_id": "x", "command_id": "c", "ok": True}),
])
async def test_every_route_requires_authentication(env, route, body):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://test") as anonymous:
        response = await anonymous.post(f"/api/playback/{route}", json=body)
        assert response.status_code == 401


async def test_account_isolation_including_same_device_ids_and_ack(env):
    command = await prepare(env)
    other = await heartbeat(env.bob, "target")
    assert other["state"] is None and other["owner"] is None and other["commands"] == []
    assert [device["id"] for device in other["devices"]] == ["target"]
    wrong = await acknowledge(env.bob, "target", command)
    assert wrong["error"] and wrong["handoff"] is None
    assert (await heartbeat(env.alice, "target"))["commands"][0]["id"] == command["id"]
    foreign = await post(env.bob, "command", device_id="target", target="source", revision=0, kind="pause")
    assert foreign["error"] and foreign["owner"] is None


@pytest.mark.parametrize("change", [
    {"index": 2}, {"index": -1}, {"occurrence": ""}, {"position": -1}, {"volume": 1.1},
    {"playing": "true"}, {"repeat": "random"}, {"cookie": "secret"},
    {"queue": [{**snapshot()["queue"][0], "path": "/music/private.mp3"}]},
    {"queue": [{**snapshot()["queue"][0], "ref": "file:ytdlp:https://host/audio?token=secret"}]},
    {"queue": [{**snapshot()["queue"][0], "ref": "nd:../private"}]},
    {"queue": [{**snapshot()["queue"][0], "nd_song_id": "https://host/token"}]},
    {"queue": [{**snapshot()["queue"][0], "ref": "", "nd_song_id": None}]},
    {"queue": [snapshot()["queue"][0]] * 1001},
])
async def test_invalid_or_sensitive_state_never_persists(env, change):
    await heartbeat(env.alice, "source")
    response = await env.alice.post("/api/playback/claim", json={"device_id": "source", "revision": 0,
                                                               "state": snapshot(**change)})
    assert response.status_code == 422
    assert (await heartbeat(env.alice, "source"))["state"] is None


@pytest.mark.parametrize("ref", ["dz:123", "hm:123", "mb:uuid-1", "file:hitmoz:123", "file:ytdlp:abc_X-1"])
async def test_canonical_catalog_and_provider_ids(env, ref):
    await heartbeat(env.alice, "source")
    track = {**snapshot()["queue"][0], "ref": ref, "nd_song_id": None}
    state = snapshot(queue=[track, track])
    view = await post(env.alice, "claim", device_id="source", revision=0, state=state)
    assert view["state"] == state and not view["error"]


async def test_readiness_stop_start_commits_only_after_final_ack(env):
    command = await prepare(env)
    assert command["kind"] == "prepare" and command["state"] == snapshot()
    # Pending commands are redelivered, not destructively popped by heartbeat.
    assert (await heartbeat(env.alice, "target"))["commands"] == [command]
    assert not (await heartbeat(env.alice, "source"))["commands"]
    wrong = await acknowledge(env.alice, "remote", command)
    assert wrong["error"] and wrong["handoff"]["phase"] == "preparing"
    ready = await acknowledge(env.alice, "target", command)
    assert ready["owner"] == "source" and ready["handoff"]["phase"] == "stopping"
    repeated = await acknowledge(env.alice, "target", command)
    assert repeated["handoff"]["phase"] == "stopping"
    stop = (await heartbeat(env.alice, "source"))["commands"][0]
    assert stop["kind"] == "stop"
    invalid = await acknowledge(env.alice, "source", stop, state=snapshot())
    assert invalid["error"] and invalid["handoff"]["phase"] == "stopping"
    stopped = await acknowledge(env.alice, "source", stop, state=snapshot(position=19.75, playing=False))
    assert stopped["owner"] == "source" and stopped["revision"] == 1
    start = (await heartbeat(env.alice, "target"))["commands"][0]
    assert start["kind"] == "start" and start["state"] == snapshot(position=19.75)
    final = await acknowledge(env.alice, "target", start, state=start["state"])
    assert final["owner"] == "target" and final["revision"] == 2
    assert final["handoff"]["phase"] == "done" and final["commands"] == []
    assert final["state"]["index"] == 1 and final["state"]["occurrence"] == "duplicate-2"
    duplicate = await acknowledge(env.alice, "target", start, state=snapshot(position=99))
    assert duplicate["state"] == final["state"] and duplicate["revision"] == 2
    old = await heartbeat(env.alice, "source", state=snapshot(), revision=2)
    assert old["error"] and old["state"] == final["state"]


@pytest.mark.parametrize("failure_phase", ["prepare", "stop", "start"])
async def test_failed_handoff_never_resumes_or_transfers_source(env, failure_phase):
    command = await prepare(env)
    device = "target"
    if failure_phase in ("stop", "start"):
        await acknowledge(env.alice, device, command)
        device = "source"
        command = (await heartbeat(env.alice, device))["commands"][0]
    if failure_phase == "start":
        await acknowledge(env.alice, device, command, state=snapshot(playing=False, position=18))
        device = "target"
        command = (await heartbeat(env.alice, device))["commands"][0]
    failed = await acknowledge(env.alice, device, command, ok=False, error="https://host/?token=secret /private/file")
    assert failed["owner"] == "source" and failed["handoff"]["phase"] == "failed"
    assert failed["error"] and "secret" not in str(failed) and "/private" not in str(failed)
    source = await heartbeat(env.alice, "source")
    assert source["commands"] == []
    if failure_phase == "start":
        assert source["state"]["playing"] is False and source["state"]["position"] == 18
    late = await acknowledge(env.alice, device, command, state=snapshot())
    assert late["owner"] == "source" and late["handoff"]["phase"] == "failed"


@pytest.mark.parametrize("phase", ["prepare", "stop", "start"])
async def test_expired_handoff_commands_cannot_advance(env, phase):
    command = await prepare(env)
    device = "target"
    if phase in ("stop", "start"):
        await acknowledge(env.alice, device, command)
        device = "source"
        command = (await heartbeat(env.alice, device))["commands"][0]
    if phase == "start":
        await acknowledge(env.alice, device, command, state=snapshot(playing=False))
        device = "target"
        command = (await heartbeat(env.alice, device))["commands"][0]
    env.clock.now = command["expires_at"]
    late = await acknowledge(env.alice, device, command, state=snapshot(playing=False))
    assert late["owner"] == "source" and late["handoff"]["phase"] == "failed" and late["error"]
    assert late["commands"] == []


async def test_stale_owner_requires_explicit_ready_acknowledged_recovery(env):
    await owned(env)
    env.clock.now += 16
    view = await heartbeat(env.alice, "target")
    assert not next(device for device in view["devices"] if device["id"] == "source")["online"]
    claim = await post(env.alice, "claim", device_id="target", revision=1, state=snapshot())
    assert claim["owner"] == "source" and claim["error"]
    pull = await post(env.alice, "handoff", device_id="target", target="target", revision=1)
    assert pull["error"] is None and pull["handoff"]["recovery"] is True
    assert pull["handoff"]["phase"] == "preparing" and pull["owner"] == "source"
    assert pull["revision"] == 2 and pull["state"]["playing"] is False
    command = pull["commands"][0]
    ready = await acknowledge(env.alice, "target", command)
    assert ready["owner"] == "source" and ready["handoff"]["phase"] == "starting"
    start = ready["commands"][0]
    assert start["kind"] == "start" and start["state"] == snapshot()
    done = await acknowledge(env.alice, "target", start, state=start["state"])
    assert done["owner"] == "target" and done["revision"] == 3
    assert done["handoff"]["phase"] == "done"


async def test_disconnected_target_cannot_complete_preparation(env):
    command = await prepare(env)
    env.clock.now += 16
    await heartbeat(env.alice, "source")
    late = await acknowledge(env.alice, "target", command)
    assert late["owner"] == "source" and late["handoff"]["phase"] == "failed"
    assert late["commands"] == []


async def test_controls_are_serialized_and_acks_are_idempotent(env):
    await owned(env)
    sent = await post(env.alice, "command", device_id="remote", target="source", revision=1, kind="seek", value=30)
    assert sent["revision"] == 1 and not sent["commands"]
    duplicate = await post(env.alice, "command", device_id="remote", target="source", revision=1, kind="next")
    assert duplicate["error"]
    frozen = await heartbeat(env.alice, "source", state=snapshot(position=15), revision=1)
    assert frozen["revision"] == 1 and frozen["state"]["position"] == 12.5
    command = frozen["commands"][0]
    assert command["kind"] == "seek" and command["value"] == 30
    missing = await acknowledge(env.alice, "source", command)
    assert missing["error"] and missing["commands"]
    actual = snapshot(position=30)
    done = await acknowledge(env.alice, "source", command, state=actual)
    assert done["state"] == actual and done["revision"] == 2
    again = await acknowledge(env.alice, "source", command, state=snapshot(position=60))
    assert again["state"] == actual and again["revision"] == 2
    stale = await post(env.alice, "command", device_id="remote", target="source", revision=1, kind="next")
    assert stale["error"] and stale["commands"] == []
    stale_publish = await heartbeat(env.alice, "source", state=snapshot(position=99), revision=1)
    assert stale_publish["error"] and stale_publish["state"] == actual


@pytest.mark.parametrize("action", ["command", "handoff"])
async def test_progress_heartbeats_do_not_invalidate_remote_actions(env, action):
    observed = await owned(env)
    for position in (14.5, 16.5, 18.5):
        env.clock.now += 2
        progress = await heartbeat(env.alice, "source", state=snapshot(position=position),
                                   revision=observed["revision"])
        assert progress["error"] is None
        assert progress["revision"] == observed["revision"]
        assert progress["state"]["position"] == position
    target = "source" if action == "command" else "target"
    body = {"kind": "pause"} if action == "command" else {}
    sent = await post(env.alice, action, device_id="remote", target=target,
                      revision=observed["revision"], **body)
    assert sent["error"] is None
    delivered = (await heartbeat(env.alice, target))["commands"]
    assert len(delivered) == 1
    if action == "command":
        assert delivered[0]["kind"] == "pause"
        done = await acknowledge(env.alice, target, delivered[0], state=snapshot(position=18.5, playing=False))
        assert done["state"]["playing"] is False
        assert done["revision"] == observed["revision"] + 1
    else:
        assert delivered[0]["kind"] == "prepare"
        assert delivered[0]["state"]["position"] == 18.5


async def test_semantic_heartbeat_change_still_rejects_stale_remote_action(env):
    observed = await owned(env)
    changed = await heartbeat(env.alice, "source", state=snapshot(playing=False), revision=observed["revision"])
    assert changed["revision"] == observed["revision"] + 1
    stale = await post(env.alice, "command", device_id="remote", target="source",
                       revision=observed["revision"], kind="play")
    assert stale["error"]
    assert (await heartbeat(env.alice, "source"))["commands"] == []


async def test_expired_control_ack_does_not_publish(env):
    await owned(env)
    await post(env.alice, "command", device_id="remote", target="source", revision=1, kind="next")
    command = (await heartbeat(env.alice, "source"))["commands"][0]
    env.clock.now = command["expires_at"]
    late = await acknowledge(env.alice, "source", command, state=snapshot(index=0, occurrence="first"))
    assert late["error"] and late["revision"] == 1 and late["state"] == snapshot()


async def test_two_independent_sqlite_workers_serialize_claim_and_control(env):
    from app.db import sessionmaker

    for device in ("source", "target"):
        await heartbeat(env.alice, device)
    other_engine = create_async_engine(f"sqlite+aiosqlite:///{env.db_file}", connect_args={"timeout": 30})
    other_sessions = async_sessionmaker(other_engine, expire_on_commit=False)
    try:
        claims = await asyncio.gather(*[
            playback.dispatch(env.ids[0], "claim", Claim(device_id=device, state=snapshot(), revision=0), sessions=factory)
            for device, factory in (("source", sessionmaker()), ("target", other_sessions))
        ])
        assert sum(view["error"] is None for view in claims) == 1
        assert claims[0]["owner"] == claims[1]["owner"]
        owner = claims[0]["owner"]
        from app.playback_schema import Control

        controls = await asyncio.gather(*[
            playback.dispatch(env.ids[0], "command", Control(device_id=owner, target=owner, revision=1, kind="next"),
                              sessions=factory)
            for factory in (sessionmaker(), other_sessions)
        ])
        assert sum(view["error"] is None for view in controls) == 1
        assert len((await heartbeat(env.alice, owner))["commands"]) == 1
    finally:
        await other_engine.dispose()


async def test_protocol_survives_new_engine_and_retries(env):
    command = await prepare(env)
    other_engine = create_async_engine(f"sqlite+aiosqlite:///{env.db_file}")
    try:
        factory = async_sessionmaker(other_engine, expire_on_commit=False)
        view = await playback.dispatch(env.ids[0], "heartbeat", Heartbeat(device_id="target", name="Телефон", kind="web"),
                                       sessions=factory)
        assert view["owner"] == "source" and view["state"] == snapshot()
        assert view["commands"] == [command]
    finally:
        await other_engine.dispose()


async def test_device_registry_evicts_only_offline_non_owner(env):
    await heartbeat(env.alice, "owner")
    await post(env.alice, "claim", device_id="owner", state=snapshot(), revision=0)
    for index in range(31):
        await heartbeat(env.alice, f"old-{index}")
    full = await heartbeat(env.alice, "extra")
    assert full["error"] and len(full["devices"]) == 32
    assert "extra" not in {device["id"] for device in full["devices"]}
    env.clock.now += 16
    view = await heartbeat(env.alice, "extra")
    ids = {device["id"] for device in view["devices"]}
    assert len(ids) == 32 and {"owner", "extra"} <= ids and "old-0" not in ids


async def test_competing_handoffs_and_duplicate_prepare_ack_are_serialized(env):
    from app.db import sessionmaker
    from app.playback_schema import Ack, Handoff

    await owned(env)
    other_engine = create_async_engine(f"sqlite+aiosqlite:///{env.db_file}", connect_args={"timeout": 30})
    factories = (sessionmaker(), async_sessionmaker(other_engine, expire_on_commit=False))
    try:
        results = await asyncio.gather(*[
            playback.dispatch(env.ids[0], "handoff", Handoff(device_id="remote", target=target, revision=1),
                              sessions=factory)
            for target, factory in zip(("target", "remote"), factories, strict=True)
        ])
        assert sum(view["error"] is None for view in results) == 1
        target = results[0]["handoff"]["target"]
        assert all(view["handoff"]["target"] == target for view in results)
        command = (await heartbeat(env.alice, target))["commands"][0]
        replies = await asyncio.gather(*[
            playback.dispatch(env.ids[0], "ack", Ack(device_id=target, command_id=command["id"], ok=True),
                              sessions=factory)
            for factory in factories
        ])
        assert all(view["handoff"]["phase"] == "stopping" for view in replies)
        source = await heartbeat(env.alice, "source")
        assert len(source["commands"]) == 1 and source["commands"][0]["kind"] == "stop"
        blocked = await post(env.alice, "claim", device_id="source", revision=1, state=snapshot(position=99))
        assert blocked["error"] and blocked["state"]["position"] == 12.5
    finally:
        await other_engine.dispose()


@pytest.mark.parametrize("kind", ["next", "prev", "play", "pause"])
async def test_button_control_ignores_generic_numeric_value(env, kind):
    await owned(env)
    sent = await post(env.alice, "command", device_id="remote", target="source", revision=1, kind=kind, value=0)
    assert sent["error"] is None
    command = (await heartbeat(env.alice, "source"))["commands"][0]
    assert command["kind"] == kind and "value" not in command


async def test_failed_recovery_keeps_reconnecting_source_fenced_until_explicit_claim(env):
    await owned(env)
    env.clock.now += 16
    await heartbeat(env.alice, "target")
    pull = await post(env.alice, "handoff", device_id="target", target="target", revision=1)
    failed = await acknowledge(env.alice, "target", pull["commands"][0], ok=False)
    assert failed["owner"] == "source" and failed["handoff"]["phase"] == "failed"
    for revision in (1, 2):
        fenced = await heartbeat(env.alice, "source", revision=revision, state=snapshot(position=99))
        assert fenced["error"] and fenced["state"] == snapshot(playing=False)
    blocked = await post(env.alice, "command", device_id="target", target="source", revision=2, kind="play")
    assert blocked["error"] and blocked["commands"] == []
    explicit = await post(env.alice, "claim", device_id="source", revision=2, state=snapshot(position=20))
    assert explicit["error"] is None and explicit["handoff"] is None
    assert explicit["owner"] == "source" and explicit["revision"] == 3


async def test_source_reconnecting_during_recovery_requires_stop_ack(env):
    await owned(env)
    env.clock.now += 16
    await heartbeat(env.alice, "target")
    pull = await post(env.alice, "handoff", device_id="target", target="target", revision=1)
    reconnected = await heartbeat(env.alice, "source", revision=2, state=snapshot(position=99))
    assert reconnected["error"] and reconnected["state"]["position"] == 12.5
    ready = await acknowledge(env.alice, "target", pull["commands"][0])
    assert ready["handoff"]["phase"] == "stopping" and ready["commands"] == []
    stop = (await heartbeat(env.alice, "source"))["commands"][0]
    assert stop["kind"] == "stop"
    await acknowledge(env.alice, "source", stop, state=snapshot(playing=False, position=17))
    start = (await heartbeat(env.alice, "target"))["commands"][0]
    assert start["state"] == snapshot(position=17)


async def test_recovery_cannot_use_disconnected_target(env):
    await owned(env)
    env.clock.now += 16
    await heartbeat(env.alice, "remote")
    rejected = await post(env.alice, "handoff", device_id="remote", target="target", revision=1)
    assert rejected["error"] and rejected["handoff"] is None
    await heartbeat(env.alice, "target")
    await post(env.alice, "handoff", device_id="remote", target="target", revision=1)
    command = (await heartbeat(env.alice, "target"))["commands"][0]
    env.clock.now += 16
    expired = await acknowledge(env.alice, "target", command)
    assert expired["owner"] == "source" and expired["handoff"]["phase"] == "failed"


async def test_recovery_and_stale_publication_race_across_workers(env):
    from app.db import sessionmaker
    from app.playback_schema import Handoff

    await owned(env)
    env.clock.now += 16
    await heartbeat(env.alice, "target")
    other_engine = create_async_engine(f"sqlite+aiosqlite:///{env.db_file}", connect_args={"timeout": 30})
    try:
        factory = async_sessionmaker(other_engine, expire_on_commit=False)
        recovery, publication = await asyncio.gather(
            playback.dispatch(env.ids[0], "handoff", Handoff(device_id="target", target="target", revision=1),
                              sessions=sessionmaker()),
            playback.dispatch(env.ids[0], "heartbeat",
                              Heartbeat(device_id="source", name="Плеер", kind="desktop",
                                        revision=1, state=snapshot(position=99)), sessions=factory),
        )
        if recovery["error"] is None:
            assert recovery["handoff"]["recovery"] is True
            assert publication["error"] and publication["state"]["position"] == 12.5
        else:
            # Publication won the SQLite lock; recovery's stale revision cannot fence the renewed source.
            assert publication["error"] is None and publication["revision"] == 2
            assert recovery["handoff"] is None and recovery["state"]["position"] == 99
    finally:
        await other_engine.dispose()
