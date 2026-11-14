"""Owned server candidate bindings, real persistence, undo and playback selection."""
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app import listen, recording_feedback as feedback
from app.models import PlayEvent
from app.providers.base import Candidate


@pytest.fixture
async def recording_db(hub_env, db_file):
    from app.db import dispose, init_engine, session_scope
    from app.models import Session, User
    from app.security import new_token
    init_engine(db_file)
    users, tokens = [], []
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
    await listen.reset()
    await dispose()


def choice(identifier="wrong"):
    return listen.Choice("dz:1", "full", "Artist", "Song", 240,
        candidate=Candidate(provider="fixture", provider_id=identifier, artist="Artist", title="Song",
                            duration_s=240, page_url=f"https://example.invalid/{identifier}"))


async def test_feedback_requires_owned_served_binding_and_does_not_write_taste(recording_db):
    from app.db import session_scope
    from app.main import create_app
    users, tokens = recording_db
    candidate = choice()
    binding = await feedback.bind_choice(users[0], candidate)
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        body = {"ref": "dz:1", "binding": binding}
        response = await client.post("/api/recording-feedback", json=body)
        assert response.status_code == 401
        client.cookies.set("hm_session", tokens[1])
        assert (await client.post("/api/recording-feedback", json=body)).status_code == 404
        client.cookies.set("hm_session", tokens[0])
        assert (await client.post("/api/recording-feedback", json=body)).status_code == 409
        await feedback.record_stream(users[0], candidate, binding_id=binding)
        assert (await client.post("/api/recording-feedback", json={**body, "ref": "dz:2"})).status_code == 404
        saved = await client.post("/api/recording-feedback", json=body)
        assert saved.status_code == 200, saved.text
        assert (await client.post("/api/recording-feedback", json=body)).json()["id"] == saved.json()["id"]
        assert await feedback.rejected_candidates(users[0], "dz:1") == {("fixture", "wrong")}
        assert await feedback.rejected_candidates(users[1], "dz:1") == set()
        async with session_scope() as s:
            assert (await s.execute(select(func.count()).select_from(PlayEvent))).scalar_one() == 0
        path = f'/api/recording-feedback/{saved.json()["id"]}'
        client.cookies.set("hm_session", tokens[1])
        assert (await client.delete(path)).status_code == 404
        client.cookies.set("hm_session", tokens[0])
        assert (await client.delete(path)).status_code == 204
        assert not await feedback.rejected_candidates(users[0], "dz:1")


async def test_rejected_pick_selects_alternative_only_for_reporting_account(recording_db, monkeypatch):
    users, _ = recording_db
    wrong, alternate = choice(), choice("alternative")
    listen._choices[wrong.ref] = wrong
    binding = await feedback.bind_choice(users[0], wrong)
    await feedback.record_stream(users[0], wrong, binding_id=binding)
    await feedback.report(users[0], wrong.ref, binding)
    calls = []
    async def resolve(ref, ident, probe, *, full_only, rejected):
        calls.append(rejected)
        return alternate
    monkeypatch.setattr(listen, "_resolve_deezer", resolve)
    assert (await listen.choose("dz:1", user_id=users[0])).candidate.provider_id == "alternative"
    assert (await listen.choose("dz:1", user_id=users[1])).candidate.provider_id == "wrong"
    assert (await listen.choose("dz:1", user_id=users[0])).candidate.provider_id == "alternative"
    assert calls == [{("fixture", "wrong")}]


async def test_browser_stream_before_metadata_and_expired_binding(recording_db, monkeypatch):
    users, _ = recording_db
    candidate = choice()
    now = datetime(2026, 11, 14)
    monkeypatch.setattr(feedback, "utcnow", lambda: now)
    await feedback.record_stream(users[0], candidate)
    binding = await feedback.bind_choice(users[0], candidate)
    assert (await feedback.report(users[0], candidate.ref, binding)).provider_id == "wrong"
    monkeypatch.setattr(feedback, "utcnow", lambda: now + timedelta(days=2))
    with pytest.raises(feedback.FeedbackError) as error:
        await feedback.report(users[0], candidate.ref, binding)
    assert error.value.status == 409


async def test_current_binding_tracks_actual_fallback_not_metadata_prefetch(recording_db):
    users, _ = recording_db
    proposed, actual = choice(), choice("actual")
    initial = await feedback.bind_choice(users[0], proposed)
    await feedback.record_stream(users[0], actual, binding_id=initial)
    current = await feedback.current_binding(users[0], "dz:1")
    assert current != initial
    with pytest.raises(feedback.FeedbackError) as error:
        await feedback.report(users[0], "dz:1", initial)
    assert error.value.status == 409
    assert (await feedback.report(users[0], "dz:1", current)).provider_id == "actual"
    assert await feedback.rejected_candidates(users[0], "dz:1") == {("fixture", "actual")}


async def test_imported_rejected_candidate_cannot_bypass_selection(recording_db, monkeypatch):
    from app.config import get_settings
    from app.db import session_scope
    from app.models import DownloadBatch, DownloadItem, Track
    users, _ = recording_db
    wrong = choice()
    binding = await feedback.bind_choice(users[0], wrong)
    await feedback.record_stream(users[0], wrong, binding_id=binding)
    await feedback.report(users[0], "dz:1", binding)
    path = get_settings().library_dir / "test.mp3"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"library fixture")
    async with session_scope() as s:
        batch = DownloadBatch(user_id=users[0], origin="catalog")
        track = Track(uid="feedback-track", path="test.mp3", artist="Artist", title="Song",
                      fingerprint="artist|song", nd_song_id="library", source="fixture",
                      source_url=wrong.candidate.page_url, deezer_track_id="1")
        s.add_all([batch, track])
        await s.flush()
        s.add(DownloadItem(batch_id=batch.id, user_id=users[0], query_json={}, status="done",
                           track_id=track.id, chosen_candidate_json=wrong.candidate.model_dump(mode="json")))
    assert await feedback.library_candidate(path) == ("fixture", "wrong")
    listen._choices["dz:1"] = listen.Choice("dz:1", "library", "Artist", "Song", path=path, nd_song_id="library")
    async def alternative(*args, **kwargs):
        assert kwargs["rejected"] == {("fixture", "wrong")}
        return choice("alternative")
    monkeypatch.setattr(listen, "_resolve_deezer", alternative)
    assert (await listen.choose("dz:1", user_id=users[0])).candidate.provider_id == "alternative"
    assert (await listen.choose("dz:1", user_id=users[1])).kind == "library"
    assert await feedback.rejected_candidates(users[0], "nd:library") == {("fixture", "wrong")}
    listen._choices["nd:library"] = listen.Choice("nd:library", "library", "Artist", "Song", path=path, nd_song_id="library")
    assert (await listen.choose("nd:library", user_id=users[0])).candidate.provider_id == "alternative"
