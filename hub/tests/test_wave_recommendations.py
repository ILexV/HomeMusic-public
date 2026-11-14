"""Session-only ranking, recorded decisions and bounded full-recording hand-outs."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app import listen, recommendations
from app.api.schemas import WavePreferences
from app.db import session_scope
from app.mixes import wave
from app.models import PlayEvent, WaveItem
from tests.test_mixes import history
from tests.test_wave import cand, env, fake_sources, profile, state, tr


class TextModel:
    model = "OpenMuQ/MuQ-MuLan-large"
    calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def _post(self, path, body):
        self.calls.append((path, body))
        high = body["text"] in (wave.PREFERENCE_PROMPTS["energy"][1], wave.PREFERENCE_PROMPTS["vocals"][1])
        return {"model": self.model, "results": [
            {"key": "dz:1", "sim": 0.1 if high else 0.6},
            {"key": "dz:2", "sim": 0.6 if high else 0.1},
        ]}


@pytest.mark.parametrize(("axis", "low", "high"), [
    ("energy", "calm", "energetic"), ("vocals", "instrumental", "vocal"),
])
async def test_real_text_audio_contrast_is_session_only(monkeypatch, axis, low, high):
    monkeypatch.setattr(wave.embedder, "configured", lambda: True)
    monkeypatch.setattr(wave.embedder, "EmbedderClient", TextModel)
    rt = wave.Runtime(1, "normal", state(cand(1), cand(2), cand(3)), profile=profile())
    original = dict(rt.profile.tracks)
    candidates = list(rt.state.pool.values())
    rt.state.preferences[axis] = low
    await wave._preferences(rt, candidates)
    assert wave.pick(rt.state, rt.profile, rt.mode, {}, n=1)[0].track.ref == "dz:1"
    assert "session_preferences" not in candidates[2].parts  # missing vector stays unknown
    assert rt.state.preferences_status
    rt.state.preferences[axis] = high
    await wave._preferences(rt, candidates)
    assert wave.pick(rt.state, rt.profile, rt.mode, {}, n=1)[0].track.ref == "dz:2"
    rt.state.preferences = WavePreferences().model_dump()
    await wave._preferences(rt, candidates)
    assert all("session_preferences" not in c.parts for c in candidates)
    assert rt.state.preferences_status is None and rt.profile.tracks == original
    assert wave.State().preferences == WavePreferences().model_dump()


async def test_fake_or_missing_model_never_claims_mood(monkeypatch):
    monkeypatch.setattr(wave.embedder, "configured", lambda: True)
    monkeypatch.setattr(wave.embedder, "EmbedderClient", TextModel)
    monkeypatch.setattr(TextModel, "model", "fake")
    rt = wave.Runtime(1, "normal", state(cand(1), cand(2)), profile=profile())
    rt.state.preferences["energy"] = "calm"
    await wave._preferences(rt, list(rt.state.pool.values()))
    assert rt.state.preferences_status
    assert all("session_preferences" not in c.parts for c in rt.state.pool.values())
    monkeypatch.setattr(wave.embedder, "configured", lambda: False)
    await wave._preferences(rt, list(rt.state.pool.values()))
    assert "недоступна" in rt.state.preferences_status


def test_discovery_and_provenance_survive_without_permanent_mutation():
    st = state(*(cand(i, familiar=True) for i in range(10)), *(cand(i + 100) for i in range(10)))
    p = profile()
    st.preferences["discovery"] = "familiar"
    assert sum(c.familiar for c in wave.pick(st, p, "normal", {})) == 4
    st.preferences["discovery"] = "new"
    assert sum(c.familiar for c in wave.pick(st, p, "normal", {})) == 1
    c = st.pool["dz:0"]
    c.parts = {"generator_contributions": {"ytm_radio": 1.25}, "selection_value": 1.25}
    c.known = {"familiar": True}
    c.explanation = ["Recorded reason"]
    restored = wave.State.load(st.dump())
    assert restored.dump() == st.dump()
    assert restored.pool["dz:0"].explanation == ["Recorded reason"]
    assert st.started == 0 and not p.tracks


async def test_ready_reserve_skips_failed_and_holds_pending_without_taste(env, monkeypatch):
    await history(env["admin"])
    fake_sources(monkeypatch, [tr(5000 + i) for i in range(20)])
    calls = []
    async def prepare(uid, refs, *, timeout_s):
        calls.append(list(refs))
        return {ref: "unavailable" if ref == "dz:5000" else "pending" if ref == "dz:5001" else "ready"
                for ref in refs}
    monkeypatch.setattr(listen, "prepare_full", prepare)
    weights_calls = []
    async def weights(uid, base):
        weights_calls.append(uid)
        return {key: value * 1.1 for key, value in base.items()}
    monkeypatch.setattr(recommendations, "adapted_weights", weights)
    response = await env["client"].post("/api/wave/start", json={"preferences": {"discovery": "new"}})
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["tracks"]) == 5 and len(calls[0]) == wave.PREPARE_POOL
    assert not {"dz:5000", "dz:5001"} & {t["ref"] for t in body["tracks"]}
    assert body["preparation"] == {"ready": 8, "pending": 1, "unavailable": 1}
    assert weights_calls == [env["admin"]]
    rt = wave._rt[body["session_id"]]
    assert "dz:5001" in rt.state.pool and "dz:5000" not in rt.state.pool
    assert rt.state.skips == {} and not rt.state.neg and rt.state.started == 0
    async with session_scope() as s:
        items = list((await s.execute(select(WaveItem).where(WaveItem.session_id == body["session_id"])) ).scalars())
        assert len(items) == 5
        assert all(i.decision_json["explanation"] for i in items)
        assert items[0].decision_json["preferences"]["discovery"] == "new"
        assert items[0].decision_json["source_weights"]["ytm_radio"] == pytest.approx(wave.weights()["ytm_radio"] * 1.1)
        events = list((await s.execute(select(PlayEvent).where(PlayEvent.context == f"wave:{body['session_id']}"))).scalars())
        assert not events


async def test_pending_seed_survives_restart_and_is_never_substituted(env, monkeypatch):
    fake_sources(monkeypatch, [tr(5000 + i) for i in range(12)])
    seed_ready = False
    async def prepare(uid, refs, *, timeout_s):
        return {ref: "pending" if ref == "dz:100" and not seed_ready else "ready" for ref in refs}
    monkeypatch.setattr(listen, "prepare_full", prepare)
    response = await env["client"].post("/api/wave/start", json={"seed": {"ref": "dz:100"}})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tracks"] == [] and body["pending"]
    assert body["preparation"]["pending"] == 1
    await wave.reset()
    seed_ready = True
    response = await env["client"].post(f"/api/wave/{body['session_id']}/next", json={"played": [], "queued": []})
    assert response.status_code == 200, response.text
    assert response.json()["tracks"][0]["ref"] == "dz:100"


async def test_unavailable_seed_is_explicit_error_and_all_failed_pool_terminates(env, monkeypatch):
    fake_sources(monkeypatch, [tr(5000 + i) for i in range(12)])
    calls = []
    async def prepare(uid, refs, *, timeout_s):
        calls.append(list(refs))
        return {ref: "unavailable" for ref in refs}
    monkeypatch.setattr(listen, "prepare_full", prepare)
    response = await env["client"].post("/api/wave/start", json={"seed": {"ref": "dz:100"}})
    assert response.status_code == 409
    await history(env["admin"])
    response = await env["client"].post("/api/wave/start", json={})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tracks"] == [] and body["preparation"]["unavailable"] == 10
    response = await env["client"].post(f"/api/wave/{body['session_id']}/next", json={"played": [], "queued": []})
    body = response.json()
    assert body["tracks"] == [] and not body["pending"] and body["preparation"]["unavailable"] == 2
    assert all(len(refs) <= wave.PREPARE_POOL for refs in calls)
    rt = wave._rt[body["session_id"]]
    assert not rt.state.skips and not rt.state.neg and not rt.state.played


async def test_preferences_refresh_reads_actual_start_and_preserves_current(env, monkeypatch):
    await history(env["admin"])
    fake_sources(monkeypatch, [tr(5000 + i) for i in range(20)])
    async def prepare(uid, refs, *, timeout_s):
        return dict.fromkeys(refs, "ready")
    monkeypatch.setattr(listen, "prepare_full", prepare)
    body = (await env["client"].post("/api/wave/start", json={})).json()
    sid, current = body["session_id"], body["tracks"][0]["ref"]
    async with session_scope() as s:
        s.add(PlayEvent(user_id=env["admin"], event_id="external-start", ts=datetime.now(UTC).replace(tzinfo=None),
                        kind="start", ref=current, context=f"wave:{sid}", client="pwa"))
    response = await env["client"].post(f"/api/wave/{sid}/next", json={
        "played": [], "queued": [t["ref"] for t in body["tracks"][1:]], "preferences": {"energy": "calm"}})
    assert response.status_code == 200, response.text
    updated = response.json()
    assert current not in {t["ref"] for t in updated["tracks"]}
    assert wave._rt[sid].state.started == 1 and wave._rt[sid].state.played[-1].ref == current
    assert updated["preferences"]["energy"] == "calm" and updated["preferences_status"]


async def test_playback_error_is_not_an_early_skip():
    rt = wave.Runtime(1, "normal", state(cand(1)), profile=profile())
    rt.state.out["dz:1"] = rt.state.pool.pop("dz:1")
    event = PlayEvent(kind="error", ref="dz:1", position_s=0, event_id="failed-audio")
    await wave.learn("s", rt, [event])
    assert rt.state.started == 0 and not rt.state.neg and not rt.state.skips
    assert "dz:1" in rt.state.out
