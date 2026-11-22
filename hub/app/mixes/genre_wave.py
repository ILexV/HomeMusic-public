"""Candidates with an actual genre tag or membership in a genre's editorial radio."""
from __future__ import annotations

import asyncio
import logging

import httpx
from sqlalchemy import func, or_, select

from app.catalog.deezer import CatalogNotFound, DeezerError
from app.catalog.models import CatalogTrack
from app.config import get_settings
from app.db import session_scope
from app.mixes.resolve import library_track
from app.models import Album, Track
from app.services import svc

log = logging.getLogger(__name__)

# Public genre key -> UI name, Deezer genre id, additional exact library tags.
_GENRES = {
    "classical": ("Классика", 98, ("classical music", "классическая музыка", "baroque", "opera", "опера")),
    "pop": ("Поп", 132, ("pop music", "поп-музыка", "поп музыка", "поп-музика")),
    "rock": ("Рок", 152, ("rock music", "рок-музыка", "alternative rock", "classic rock", "hard rock")),
    "jazz": ("Джаз", 129, ("vocal jazz", "smooth jazz", "bebop")),
    "electronic": ("Электроника", 106, ("electro", "electronica", "electronic music", "электронная музыка", "techno", "house")),
    "hiphop": ("Хип-хоп", 116, ("hip-hop", "hip hop", "rap", "рэп", "rap/hip hop", "rap/hip-hop")),
}
GENRE_TITLES = {key: entry[0] for key, entry in _GENRES.items()}
_LIBRARY_PAGE = 40


async def genre_tracks(genre: str, page: int) -> list[CatalogTrack]:
    title, genre_id, aliases = _GENRES[genre]
    # A tagged song on a compilation keeps its own genre. Only inherit when its tag is absent.
    tag = func.coalesce(func.nullif(func.trim(Track.genre), ""), func.trim(Album.genre))
    query = (select(Track).outerjoin(Album, Album.id == Track.album_id)
             .where(func.unicode_casefold(tag).in_((genre, title.casefold(), *aliases)),
                    or_(Track.nd_song_id.is_not(None), Track.deezer_track_id.is_not(None)))
             .order_by(Track.id).offset(page * _LIBRARY_PAGE).limit(_LIBRARY_PAGE))
    async with session_scope() as s:
        tracks = [library_track(t) for t in (await s.scalars(query)).all()]

    if svc.deezer is not None:
        try:
            # Don't leave already playable library candidates behind a slow catalog request.
            async with asyncio.timeout(get_settings().wave_wait_s / 2 if tracks else None):
                tracks.extend(await svc.deezer.genre_radio_tracks(genre_id, page))
        except (TimeoutError, httpx.HTTPError, CatalogNotFound, DeezerError) as exc:
            if not tracks:
                raise
            log.info("genre wave %s: using library while catalog is unavailable: %s", genre, exc)
    return tracks
