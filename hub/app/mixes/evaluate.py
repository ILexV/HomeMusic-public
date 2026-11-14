"""Read-only local replay: python -m app.mixes.evaluate --help.

This evaluates the *history-score proxy*, not an unrecorded recommendation policy.
No application lifecycle, migrations, catalog, embedder or Navidrome client is started.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.mixes.taste_math import (
    EXPERIMENTS,
    V2Profile,
    calculate_baseline,
    calculate_offline,
    calculate_profile,
    recommended,
    visible_events,
)
from app.mixes.taste_snapshot import TasteEvent, TasteSnapshot, utc


def parse_time(value: str) -> datetime:
    return utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


@contextmanager
def readonly_db(path: Path):
    """No init_engine: that path enables WAL and creates files/migrations."""
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.row_factory = sqlite3.Row
        yield connection
    finally:
        connection.close()


def read_snapshot(connection, *, user_id: int, until: datetime,
                  excluded_user_ids: frozenset[int] = frozenset()) -> TasteSnapshot:
    """Only immutable journal facts enter a historical cut.

    Current subjects, downloads, onboarding, disliked rows and local catalog metadata
    have no version history. They are intentionally omitted, even when created_at is old:
    their contents/status may have changed after T. Event target_data remains available.
    """
    fields = TasteEvent.__dataclass_fields__
    events = []
    missing_received = False
    for row in connection.execute("SELECT * FROM play_events WHERE user_id = ?", (user_id,)):
        data = {k: row[k] for k in row.keys() if k in fields}
        data["ts"] = parse_time(data["ts"])
        if data["ts"] > utc(until):
            continue
        if data.get("received_at"):
            data["received_at"] = parse_time(data["received_at"])
        else:
            # Unknown availability cannot honestly be substituted by occurred-at time.
            missing_received = True
            data["received_at"] = datetime.max
        if isinstance(data.get("target_data"), str):
            data["target_data"] = json.loads(data["target_data"])
        events.append(TasteEvent(**data))
    limitations = ["Historical state before journal coverage is incomplete; unjournalled removals are unknown.",
        "Current likes/dislikes, onboarding, downloads, aliases and metadata were not imported into the past.",
        "Historical vectors, facets, generator contributions and exposure propensities are not recorded.",
        "v1 baseline uses corrected B ledger and local events only; phantom ND/network heuristics are not reproduced."]
    if missing_received:
        limitations.append("Events without received_at are unavailable in available-at mode.")
    return TasteSnapshot(user_id, tuple(events), excluded_user_ids=excluded_user_ids, limitations=tuple(limitations))


def profile_for(snapshot, *, until, timezone, model, mode):
    if model == "v1":
        return calculate_baseline(snapshot, now=until, timezone=timezone, mode=mode)
    if model == "v2":
        return calculate_profile(snapshot, now=until, timezone=timezone, mode=mode)
    return calculate_offline(snapshot, now=until, timezone=timezone, mode=mode, experiment=model)


def auc(labels: list[int], scores: list[float]) -> float | None:
    """Mann–Whitney AUC with average ties, without a heavyweight ML dependency."""
    positives, negatives = sum(labels), len(labels) - sum(labels)
    if not positives or not negatives:
        return None
    ordered = sorted(zip(scores, labels, strict=True))
    rank_sum, i = 0.0, 0
    while i < len(ordered):
        j = i + 1
        while j < len(ordered) and ordered[j][0] == ordered[i][0]:
            j += 1
        rank_sum += sum(label for _, label in ordered[i:j]) * (i + 1 + j) / 2
        i = j
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def bootstrap_days(rows: list[dict], *, seed: int, samples: int = 300) -> dict:
    labels = [r["label"] for r in rows]
    value = auc(labels, [r["score"] for r in rows])
    if value is None:
        return {"auc": None, "reason": "need_finish_and_earlyskip_classes", "n": len(rows), "ci95": None}
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["day"]].append(row)
    days = sorted(grouped)
    if len(days) < 2:
        return {"auc": value, "reason": "need_two_days_for_bootstrap", "n": len(rows), "ci95": None}
    rng = random.Random(seed)
    values = []
    for _ in range(samples):
        draw = [row for day in rng.choices(days, k=len(days)) for row in grouped[day]]
        result = auc([r["label"] for r in draw], [r["score"] for r in draw])
        if result is not None:
            values.append(result)
    values.sort()
    interval = [values[int((len(values) - 1) * .025)], values[int((len(values) - 1) * .975)]] if values else None
    return {"auc": value, "reason": None if values else "bootstrap_no_two_class_samples", "n": len(rows),
            "days": len(days), "ci95": interval, "bootstrap_samples": len(values)}


def outcomes(snapshot, *, until, timezone, model, mode):
    rows = []
    zone = ZoneInfo(timezone)
    for e in visible_events(snapshot, utc(until), mode):
        if e.target_type != "track" or not recommended(e.context):
            continue
        if e.kind != "finish" and not (e.kind == "skip" and e.position_s is not None and e.position_s < 30):
            continue
        # Never train on the outcome being evaluated, nor on future/same-ts outcomes.
        at = e.ts - timedelta(microseconds=1)
        past = replace(snapshot, subjects=(), metadata=(), downloads=(), dislikes=(), picked_artists=())
        p = profile_for(past, until=at, timezone=timezone, model=model, mode=mode)
        t = p.find(ref=e.ref, nd_song_id=e.nd_song_id)
        rows.append({"event_id": e.event_id, "label": int(e.kind == "finish"),
                     "score": t.score if t else 0.0,
                     "day": e.ts.replace(tzinfo=UTC).astimezone(zone).date().isoformat()})
    return rows


def _ranking(profile):
    return [t.key for t in sorted(profile.tracks.values(), key=lambda t: (-t.score, t.key)) if t.score > 0][:20]


def _summary(profile):
    if isinstance(profile, V2Profile):
        seeds = [t.key for t in profile.active_seed_pool()]
    else:
        seeds = [t.key for t in profile.seeds(8)]
    return {"top20": _ranking(profile), "seed_pool": seeds,
        "seed_pool_semantics": "eligible v2 pool; final facet-weighted rotation belongs to F" if isinstance(
            profile, V2Profile) else "local v1 seeds; metadata may be missing",
        "artists": [{"key": k, "name": profile.artist_names.get(k), "score": v}
                    for k, v in sorted(profile.artists.items(), key=lambda x: (-x[1], x[0]))[:20]],
        "evidence": getattr(profile, "evidence", None), "maturity": getattr(profile, "maturity", None),
        "tracks": {key: {"score": t.score, "P": t.positive_raw, "N": t.negative, "liked": t.liked,
                         "heard30": t.heard30, "familiar": t.familiar,
                         "last_positive": t.last_positive.isoformat() if t.last_positive else None}
                   for key, t in sorted(profile.tracks.items())}}


def _spearman(left: dict, right: dict):
    keys = sorted(left.keys() & right.keys())
    if len(keys) < 2:
        return None

    def ranks(source):
        ordered = sorted(keys, key=lambda k: source[k].score)
        result, i = {}, 0
        while i < len(ordered):
            j = i + 1
            while j < len(ordered) and source[ordered[j]].score == source[ordered[i]].score:
                j += 1
            for key in ordered[i:j]:
                result[key] = (i + j - 1) / 2
            i = j
        return [result[k] for k in keys]

    x, y = ranks(left), ranks(right)
    mx, my = sum(x) / len(x), sum(y) / len(y)
    denom = math.sqrt(sum((v - mx) ** 2 for v in x) * sum((v - my) ** 2 for v in y))
    return sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True)) / denom if denom else None


def evaluate(snapshot: TasteSnapshot, *, until: datetime, timezone: str, model: str, seed: int,
             mode: str = "available-at") -> dict:
    if model not in ("v1", "v2", *EXPERIMENTS):
        raise ValueError("unknown replay model")
    p = profile_for(snapshot, until=until, timezone=timezone, model=model, mode=mode)
    baseline = profile_for(snapshot, until=until, timezone=timezone, model="v1", mode=mode)
    selected, previous = _summary(p), _summary(baseline)
    left, right = set(selected["top20"]), set(previous["top20"])
    return {"schema": 1, "user_id": snapshot.user_id, "until": utc(until).isoformat(), "timezone": timezone,
        "mode": mode, "model": model, "seed": seed, "profile": selected, "v1_baseline": previous,
        "diff": {"top20_jaccard": len(left & right) / len(left | right) if left | right else None,
                 "seeds_added": sorted(set(selected["seed_pool"]) - set(previous["seed_pool"])),
                 "seeds_removed": sorted(set(previous["seed_pool"]) - set(selected["seed_pool"])),
                 "score_spearman": _spearman(p.tracks, baseline.tracks)},
        "facets": None, "vectors": {"status": "missing_historical_snapshot", "missing": sorted(p.tracks)},
        "similarity_distribution": None,
        "score_contributions": {"history": {k: t.score for k, t in sorted(p.tracks.items())},
                                "generators": None, "sound": None, "popularity": None},
        "quality": {"metric": "pre_outcome_history_score_proxy_auc; not full recommendation-policy quality",
                    "selected": bootstrap_days(outcomes(snapshot, until=until, timezone=timezone, model=model,
                                                         mode=mode), seed=seed),
                    "v1": bootstrap_days(outcomes(snapshot, until=until, timezone=timezone, model="v1",
                                                   mode=mode), seed=seed)},
        "limitations": list(snapshot.limitations)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--until", type=parse_time, required=True, help="UTC ISO timestamp")
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--model", choices=("v1", "v2", *EXPERIMENTS), required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timezone", required=True, help="IANA zone, e.g. Europe/Moscow")
    parser.add_argument("--mode", choices=("occurred-at", "available-at"), default="available-at")
    parser.add_argument("--exclude-user-id", type=int, action="append", default=[], help="explicit known QA ids")
    args = parser.parse_args(argv)
    database = args.db.resolve()
    output = args.output.resolve()
    if (output in {database, Path(str(database) + "-wal"), Path(str(database) + "-shm"),
                   Path(str(database) + "-journal")} or
            (output.exists() and database.exists() and output.samefile(database))):
        parser.error("output must not overwrite the source database")
    with readonly_db(args.db) as connection:
        snapshot = read_snapshot(connection, user_id=args.user_id, until=args.until,
                                 excluded_user_ids=frozenset(args.exclude_user_id))
        from app.matching.replay_quality import observed_quality
        observed = (observed_quality(connection, user_id=args.user_id, until=args.until,
                                     available_at=args.mode == "available-at")
                    if args.user_id not in args.exclude_user_id else None)
    result = evaluate(snapshot, until=args.until, timezone=args.timezone, model=args.model, seed=args.seed,
                      mode=args.mode)
    result["observed_quality"] = observed
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
                           encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
