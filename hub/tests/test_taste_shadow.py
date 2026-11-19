from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from app.catalog.models import CatalogTrack
from app.config import MixesCfg
from app.mixes.build import Cand, ReplayCandidate, ReplayInputs
from app.mixes.shadow import compare, profiles_for, sound_diff
from app.mixes.taste_math import calculate_baseline
from app.mixes.taste_snapshot import TasteEvent, TasteSnapshot, TrackMetadata


def frozen(candidates, cfg, now, snapshot):
    inputs = ReplayInputs(tuple(ReplayCandidate(c, False, 1.0) for c in candidates),
                          len(candidates), dict(cfg.weights), {})
    active = calculate_baseline(snapshot, now=now, timezone="UTC", mode="occurred-at")
    return inputs, profiles_for(snapshot, active, "UTC")


def local_report(snapshot, candidates, now, cfg):
    inputs, profiles = frozen(candidates, cfg, now, snapshot)
    return compare(snapshot, inputs, profiles, active_model="v1", mode="normal", timezone="UTC", cfg=cfg)


def test_shadow_is_deterministic_and_does_not_mutate_candidates():
    now = datetime(2026, 10, 27, 12)
    event = TasteEvent(1, "one", now - timedelta(days=9), "finish", "dz:1", received_at=now)
    snapshot = TasteSnapshot(1, (event,), metadata=(TrackMetadata("dz:1", artist="Artist", title="Song"),))
    candidate = Cand(CatalogTrack(ref="dz:1", source="dz", id="1", title="Song", artist="Artist"),
                     gens={"favorites": 0}, sim=.9, score=42)
    before = deepcopy(candidate)
    first = local_report(snapshot, [candidate], now, MixesCfg())
    assert first == local_report(snapshot, [candidate], now, MixesCfg())
    assert candidate == before
    assert first["sound"] is None
    assert first["profiles"]["v2"]["ranking"][0]["score_without_sound"] == 1
    assert first["profiles"]["v2"]["favorites"] == ["dz:1"]


def test_rejected_candidates_and_score_contributions_explain_ranking_diff():
    now = datetime(2026, 10, 27, 12)
    events = (TasteEvent(1, "skip", now - timedelta(days=5), "skip", "dz:1",
                         received_at=now, position_s=5),)
    snapshot = TasteSnapshot(1, events, metadata=(TrackMetadata("dz:1", artist="Artist", title="Song"),))
    candidates = [Cand(CatalogTrack(ref="dz:1", source="dz", id="1", title="Song", artist="Artist"),
                       gens={"favorites": 0}),
                  Cand(CatalogTrack(ref="dz:2", source="dz", id="2", title="Other (Live)", artist="Artist"),
                       gens={"favorites": 1})]
    result = local_report(snapshot, candidates, now, MixesCfg())
    v1 = {row["key"]: row for row in result["profiles"]["v1"]["candidate_decisions"]}
    v2 = {row["key"]: row for row in result["profiles"]["v2"]["candidate_decisions"]}
    assert v1["dz:1"]["rejection_reason"] == "negative_score"
    assert v2["dz:1"]["kept"] and v2["dz:1"]["rank"] == 1
    assert v2["dz:2"]["rejection_reason"] == "version"
    c = v2["dz:1"]["contributions"]
    assert v2["dz:1"]["score_without_sound"] == sum(c["generators"].values()) + c["popularity"] + \
        c["track_penalty"] + c["artist_penalty"]
    assert result["diff"]["added"] == ["dz:1"]


def test_local_seeds_do_not_claim_cold_or_metadata_enrichment():
    now = datetime(2026, 10, 27, 12)
    event = TasteEvent(1, "like", now, "like", "dz:1", received_at=now)
    snapshot = TasteSnapshot(1, (event,), picked_artists=({"name": "Picked", "ref": "dz:9"},))
    result = local_report(snapshot, [], now, MixesCfg(seeds=20))
    assert result["profiles"]["v2"]["maturity"] < 1
    assert "local_profile_seeds" in result["profiles"]["v2"]
    assert "seeds" not in result["profiles"]["v2"]
    assert any("cold allocation" in item for item in result["limitations"])


async def test_shadow_sound_uses_frozen_scores_and_preserves_missing(monkeypatch):
    from app.mixes import embedder

    now = datetime(2026, 10, 27, 12)
    event = TasteEvent(1, "one", now - timedelta(days=9), "finish", "dz:1", received_at=now)
    snapshot = TasteSnapshot(1, (event,), metadata=(TrackMetadata("dz:1", artist="Artist", title="Song"),))
    candidates = [Cand(CatalogTrack(ref=f"dz:{n}", source="dz", id=str(n), title=f"Song {n}", artist="Artist"),
                       gens={"favorites": 0}) for n in (1, 2)]
    cfg = MixesCfg(size=1, reserve=1)
    inputs, profiles = frozen(candidates, cfg, now, snapshot)
    candidates[0].recent_offer_penalty = .1
    inputs = replace(inputs, candidates=(replace(inputs.candidates[0], multiplier=.25), inputs.candidates[1]),
                     weights={"favorites": .8})
    report = compare(snapshot, inputs, profiles, active_model="v1", mode="normal", timezone="UTC", cfg=cfg)
    live_selection = deepcopy(report["selection"])
    calls = []

    async def post(self, path, body):
        calls.append(path)
        return {"model": "fake", "space_id": "snapshot", "dim": 2,
                "v1": {"dz:1": -.8, "dz:2": .5}, "v2": {"dz:1": .8},
                "missing_v1": [], "missing_v2": ["dz:2"], "missing_baseline": [],
                "missing_positive": [], "missing_negative": [], "raw_vectors": {"dz:2": [1, 0]},
                "centered_vectors": {"dz:1": [0, 1]}}

    monkeypatch.setattr(embedder, "configured", lambda: True)
    monkeypatch.setattr(embedder.EmbedderClient, "_post", post)
    await sound_diff(report, inputs, profiles, cfg=cfg)
    assert calls == ["/v2/shadow"]
    assert report["sound"]["space_id"] == "snapshot"
    assert report["sound"]["common_count"] == 1
    assert report["sound"]["missing_v2"] == ["dz:2"]
    assert [row["key"] for row in report["sound"]["rankings"]["v1"]] == ["dz:1"]
    assert report["sound"]["rankings"]["v1"][0]["score"] == pytest.approx((.8 - 1.5 * .8) / .25 - .1)
    for model in ("v1", "v2"):
        row = report["profiles"][model]["candidate_decisions"][0]
        assert row["score_without_sound"] == pytest.approx(.8 * .25 - .1)
    assert report["sound"]["selection"]["models"]["v1"]["order_vector_missing"] == ["dz:1"]
    assert report["sound"]["selection"]["models"]["v2"]["sound_missing"] == ["dz:2"]
    assert report["selection"] == live_selection


async def test_shadow_sound_old_service_keeps_profile_report(monkeypatch):
    from app.mixes import embedder

    async def post(self, path, body):
        raise embedder.EmbedderDown("old service")

    monkeypatch.setattr(embedder, "configured", lambda: True)
    monkeypatch.setattr(embedder.EmbedderClient, "_post", post)
    now = datetime(2026, 10, 27, 12)
    snapshot = TasteSnapshot(1)
    cfg = MixesCfg()
    inputs, profiles = frozen([], cfg, now, snapshot)
    report = compare(snapshot, inputs, profiles, active_model="v1", mode="normal", timezone="UTC", cfg=cfg)
    await sound_diff(report, inputs, profiles, cfg=cfg)
    assert report["sound"] is None and report["sound_status"] == "unavailable"
    assert report["profiles"]["v1"]["candidate_decisions"] == []


def test_replay_selection_preserves_pool_order_and_limits_sound_to_first_300():
    cfg = MixesCfg(size=2, reserve=0)
    now = datetime(2026, 10, 27, 12)
    candidates = [Cand(CatalogTrack(ref=f"dz:{i}", source="dz", id=str(i), title=f"Song {i}", artist=f"Artist {i}"),
                       gens={"favorites": 0}) for i in range(301)]
    keys = [c.key for c in candidates]
    candidates[-1].sim = 100.0
    report = local_report(TasteSnapshot(1), candidates, now, cfg)
    assert report["selection"]["models"]["v1"]["active"] == keys[:2]
    assert report["selection"]["models"]["v2"]["active"] == keys[:2]
    assert report["selection"]["models"]["v1"]["sound_considered"] == 300
    assert all(c.score == 0 for c in candidates) and candidates[-1].sim == 100


def test_replay_selection_refuses_incomplete_pool():
    cfg = MixesCfg()
    snapshot = TasteSnapshot(1)
    inputs, profiles = frozen([], cfg, datetime(2026, 10, 27), snapshot)
    report = compare(snapshot, replace(inputs, total=501), profiles, active_model="v2", mode="normal",
                     timezone="UTC", cfg=cfg)
    assert not report["pool_complete"] and report["total_candidates"] == 501
    assert report["selection"] == {"status": "incomplete_pool"}


@pytest.mark.parametrize("model", ["v1", "v2"])
def test_replay_selection_uses_frozen_pairwise_coordinates_for_order(model):
    cfg = MixesCfg(size=3, reserve=0)
    now = datetime(2026, 10, 27, 12)
    candidates = [Cand(CatalogTrack(ref=f"dz:{i}", source="dz", id=str(i), title=f"Song {i}", artist=f"Artist {i}"),
                       gens={"favorites": 0}) for i in range(3)]
    keys = [c.key for c in candidates]
    for c in candidates:
        c.sim = 0.0
    vectors = {keys[0]: [1.0, 0.0], keys[1]: [0.0, 1.0], keys[2]: [1.0, 0.0]}
    snapshot = TasteSnapshot(1)
    inputs, profiles = frozen(candidates, cfg, now, snapshot)
    report = compare(snapshot, replace(inputs, vectors=vectors), profiles, active_model=model, mode="normal",
                     cfg=cfg, timezone="UTC")
    selected = report["selection"]["models"]
    assert selected[model]["active"] == [keys[0], keys[2], keys[1]]
    assert selected[model]["audio_source"] == "live_build"
    other = "v2" if model == "v1" else "v1"
    assert selected[other]["active"] == keys and selected[other]["audio_source"] == "no_alternate_audio"
