"""The taste profile of a user (MIXES.md 5): what they listen to, like, download and dislike.

- Weights (M4, 2026-10-23): own ♥ +1, played to the end +2, skipped before 30 s −1, «Не нравится» −5; a skip after
  30 s — up to +2 by how much of the rest was heard, rising slowly at first and steeply near the end (`skip_weight`);
  each × `0.5 ^ (days / 30)`. A download of the user's own weighs nothing: such a track is in the profile (`downloaded`,
  the stash, fallback seeds) with the score of its other events, 0 without them. ♥ that an import of another service
  set don't count (`plays.imported_likes`).
- A track is known by its catalog `ref` and/or its Navidrome song; `key` is `nd:<song>` when the library has it
  (the key of its audio vector), else the `ref`.
- Seeds: up to `seeds` best tracks of the last 30 days, ≤ 2 of an artist; without such — own ♥ and downloads, the
  newest first; a new user without any — the top tracks of the artists picked on «Что вы слушаете?».
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import cached_property
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog.models import CatalogTrack, split_ref
from app.library.owned import quiet
from app.matching.normalize import fingerprint, normalize_artist
from app.mixes.embedder import song_key
from app.mixes.taste_snapshot import PLAYBACK_KINDS, TasteEvent, playback_attempts
from app.models import Dislike, DownloadItem, MixSettings, Track, User, utcnow
from app.plays import Ids, user_events

if TYPE_CHECKING:
    from app.favorites import State

log = logging.getLogger(__name__)

HALF_LIFE_DAYS = 30
W_LIKE, W_FINISH, W_SKIP, W_DISLIKE = 1.0, 2.0, -1.0, -5.0
W_SKIP_MAX = 2.0  # a skip after 30 s, at the very end
EARLY_SKIP_S = 30
SKIP_CURVE = 3.0  # the bend of `skip_weight`: half of the rest heard gives W_SKIP_MAX / (2 + SKIP_CURVE)
HISTORY_DAYS = 365
SEED_DAYS = 30


def decay(at: datetime | None, now: datetime) -> float:
    if at is None:
        return 1.0
    return 0.5 ** (max(0.0, (now - at).total_seconds()) / 86400 / HALF_LIFE_DAYS)


def skip_weight(position_s: float, duration_s: float | None) -> float:
    """A skip after the first 30 s: x — the share heard of the track past them, the weight a hyperbola from 0 at
    x = 0 to `W_SKIP_MAX` at x = 1 (x / (1 + c·(1 − x))), so a skip near the end counts almost as a listen."""
    if not duration_s or duration_s <= EARLY_SKIP_S:
        return 0.0
    x = min(max((position_s - EARLY_SKIP_S) / (duration_s - EARLY_SKIP_S), 0.0), 1.0)
    return W_SKIP_MAX * x / (1 + SKIP_CURVE * (1 - x))


def artist_key(name: str | None) -> str:
    n = normalize_artist(name or "")
    return n.latin or n.text


@dataclass
class TasteTrack:
    key: str
    ref: str | None = None
    nd_song_id: str | None = None
    artist: str = ""
    title: str = ""
    duration_s: int | None = None
    score: float = 0.0
    last_played: datetime | None = None
    last_event: datetime | None = None  # any event of the user with it (the seeds' 30 days)
    liked: bool = False  # own ♥ now
    downloaded: bool = False  # the user downloaded it themselves
    track: CatalogTrack | None = None  # the catalog record (artist ref for Deezer radio)
    positive_raw: float = 0.0
    negative: float = 0.0
    last_positive: datetime | None = None
    last_heard: datetime | None = None
    heard30: bool = False
    album_liked: bool = False
    positive_age: float | None = None

    @property
    def familiar(self) -> bool:
        return self.heard30 or self.liked or self.downloaded or self.album_liked

    @property
    def fp(self) -> str:
        return fingerprint(self.artist, self.title) if self.artist and self.title else ""


@dataclass
class Profile:
    user_id: int
    now: datetime
    tracks: dict[str, TasteTrack] = field(default_factory=dict)
    artists: dict[str, float] = field(default_factory=dict)  # artist key -> sum of its tracks' scores
    artist_names: dict[str, str] = field(default_factory=dict)
    artist_listens: dict[str, int] = field(default_factory=dict)  # listens (finish, skip ≥ 30 s) per artist
    by_id: dict[str, str] = field(default_factory=dict)  # ref / nd:<song> / fingerprint -> track key
    disliked_ids: set[str] = field(default_factory=set)  # refs and nd:<song> of disliked tracks
    disliked_artist_refs: set[str] = field(default_factory=set)
    disliked_artists: set[str] = field(default_factory=set)  # artist keys
    picked_artists: list[dict[str, Any]] = field(default_factory=list)  # onboarding: [{ref, name}]
    favorite_states: dict[tuple[str, str], State] = field(default_factory=dict)

    @cached_property
    def preferred_artists(self) -> tuple[set[str], set[str]]:
        """Index explicit artist evidence once per loaded profile; a mere metadata row isn't evidence."""
        names, refs = set(), set()

        def add(name, ref):
            if key := artist_key(name):
                names.add(key)
            if ref:
                refs.add(ref)

        for picked in self.picked_artists:
            add(picked.get("name"), picked.get("ref"))
        for track in self.tracks.values():
            if track.liked or track.downloaded or track.album_liked:
                add(track.artist, track.track.artist_ref if track.track else None)
        for state in self.favorite_states.values():
            if state.liked:
                data = state.data or {}
                if state.target_type == "artist":
                    add(data.get("name"), state.ref)
                else:
                    add(data.get("artist"), data.get("artist_ref"))
        return names, refs

    def find(self, *, ref: str | None = None, nd_song_id: str | None = None, fp: str | None = None) -> TasteTrack | None:
        for k in (ref, song_key(nd_song_id) if nd_song_id else None, fp):
            if k and (key := self.by_id.get(k)):
                return self.tracks[key]
        return None

    def known(self, *, ref: str | None = None, nd_song_id: str | None = None, fp: str | None = None) -> bool:
        t = self.find(ref=ref, nd_song_id=nd_song_id, fp=fp)
        return bool(t and t.familiar)

    def disliked(self, *, ref: str | None, nd_song_id: str | None, artist: str, artist_ref: str | None) -> bool:
        if (ref and ref in self.disliked_ids) or (nd_song_id and song_key(nd_song_id) in self.disliked_ids):
            return True
        return bool(artist_ref and artist_ref in self.disliked_artist_refs) or artist_key(artist) in self.disliked_artists

    @property
    def empty(self) -> bool:
        return not any(t.score > 0 or t.liked or t.downloaded for t in self.tracks.values()) and not self.picked_artists

    def top_artists(self, n: int) -> list[str]:
        return [a for a, sc in sorted(self.artists.items(), key=lambda x: -x[1]) if sc > 0][:n]

    def vector_profile(self) -> dict[str, float]:
        """Track key -> score for the embedder's weighted centers (positive and negative)."""
        return {k: round(t.score, 4) for k, t in self.tracks.items() if abs(t.score) > 1e-3}

    def seeds(self, n: int, max_per_artist: int = 2) -> list[TasteTrack]:
        since = self.now - timedelta(days=SEED_DAYS)
        recent = sorted((t for t in self.tracks.values() if t.score > 0 and t.last_event and t.last_event >= since),
                        key=lambda t: -t.score)
        pool = recent or sorted((t for t in self.tracks.values() if (t.liked or t.downloaded) and t.score >= 0),
                                key=lambda t: t.last_event or datetime.min, reverse=True)
        out: list[TasteTrack] = []
        per: dict[str, int] = {}
        for t in pool:
            a = artist_key(t.artist)
            if not t.artist or not t.title or per.get(a, 0) >= max_per_artist:
                continue
            per[a] = per.get(a, 0) + 1
            out.append(t)
            if len(out) >= n:
                break
        return out


class _Builder:
    def __init__(self, s: AsyncSession, p: Profile):
        self.s, self.p = s, p
        self.ids = Ids(s)

    async def track(self, ref: str | None, nd: str | None) -> TasteTrack | None:
        ref, nd = await self.ids.complete(ref, nd)
        if not (ref or nd):
            return None
        for k in (ref, song_key(nd) if nd else None):
            if k and (key := self.p.by_id.get(k)):
                t = self.p.tracks[key]
                t.ref, t.nd_song_id = t.ref or ref, t.nd_song_id or nd
                self._index(t)
                return t
        t = TasteTrack(key=song_key(nd) if nd else ref or "", ref=ref, nd_song_id=nd)
        self.p.tracks[t.key] = t
        self._index(t)
        return t

    def _index(self, t: TasteTrack) -> None:
        for k in (t.ref, song_key(t.nd_song_id) if t.nd_song_id else None, t.fp or None):
            if k:
                self.p.by_id.setdefault(k, t.key)


async def _catalog_info(refs: list[str]) -> dict[str, CatalogTrack]:
    """Catalog records of Deezer tracks the profile knows only by ref (streamed, not in the library)."""
    from app.services import svc

    if svc.deezer is None:
        return {}
    sem = asyncio.Semaphore(4)

    async def one(ref: str) -> tuple[str, CatalogTrack | None]:
        src, id_ = split_ref(ref)
        if src != "dz":
            return ref, None
        async with sem:
            try:
                return ref, await svc.deezer.get_track(id_)
            except Exception as e:
                log.info("mix profile: catalog track %s: %r", ref, e)
                return ref, None

    return {r: t for r, t in await asyncio.gather(*(one(r) for r in refs)) if t is not None}


async def build_profile(s: AsyncSession, user: User, now: datetime | None = None,
                        starred: list[dict[str, Any]] | None = None, *, model: str = "v1",
                        timezone: str | None = None) -> Profile:
    """v1 scoring with permanent, deduplicated favorite state. `starred` is an optional successful snapshot."""
    now = now or utcnow()
    if model == "v2":
        from app.config import get_settings
        from app.mixes.taste_math import calculate_profile
        from app.mixes.taste_snapshot import load_snapshot

        return calculate_profile(await load_snapshot(s, user.id, now=now), now=now,
                                 timezone=timezone or get_settings().timezone)
    if model != "v1":
        raise ValueError("online taste model must be v1 or v2")
    p = Profile(user_id=user.id, now=now)
    b = _Builder(s, p)
    from app.favorites import AliasIndex, reconcile, snapshot
    from app.insights.launches import test_traffic
    from app.mixes.taste_math import _heard

    # An explicitly supplied successful snapshot is useful for isolated callers/tests.
    # Ordinary profile reads use the local ledger and never reinterpret network errors as unstars.
    if starred is not None:
        await reconcile(s, user, {"song": starred}, now=now)
    favorites = p.favorite_states = await snapshot(s, user.id, now, available_only=True)
    late_skips: list[tuple[TasteTrack, float, float]] = []  # skips after 30 s without the length in the event

    events = [TasteEvent.from_row(e) for e in await user_events(s, user.id, HISTORY_DAYS, kinds=PLAYBACK_KINDS, now=now)
              if e.received_at <= now and not test_traffic(e.client) and (e.target_data or {}).get("provenance") != "import"]
    await b.ids.prime([(e.ref, e.nd_song_id) for e in events] +
                      [(x.ref, x.nd_id) for x in favorites.values() if x.target_type == "track"])
    for attempt in playback_attempts(events, AliasIndex(events).key):
        # A failed/interrupted attempt or bare start is not a v1 outcome.
        if not (e := attempt.outcome):
            continue
        t = await b.track(e.ref, e.nd_song_id)
        if t is None:
            continue
        d = decay(e.ts, now)
        t.last_event = max(t.last_event or e.ts, e.ts)
        t.last_played = max(t.last_played or e.ts, e.ts)
        seconds = e.seconds
        if e.kind == "finish":
            t.score += W_FINISH * d
        elif e.early_skip:
            t.score += W_SKIP * d
        elif e.kind == "skip" and seconds is not None:
            if e.duration_s:
                t.score += skip_weight(seconds, e.duration_s) * d
            else:
                late_skips.append((t, seconds, d))
        if _heard(e, e.duration_s):
            t.heard30 = True
            t.last_heard = max(t.last_heard or e.ts, e.ts)
            p.artist_listens[t.key] = p.artist_listens.get(t.key, 0) + 1  # per track for now, per artist below

    for state in favorites.values():
        if state.target_type != "track" or not state.liked:
            continue
        t = await b.track(state.ref, state.nd_id)
        if t is None:
            continue
        t.liked = True
        if state.since and state.since >= now - timedelta(days=HISTORY_DAYS):
            t.score += W_LIKE * decay(state.since, now)
        t.last_event = max(filter(None, [t.last_event, state.since]), default=None)
        data = state.data or {}
        t.artist, t.title = t.artist or data.get("artist") or "", t.title or data.get("title") or ""
        b._index(t)

    rows = (await s.execute(select(DownloadItem, Track).join(Track, Track.id == DownloadItem.track_id)
                            .where(DownloadItem.user_id == user.id, DownloadItem.status == "done",
                                   DownloadItem.created_at <= now))).all()
    for item, tr in rows:
        if quiet(item.query_json):
            continue
        t = await b.track((item.query_json.get("meta") or {}).get("track_ref"), tr.nd_song_id)
        if t is None or t.downloaded:
            continue
        t.downloaded = True  # no weight of its own (M4)
        t.last_event = max(filter(None, [t.last_event, item.created_at]), default=None)

    for d in (await s.execute(select(Dislike).where(Dislike.user_id == user.id, Dislike.created_at <= now))).scalars():
        if d.artist_ref:
            p.disliked_artist_refs.add(d.artist_ref)
            if d.artist:
                p.disliked_artists.add(artist_key(d.artist))
            continue
        p.disliked_ids.update(k for k in (d.ref, song_key(d.nd_song_id) if d.nd_song_id else None) if k)
        t = await b.track(d.ref, d.nd_song_id)
        if t is not None:
            t.score += W_DISLIKE * decay(d.created_at, now)
            t.artist, t.title = t.artist or d.artist or "", t.title or d.title or ""
            b._index(t)

    await _names(s, p, b)
    for t, pos, d in late_skips:  # the length of the library file or the catalog track
        t.score += skip_weight(pos, t.duration_s) * d
    for t in p.tracks.values():
        if not t.artist:
            continue
        a = artist_key(t.artist)
        p.artist_names.setdefault(a, t.artist)
        p.artists[a] = p.artists.get(a, 0.0) + t.score
    listens: dict[str, int] = {}
    for key, n in p.artist_listens.items():
        if (t := p.tracks.get(key)) and t.artist:
            listens[artist_key(t.artist)] = listens.get(artist_key(t.artist), 0) + n
    p.artist_listens = listens
    settings = await s.get(MixSettings, user.id)
    p.picked_artists = list(settings.artists_json or []) if settings else []
    return p


async def _names(s: AsyncSession, p: Profile, b: _Builder) -> None:
    """Artist and title of every track: the library's, else the catalog's."""
    songs = [t.nd_song_id for t in p.tracks.values() if t.nd_song_id]
    lib: dict[str, Track] = {}
    for i in range(0, len(songs), 500):
        for tr in (await s.execute(select(Track).where(Track.nd_song_id.in_(songs[i:i + 500])))).scalars():
            lib[tr.nd_song_id] = tr  # type: ignore[index]
    for t in p.tracks.values():
        if t.nd_song_id and (tr := lib.get(t.nd_song_id)):
            t.artist, t.title, t.duration_s = tr.artist, tr.title, tr.duration_s
            if not t.ref and tr.deezer_track_id:
                t.ref = f"dz:{tr.deezer_track_id}"
    need = [t.ref for t in p.tracks.values() if t.ref and (not t.artist or t.score > 0)]
    info = await _catalog_info(list(dict.fromkeys(need)))
    for t in p.tracks.values():
        if t.ref and (ct := info.get(t.ref)):
            t.track = ct
            if not t.artist:
                t.artist, t.title, t.duration_s = ct.artist, ct.title, ct.duration_s
        b._index(t)
