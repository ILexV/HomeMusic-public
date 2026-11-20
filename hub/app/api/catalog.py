"""Spotify-like artist and album pages with library ownership (FR-4)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import AlbumOut, ArtistPageOut, TrackOut
from app.auth import current_user
from app.catalog.models import ArtistPage, CatalogAlbum, split_ref
from app.db import get_session
from app.library.owned import annotate_albums, annotate_tracks
from app.models import User
from app.services import svc

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


def _split(ref: str) -> tuple[str, str]:
    try:
        return split_ref(ref)
    except ValueError as e:
        raise HTTPException(400, "Неверная ссылка на каталог") from e


async def get_artist_page(ref: str) -> ArtistPage:
    src, id_ = _split(ref)
    try:
        if src == "dz" and svc.deezer:
            return await svc.deezer.get_artist_page(id_)
        if src == "hm" and svc.hitmoz_catalog:
            return await svc.hitmoz_catalog.get_artist(id_)
    except HTTPException:
        raise
    except Exception as e:
        if type(e).__name__ == "CatalogNotFound":
            raise HTTPException(404, "Исполнитель не найден") from e
        raise HTTPException(502, f"Каталог недоступен: {e}") from e
    raise HTTPException(404, "Исполнитель не найден")


async def get_album(ref: str) -> CatalogAlbum:
    src, id_ = _split(ref)
    try:
        if src == "dz" and svc.deezer:
            return await svc.deezer.get_album(id_)
        if src == "hm" and svc.hitmoz_catalog:
            return await svc.hitmoz_catalog.get_album(id_)
    except Exception as e:
        if type(e).__name__ == "CatalogNotFound":
            raise HTTPException(404, "Альбом не найден") from e
        raise HTTPException(502, f"Каталог недоступен: {e}") from e
    raise HTTPException(404, "Альбом не найден")


async def library_album_tracks(s: AsyncSession, user_id: int, artist: str, title: str, year: int | None,
                               deezer_id: str | None) -> list[TrackOut]:
    """Read catalog metadata only. Without a stored release ID, never guess between editions."""
    def key(text: str) -> str:
        return " ".join(text.casefold().split())

    if deezer_id:
        try:
            release = await get_album(f"dz:{deezer_id}")
        except HTTPException:
            log.info("Stored Deezer album unavailable: %s", deezer_id)
        else:
            return await annotate_tracks(s, release.tracks or [], user_id)
    if not artist or not title:
        return []
    for source in ("dz", "hm"):
        try:
            if source == "dz" and not deezer_id and svc.deezer:
                candidates = await svc.deezer.search_albums(artist, title)
            elif source == "hm" and svc.hitmoz_catalog:
                candidates = await svc.hitmoz_catalog.search_albums(f"{artist} {title}")
            else:
                continue
            matches = set()
            for a in candidates:
                candidate_year = a.year or (a.release_date and a.release_date.year)
                if key(a.artist) == key(artist) and key(a.title) == key(title):
                    if not year or not candidate_year or year == candidate_year:
                        matches.add(a.ref)
            if len(matches) > 1:
                return []
            if not matches:
                continue
            release = await get_album(matches.pop())
        except Exception:
            log.warning("%s album lookup unavailable: %s / %s", source, artist, title, exc_info=True)
            continue
        if release.tracks:
            return await annotate_tracks(s, release.tracks, user_id)
    return []


@router.get("/artist/{ref}", response_model=ArtistPageOut)
async def artist(ref: str, user: User = Depends(current_user), s: AsyncSession = Depends(get_session, scope="function")):
    page = await get_artist_page(ref)
    albums = await annotate_albums(s, page.albums, user.id)
    albums.sort(key=lambda a: (a.release_date is not None, a.release_date), reverse=True)
    return ArtistPageOut(
        artist=page.artist,
        top_tracks=await annotate_tracks(s, page.top_tracks, user.id),
        albums=[a for a in albums if a.record_type == "album"],
        singles=[a for a in albums if a.record_type in ("single", "ep")],
        compilations=[a for a in albums if a.record_type == "compile"],
    )


@router.get("/album/{ref}", response_model=AlbumOut)
async def album(ref: str, user: User = Depends(current_user), s: AsyncSession = Depends(get_session, scope="function")):
    a = await get_album(ref)
    return (await annotate_albums(s, [a], user.id))[0]
