#!/usr/bin/env python3
"""Offline metadata match evaluation: hub/.venv/bin/python eval/run_music_eval.py.

Runs production ranking and automatic selection with frozen labels, never providers,
application startup, settings/env loading, user history or an audio download.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hub"))

from app.config import MatchingCfg
from app.matching.select import classify, rank_candidates
from app.providers.base import Candidate, TrackQuery


def evaluate(corpus: dict, cfg: MatchingCfg | None = None) -> dict:
    cfg = cfg or MatchingCfg()
    rows = []
    for case in corpus["cases"]:
        candidates = [Candidate(provider="fixture", provider_id=row["id"],
            page_url=f"https://example.invalid/{row['id']}",
            **{key: value for key, value in row.items() if key != "id"}) for row in case["candidates"]]
        ranked = rank_candidates(TrackQuery(**case["query"]), candidates, cfg)
        verdict = classify(ranked, cfg)
        selected = ranked[0].provider_id if ranked else None
        automatic = verdict == "auto"
        false_auto = automatic and (selected not in case["acceptable"] or not case["automatic_allowed"])
        rows.append({"id": case["id"], "verdict": verdict, "selected": selected,
            "top_correct": selected in case["acceptable"] if selected else not case["acceptable"],
            "automatic": automatic, "false_automatic": false_auto,
            "ranked": [{"id": c.provider_id, "score": c.score} for c in ranked]})
    auto_n = sum(row["automatic"] for row in rows)
    false_n = sum(row["false_automatic"] for row in rows)
    return {"corpus_version": corpus["version"], "provenance": corpus["provenance"],
        "policy_version": "metadata-ranking-v2-named-versions", "policy": cfg.model_dump(mode="json"),
        "cases": len(rows), "automatic_matches": auto_n, "false_automatic_matches": false_n,
        "false_automatic_rate": false_n / auto_n if auto_n else None,
        "top_correct": sum(row["top_correct"] for row in rows), "results": rows,
        "limitations": ["Small fixed synthetic challenge set, not a production accuracy estimate.",
            "False automatic rate denominator is automatic decisions; all cases and abstentions are also reported.",
            "Metadata cannot prove audio identity, censoring or completeness; unknown duration is not verified full audio."]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=Path(__file__).with_name("music_matches.json"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    raw = args.corpus.read_bytes()
    result = evaluate(json.loads(raw))
    result["corpus_sha256"] = hashlib.sha256(raw).hexdigest()
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output:
        if args.output.resolve() == args.corpus.resolve():
            parser.error("output must not overwrite the labelled corpus")
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return int(result["false_automatic_matches"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
