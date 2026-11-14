"""Reversible recommendation-only restrictions, resolved against server-verified identities."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import session_scope
from app.favorites import AliasIndex
from app.mixes.resolve import catalog_track
from app.mixes.taste import artist_key
from app.models import FavoriteSubject, Track, User, utcnow
from app.plays import Ids
from app.recommendation_models import RecommendationPause


class PolicyError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def expires_at(action: str, now: datetime | None = None, timezone: str | None = None) -> datetime:
    now = now or utcnow()
    utc = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    if action == "today":
        zone = ZoneInfo(timezone or get_settings().timezone)
        tomorrow = utc.astimezone(zone).date() + timedelta(days=1)
        return datetime.combine(tomorrow, time.min, zone).astimezone(UTC).replace(tzinfo=None)
    if action not in ("week", "less_artist"):
        raise PolicyError(422, "Неизвестное ограничение")
    return (utc + timedelta(days=7 if action == "week" else 30)).replace(tzinfo=None)


def identity_aliases(ref: str | None, nd_song_id: str | None) -> set[str]:
    if ref and ref.startswith("nd:"):
        nd_song_id, ref = ref[3:], None
    return {x for x in (f"ref:{ref}" if ref else None, f"nd:{nd_song_id}" if nd_song_id else None) if x}


@dataclass
class Policy:
    blocked_aliases: set[str] = field(default_factory=set)
    quiet_artists: set[str] = field(default_factory=set)

    def blocked(self, candidate: Any) -> bool:
        return bool(self.blocked_aliases & identity_aliases(candidate.track.ref, candidate.nd_song_id))

    def multiplier(self, candidate: Any) -> float:
        return 0.25 if artist_key(candidate.track.artist) in self.quiet_artists else 1.0


async def _alias_index(s: AsyncSession, pairs: list[tuple[str | None, str | None]]) -> AliasIndex:
    subjects = list((await s.execute(select(FavoriteSubject).where(FavoriteSubject.target_type == "track"))).scalars())
    index = AliasIndex(subjects=subjects)
    ids = Ids(s)
    # Complete both directions separately; a submitted pair itself is never proof of identity.
    partial = [(ref, None) for ref, _ in pairs if ref and not ref.startswith("nd:")]
    partial += [(None, nd) for _, nd in pairs if nd]
    partial += [(None, ref[3:]) for ref, _ in pairs if ref and ref.startswith("nd:")]
    await ids.prime(partial)
    for ref, nd in partial:
        index.join("track", identity_aliases(*(await ids.complete(ref, nd))))
    # Several library files can carry the same verified catalog identity. Keep every
    # such alias, not only the newest row selected by Ids for ordinary playback.
    relevant = set().union(*(identity_aliases(ref, nd) for ref, nd in pairs))
    relevant = _expanded(index, relevant)
    dz = {key[7:] for key in relevant if key.startswith("ref:dz:")}
    songs = {key[3:] for key in relevant if key.startswith("nd:")}
    tracks = (await s.execute(select(Track).where(
        or_(Track.deezer_track_id.in_(dz), Track.nd_song_id.in_(songs))))).scalars()
    for track in tracks:
        ref = f"dz:{track.deezer_track_id}" if track.deezer_track_id else None
        index.join("track", identity_aliases(ref, track.nd_song_id))
    return index


def _expanded(index: AliasIndex, keys: set[str]) -> set[str]:
    roots = {index.root(("track", key)) for key in keys}
    return keys | {key for typ, key in list(index.parent) if typ == "track" and index.root((typ, key)) in roots}


async def load_policy(user_id: int) -> Policy:
    """One batched read per recommendation selection; candidate checks never query the DB."""
    async with session_scope() as s:
        rows = list((await s.execute(select(RecommendationPause).where(
            RecommendationPause.user_id == user_id, RecommendationPause.expires_at > utcnow()))).scalars())
        if not rows:
            return Policy()
        index = await _alias_index(s, [(r.ref, r.nd_song_id) for r in rows])
        blocked, artists = set(), set()
        for row in rows:
            if row.action == "less_artist":
                artists.add(row.artist_key)
            else:
                keys = set(row.aliases or []) | identity_aliases(row.ref, row.nd_song_id)
                blocked.update(_expanded(index, keys))
    return Policy(blocked, artists)


async def resolve_target(s: AsyncSession, user: User, ref: str | None, nd_song_id: str | None) -> dict[str, Any]:
    """Resolve authoritative metadata; reject unverified ref/song pairs rather than joining client aliases."""
    if ref and ref.startswith("nd:"):
        if nd_song_id and nd_song_id != ref[3:]:
            raise PolicyError(422, "Идентификаторы трека не совпадают")
        nd_song_id, ref = ref[3:], None
    if not ref and not nd_song_id:
        raise PolicyError(422, "Нужен ref или nd_song_id")
    index = await _alias_index(s, [(ref, nd_song_id)])
    if ref and nd_song_id:
        if index.root(("track", f"ref:{ref}")) != index.root(("track", f"nd:{nd_song_id}")):
            raise PolicyError(422, "Связь каталога с библиотекой не подтверждена")
    ids = Ids(s)
    ref, nd_song_id = await ids.complete(ref, nd_song_id)
    title = artist = None
    if nd_song_id:
        row = (await s.execute(select(Track).where(Track.nd_song_id == nd_song_id)
                               .order_by(Track.id.desc()).limit(1))).scalar_one_or_none()
        if row:
            title, artist = row.title, row.artist
        else:
            from app.auth import user_navidrome
            nd = user_navidrome(user)
            try:
                song = await nd.get_song(nd_song_id)
                title, artist = song.get("title"), song.get("artist")
            except Exception as exc:
                raise PolicyError(404, "Трек библиотеки не найден") from exc
            finally:
                await nd.aclose()
    elif ref and ref.startswith("dz:"):
        track = await catalog_track(ref)
        if track:
            title, artist = track.title, track.artist
    elif ref and ref.startswith("file:"):
        from app import listen
        track = await listen.file_candidate(ref)
        if track:
            title, artist = track.title, track.artist
    if not title or not artist or not artist_key(artist):
        raise PolicyError(404, "Не удалось определить трек и исполнителя")
    keys = _expanded(index, identity_aliases(ref, nd_song_id))
    return {"ref": ref, "nd_song_id": nd_song_id, "artist": artist, "artist_key": artist_key(artist),
            "label": f"{artist} — {title}", "aliases": sorted(keys)}
