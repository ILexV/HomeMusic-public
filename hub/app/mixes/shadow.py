"""Replay the local selection on build inputs; optional cross-model audio diagnostics are separate."""
import asyncio
from copy import copy

from app.mixes import build, embedder
from app.mixes.taste import Profile
from app.mixes.taste_math import V2Profile, calculate_baseline, calculate_profile, saturation
from app.mixes.taste_select import source_weights
from app.mixes.taste_snapshot import TasteSnapshot, utc


def profiles_for(snapshot: TasteSnapshot, active: Profile, timezone: str) -> dict[str, Profile]:
    """The active profile is the actual (possibly enriched) instance, not a local approximation."""
    if isinstance(active, V2Profile):
        baseline = calculate_baseline(snapshot, now=active.now, timezone=timezone, mode="occurred-at")
        return {"v1": baseline, "v2": active}
    return {"v1": active, "v2": calculate_profile(snapshot, now=active.now, timezone=timezone, mode="occurred-at")}


def _without_sound(inputs: build.ReplayInputs, profile: Profile, cfg):
    rows, pool = [], []
    for entry in inputs.candidates:
        c = copy(entry.candidate)
        c.sim, c.familiar, c.novelty = None, False, None
        reason = "temporary_pause" if entry.blocked else build.keep_reason(c, profile, cfg)
        contributions = None
        if reason is None:
            build.apply_score(c, inputs.weights, profile, entry.multiplier)
            contributions = c.parts  # apply_score allocated this row's parts on the candidate copy
            contributions["generators"] = contributions.pop("generator_contributions")
            contributions["sound"] = None
        row = {"key": c.key, "kept": reason is None, "rejection_reason": reason,
               "familiar": c.familiar if reason is None else None, "novelty": c.novelty if reason is None else None,
               "rank": None,
               "score_without_sound": c.score if reason is None else None, "contributions": contributions}
        rows.append(row)
        if reason is None:
            pool.append((c, entry.multiplier, row))
    pool.sort(key=lambda item: -item[0].score)  # stable: merge order breaks score ties
    for rank, (_, _, row) in enumerate(pool, 1):
        row["rank"] = rank
    return rows, pool


def _selection(pool, weights, profile, scores, vectors, cfg, mode, source):
    considered = set()
    for i, (c, multiplier, _) in enumerate(pool):
        if i < build.EMBED_CANDIDATES:
            considered.add(c.key)
            c.sim = scores.get(c.key)
        else:
            c.sim = None
        build.apply_score(c, weights, profile, multiplier)
    active, reserve = build.select_songs([c for c, _, _ in pool], mode, cfg.size, cfg.reserve, cfg.max_per_artist)
    active = build.order(active, {k: v for k, v in vectors.items() if k in considered})
    return {"active": [c.key for c in active], "reserve": [c.key for c in reserve], "audio_source": source,
            "sound_considered": len(considered), "sound_missing": sorted(considered - scores.keys()),
            "order_vector_missing": sorted(considered - vectors.keys())}


def _diff(a, b):
    return {"added": sorted(b - a), "removed": sorted(a - b), "overlap": len(a & b)}


def _selection_report(models, complete):
    if not complete:
        return {"status": "incomplete_pool"}
    if len(models) != 2:
        return {"status": "vectors_unavailable"}
    return {"status": "ok", "models": models, **_diff(set(models["v1"]["active"]), set(models["v2"]["active"]))}


def compare(snapshot: TasteSnapshot, inputs: build.ReplayInputs, profiles: dict[str, Profile], *,
            active_model: str, mode: str, timezone: str, cfg) -> dict:
    now = profiles[active_model].now
    complete = len(inputs.candidates) == inputs.total
    summaries, selections = {}, {}
    scores = {e.candidate.key: e.candidate.sim for e in inputs.candidates if e.candidate.sim is not None}
    for model, profile in profiles.items():
        rows, pool = _without_sound(inputs, profile, cfg)
        ranked = [row for _, _, row in pool]
        if complete:
            live = model == active_model
            selections[model] = _selection(pool, inputs.weights, profile, scores if live else {},
                                           inputs.vectors if live else {}, cfg, mode,
                                           "live_build" if live else "no_alternate_audio")
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
            "active_model": active_model, "mode": mode, "source_weights": inputs.weights,
            "candidate_count": len(inputs.candidates), "total_candidates": inputs.total, "pool_complete": complete,
            "profiles": summaries,
            "policy_versions": {"baseline": "v1", "candidate": "v2"},
            "outcome_comparison": {
                "provenance": "frozen candidate pool; no counterfactual playback",
                "since": None, "until": utc(now).isoformat(), "sample_size": 0,
                "completion_rate": None, "early_skip_rate": None, "playable_full_recording_rate": None,
                "unknown_candidates": len(inputs.candidates),
                "limitations": ["Shadow ranking is not exposure: completion, early skips and full recording availability are unknown.",
                                "Observed account outcomes with N and date windows are reported by /api/recommendations/quality.",
                                "Neither ranking overlap nor historical observations establish causal policy improvement."]},
            "diff": _diff(a, b), "sound": None, "facets": None,
            "selection": _selection_report(selections, complete),
            "limitations": ["alternate profile is local; no generators or enrichment were rerun",
                            "only the first 500 candidates are captured; no selection replay for a truncated pool",
                            "profile rankings omit sound; selection reports missing scores and order vectors separately",
                            f"audio selection considers at most the first {build.EMBED_CANDIDATES} by the pre-audio score",
                            "only the active selection reuses live audio; the alternate selection omits audio",
                            "local profile seeds limited to 8 omit cold allocation, enrichment and metadata filtering",
                            "active seed keys are limited to 100",
                            *snapshot.limitations]}


async def sound_diff(report: dict, inputs: build.ReplayInputs, profiles: dict[str, Profile], *, cfg) -> None:
    """Optional /v2/shadow comparison on a separate geometry; never substitutes for captured build audio."""
    if not embedder.configured():
        report["sound_status"] = "off"
        return
    positive, negative = source_weights(profiles["v2"])
    weights = dict(list(profiles["v1"].vector_profile().items())[:5000])
    keys = list(dict.fromkeys(e.candidate.key for e in inputs.candidates))
    try:
        async with embedder.EmbedderClient() as client:
            data = await asyncio.wait_for(client._post("/v2/shadow", {
                "candidates": keys, "include_vectors": report["pool_complete"],
                "baseline": [{"key": k, "weight": w} for k, w in weights.items()],
                "positives": [{"key": k, "weight": w} for k, w in positive.items()],
                "negatives": negative,
            }), timeout=5)
        sound = embedder.ShadowSound.model_validate(data)
        for model in ("v1", "v2"):
            values = getattr(sound, model)
            missing = getattr(sound, f"missing_{model}")
            embedder._validate_coverage(keys, list(values), missing)
        common = sound.v1.keys() & sound.v2.keys()
        ranked, selections = {}, {}
        for model, profile in profiles.items():
            scores = getattr(sound, model)
            vectors = sound.raw_vectors if model == "v1" else sound.centered_vectors
            if vectors is not None and (not vectors.keys() <= set(keys) or any(
                    len(v) != sound.dim for v in vectors.values())):
                raise ValueError("invalid diagnostic coordinates")
            _, pool = _without_sound(inputs, profile, cfg)
            scored = []
            for c, multiplier, row in pool:
                if c.key in common:
                    c.sim = scores[c.key]
                    build.apply_score(c, inputs.weights, profile, multiplier)
                    scored.append({"key": c.key, "similarity": c.sim, "score": c.score,
                                   "sound_contribution": c.score - row["score_without_sound"]})
            ranked[model] = sorted(scored, key=lambda r: -r["score"])
            if report["pool_complete"] and vectors is not None:
                selections[model] = _selection(pool, inputs.weights, profile, scores, vectors, cfg,
                                               report["mode"], "diagnostic_snapshot")
        a = {x["key"] for x in ranked["v1"][:20]}
        b = {x["key"] for x in ranked["v2"][:20]}
        report["sound"] = {"status": "ok", "space_id": sound.space_id, "model": sound.model,
                           "dim": sound.dim, "common_count": len(common),
                           "missing_v1": sound.missing_v1, "missing_v2": sound.missing_v2,
                           "missing_baseline": sound.missing_baseline, "missing_positive": sound.missing_positive,
                           "missing_negative": sound.missing_negative,
                           "scope": "separate audio snapshot, not the live build; rankings use only common scores",
                           "selection": _selection_report(selections, report["pool_complete"]),
                           "rankings": {model: rows[:20] for model, rows in ranked.items()},
                           "diff": _diff(a, b)}
    except (TimeoutError, embedder.EmbedderDown, ValueError):
        report["sound_status"] = "unavailable"
