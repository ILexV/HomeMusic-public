"""Fuzzy similarity and the candidate score (PLAN.md 8.5.3): 0.5*title + 0.35*artist + 0.15*bonus - version penalty."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from rapidfuzz import fuzz

from app.matching.normalize import Norm, normalize_artist, normalize_title, to_latin, version_kind

W_TITLE, W_ARTIST, W_BONUS = 0.5, 0.35, 0.15
VERSION_PENALTY = 0.2
ALBUM_MATCH_RATIO = 0.85
MIN_PREFIX_LEN = 3  # shorter truncated titles fall back to the plain ratio


@dataclass(frozen=True)
class ScoreBreakdown:
    title: float
    artist: float
    bonus: float
    version_penalty: float
    total: float


def version_identity(version: str | None) -> tuple[str | None, str]:
    """Keep named mixes/venues distinct; a version kind is not a recording identity."""
    kind = version_kind(version)
    if kind not in ("remix", "live"):
        return kind, ""
    detail = re.sub(r"\b(?:remix|remiks|rmx|mix|live|laiv|concert|kontsert)\b", " ",
                    to_latin(version or "").casefold())
    return kind, " ".join(re.sub(r"[^\w]+", " ", detail).split())

def _ratio(a: str, b: str) -> float:
    """0..1, max of plain and token-sort ratio (word order does not matter)."""
    if not a or not b:
        return 0.0
    return max(fuzz.ratio(a, b), fuzz.token_sort_ratio(a, b)) / 100


def title_sim(a: Norm, b: Norm) -> float:
    """Best of original/Latin forms. A truncated `a` is compared with the same-length prefix of `b`."""
    if a.truncated and len(a.text) >= MIN_PREFIX_LEN:
        best = 0.0
        for x, y in ((a.text, b.text), (a.latin, b.latin)):
            if x and y:
                best = max(best, fuzz.ratio(x, y[: len(x)]) / 100)
        return best
    return max(_ratio(a.text, b.text), _ratio(a.latin, b.latin))


def artist_sim(a: Norm, b: Norm) -> float:
    """Main artists must match; guests never lower the score. The full credit list (any order) is a fallback."""
    best = max(_ratio(a.text, b.text), _ratio(a.latin, b.latin))
    if best < 1.0 and (a.featured or b.featured):
        fa, fb = " ".join((a.text, *a.featured)), " ".join((b.text, *b.featured))
        full = max(fuzz.token_sort_ratio(fa, fb), fuzz.token_sort_ratio(to_latin(fa), to_latin(fb))) / 100
        best = max(best, full)
    return best


def _bonus(
    query_album: str | None, cand_album: str | None, query_duration: int | None, cand_duration: int | None
) -> float:
    """Duration +-3 s -> 1.0, +-10 s -> 0.5, worse -> 0; a matching album adds 0.5 (cap 1.0); nothing known -> 0.5."""
    album_known = bool(query_album and cand_album)
    album_ok = album_known and title_sim(normalize_title(query_album), normalize_title(cand_album)) >= ALBUM_MATCH_RATIO
    if query_duration and cand_duration:
        diff = abs(query_duration - cand_duration)
        bonus = 1.0 if diff <= 3 else 0.5 if diff <= 10 else 0.0
        return min(1.0, bonus + (0.5 if album_ok else 0.0))
    if album_known:
        return 1.0 if album_ok else 0.5  # a different album is weak evidence (single vs album release)
    return 0.5


def score(
    query_artist: str,
    query_title: str,
    cand_artist: str,
    cand_title: str,
    *,
    query_album: str | None = None,
    cand_album: str | None = None,
    query_duration: int | None = None,
    cand_duration: int | None = None,
    query_version: str | None = None,
    truncated: bool = False,
) -> ScoreBreakdown:
    """Score a candidate against a query. `query_version` overrides the version parsed from `query_title`.

    Named remixes/live venues must agree; remaster/OST/album version count as no version.
    """
    qt, ct = normalize_title(query_title), normalize_title(cand_title)
    if truncated and not qt.truncated:
        qt = replace(qt, truncated=True)
    t = title_sim(qt, ct)
    a = artist_sim(normalize_artist(query_artist), normalize_artist(cand_artist))
    b = _bonus(query_album, cand_album, query_duration, cand_duration)
    qv = query_version if query_version is not None else qt.version
    qkind, qdetail = version_identity(qv)
    ckind, cdetail = version_identity(ct.version)
    pen = VERSION_PENALTY if qkind != ckind or (qdetail and qdetail != cdetail) else 0.0
    total = max(0.0, W_TITLE * t + W_ARTIST * a + W_BONUS * b - pen)
    return ScoreBreakdown(round(t, 4), round(a, 4), round(b, 4), pen, round(total, 4))
