"""Candidate filtering, ranking and auto/confirm classification (PLAN.md 8.3, 8.5.3)."""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from app.catalog.models import CatalogTrack
from app.config import MatchingCfg
from app.matching.normalize import (
    _prep,
    explicit_marker,
    fingerprint,
    is_russian_query,
    normalize_title,
    to_latin,
    version_kind,
)
from app.matching.score import ScoreBreakdown, score, version_identity
from app.providers.base import Candidate, TrackQuery

Verdict = Literal["auto", "confirm", "not_found"]
BUCKET = 0.05


def _hay(*parts: str | None) -> str:
    s = _prep(" ".join(p for p in parts if p)).casefold()
    s = re.sub(r"[-_]+", " ", s)
    return f"{s} | {to_latin(s)}"


def _has_word(hay: str, word: str) -> bool:
    """Word-prefix match ("минус" hits "минусовка"); spaces in `word` also match "-", "_" or nothing."""
    pat = r"[\s\-_]*".join(re.escape(w) for w in word.casefold().split())
    return bool(pat) and re.search(rf"(?<!\w){pat}", hay) is not None


def _rejected(c: Candidate, q_hay: str, q_kind: str | None, cfg: MatchingCfg) -> bool:
    """A reject word in the candidate that the query does not ask for (by word or by version kind)."""
    c_hay = _hay(c.artist, c.title, normalize_title(c.title).version)
    for word in cfg.reject_words:
        if not _has_word(c_hay, word) or _has_word(q_hay, word):
            continue
        kind = version_kind(word)
        if kind is not None and kind == q_kind:
            continue
        return True
    return False


def _explicit(c: Candidate) -> bool | None:
    return c.explicit if c.explicit is not None else explicit_marker(c.title)


def needs_uncensored(q: TrackQuery) -> bool:
    """An explicit Russian track: since the 2026 law its new uploads and the official copies may be edited (PLAN 3.2),
    so a download takes a copy known to be the original before a possibly censored one."""
    return q.explicit is True and is_russian_query(q)


def trusted_original(c: Candidate, cfg: MatchingCfg) -> bool:
    """A copy that is not censored: a re-upload titled "(без цензуры)", or a file uploaded before the law that isn't
    a clean version. Official YouTube Music copies don't count by date: labels replace their audio in place."""
    marker = c.extra.get("uncensored")
    if marker is not None:
        return bool(marker)
    if _explicit(c) is False or c.extra.get("source") == "ytmusic":
        return False
    return c.upload_date is not None and c.upload_date < cfg.original_before


def uploaded_in_time(c: Candidate, cutoffs: dict[str, date]) -> bool:
    """False for a file its source uploaded on or after that source's cutoff (`sources.uploaded_before`); a file
    without a known upload date passes."""
    before = cutoffs.get(c.provider)
    return before is None or c.upload_date is None or c.upload_date < before


def rank_candidates(q: TrackQuery, cands: list[Candidate], cfg: MatchingCfg) -> list[Candidate]:
    """Score, hard-filter and sort candidates; returns scored copies, best first.

    Filters: artist similarity >= cfg.artist_min_ratio; duration within cfg.duration_tolerance_s (when both known);
    cfg.reject_words present in the candidate but not requested by the query.
    Sort: score in 0.05 buckets desc -> explicit (clean if q.explicit is False) -> bitrate >= 320 -> upload date
    (before cfg.original_before first, then unknown, then newer; earlier first within a group) -> original order.
    """
    q_version = q.version or normalize_title(q.title).version
    q_kind = version_kind(q_version)
    q_hay = _hay(q.artist, q.title, q.version)
    kept: list[tuple[tuple, Candidate]] = []
    for idx, c in enumerate(cands):
        if q.duration_s and c.duration_s and abs(q.duration_s - c.duration_s) > cfg.duration_tolerance_s:
            continue
        if _rejected(c, q_hay, q_kind, cfg):
            continue
        bd = score(
            q.artist,
            q.title,
            c.artist,
            c.title,
            query_album=q.album,
            cand_album=c.album,
            query_duration=q.duration_s,
            cand_duration=c.duration_s,
            query_version=q.version,
        )
        if bd.artist < cfg.artist_min_ratio:
            continue
        kept.append((_sort_key(q, c, bd.total, idx, cfg), c.model_copy(update={"score": bd.total}, deep=True)))
    kept.sort(key=lambda kc: kc[0])
    return [c for _, c in kept]


def _sort_key(q: TrackQuery, c: Candidate, total: float, idx: int, cfg: MatchingCfg) -> tuple:
    bucket = int(total / BUCKET + 1e-9)
    explicit = _explicit(c)
    order = (False, None, True) if q.explicit is False else (True, None, False)
    exp_rank = order.index(explicit)
    br = c.bitrate_kbps
    br_rank = 1 if br is None else 0 if br >= 320 else 2
    d: date | None = c.upload_date
    if d is None:
        up = (1, 0)
    else:
        up = (0 if d < cfg.original_before else 2, d.toordinal())
    return (-bucket, exp_rank, br_rank, up, idx)


def classify_scores(scores: list[float], cfg: MatchingCfg) -> Verdict:
    """auto: top >= auto_threshold and margin to the second >= auto_margin; confirm: top >= confirm_threshold."""
    if not scores:
        return "not_found"
    ordered = sorted(scores, reverse=True)
    top = ordered[0]
    if top >= cfg.auto_threshold and (len(ordered) < 2 or top - ordered[1] >= cfg.auto_margin - 1e-9):
        return "auto"
    if top >= cfg.confirm_threshold:
        return "confirm"
    return "not_found"


def classify(ranked: list[Candidate], cfg: MatchingCfg) -> Verdict:
    """classify_scores over ranked candidates. Other uploads of the same recording as the top one (same fingerprint
    and named version, e.g. hitmoz duplicates) do not count as competitors for the margin."""
    if not ranked:
        return "not_found"

    def key(c: Candidate) -> tuple[str, tuple[str | None, str]]:
        return fingerprint(c.artist, c.title), version_identity(normalize_title(c.title).version)

    top_key = key(ranked[0])
    return classify_scores([ranked[0].score] + [c.score for c in ranked[1:] if key(c) != top_key], cfg)


def match_catalog(
    artist: str,
    title: str,
    tracks: list[CatalogTrack],
    *,
    album: str | None = None,
    duration: int | None = None,
    truncated: bool = False,
    version: str | None = None,
) -> list[tuple[CatalogTrack, ScoreBreakdown]]:
    """Score catalog tracks (e.g. an album tracklist or search results) against a recognized line, best first."""
    scored = [
        (
            t,
            score(
                artist,
                title,
                t.artist,
                t.title,
                query_album=album,
                cand_album=t.album_title,
                query_duration=duration,
                cand_duration=t.duration_s,
                query_version=version,
                truncated=truncated,
            ),
        )
        for t in tracks
    ]
    scored.sort(key=lambda ts: -ts[1].total)
    return scored
