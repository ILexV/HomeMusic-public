"""Unified search: library + catalog (Deezer, fallback hitmoz), and raw provider search."""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import listen
from app.api.library import track_out
from app.api.schemas import ProviderSearchOut, SearchHistoryIn, SearchHistoryOut, SearchOut, SearchTracksOut
from app.auth import current_user
from app.catalog.models import CatalogTrack, SearchResults
from app.config import get_settings
from app.db import get_session
from app.events import bus
from app.library.owned import annotate_albums, annotate_tracks
from app.matching.normalize import has_cyrillic, normalize_title
from app.matching.select import rank_candidates
from app.models import SearchHistory, Track, User, utcnow
from app.providers.base import TrackQuery
from app.services import svc

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/search", tags=["search"])

ROW_LIMIT = 12  # artists and albums: one horizontal row each
TRACK_PAGE = 25  # tracks: the first page, then more as the list is scrolled (GET /api/search/tracks)
HISTORY_LIMIT = 30


def _recent_queries(user_id: int):
    return select(SearchHistory).where(SearchHistory.user_id == user_id).order_by(
        SearchHistory.searched_at.desc(), SearchHistory.query_key
    ).limit(HISTORY_LIMIT)


@router.get("/history", response_model=SearchHistoryOut)
async def search_history(response: Response, user: User = Depends(current_user),
                         s: AsyncSession = Depends(get_session, scope="function")):
    response.headers["Cache-Control"] = "no-store"
    rows = (await s.execute(_recent_queries(user.id))).scalars()
    return SearchHistoryOut(queries=[row.query for row in rows])


@router.post("/history", response_model=dict[str, bool])
async def remember_search(body: SearchHistoryIn, user: User = Depends(current_user),
                          s: AsyncSession = Depends(get_session, scope="function")):
    # Start with a write: concurrent devices serialize their upsert and pruning in SQLite.
    stmt = insert(SearchHistory).values(
        user_id=user.id, query_key=body.query.casefold(), query=body.query, searched_at=utcnow()
    )
    await s.execute(stmt.on_conflict_do_update(
        index_elements=[SearchHistory.user_id, SearchHistory.query_key],
        set_={"query": stmt.excluded.query, "searched_at": stmt.excluded.searched_at},
    ))
    keep = _recent_queries(user.id).with_only_columns(SearchHistory.query_key)
    await s.execute(delete(SearchHistory).where(
        SearchHistory.user_id == user.id, SearchHistory.query_key.not_in(keep)
    ))
    await s.commit()
    bus.publish(user.id, "search_history", {})
    return {"ok": True}


@router.delete("/history", response_model=dict[str, bool])
async def clear_search_history(user: User = Depends(current_user),
                               s: AsyncSession = Depends(get_session, scope="function")):
    await s.execute(delete(SearchHistory).where(SearchHistory.user_id == user.id))
    await s.commit()
    bus.publish(user.id, "search_history", {})
    return {"ok": True}


async def search_library(s: AsyncSession, q: str, limit: int = 20) -> list[Track]:
    needle = q.casefold()
    if not needle:
        return []
    rows = (
        await s.execute(
            select(Track)
            .where(or_(*(func.instr(func.unicode_casefold(field), needle) > 0
                         for field in (Track.title, Track.artist, Track.album))))
            .limit(limit)
        )
    ).scalars().all()
    if rows:
        return list(rows)
    # No literal phrase: allow normalized words across different fields.
    words = normalize_title(q).text.split()
    if not words:
        return []
    out = []
    for t in (await s.execute(select(Track))).scalars():
        hay = normalize_title(f"{t.artist} {t.title} {t.album or ''}").text
        if all(w in hay for w in words):
            out.append(t)
            if len(out) >= limit:
                break
    return out


@router.get("", response_model=SearchOut)
async def search(q: str = Query(min_length=1), user: User = Depends(current_user),
                 s: AsyncSession = Depends(get_session, scope="function")):
    q = q.strip()
    out = SearchOut(library=[track_out(t) for t in await search_library(s, q)])
    res: SearchResults | None = None
    if svc.deezer is not None:
        try:
            res, (tracks, out.tracks_next) = await asyncio.gather(
                svc.deezer.search(q, kinds=("artist", "album"), limit=ROW_LIMIT),
                svc.deezer.search_track_page(q, 0, TRACK_PAGE),
            )
            res.tracks = tracks
        except Exception as e:
            log.warning("deezer search failed: %r", e)
            out.errors.append("Каталог Deezer недоступен")
    if (res is None or not (res.artists or res.tracks)) and has_cyrillic(q) and svc.hitmoz_catalog is not None:
        try:
            artists = await svc.hitmoz_catalog.search_artists(q)
            res = SearchResults(artists=artists, albums=res.albums if res else [], tracks=res.tracks if res else [])
        except Exception as e:
            log.warning("hitmoz catalog search failed: %r", e)
            out.errors.append("Каталог hitmoz недоступен")
    if res is not None:
        out.artists = res.artists
        out.albums = await annotate_albums(s, res.albums, user.id)
        out.tracks = await annotate_tracks(s, _unique(res.tracks), user.id)
    return out


def _unique(tracks: list[CatalogTrack]) -> list[CatalogTrack]:
    """Deezer may list a track twice; the PWA keys rows by ref."""
    seen: set[str] = set()
    return [t for t in tracks if not (t.ref in seen or seen.add(t.ref))]


@router.get("/tracks", response_model=SearchTracksOut)
async def search_tracks(q: str = Query(min_length=1), offset: int = Query(0, ge=0),
                        limit: int = Query(TRACK_PAGE, ge=1, le=100), user: User = Depends(current_user),
                        s: AsyncSession = Depends(get_session, scope="function")):
    """More catalog tracks for `/api/search` (Deezer only: the hitmoz fallback has no pages)."""
    if svc.deezer is None:
        return SearchTracksOut()
    try:
        tracks, nxt = await svc.deezer.search_track_page(q.strip(), offset, limit)
    except Exception as e:
        log.warning("deezer search page failed: %r", e)
        raise HTTPException(502, "Каталог Deezer недоступен") from e
    return SearchTracksOut(tracks=await annotate_tracks(s, _unique(tracks), user.id), next_offset=nxt)


@router.get("/providers", response_model=ProviderSearchOut)
async def search_providers(q: str = Query(min_length=1), provider: str | None = None,
                           group: Literal["sources", "reuploads"] | None = None,
                           user: User = Depends(current_user)):
    """Raw downloadable files. "Artist - Title" is split for ranking; otherwise results keep provider order.
    `group`: only the regular sources, or only re-uploads by small channels (slower: SoundCloud takes seconds).
    Each file can then be listened to and downloaded by its ref "file:<provider>:<provider_id>" (`app.listen`)."""
    artist, _, title = q.partition(" - ")
    tq = TrackQuery(artist=artist.strip() if title else q.strip(), title=title.strip() if title else "")
    reuploads = {p.name for p in svc.reupload_providers()}
    names = [provider] if provider else [
        n for n in svc.providers if group is None or (n in reuploads) == (group == "reuploads")
    ]
    out = ProviderSearchOut()

    async def one(name: str):
        p = svc.providers.get(name)
        if p is None:
            return []
        try:
            raw = await p.search(TrackQuery(artist=tq.artist, title=tq.title or ""), limit=15)
        except Exception as e:
            out.errors.append(f"{name}: {e}")
            return []
        if title:
            ranked = rank_candidates(tq, raw, get_settings().cfg.matching)
            return ranked or raw
        return raw

    for lst in await asyncio.gather(*(one(n) for n in names)):
        out.candidates.extend(lst)
    await listen.remember_files(out.candidates)
    return out
