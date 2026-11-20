"""Spotify-like artist and album pages with library ownership (FR-4)."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import AlbumOut, ArtistPageOut, CatalogStatus, LibTrackOut, TrackOut
from app.auth import current_user
from app.catalog.deezer import CatalogNotFound
from app.catalog.models import ArtistPage, CatalogAlbum, CatalogTrack, split_ref
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


def _key(text: str) -> str:
    # Keep edition/version qualifiers: "live", "remix", "deluxe", etc. are significant.
    return " ".join(text.casefold().split())


def _isrc(t: LibTrackOut | CatalogTrack) -> str:
    return (getattr(t, "isrc", None) or "").replace("-", "").strip().upper()


def match_album_tracks(local: Sequence[LibTrackOut | CatalogTrack], release: Sequence[CatalogTrack]) -> dict[int, int]:
    """Catalog slot → local slot, one-to-one. Reserve exact identities before comparing tags."""
    matches: dict[int, int] = {}
    used: set[int] = set()

    def name(t):
        return (_key(t.artist), _key(t.title)) if t.artist and t.title else None

    def slot(t):
        return (names, t.disc_no or 1, t.track_no) if t.track_no and (names := name(t)) else None

    for key in (lambda t: t.ref, _isrc, slot, name):
        index = defaultdict(list)
        for i, t in enumerate(local):
            if i not in used and (k := key(t)):
                index[k].append(i)
        proposals: dict[int, int] = {}
        for j, t in enumerate(release):
            if j in matches:
                continue
            k = key(t)
            candidates = index.get(k, ())
            if key is slot or key is name:
                candidates = [i for i in candidates if _tags_agree(local[i], t)]
            if len(candidates) == 1:
                i = candidates[0]
                proposals[i] = -1 if i in proposals else j
        for i, j in proposals.items():
            if j >= 0:  # neither side could be assigned to another slot on this evidence
                matches[j] = i
                used.add(i)
    return matches


def _tags_agree(a: LibTrackOut | CatalogTrack, b: CatalogTrack) -> bool:
    if _isrc(a) and _isrc(b) and _isrc(a) != _isrc(b):
        return False
    if a.explicit is not None and b.explicit is not None and a.explicit != b.explicit:
        return False
    if a.duration_s and b.duration_s:
        return abs(a.duration_s - b.duration_s) <= 3
    return bool(a.track_no and a.track_no == b.track_no and (a.disc_no or 1) == (b.disc_no or 1))


def _release_year(a: CatalogAlbum) -> int | None:
    return a.year or (a.release_date.year if a.release_date else None)


def _complete(a: CatalogAlbum) -> bool:
    return bool(a.tracks and len(a.tracks) >= (a.nb_tracks or 0))


_MAX_DETAILS = 4
_PROVIDER_SECONDS = 9  # leave room for the second catalog within the player's 25 s read timeout


@dataclass
class _ReleaseHint:
    """Best available identity, retained for the second provider even if the primary times out."""

    artist: str
    title: str
    year: int | None
    edition: CatalogAlbum | None = None


async def _search_release(
    provider, source: str, hint: _ReleaseHint, local: Sequence[LibTrackOut | CatalogTrack],
) -> tuple[CatalogStatus, CatalogAlbum | None]:
    artist, title, year, edition = hint.artist, hint.title, hint.year, hint.edition
    if not artist or not title:
        return "not_found", None
    # Hitmoz indexes the album block by title. "Artist Title" often returns only song
    # search results even when the album exists. Resolve its artist on the album page.
    albums = await provider.search_albums(artist, title) if source == "dz" else await provider.search_albums(title)

    def compatible(a: CatalogAlbum, *, summary: bool) -> bool:
        # Hitmoz summaries may lack artist; Deezer summaries may lack release_date. Check the page itself.
        return (_key(title) == _key(a.title) and (_key(artist) == _key(a.artist) or (summary and not a.artist))
                and (not year or not _release_year(a) or year == _release_year(a)))

    known_refs = {t.ref for t in local if t.ref and t.ref.startswith("dz:")} if source == "dz" else set()
    ids = {a.id for a in albums if compatible(a, summary=True)}
    if len(ids) > _MAX_DETAILS:
        return "ambiguous", None
    found = None
    incomplete = False
    for id_ in sorted(ids):
        try:
            candidate = await provider.get_album(id_)
        except CatalogNotFound:
            continue
        if candidate.id != id_ or not compatible(candidate, summary=False):
            continue
        if not _complete(candidate):
            incomplete = True
            continue
        if edition and edition.nb_tracks and len(candidate.tracks) != edition.nb_tracks:
            continue  # e.g. a namesake LP that includes the single we actually identified
        if edition and edition.upc and candidate.upc and edition.upc != candidate.upc:
            continue
        if known_refs and not known_refs.issubset({t.ref for t in candidate.tracks}):
            continue
        if local and not match_album_tracks(local, candidate.tracks):
            continue
        if found is not None:
            return "ambiguous", None
        found = candidate
    if incomplete:
        return "unavailable", None
    return ("matched", found) if found else ("not_found", None)


async def _exact_album(provider, id_: str, hint: _ReleaseHint, known_refs: set[str]) -> tuple[CatalogStatus, CatalogAlbum | None]:
    try:
        a = await provider.get_album(id_)
    except CatalogNotFound:
        return "not_found", None
    except Exception:
        log.warning("Deezer album %s unavailable", id_, exc_info=True)
        return "unavailable", None
    if a.id != id_:
        return "unavailable", None
    hint.edition = a
    hint.artist, hint.title, hint.year = a.artist, a.title, _release_year(a)
    if not _complete(a):
        return "unavailable", None
    if known_refs and not known_refs.issubset(t.ref for t in a.tracks):
        return "ambiguous", None
    return "matched", a


async def _deezer_release(provider, hint: _ReleaseHint, album_id: str | None,
                          local: Sequence[LibTrackOut | CatalogTrack]) -> tuple[CatalogStatus, CatalogAlbum | None]:
    status: CatalogStatus = "not_found"
    known_refs = {t.ref for t in local if t.ref and t.ref.startswith("dz:")}
    if album_id:
        status, release = await _exact_album(provider, album_id, hint, known_refs)
        if release or status == "ambiguous":
            return status, release

    # An exact Deezer track ID belongs to a release; a recording title or an ISRC alone doesn't.
    for ref in sorted(known_refs)[:3]:
        try:
            track = await provider.get_track(split_ref(ref)[1])
        except CatalogNotFound:
            continue
        except Exception:
            status = "unavailable"
            break
        if track.ref != ref:
            continue
        if track.album_title and hint.edition is None:
            hint.artist, hint.title = track.album_artist or track.artist, track.album_title
            hint.year = track.release_date.year if track.release_date else None
        if not track.album_ref or not track.album_ref.startswith("dz:"):
            continue
        parent_id = split_ref(track.album_ref)[1]
        if album_id:
            return ("ambiguous", None) if parent_id != album_id else (status, None)
        return await _exact_album(provider, parent_id, hint, known_refs)
    if album_id:
        return status, None  # don't substitute a different Deezer edition for the known ID
    result, release = await _search_release(provider, "dz", hint, local)
    return (status if result == "not_found" else result), release


async def library_album_tracks(
    s: AsyncSession, user_id: int, artist: str, title: str, year: int | None,
    deezer_id: str | None, local: Sequence[LibTrackOut | CatalogTrack],
) -> tuple[CatalogStatus, list[TrackOut]]:
    """Prefer the stored release, then its exact recording, then a checked album search. Metadata only."""
    hint = _ReleaseHint(artist, title, year)
    status: CatalogStatus = "not_found" if svc.deezer or svc.hitmoz_catalog else "unavailable"

    for source, provider in (("dz", svc.deezer), ("hm", svc.hitmoz_catalog)):
        if provider is None:
            continue
        release: CatalogAlbum | None
        try:
            async with asyncio.timeout(_PROVIDER_SECONDS):
                if source == "dz":
                    result, release = await _deezer_release(provider, hint, deezer_id, local)
                else:
                    result, release = await _search_release(provider, source, hint, local)
        except Exception:
            status = "unavailable"
            log.warning("%s album lookup unavailable: %s / %s", source, artist, title, exc_info=True)
            continue
        if result == "ambiguous":
            return result, []
        if result == "unavailable":
            status = result
        if release:
            return "matched", await annotate_tracks(s, release.tracks or [], user_id)
    return status, []


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
