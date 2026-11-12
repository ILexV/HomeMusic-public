"""Saved library predicates, deliberately independent of recommendation/smart-shuffle sessions."""
from __future__ import annotations

import asyncio
import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import JSON, DateTime, ForeignKey, String, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.auth import user_navidrome
from app.models import Base, PlayEvent, User, utcnow


class SmartPlaylist(Base):
    __tablename__ = "smart_playlists"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    definition: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LikedRule(StrictModel):
    kind: Literal["liked"]
    value: bool = True


class NotListenedRule(StrictModel):
    kind: Literal["not_listened"]
    days: int = Field(ge=1, le=36500)
    include_unheard: bool = False


class UnheardRule(StrictModel):
    kind: Literal["unheard"]


class GenreRule(StrictModel):
    kind: Literal["genre"]
    value: str = Field(min_length=1, max_length=128)


class YearRule(StrictModel):
    kind: Literal["year"]
    minimum: int = Field(ge=1, le=9999)
    maximum: int = Field(ge=1, le=9999)

    @model_validator(mode="after")
    def ordered(self):
        if self.maximum < self.minimum:
            raise ValueError("Начальный год должен быть не больше конечного")
        return self


class AddedRule(StrictModel):
    kind: Literal["recently_added"]
    days: int = Field(ge=1, le=36500)


Rule = Annotated[LikedRule | NotListenedRule | UnheardRule | GenreRule | YearRule | AddedRule,
                 Field(discriminator="kind")]


class Definition(StrictModel):
    match: Literal["all", "any"] = "all"
    rules: list[Rule] = Field(min_length=1, max_length=32)
    sort: Literal["title", "artist", "year", "added", "last_listened", "random"] = "title"
    descending: bool = False
    limit: int = Field(default=100, ge=1, le=2000)


class PlaylistInput(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    definition: Definition


class PlaylistOut(PlaylistInput):
    id: str
    created_at: datetime
    updated_at: datetime


class TrackOut(StrictModel):
    nd_song_id: str
    title: str
    artist: str
    album: str
    duration_s: float


class Evaluation(StrictModel):
    count: int  # before limit, not the length of the returned queue
    tracks: list[TrackOut]


def timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt
    except ValueError:
        return None


def listening(song: dict, history: dict[str, datetime], now: datetime) -> tuple[bool, datetime | None]:
    """No recent-history window: missing recent events do not mean a track was never heard.

    Navidrome's per-account play count can prove listening even on older servers that omit
    the last-play date. Such a song is neither unheard nor provably older than N days.
    """
    candidates = [at for at in (history.get(str(song["id"])), timestamp(song.get("played")),
                                timestamp(song.get("lastPlayed"))) if at is not None and at <= now]
    last = max(candidates, default=None)
    return last is not None or float(song.get("playCount") or 0) > 0, last


def matches(rule: Rule, song: dict, liked: set[str], heard: bool, last: datetime | None, now: datetime) -> bool:
    if isinstance(rule, LikedRule):
        return (str(song["id"]) in liked) == rule.value
    if isinstance(rule, UnheardRule):
        return not heard
    if isinstance(rule, NotListenedRule):
        return (not heard and rule.include_unheard) or (last is not None and last <= now - timedelta(days=rule.days))
    if isinstance(rule, GenreRule):
        genres = {str(song.get("genre") or "").casefold()}
        genres.update(str(g.get("name") or "").casefold() for g in song.get("genres", []) if isinstance(g, dict))
        return rule.value.casefold() in genres
    if isinstance(rule, YearRule):
        year = int(song.get("year") or 0)
        return rule.minimum <= year <= rule.maximum
    if isinstance(rule, AddedRule):
        added = timestamp(song.get("created"))
        return added is not None and now - timedelta(days=rule.days) <= added <= now
    raise TypeError("Unsupported validated rule")


def evaluate_tracks(definition: Definition, songs: list[dict], liked: set[str], history: dict[str, datetime],
                    now: datetime | None = None) -> Evaluation:
    now = now or utcnow()
    accepted = []
    last_played = {}
    combine = all if definition.match == "all" else any
    for song in songs:
        heard, last = listening(song, history, now)
        if combine(matches(rule, song, liked, heard, last, now) for rule in definition.rules):
            accepted.append(song)
            last_played[str(song["id"])] = last
    count = len(accepted)
    if definition.sort == "random":
        random.SystemRandom().shuffle(accepted)
    else:
        def key(song):
            kind = definition.sort
            value = (int(song.get("year") or 0) if kind == "year" else
                     timestamp(song.get("created")) or datetime.min if kind == "added" else
                     last_played[str(song["id"])] or datetime.min if kind == "last_listened" else
                     str(song.get(kind) or "").casefold())
            return value, str(song.get("title") or "").casefold(), str(song["id"])
        accepted.sort(key=key, reverse=definition.descending)
    return Evaluation(count=count, tracks=[TrackOut(
        nd_song_id=str(song["id"]), title=str(song.get("title") or "Без названия"),
        artist=str(song.get("artist") or ""), album=str(song.get("album") or ""),
        duration_s=max(0, float(song.get("duration") or 0)),
    ) for song in accepted[:definition.limit]])


async def listening_history(s: AsyncSession, user_id: int, now: datetime) -> dict[str, datetime]:
    # Likes, starts, errors and zero-playback finishes are not listening. Legacy completed
    # plays and imported scrobbles lack listened_ms; keep them. Aggregate *all* saved history.
    q = select(PlayEvent.nd_song_id, func.max(PlayEvent.ts)).where(
        PlayEvent.user_id == user_id, PlayEvent.target_type == "track", PlayEvent.nd_song_id.is_not(None),
        PlayEvent.ts <= now, PlayEvent.superseded_scrobble.is_(False),
        or_(and_(PlayEvent.kind.in_(("progress", "skip", "finish", "interrupted")), PlayEvent.listened_ms > 0),
            and_(PlayEvent.kind == "finish", PlayEvent.listened_ms.is_(None))),
    ).group_by(PlayEvent.nd_song_id)
    return dict((await s.execute(q)).all())


async def library_songs(nd) -> list[dict]:
    """Empty OpenSubsonic search enumerates only this authenticated user's visible library."""
    songs = []
    seen = set()
    offset = 0
    while True:
        page = (await nd.search3("", song_count=500, song_offset=offset)).get("song", [])
        if not page:
            return songs
        added = 0
        for song in page:
            song_id = str(song.get("id") or "")
            if song_id and song_id not in seen:
                songs.append(song)
                seen.add(song_id)
                added += 1
        if not added:
            raise ValueError("Navidrome не поддерживает постраничное чтение медиатеки")
        offset += len(page)
        # Request until empty: some servers cap pages below the requested count.


async def evaluate(s: AsyncSession, user: User, definition: Definition) -> Evaluation:
    # One total deadline, below the desktop Hub timeout. Never return a truncated library
    # as a successful match count; failures leave both the saved definition and queue intact.
    async with asyncio.timeout(20):
        async with user_navidrome(user) as nd:
            if any(isinstance(rule, LikedRule) for rule in definition.rules):
                songs, starred = await asyncio.gather(library_songs(nd), nd.get_starred2())
                liked = {str(song["id"]) for song in starred.get("song", []) if song.get("id")}
                from app.favorites import snapshot
                for state in (await snapshot(s, user.id)).values():
                    if state.target_type == "track" and state.event.mirror_pending:
                        ids = {a[3:] for a in state.aliases if a.startswith("nd:")}
                        if state.nd_id:
                            ids.add(state.nd_id)
                        if state.liked:
                            liked.update(ids)
                        else:
                            liked.difference_update(ids)
            else:
                songs, liked = await library_songs(nd), set()
        now = utcnow()
        history = await listening_history(s, user.id, now)
        return evaluate_tracks(definition, songs, liked, history, now)
