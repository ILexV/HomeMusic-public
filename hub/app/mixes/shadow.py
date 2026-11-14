"""Bounded local diagnostic on one snapshot and a frozen candidate pool.

This is not a replay of generators or audio geometry and never calls external services.
"""
import asyncio
from copy import deepcopy
from datetime import datetime
from math import log2

from app.matching.normalize import fingerprint
from app.mixes import build, embedder
from app.mixes.taste_math import V2Profile, calculate_baseline, calculate_profile, saturation
from app.mixes.taste_select import source_weights
from app.mixes.taste_snapshot import TasteSnapshot, utc


def compare(snapshot: TasteSnapshot, candidates: list[build.Cand], *, now: datetime, timezone: str, cfg) -> dict:
    profiles = {
        "v1": calculate_baseline(snapshot, now=now, timezone=timezone, mode="occurred-at"),
        "v2": calculate_profile(snapshot, now=now, timezone=timezone, mode="occurred-at"),
    }
    summaries = {}
    for model, profile in profiles.items():
        rows = []
        for original in candidates:
            c = deepcopy(original)
            c.sim = None  # there is no shared geometry snapshot
            reason = build.keep_reason(c, profile, cfg)
            contributions = None
            if reason is None:
                generators = {g: cfg.weights.get(g, .5) / log2(pos + 2) for g, pos in c.gens.items()}
                known = profile.find(ref=c.track.ref, nd_song_id=c.nd_song_id,
                                     fp=fingerprint(c.track.artist, c.track.title))
                artist_score = profile.artists.get(c.artist, 0)
                contributions = {"generators": generators, "popularity": build.POP_WEIGHT * build.popularity(c.track.rank),
                                 "track_penalty": .5 * max(known.score, -4) if isinstance(profile, V2Profile)
                                 and known and known.score < 0 else 0,
                                 "artist_penalty": .3 * max(artist_score, -3) if isinstance(profile, V2Profile)
                                 and artist_score < 0 else 0, "sound": None}
            rows.append({"key": c.key, "kept": reason is None, "rejection_reason": reason,
                         "familiar": c.familiar if reason is None else None,
                         "score_without_sound": build.score(c, cfg.weights, profile) if reason is None else None,
                         "contributions": contributions, "rank": None})
        ranked = sorted((r for r in rows if r["kept"]), key=lambda r: (-r["score_without_sound"], r["key"]))
        for rank, row in enumerate(ranked, 1):
            row["rank"] = rank
        summaries[model] = {
            "local_profile_seeds": [t.key for t in profile.seeds(min(cfg.seeds, 8))],
            "eligible_seeds": [t.key for t in profile.active_seed_pool()][:100] if model == "v2" else None,
            "favorites": [t.ref for t in build.favorites(profile)[:20]],
            "artists": [{"key": k, "score": v} for k, v in
                        sorted(profile.artists.items(), key=lambda x: (-x[1], x[0]))[:20]],
            "maturity": getattr(profile, "maturity", None),
            "tracks": {k: {"score": t.score, "P": t.positive_raw, "N": t.negative,
                           "saturation": saturation(t.positive_raw), "age": t.positive_age}
                       for k, t in sorted(profile.tracks.items(), key=lambda x: (-x[1].score, x[0]))[:20]},
            "kept": len(ranked), "ranking": ranked[:20], "candidate_decisions": rows,
        }
    a = {r["key"] for r in summaries["v1"]["ranking"]}
    b = {r["key"] for r in summaries["v2"]["ranking"]}
    return {"schema": 1, "snapshot_time": utc(now).isoformat(), "timezone": timezone,
            "candidate_count": len(candidates), "profiles": summaries,
            "policy_versions": {"baseline": "v1", "candidate": "v2"},
            "outcome_comparison": {
                "provenance": "frozen candidate pool; no counterfactual playback",
                "since": None, "until": utc(now).isoformat(), "sample_size": 0,
                "completion_rate": None, "early_skip_rate": None, "playable_full_recording_rate": None,
                "unknown_candidates": len(candidates),
                "limitations": ["Shadow ranking is not exposure: completion, early skips and full recording availability are unknown.",
                                "Observed account outcomes with N and date windows are reported by /api/recommendations/quality.",
                                "Neither ranking overlap nor historical observations establish causal policy improvement."]},
            "diff": {"added": sorted(b - a), "removed": sorted(a - b), "overlap": len(a & b)},
            "sound": None, "facets": None,
            "limitations": ["local v1 baseline is not the enriched online v1 profile",
                            "candidate pool comes from the active model, limited to first 500; generators were not rerun",
                            "rankings omit sound and final selection/order",
                            "local profile seeds limited to 8 omit cold allocation, enrichment and metadata filtering",
                            "active seed keys are limited to 100",
                            *snapshot.limitations]}


async def sound_diff(report: dict, snapshot: TasteSnapshot, candidates: list[build.Cand], *, now: datetime,
                     timezone: str, cfg=None, mode: str | None = None) -> None:
    """Read only frozen-vector comparison; absent vectors are never interpreted as zero."""
    if not embedder.configured():
        report["sound_status"] = "off"
        return
    baseline = calculate_baseline(snapshot, now=now, timezone=timezone, mode="occurred-at")
    v2 = calculate_profile(snapshot, now=now, timezone=timezone, mode="occurred-at")
    positive, negative = source_weights(v2)
    weights = dict(list(baseline.vector_profile().items())[:5000])
    try:
        async with embedder.EmbedderClient() as client:
            data = await asyncio.wait_for(client._post("/v2/shadow", {
                "candidates": [c.key for c in candidates], "include_vectors": bool(mode and report.get("pool_complete")),
                "baseline": [{"key": k, "weight": w} for k, w in weights.items()],
                "positives": [{"key": k, "weight": w} for k, w in positive.items()],
                "negatives": negative,
            }), timeout=5)
        sound = embedder.ShadowSound.model_validate(data)
        keys = {c.key for c in candidates}
        for model in ("v1", "v2"):
            values = getattr(sound, model)
            missing = getattr(sound, f"missing_{model}")
            embedder._validate_coverage(list(keys), list(values), missing)
        if mode is not None and cfg is not None:
            replay_selection(report, candidates, {"v1": baseline, "v2": v2}, sound, cfg, mode)
        # Rank only the candidates scored for both models; missing audio cannot imply similarity zero.
        common = set(sound.v1) & set(sound.v2)
        ranked = {}
        for model in ("v1", "v2"):
            rows = report["profiles"][model]["candidate_decisions"]
            values = getattr(sound, model)
            factor = build.SIM_WEIGHT * (v2.maturity if model == "v2" else 1)
            scored = [{"key": row["key"], "similarity": values[row["key"]],
                       "sound_contribution": factor * values[row["key"]],
                       "score": row["score_without_sound"] + factor * values[row["key"]]}
                      for row in rows if row["kept"] and row["key"] in common]
            ranked[model] = sorted(scored, key=lambda r: (-r["score"], r["key"]))
        a = {x["key"] for x in ranked["v1"][:20]}
        b = {x["key"] for x in ranked["v2"][:20]}
        report["sound"] = {"status": "ok", "space_id": sound.space_id, "model": sound.model,
                           "dim": sound.dim, "common_count": len(common),
                           "missing_v1": sound.missing_v1, "missing_v2": sound.missing_v2,
                           "missing_baseline": sound.missing_baseline, "missing_positive": sound.missing_positive,
                           "missing_negative": sound.missing_negative,
                           "scope": "only common candidates with vectors; no selection or final order",
                           "rankings": {model: rows[:20] for model, rows in ranked.items()},
                           "diff": {"added": sorted(b - a), "removed": sorted(a - b), "overlap": len(a & b)}}
    except (TimeoutError, embedder.EmbedderDown, ValueError):
        report["sound_status"] = "unavailable"


def replay_selection(report: dict, candidates: list[build.Cand], profiles: dict, sound: embedder.ShadowSound,
                     cfg, mode: str) -> None:
    """Re-run local keep/select/order with copies and the same frozen vector snapshot."""
    if not report.get("pool_complete"):
        report["selection"] = {"status": "incomplete_pool"}
        return
    if sound.raw_vectors is None or sound.centered_vectors is None:
        report["selection"] = {"status": "vectors_unavailable"}
        return
    keys = {c.key for c in candidates}
    for vectors in (sound.raw_vectors, sound.centered_vectors):
        if not set(vectors) <= keys or any(len(v) != sound.dim for v in vectors.values()):
            raise ValueError("invalid frozen coordinates")
    selections = {}
    for model, profile in profiles.items():
        pool = [c for c in deepcopy(candidates) if build.keep(c, profile, cfg)]
        for c in pool:
            c.sim = None
            c.score = build.score(c, cfg.weights, profile)
        pool.sort(key=lambda c: -c.score)  # stable: merge order breaks ties in the active build
        sound_keys = {c.key for c in pool[:build.EMBED_CANDIDATES]}
        scores = getattr(sound, model)
        vectors = sound.raw_vectors if model == "v1" else sound.centered_vectors
        for c in pool:
            if c.key in sound_keys:
                c.sim = scores.get(c.key)
            c.score = build.score(c, cfg.weights, profile)
        active, reserve = build.select_songs(pool, mode, cfg.size, cfg.reserve, cfg.max_per_artist)
        active = build.order(active, {k: v for k, v in vectors.items() if k in sound_keys})
        selections[model] = {"active": [c.key for c in active], "reserve": [c.key for c in reserve],
                             "sound_considered": len(sound_keys),
                             "sound_missing": sorted(sound_keys - set(scores)),
                             "order_vector_missing": sorted(sound_keys - set(vectors))}
    a, b = set(selections["v1"]["active"]), set(selections["v2"]["active"])
    report["selection"] = {"status": "ok", "models": selections,
                           "added": sorted(b - a), "removed": sorted(a - b),
                           "scope": "frozen pool and vector snapshot; no alternate generators or enrichment"}
