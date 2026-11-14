"""Read-only catalog feed from onboarding and the current account's favorite ledger.

Only public catalog responses are cached. Profile selection and seen flags are never
cached together, so account changes and read reversals are immediately visible.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog.cache import cached
from app.catalog.deezer import CatalogNotFound, map_album
from app.catalog.models import CatalogAlbum
from app.config import get_settings
from app.favorites import snapshot
from app.matching.normalize import normalize_artist
from app.models import MixSettings, Track, utcnow
from app.release_models import ReleaseSeen
from app.services import svc

Period = Literal["week", "month"]
MAX_ARTISTS = 20
MAX_PAGES = 3
PAGE_SIZE = 100
CONCURRENCY = 4
ARTIST_TIMEOUT_S = 6.0
FEED_TIMEOUT_S = 15.0
CACHE_TTL_S = 3600
CATALOG_ERROR = "Часть каталога недоступна; показаны доступные релизы."
LIMIT_ERROR = "Достигнут лимит проверки исполнителей или релизов; список может быть неполным."
IDENTITY_ERROR = "Не для всех избранных исполнителей найдена точная запись в каталоге."


def catalog_ref(ref: str) -> tuple[str, str]:
    """Only album sources understood by the existing album navigation/API."""
    match = re.fullmatch(r"(dz|hm):([1-9][0-9]{0,19})", ref)
    if not match:
        raise ValueError("invalid catalog reference")
    return match[1], match[2]


def date_window(period: Period, now: datetime, timezone: str) -> tuple[date, date]:
    if period not in ("week", "month"):
        raise ValueError("invalid release period")
    aware = now.replace(tzinfo=UTC) if now.tzinfo is None else now
    today = aware.astimezone(ZoneInfo(timezone)).date()
    return today - timedelta(days=(7 if period == "week" else 30) - 1), today


def release_date(value: object) -> date | None:
    # Catalog dates are calendar dates, not timestamps or incomplete years/months.
    if type(value) is date:
        return value
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


async def _targets(s: AsyncSession, user_id: int, now: datetime) -> tuple[list[dict], bool]:
    settings = await s.get(MixSettings, user_id)
    targets = [{"ref": a.get("ref"), "name": a.get("name"), "kind": "artist"}
               for a in (settings.artists_json or [] if settings else []) if isinstance(a, dict)]
    states = sorted((x for x in (await snapshot(s, user_id, now)).values() if x.liked),
                    key=lambda x: x.since or datetime.min, reverse=True)
    # Read only metadata of library tracks actually favored by this account.
    nd_ids = {x.nd_id for x in states if x.target_type == "track" and x.nd_id}
    library = dict((await s.execute(select(Track.nd_song_id, Track.artist)
                                   .where(Track.nd_song_id.in_(nd_ids)))).all()) if nd_ids else {}
    for state in states:
        data = state.data or {}
        name = (data.get("name") or data.get("artist")) if state.target_type == "artist" else data.get("artist")
        name = name or (library.get(state.nd_id) if state.target_type == "track" else None)
        targets.append({"ref": data.get("artist_ref") or (state.ref if state.target_type == "artist" else None),
                        "name": name, "kind": "artist", "subject_ref": state.ref,
                        "subject_kind": state.target_type, "subject_nd_id": state.nd_id})
    unique = {}
    for target in targets:
        key = (target.get("ref") or str(target.get("name") or "").strip().casefold()
               or target.get("subject_ref") or
               (f"nd:{target.get('subject_kind')}:{target['subject_nd_id']}" if target.get("subject_nd_id") else None))
        if key:
            unique.setdefault(key, target)
    return list(unique.values())[:MAX_ARTISTS], len(unique) > MAX_ARTISTS


async def _artist(target: dict):
    dz = svc.deezer
    if dz is None:
        raise RuntimeError("catalog unavailable")
    ref, name = target.get("ref"), target.get("name")
    if ref:
        src, id_ = catalog_ref(ref)
        if src == "dz":
            artist = await dz.get_artist(id_)
            if artist.ref != ref:
                return None
            return artist
    if not name and target.get("subject_ref"):
        src, id_ = catalog_ref(target["subject_ref"])
        if src == "dz":
            if target.get("subject_kind") == "track":
                subject = await dz.get_track(id_)
            elif target.get("subject_kind") == "album":
                subject = await dz.get_album(id_)
            else:
                return None
            if subject.ref != target["subject_ref"]:
                return None
            if subject.artist_ref and subject.artist_ref.startswith("dz:"):
                return await dz.get_artist(catalog_ref(subject.artist_ref)[1])
            name = subject.artist
    if not name:
        return None
    results = await dz.search(name, kinds=("artist",), limit=5)
    want = normalize_artist(name)
    matches = []
    for artist in results.artists:
        got = normalize_artist(artist.name)
        if (got.text and got.text == want.text) or (got.latin and got.latin == want.latin):
            matches.append(artist)
    # Ambiguous names must not silently subscribe the user to a different artist.
    return matches[0] if len(matches) == 1 else None


async def _artist_releases(artist) -> tuple[list[CatalogAlbum], bool]:
    """Use the existing rate-limited transport/cache but bounded pagination.

    get_artist_albums intentionally merges clean/explicit versions and reads every
    page. A release feed must retain distinct catalog identities and expose caps.
    """
    async def produce():
        albums = []
        more = False
        for page in range(MAX_PAGES):
            payload = await svc.deezer._get(f"artist/{artist.id}/albums",
                                           {"limit": PAGE_SIZE, "index": page * PAGE_SIZE})
            rows = payload.get("data")
            if not isinstance(rows, list):
                raise ValueError("invalid album listing")
            for row in rows[:PAGE_SIZE]:
                if not isinstance(row, dict) or not str(row.get("id", "")).isdigit():
                    continue
                album = map_album(row, {"id": artist.id, "name": artist.name})
                album.release_date = release_date(row.get("release_date"))
                albums.append(album.model_dump(mode="json"))
            more = bool(payload.get("next")) or len(rows) > PAGE_SIZE
            if not more:
                break
        return {"albums": albums, "limited": more}

    result = await cached(f"releases:v1:{artist.ref}", CACHE_TTL_S, produce)
    return [CatalogAlbum.model_validate(a) for a in result["albums"]], result["limited"]


async def feed(s: AsyncSession, user_id: int, *, period: Period = "week", unseen: bool = False) -> dict:
    now = utcnow()
    start, end = date_window(period, now, get_settings().timezone)
    targets, limited = await _targets(s, user_id, now)
    errors = {LIMIT_ERROR} if limited else set()
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(target):
        async with sem:
            try:
                async with asyncio.timeout(ARTIST_TIMEOUT_S):
                    artist = await _artist(target)
                    if artist is None:
                        return [], IDENTITY_ERROR
                    albums, capped = await _artist_releases(artist)
                    return albums, LIMIT_ERROR if capped else None
            except (ValueError, CatalogNotFound):
                return [], IDENTITY_ERROR
            except Exception:
                # Never expose upstream URLs, credentials, exception text or account metadata.
                return [], CATALOG_ERROR

    tasks = [asyncio.create_task(one(target)) for target in targets]
    results = []
    if tasks:
        try:
            done, pending = await asyncio.wait(tasks, timeout=FEED_TIMEOUT_S)
            if pending:
                errors.add(CATALOG_ERROR)
            results = [task.result() for task in tasks if task in done]
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    albums = {}
    for rows, error in results:
        if error:
            errors.add(error)
        for album in rows:
            rd = release_date(album.release_date)
            if rd is None or not start <= rd <= end:
                continue
            try:
                source, id_ = catalog_ref(album.ref)
            except ValueError:
                continue
            if source != album.source or id_ != album.id:
                continue
            # Catalog identity only: remasters, clean versions and singles stay distinct.
            albums.setdefault(album.ref, album)
    seen = set((await s.execute(select(ReleaseSeen.ref).where(ReleaseSeen.user_id == user_id,
                                                            ReleaseSeen.ref.in_(albums)))).scalars()) if albums else set()
    items = [{"ref": a.ref, "title": a.title, "artist": a.artist, "artist_ref": a.artist_ref,
              "cover_url": a.cover_small_url or a.cover_url, "release_date": a.release_date,
              "track_count": a.nb_tracks, "seen": a.ref in seen}
             for a in sorted(albums.values(), key=lambda a: (a.release_date, a.ref), reverse=True)
             if not unseen or a.ref not in seen]
    return {"period": period, "generated_at": now.replace(tzinfo=UTC), "partial": bool(errors),
            "errors": sorted(errors), "items": items}


async def mark_seen(s: AsyncSession, user_id: int, ref: str, seen: bool) -> dict:
    try:
        source, id_ = catalog_ref(ref)
    except ValueError:
        raise HTTPException(422, "Неверная ссылка на альбом") from None
    if not seen:
        await s.execute(delete(ReleaseSeen).where(ReleaseSeen.user_id == user_id, ReleaseSeen.ref == ref))
        return {"ref": ref, "seen": False}
    client = svc.deezer if source == "dz" else svc.hitmoz_catalog
    if client is None:
        raise HTTPException(503, "Каталог недоступен")
    try:
        async with asyncio.timeout(ARTIST_TIMEOUT_S):
            album = await client.get_album(id_)
    except CatalogNotFound:
        raise HTTPException(404, "Альбом не найден") from None
    except Exception:
        raise HTTPException(503, "Каталог недоступен") from None
    if not isinstance(album, CatalogAlbum) or album.ref != ref or album.source != source or album.id != id_:
        raise HTTPException(404, "Альбом не найден")
    stmt = insert(ReleaseSeen).values(user_id=user_id, ref=ref, seen_at=utcnow())
    await s.execute(stmt.on_conflict_do_nothing(index_elements=[ReleaseSeen.user_id, ReleaseSeen.ref]))
    return {"ref": ref, "seen": seen}
