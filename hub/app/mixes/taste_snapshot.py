"""Explicit inputs to taste math; local batch loading has no network or write side effects."""
from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any


def utc(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def calendar_cutoff(now: datetime) -> datetime:
    try:
        return now.replace(year=now.year - 3)
    except ValueError:
        return now.replace(year=now.year - 3, day=28)


@dataclass(frozen=True)
class TasteEvent:
    user_id: int
    event_id: str
    ts: datetime
    kind: str
    ref: str | None = None
    nd_song_id: str | None = None
    target_type: str = "track"
    nd_album_id: str | None = None
    nd_artist_id: str | None = None
    target_data: dict[str, Any] | None = None
    reverts_event_id: str | None = None
    position_s: float | None = None
    duration_s: float | None = None
    context: str | None = None
    client: str = "pwa"
    received_at: datetime | None = None
    id: int = 0
    playback_id: str | None = None
    device_id: str | None = None
    listened_ms: int | None = None

    @classmethod
    def from_row(cls, row) -> TasteEvent:
        return cls(**{k: getattr(row, k) for k in cls.__dataclass_fields__})

    @property
    def seconds(self) -> float | None:
        """Cumulative listening, not the seek cursor. Only legacy rows use position."""
        if self.listened_ms is not None:
            return self.listened_ms / 1000
        return None if self.playback_id else self.position_s

    @property
    def early_skip(self) -> bool:
        return self.kind == "skip" and self.seconds is not None and self.seconds < 30


PLAYBACK_KINDS = ("start", "progress", "skip", "finish", "interrupted", "error")


@dataclass(frozen=True)
class PlaybackAttempt:
    first: TasteEvent
    start: TasteEvent | None
    terminal: TasteEvent | None  # first decisive terminal; error/interrupted override finish/skip
    context: str | None
    events: list[TasteEvent]

    @property
    def outcome(self) -> TasteEvent | None:
        return self.terminal if self.terminal and self.terminal.kind in ("finish", "skip") else None


def playback_attempts(events: Iterable[TasteEvent], track_key: Callable[[TasteEvent], Hashable]) -> list[PlaybackAttempt]:
    """Fold only the playback journal visible at the caller's cut. Explicit actions are independent.

    Modern retries share a device/attempt id. Without that identity a start may pair with a terminal on
    the same stream, but distinct standalone scrobbles/terminals aren't evidence of a single play.
    """
    groups = []
    identified, legacy = {}, {}
    seen = set()
    for e in sorted((x for x in events if x.kind in PLAYBACK_KINDS and (x.target_type or "track") == "track"),
                    key=lambda x: (x.ts, x.id or 0)):
        if (e.user_id, e.event_id) in seen:
            continue
        seen.add((e.user_id, e.event_id))
        if e.playback_id:
            identity = (e.user_id, e.device_id or e.client, e.playback_id)
            rows = identified.get(identity)
            if rows is None:
                identified[identity] = rows = []
                groups.append(rows)
        else:
            stream = (e.user_id, e.client, track_key(e), e.context)
            rows = legacy.get(stream)
            if rows:
                prev = rows[-1]
                # Old journals occasionally sent a skip after failure, or retried a paired terminal.
                failure_tail = (e.kind in ("error", "interrupted") or prev.kind in ("error", "interrupted")) \
                    and e.ts - prev.ts <= timedelta(minutes=5)
                retry = rows[0].kind == "start" and (e.kind, e.ts, e.position_s) == (prev.kind, prev.ts, prev.position_s)
                if not retry and (e.kind == "start" or e.ts - rows[0].ts > timedelta(hours=6) or
                                  (prev.kind not in ("start", "progress") and not failure_tail)):
                    rows = None
            if not rows:
                legacy[stream] = rows = []
                groups.append(rows)
        rows.append(e)

    attempts = []
    for rows in groups:
        by_kind = {}
        for e in rows:
            by_kind.setdefault(e.kind, e)
        terminal = next((by_kind[k] for k in ("error", "interrupted", "finish", "skip") if k in by_kind), None)
        contexts = {e.context for e in rows if e.context}
        context = next(iter(contexts)) if len(contexts) == 1 else None
        if terminal:
            measured = max((e.listened_ms for e in rows if e.listened_ms is not None), default=None)
            duration = terminal.duration_s or next((e.duration_s for e in rows if e.duration_s), None)
            terminal = replace(terminal, listened_ms=measured, duration_s=duration, context=context)
        attempts.append(PlaybackAttempt(rows[0], by_kind.get("start"), terminal, context, rows))
    return attempts


def playback_evidence(attempts: Iterable[PlaybackAttempt]) -> list[TasteEvent]:
    """One start (zero weight) and at most one eligible outcome per attempt, in event order."""
    return sorted((e for a in attempts for e in (a.start, a.outcome) if e is not None),
                  key=lambda e: (e.ts, e.id or 0))


@dataclass(frozen=True)
class Subject:
    target_type: str
    aliases: tuple[str, ...]
    ref: str | None = None
    nd_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TrackMetadata:
    ref: str | None = None
    nd_song_id: str | None = None
    artist: str = ""
    title: str = ""
    duration_s: int | None = None
    album_ref: str | None = None
    album_nd_id: str | None = None


@dataclass(frozen=True)
class SavedDownload:
    ref: str | None
    nd_song_id: str | None
    at: datetime


@dataclass(frozen=True)
class DislikedTarget:
    ref: str | None = None
    nd_song_id: str | None = None
    artist_ref: str | None = None
    artist: str = ""
    title: str = ""
    at: datetime | None = None


@dataclass(frozen=True)
class TasteSnapshot:
    user_id: int
    events: tuple[TasteEvent, ...] = ()
    subjects: tuple[Subject, ...] = ()
    metadata: tuple[TrackMetadata, ...] = ()
    downloads: tuple[SavedDownload, ...] = ()
    dislikes: tuple[DislikedTarget, ...] = ()
    picked_artists: tuple[dict[str, Any], ...] = ()
    excluded_user_ids: frozenset[int] = frozenset()
    limitations: tuple[str, ...] = ()


async def load_snapshot(session, user_id: int, *, now: datetime) -> TasteSnapshot:
    """Read local inputs in batches, keeping permanent decisions outside the score window.

    Historical evaluation uses evaluate.read_snapshot instead: mutable current tables are
    suitable for an online read only. No profile cache or ORM mutation is introduced here.
    """
    from sqlalchemy import or_, select

    from app.favorites import AliasIndex, aliases
    from app.library.owned import quiet
    from app.models import Album, Dislike, DownloadItem, FavoriteSubject, MixSettings, PlayEvent, Track
    from app.plays import Ids

    now = utc(now)
    rows = list((await session.execute(select(PlayEvent).where(
        PlayEvent.user_id == user_id, PlayEvent.superseded_scrobble.is_(False), PlayEvent.ts <= now,
        PlayEvent.received_at <= now,
        or_(PlayEvent.ts >= calendar_cutoff(now),
            PlayEvent.kind.in_(("like", "unlike", "dislike", "undislike")))))).scalars())
    subjects = [Subject(x.target_type, tuple(x.aliases or ()), x.ref, x.nd_id, x.data or {})
                for x in (await session.execute(select(FavoriteSubject))).scalars()]
    downloads = []
    for item, nd in (await session.execute(select(DownloadItem, Track.nd_song_id).join(
            Track, Track.id == DownloadItem.track_id).where(DownloadItem.user_id == user_id,
                DownloadItem.status == "done", DownloadItem.created_at <= now))).all():
        if not quiet(item.query_json):
            downloads.append(SavedDownload((item.query_json.get("meta") or {}).get("track_ref"), nd,
                                           item.created_at))
    dislikes = tuple(DislikedTarget(x.ref, x.nd_song_id, x.artist_ref, x.artist or "", x.title or "", x.created_at)
        for x in (await session.execute(select(Dislike).where(Dislike.user_id == user_id,
                                                             Dislike.created_at <= now))).scalars())
    # Local verified pairs are metadata, not additional user events.
    ids = Ids(session)
    pairs = {(x.ref, x.nd_song_id) for x in rows if x.target_type == "track"}
    pairs.update((x.ref, x.nd_song_id) for x in (*downloads, *dislikes) if not getattr(x, "artist_ref", None))
    await ids.prime(pairs)
    for ref, nd in pairs:
        ref, nd = await ids.complete(ref, nd)
        if ref and nd:
            subjects.append(Subject("track", (f"ref:{ref}", f"nd:{nd}"), ref, nd))
    index = AliasIndex(rows, subjects)
    wanted = {index.key(e) for e in rows if aliases(e)}
    wanted.update(index.root(("track", alias)) for ref, nd in pairs for alias in
                  ((f"ref:{ref}",) if ref else (f"nd:{nd}",)) if ref or nd)
    subjects = [x for x in subjects if x.aliases and index.root((x.target_type, min(x.aliases))) in wanted]
    refs = {x.ref for x in rows if x.target_type == "track" and x.ref}
    songs = {x.nd_song_id for x in rows if x.target_type == "track" and x.nd_song_id}
    refs.update(ref for ref, nd in pairs if ref)
    songs.update(nd for ref, nd in pairs if nd)
    album_refs = {x.ref for x in rows if x.target_type == "album" and x.ref}
    for x in rows:
        if x.target_type == "album":
            refs.update((x.target_data or {}).get("track_refs") or ())
    for x in subjects:
        if x.target_type == "track":
            refs.update(a[4:] for a in x.aliases if a.startswith("ref:"))
            songs.update(a[3:] for a in x.aliases if a.startswith("nd:"))
        elif x.target_type == "album":
            if x.ref:
                album_refs.add(x.ref)
            refs.update(x.data.get("track_refs") or ())
    metadata = []
    query = select(Track, Album).outerjoin(Album, Album.id == Track.album_id).where(or_(
        Track.nd_song_id.in_(songs), Track.deezer_track_id.in_([r[3:] for r in refs if r.startswith("dz:")]),
        Album.deezer_album_id.in_([r[3:] for r in album_refs if r.startswith("dz:")]))).order_by(Track.id.desc())
    for tr, album in (await session.execute(query)).all():
        metadata.append(TrackMetadata(f"dz:{tr.deezer_track_id}" if tr.deezer_track_id else None,
            tr.nd_song_id, tr.artist, tr.title, tr.duration_s,
            f"dz:{album.deezer_album_id}" if album and album.deezer_album_id else None,
            None))  # Album.uid is a Hub id, not a verified Navidrome album id.
    settings = await session.get(MixSettings, user_id)
    events = tuple(TasteEvent.from_row(x) for x in rows)
    return TasteSnapshot(user_id, events, tuple(subjects), tuple(metadata), tuple(downloads), dislikes,
                         tuple(settings.artists_json or ()) if settings else ())
