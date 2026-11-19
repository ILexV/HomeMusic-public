"""Listening data for mixes and «Моя волна» (MIXES.md 3): events from the app and the PWA, «Не нравится», and the
scrobble history of Navidrome (other clients) imported every hour.

- An event names its track by a catalog `ref` and/or a Navidrome `nd_song_id`; the Hub fills in the other one where
  it knows it (a library track's Deezer id, the download or import row that brought the file in).
- `event_id` is the client's: a batch sent again (the app retries when offline) stores nothing twice.
- A Navidrome scrobble is a `finish` with client "navidrome". The app's own events of the same song near it win: a
  scrobble is not imported next to them, and a scrobble imported first is dropped when they arrive (`DEDUP_WINDOW`
  plus the track's length: Navidrome counts a play at half of it, the app's `finish` comes at its end).
- ♥ that an import of another service set are not the user's own (M4): `imported_likes`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import PlayEventIn
from app.catalog.models import split_ref
from app.db import session_scope
from app.models import CatalogCache, DownloadItem, Dislike, Import, ImportItem, NavidromeSync, PlayEvent, Track, User, utcnow

log = logging.getLogger(__name__)

PLAY_KINDS = ("start", "skip", "finish")
DEDUP_WINDOW = timedelta(minutes=5)
MAX_FUTURE = timedelta(hours=1)  # a client clock this far ahead is wrong: the event gets the time it came in
SYNC_EVERY_S = 3600
FIRST_SYNC_DELAY_S = 120


# --- ids -----------------------------------------------------------------------------------------------------


async def _song_of_ref(s: AsyncSession, ref: str) -> str | None:
    """The Navidrome song of a catalog track the library has."""
    try:
        src, id_ = split_ref(ref)
    except ValueError:
        src, id_ = "", ""
    if src == "dz":
        nd = (await s.execute(select(Track.nd_song_id).where(Track.deezer_track_id == id_, Track.nd_song_id.is_not(None))
                              .order_by(Track.id.desc()).limit(1))).scalar_one_or_none()
        if nd:
            return nd
    # the download (any ref: hitmoz, a provider's file) or the import row that brought the file in
    by_download = (select(Track.nd_song_id).join(DownloadItem, DownloadItem.track_id == Track.id)
                   .where(func.json_extract(DownloadItem.query_json, "$.meta.track_ref") == ref,
                          Track.nd_song_id.is_not(None)).order_by(DownloadItem.id.desc()).limit(1))
    nd = (await s.execute(by_download)).scalar_one_or_none()
    if nd:
        return nd
    by_import = (select(Track.nd_song_id).join(ImportItem, ImportItem.track_id == Track.id)
                 .where(ImportItem.ref == ref, Track.nd_song_id.is_not(None)).order_by(ImportItem.id.desc()).limit(1))
    return (await s.execute(by_import)).scalar_one_or_none()


async def _ref_of_song(s: AsyncSession, nd_song_id: str) -> str | None:
    """The catalog track of a library song."""
    t = (await s.execute(select(Track).where(Track.nd_song_id == nd_song_id).order_by(Track.id.desc()).limit(1))
         ).scalar_one_or_none()
    if t is None:
        return None
    if t.deezer_track_id:
        return f"dz:{t.deezer_track_id}"
    ref = (await s.execute(
        select(func.json_extract(DownloadItem.query_json, "$.meta.track_ref"))
        .where(DownloadItem.track_id == t.id,
               func.json_extract(DownloadItem.query_json, "$.meta.track_ref").is_not(None))
        .order_by(DownloadItem.id.desc()).limit(1))).scalar_one_or_none()
    if ref:
        return str(ref)
    return (await s.execute(select(ImportItem.ref).where(ImportItem.track_id == t.id, ImportItem.ref.is_not(None))
                            .order_by(ImportItem.id.desc()).limit(1))).scalar_one_or_none()


@dataclass
class Ids:
    """Fills in `ref` ↔ `nd_song_id` (cached for one batch)."""

    s: AsyncSession
    _songs: dict[str, str | None] = field(default_factory=dict)
    _refs: dict[str, str | None] = field(default_factory=dict)

    async def prime(self, pairs: Iterable[tuple[str | None, str | None]]) -> None:
        """Complete old partial events in batches, never one SQL query per historical event."""
        pairs = list(pairs)
        refs = {r for r, nd in pairs if r and not nd}
        songs = {nd for r, nd in pairs if nd and not r}
        self._songs.update({r: None for r in refs})
        self._refs.update({nd: None for nd in songs})
        # Prefer the newest local row, just like the single-target path.
        dz = {r[3:] for r in refs if r.startswith("dz:")}
        for tr in (await self.s.execute(select(Track).where(
            or_(Track.nd_song_id.in_(songs), Track.deezer_track_id.in_(dz))).order_by(Track.id.desc()))).scalars():
            ref = f"dz:{tr.deezer_track_id}" if tr.deezer_track_id else None
            if ref in refs and tr.nd_song_id and not self._songs[ref]:
                self._songs[ref] = tr.nd_song_id
            if tr.nd_song_id in songs and ref and not self._refs[tr.nd_song_id]:
                self._refs[tr.nd_song_id] = ref
        for model, column in ((DownloadItem, func.json_extract(DownloadItem.query_json, "$.meta.track_ref")),
                              (ImportItem, ImportItem.ref)):
            rows = (await self.s.execute(select(column, Track.nd_song_id).join(Track, model.track_id == Track.id)
                .where(or_(column.in_(refs), Track.nd_song_id.in_(songs)), Track.nd_song_id.is_not(None))
                .order_by(model.id.desc()))).all()
            for ref, nd in rows:
                if ref in refs and not self._songs[ref]:
                    self._songs[ref] = nd
                if nd in songs and ref and not self._refs[nd]:
                    self._refs[nd] = ref

    async def complete(self, ref: str | None, nd_song_id: str | None) -> tuple[str | None, str | None]:
        if ref and not nd_song_id:
            if ref not in self._songs:
                self._songs[ref] = await _song_of_ref(self.s, ref)
            nd_song_id = self._songs[ref]
        elif nd_song_id and not ref:
            if nd_song_id not in self._refs:
                self._refs[nd_song_id] = await _ref_of_song(self.s, nd_song_id)
            ref = self._refs[nd_song_id]
        return ref, nd_song_id


# --- events --------------------------------------------------------------------------------------------------


def event_time(ts: datetime, now: datetime | None = None) -> datetime:
    """UTC without tzinfo; an obviously wrong clock is corrected, with a three-calendar-year intake window."""
    now = now or utcnow()
    if ts.tzinfo is not None:
        ts = ts.astimezone(UTC).replace(tzinfo=None)
    try:
        cutoff = now.replace(year=now.year - 3)
    except ValueError:  # February 29 in a non-leap target year
        cutoff = now.replace(year=now.year - 3, day=28)
    return now if ts > now + MAX_FUTURE or ts < cutoff else ts


def dislike_source(client: str) -> str:
    return "app" if client.startswith("app") else client[:16]


@dataclass
class Recorded:
    accepted: int = 0
    duplicates: int = 0
    rejected: list[dict[str, Any]] = field(default_factory=list)


async def _track_length(s: AsyncSession, nd_song_id: str, duration_s: float | None) -> timedelta:
    if not duration_s:
        duration_s = (await s.execute(select(Track.duration_s).where(Track.nd_song_id == nd_song_id).limit(1))
                      ).scalar_one_or_none()
    return timedelta(seconds=min(float(duration_s or 0), 3600))


async def _drop_scrobbles_near(s: AsyncSession, user_id: int, ev: PlayEvent) -> None:
    """The app's own event of a song replaces a Navidrome scrobble of that very listen imported before it came."""
    assert ev.nd_song_id
    w = DEDUP_WINDOW + await _track_length(s, ev.nd_song_id, ev.duration_s)
    await s.execute(update(PlayEvent).where(
        PlayEvent.user_id == user_id, PlayEvent.client == "navidrome", PlayEvent.kind == "finish",
        PlayEvent.nd_song_id == ev.nd_song_id,
        PlayEvent.ts.between(ev.ts - w, ev.ts + w)).values(superseded_scrobble=True))


async def _listening_metadata(s: AsyncSession, e: PlayEventIn, nd: str | None) -> dict[str, Any] | None:
    """Snapshot known facts so later removal of a catalog cache entry or library file cannot erase them."""
    data: dict[str, Any] = {}
    provenance: dict[str, str] = {}
    if e.track_metadata:
        data = e.track_metadata.model_dump(exclude_none=True)
        provenance = {key: "client" for key in data}
    if nd:
        track = (await s.execute(select(Track).where(Track.nd_song_id == nd).order_by(Track.id.desc()).limit(1))).scalar_one_or_none()
        if track:
            for key, value in {"title": track.title, "album": track.album, "artist_credit": track.artist,
                               "duration_s": track.duration_s, "isrc": track.isrc, "year": track.year,
                               "genre": track.genre}.items():
                if value is not None and not data.get(key):
                    data[key] = value
                    provenance[key] = "library"
    if e.ref:
        cache_keys = [f"mix:track:{e.ref}"]
        if e.ref.startswith("dz:"):
            cache_keys.append(f"dz:track:{e.ref[3:]}")
        for key in cache_keys:
            cached = await s.get(CatalogCache, key)
            raw = cached.payload_json if cached else None
            if not isinstance(raw, dict) or raw.get("ref") != e.ref:
                continue
            values = {"title": raw.get("title"), "album": raw.get("album_title"),
                      "artist_credit": raw.get("artist"), "duration_s": raw.get("duration_s"), "isrc": raw.get("isrc")}
            release = raw.get("release_date")
            if isinstance(release, str) and len(release) >= 4 and release[:4].isdigit():
                values["year"] = int(release[:4])
            names = raw.get("artists")
            if isinstance(names, list) and all(isinstance(x, str) and x for x in names):
                values["artists"] = [{"name": name, **({"ref": raw["artist_ref"]} if index == 0 and raw.get("artist_ref") and name == raw.get("artist") else {})}
                                     for index, name in enumerate(names)]
            for name, value in values.items():
                if name == "artists" and value and data.get(name):
                    existing = data[name]
                    old_names = [artist.get("name") for artist in existing]
                    new_names = [artist.get("name") for artist in value]
                    changed = False
                    for artist in value:
                        artist_name = artist.get("name")
                        if old_names.count(artist_name) == 1 and new_names.count(artist_name) == 1:
                            match = existing[old_names.index(artist_name)]
                            if artist.get("ref") and not match.get("ref"):
                                match["ref"] = artist["ref"]
                                changed = True
                        elif artist_name not in old_names:
                            existing.append(artist.copy())
                            changed = True
                    if changed:
                        provenance[name] = "+".join(sorted({provenance.get(name, "client"), "catalog_cache"}))
                elif value and not data.get(name):
                    data[name] = value
                    provenance[name] = "catalog_cache"
    if not data:
        return None
    data["provenance"] = provenance
    return data


async def record_events(s: AsyncSession, user_id: int, raw: Iterable[Any], now: datetime | None = None) -> Recorded:
    """Stores a batch of events (`PlayEventIn` each). A malformed event is rejected alone, so a client can drop the
    whole batch it sent; an `event_id` seen before is a duplicate."""
    now = now or utcnow()
    out = Recorded()
    events: list[PlayEventIn] = []
    for i, item in enumerate(raw):
        try:
            events.append(PlayEventIn.model_validate(item))
        except ValidationError as e:
            out.rejected.append({"index": i, "event_id": item.get("event_id") if isinstance(item, dict) else None,
                                 "detail": "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())})
    ids = {e.event_id for e in events}
    seen = set((await s.execute(select(PlayEvent.event_id).where(PlayEvent.user_id == user_id,
                                                                  PlayEvent.event_id.in_(ids)))).scalars())
    # Causal retractions can restore an old decision after its mirror was already
    # delivered. Requeue the resulting state, not the causal event's kind.
    causal_batch = any(e.reverts_event_id for e in events)
    if not causal_batch and ids:
        causal_batch = (await s.execute(select(PlayEvent.id).where(
            PlayEvent.user_id == user_id, PlayEvent.reverts_event_id.in_(ids)).limit(1))).first() is not None
    if causal_batch:
        from app.favorites import snapshot
        before_favorites = await snapshot(s, user_id, now)
    fill = Ids(s)
    for e in events:
        if e.event_id in seen:
            out.duplicates += 1
            continue
        seen.add(e.event_id)
        ref, nd = await fill.complete(e.ref, e.nd_song_id) if e.target_type == "track" else (e.ref, None)
        metadata = await _listening_metadata(s, e, nd) if e.kind in ("start", "progress", "interrupted", "error", "skip", "finish") else None
        values = dict(user_id=user_id, event_id=e.event_id, ts=event_time(e.ts, now), kind=e.kind, ref=ref,
                      nd_song_id=nd, position_s=e.position_s, duration_s=e.duration_s, context=e.context,
                      client=e.client, received_at=now, playback_id=e.playback_id, device_id=e.device_id,
                      listened_ms=e.listened_ms, track_metadata=metadata, target_type=e.target_type, nd_album_id=e.nd_album_id,
                      nd_artist_id=e.nd_artist_id, target_data=e.target_data, reverts_event_id=e.reverts_event_id,
                      launch_id=e.launch_id, launch_origin=e.launch_origin)
        res = await s.execute(sqlite_insert(PlayEvent).values(**values).on_conflict_do_nothing())
        if not res.rowcount:  # a concurrent request stored it first
            out.duplicates += 1
            continue
        out.accepted += 1
        if e.kind == "start" and ref and (e.context or "").startswith("wave:"):
            from app.listen import confirm_wave_start

            confirm_wave_start(user_id, ref, values["ts"])
        if e.kind in ("like", "unlike"):
            from app.favorites import analyze_liked_track, remember
            from app.api.schemas import FavoriteTarget

            subject = await remember(s, FavoriteTarget(target_type=e.target_type, ref=ref, nd_song_id=nd,
                                                       nd_album_id=e.nd_album_id, nd_artist_id=e.nd_artist_id,
                                                       target_data=e.target_data))
            if e.kind == "like" and not e.reverts_event_id:
                analyze_liked_track(user_id, subject)
        if e.kind in PLAY_KINDS and nd and e.client != "navidrome":
            await _drop_scrobbles_near(s, user_id, PlayEvent(**values))
        if e.kind == "dislike":
            await add_dislike(s, user_id, ref=ref if e.target_type == "track" else None, nd_song_id=nd,
                              artist_ref=(ref or f"nd:{e.nd_artist_id}") if e.target_type == "artist" else None,
                              artist=(e.target_data or {}).get("name") if e.target_type == "artist" else None,
                              source=dislike_source(e.client), complete=False)
        elif e.kind == "undislike":
            await remove_dislikes(s, user_id, ref=ref if e.target_type == "track" else None, nd_song_id=nd,
                                  artist_ref=(ref or f"nd:{e.nd_artist_id}") if e.target_type == "artist" else None, complete=False)
    if causal_batch and out.accepted:
        after_favorites = await snapshot(s, user_id, now)
        for key, state in after_favorites.items():
            previous = before_favorites.get(key)
            if previous is None or previous.liked != state.liked:
                state.event.mirror_pending = True
                if state.target_type == "track" and state.ref and not state.liked:
                    from app.favorites import clear_pending
                    await clear_pending(s, user_id, state.ref)
        for key, previous in before_favorites.items():
            if key not in after_favorites and previous.liked and previous.target_type == "track" and previous.ref:
                from app.favorites import clear_pending
                await clear_pending(s, user_id, previous.ref)
    return out


async def user_events(s: AsyncSession, user_id: int, days: float = 30, *, kinds: Iterable[str] | None = None,
                      now: datetime | None = None) -> list[PlayEvent]:
    """The user's events of the last `days`, oldest first (the raw material of the taste profile, MIXES.md 5)."""
    now = now or utcnow()
    q = select(PlayEvent).where(PlayEvent.user_id == user_id, PlayEvent.superseded_scrobble.is_(False),
                                PlayEvent.ts >= now - timedelta(days=days), PlayEvent.ts <= now)
    if kinds is not None:
        q = q.where(PlayEvent.kind.in_(list(kinds)))
    return list((await s.execute(q.order_by(PlayEvent.ts, PlayEvent.id))).scalars())


async def imported_likes(s: AsyncSession, user_id: int) -> set[str]:
    """Navidrome songs whose ♥ an import of another service set (`imports.options_json.liked`) and the user did not
    like again themselves since (a `like` event): such ♥ don't count for the taste profile (M4)."""
    liked: set[str] = set()
    for opts in (await s.execute(select(Import.options_json).where(Import.user_id == user_id))).scalars():
        liked.update((opts or {}).get("liked") or [])
    if not liked:
        return liked
    own = set((await s.execute(select(PlayEvent.nd_song_id).where(
        PlayEvent.user_id == user_id, PlayEvent.kind == "like", PlayEvent.nd_song_id.in_(liked)))).scalars())
    return liked - own


# --- «Не нравится» ---------------------------------------------------------------------------------------------


def _target(ref: str | None, nd_song_id: str | None, artist_ref: str | None):
    if artist_ref:
        return Dislike.artist_ref == artist_ref
    conds = [c for c in (Dislike.ref == ref if ref else None, Dislike.nd_song_id == nd_song_id if nd_song_id else None)
             if c is not None]
    return or_(*conds) if conds else None


async def add_dislike(s: AsyncSession, user_id: int, *, ref: str | None = None, nd_song_id: str | None = None,
                      artist_ref: str | None = None, artist: str | None = None, title: str | None = None,
                      source: str, complete: bool = True) -> Dislike | None:
    """«Не нравится» a track (by `ref` and/or `nd_song_id`) or an artist (`artist_ref`); the same target twice is
    one row (the ids it lacked are filled in). None: nothing to name."""
    if not artist_ref and complete:
        ref, nd_song_id = await Ids(s).complete(ref, nd_song_id)
    cond = _target(ref, nd_song_id, artist_ref)
    if cond is None:
        return None
    row = (await s.execute(select(Dislike).where(Dislike.user_id == user_id, cond).limit(1))).scalar_one_or_none()
    if row is None:
        row = Dislike(user_id=user_id, artist_ref=artist_ref, source=source, created_at=utcnow())
        s.add(row)
    if not artist_ref:
        row.ref, row.nd_song_id = row.ref or ref, row.nd_song_id or nd_song_id
    row.artist, row.title = row.artist or artist, row.title or title
    await s.flush()
    if row.ref and not row.nd_song_id and not artist_ref and source != "yandex_import":
        from app.mixes.heard_audio import schedule

        schedule(user_id, row.ref)
    return row


async def remove_dislikes(s: AsyncSession, user_id: int, *, ref: str | None = None, nd_song_id: str | None = None,
                          artist_ref: str | None = None, complete: bool = True) -> int:
    if not artist_ref and complete:
        ref, nd_song_id = await Ids(s).complete(ref, nd_song_id)
    cond = _target(ref, nd_song_id, artist_ref)
    if cond is None:
        return 0
    res = await s.execute(delete(Dislike).where(Dislike.user_id == user_id, cond))
    return res.rowcount or 0


async def dislike_event(s: AsyncSession, user_id: int, kind: str, *, ref: str | None, nd_song_id: str | None,
                        client: str, context: str | None = None, target_type: str = "track") -> None:
    """The `dislike`/`undislike` event of a track marked through `/api/dislikes` (the history keeps it too)."""
    await s.execute(sqlite_insert(PlayEvent).values(
        user_id=user_id, event_id=f"hub:{uuid.uuid4().hex}", ts=utcnow(), kind=kind, ref=ref, nd_song_id=nd_song_id,
        context=context, client=client, received_at=utcnow(), target_type=target_type))


# --- Navidrome scrobbles ---------------------------------------------------------------------------------------


async def _listened_near(s: AsyncSession, user_id: int, nd_song_id: str, ts: datetime, length: timedelta) -> bool:
    w = DEDUP_WINDOW + length
    found = (await s.execute(select(PlayEvent.id).where(
        PlayEvent.user_id == user_id, PlayEvent.nd_song_id == nd_song_id, PlayEvent.client != "navidrome",
        PlayEvent.kind.in_(PLAY_KINDS), PlayEvent.ts.between(ts - w, ts + w)).limit(1))).scalar_one_or_none()
    return found is not None


async def import_scrobbles(user: User) -> int:
    """New scrobbles of the user in Navidrome → `finish` events (client "navidrome"). Returns how many were added."""
    from app.library.navidrome import NavidromeHistory
    from app.security import decrypt

    async with session_scope() as s:
        sync = await s.get(NavidromeSync, user.id)
        last = sync.last_scrobble_id if sync else 0
    async with NavidromeHistory(user.nd_username, decrypt(user.nd_password_enc)) as nd:
        rows = await nd.scrobbles_after(last)
    added = 0
    async with session_scope() as s:
        fill = Ids(s)
        for x in rows:
            song, sid = x.get("mediaFileId"), int(x["id"])
            if not song or not x.get("submissionTime"):
                continue
            ts = datetime.fromtimestamp(int(x["submissionTime"]), UTC).replace(tzinfo=None)
            length = await _track_length(s, song, None)
            superseded = await _listened_near(s, user.id, song, ts, length)
            ref, _ = await fill.complete(None, song)
            metadata = await _listening_metadata(s, PlayEventIn(event_id=f"nd:{sid}", ts=ts, kind="finish",
                                                                  ref=ref, nd_song_id=song, client="navidrome"), song)
            res = await s.execute(sqlite_insert(PlayEvent).values(
                user_id=user.id, event_id=f"nd:{sid}", ts=ts, kind="finish", ref=ref, nd_song_id=song,
                duration_s=length.total_seconds() or None,
                client="navidrome", received_at=utcnow(), track_metadata=metadata,
                superseded_scrobble=superseded).on_conflict_do_nothing())
            added += (res.rowcount or 0) if not superseded else 0
        sync = await s.get(NavidromeSync, user.id)
        if sync is None:
            sync = NavidromeSync(user_id=user.id, last_scrobble_id=0)
            s.add(sync)
        sync.last_scrobble_id = max([last, *(int(x["id"]) for x in rows)])
        sync.synced_at = utcnow()
    return added


async def sync_navidrome() -> int:
    """Imports the new scrobbles of every user; a user Navidrome doesn't let in (password changed) is skipped."""
    async with session_scope() as s:
        users = list((await s.execute(select(User).order_by(User.id))).scalars())
    total = 0
    for u in users:
        try:
            total += await import_scrobbles(u)
        except Exception as e:
            log.warning("navidrome scrobbles of user %s: %r", u.id, e)
        try:
            from app.favorites import sync_user
            await sync_user(u)
        except Exception as e:
            log.warning("navidrome favorites of user %s: %r", u.id, e)
    if total:
        log.info("navidrome scrobbles: %s new plays", total)
    return total


async def scrobbles_loop() -> None:
    await asyncio.sleep(FIRST_SYNC_DELAY_S)
    while True:
        await sync_navidrome()
        await asyncio.sleep(SYNC_EVERY_S)
