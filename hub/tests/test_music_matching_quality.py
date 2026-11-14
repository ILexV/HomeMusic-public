"""Fixed identity labels exercise production scoring/ranking, not a copied score formula."""
import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from app.config import MatchingCfg
from app.matching.score import score
from app.matching.select import classify, rank_candidates
from app.matching.quality import recording_quality
from app.providers.base import Candidate, TrackQuery


def test_named_remixes_and_concert_venues_are_not_interchangeable():
    for query, wrong in (("Song (Alpha Remix)", "Song (Beta Remix)"),
                         ("Song (Live at Wembley)", "Song (Live in Rio)")):
        q = TrackQuery(artist="Artist", title=query, duration_s=240)
        candidate = Candidate(provider="fixture", provider_id="wrong", artist="Artist", title=wrong,
                              duration_s=240, page_url="https://example.invalid/wrong")
        ranked = rank_candidates(q, [candidate], MatchingCfg())
        assert classify(ranked, MatchingCfg()) != "auto"
        assert score(q.artist, q.title, candidate.artist, candidate.title).version_penalty > 0


def test_unspecified_remix_keeps_distinct_recordings_as_margin_competitors():
    candidates = [Candidate(provider="fixture", provider_id=label, artist="Artist", title=f"Song ({label} Remix)",
                            duration_s=240, page_url=f"https://example.invalid/{label}") for label in ("Alpha", "Beta")]
    ranked = rank_candidates(TrackQuery(artist="Artist", title="Song (Remix)", duration_s=240), candidates, MatchingCfg())
    assert len(ranked) == 2 and classify(ranked, MatchingCfg()) == "confirm"


def test_fixed_corpus_runs_production_selection_with_false_auto_denominator():
    root = Path(__file__).resolve().parents[2] / "eval"
    spec = importlib.util.spec_from_file_location("music_eval", root / "run_music_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    corpus = json.loads((root / "music_matches.json").read_text())
    report = module.evaluate(corpus)
    assert report["cases"] == 10
    assert report["false_automatic_matches"] == 0
    assert 0 < report["automatic_matches"] < report["cases"]
    # Adversarial policy must expose label violations rather than redefining the expected result.
    unsafe = module.evaluate(corpus, MatchingCfg(auto_threshold=0, auto_margin=0, artist_min_ratio=0,
                                               duration_tolerance_s=1000, reject_words=[]))
    assert unsafe["false_automatic_matches"] > 0
    assert unsafe["false_automatic_rate"] == unsafe["false_automatic_matches"] / unsafe["automatic_matches"]


def test_full_recording_metrics_preserve_unknown_and_asof_ownership():
    now = datetime(2026, 11, 14)
    def binding(value, *, user=1, served=True, verified_at=None):
        return SimpleNamespace(user_id=user, created_at=now - timedelta(hours=1),
            served_at=now if served else None, full_verified=value,
            verified_at=verified_at or (now if value is not None else None), provenance="stream-observation-v1")
    rows = [binding(True), binding(False), binding(None), binding(True, user=2),
            binding(True, served=False), binding(True, verified_at=now + timedelta(days=1))]
    result = recording_quality(rows, user_id=1, since=now - timedelta(days=7), until=now)
    assert (result["observations"], result["known"], result["full"], result["unknown"], result["rate"]) == (5, 2, 1, 3, .5)
    assert recording_quality([], user_id=1, since=now, until=now)["rate"] is None
