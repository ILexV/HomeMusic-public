"""Read-only artist identities for browsing a track's discography in either catalog."""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.medialib import nd_call
from app.auth import current_user
from app.catalog.cache import cache_get
from app.catalog.deezer import CatalogNotFound
from app.db import get_session
from app.models import User
from app.plays import Ids
from app.services import svc

router = APIRouter(prefix="/api/discography", tags=["catalog"])


class Credit(BaseModel):
    name: str
    nd_artist_id: str | None = None
    ref: str | None = None


class Credits(BaseModel):
    artists: list[Credit]
    warning: str | None = None


def _name(value: str) -> str:
    return " ".join(value.casefold().split())


async def _library_credits(user: User, song_id: str) -> list[Credit]:
    async def load(nd):
        song = await nd.get_song(song_id)
        if song.get("id") != song_id:
            raise HTTPException(404, "Трек не найден в библиотеке")
        out = []
        seen = set()
        for artist in song.get("artists") or []:
            name = (artist.get("name") or "").strip()
            ident = artist.get("id")
            if name and (ident or name) not in seen:
                out.append(Credit(name=name, nd_artist_id=ident))
                seen.add(ident or name)
        # The display credit can include several performers. Ask for the canonical
        # name behind the primary ID rather than assigning it to the whole credit.
        primary = song.get("artistId")
        if primary and primary not in seen:
            row = await nd.get_artist(primary)
            if row.get("id") == primary and row.get("name"):
                same = [i for i, c in enumerate(out) if not c.nd_artist_id and _name(c.name) == _name(row["name"])]
                if len(same) == 1:
                    out.pop(same[0])
                out.insert(0, Credit(name=row["name"], nd_artist_id=primary))
        elif primary:
            out.sort(key=lambda c: c.nd_artist_id != primary)
        return out

    return await nd_call(user, load)


async def _catalog_credits(ref: str) -> list[Credit]:
    if ref.startswith("dz:"):
        ident = ref[3:]
        if not ident.isascii() or not ident.isdigit():
            raise HTTPException(422, "Неверный идентификатор Deezer")
        if not svc.deezer:
            raise HTTPException(503, "Каталог недоступен")
        try:
            return [Credit(**c) for c in await svc.deezer.get_track_credits(ident)]
        except CatalogNotFound as e:
            raise HTTPException(404, "Трек не найден в каталоге") from e
        except Exception as e:
            raise HTTPException(502, "Не удалось получить участников из каталога") from e
    # Sources without a track credits API may still have a known artist in the
    # Hub's metadata. Never split their display string into invented people.
    track = await cache_get(f"mix:track:{ref}") or await cache_get(f"listen:{ref}") or {}
    if track.get("artist"):
        return [Credit(name=track["artist"], ref=track.get("artist_ref"))]
    return []


def _merge(local: list[Credit], catalog: list[Credit]) -> list[Credit]:
    """Only combine unique names on this same recording, never merge homonyms."""
    names = Counter(_name(c.name) for c in catalog)
    counts = Counter(_name(c.name) for c in local)
    unique = {_name(c.name): i for i, c in enumerate(local) if counts[_name(c.name)] == 1}
    out = list(local)
    for c in catalog:
        key = _name(c.name)
        if key in unique and names[key] == 1:
            i = unique[key]
            out[i] = out[i].model_copy(update={"ref": c.ref})
        else:
            out.append(c)
    return out


@router.get("/credits")
async def credits(nd_song_id: str | None = Query(None, min_length=1, max_length=256),
                  ref: str | None = Query(None, min_length=1, max_length=256),
                  user: User = Depends(current_user), s: AsyncSession = Depends(get_session, scope="function")) -> Credits:
    if ref and ref.startswith("nd:"):
        if nd_song_id and nd_song_id != ref[3:]:
            raise HTTPException(422, "Разные идентификаторы трека библиотеки")
        nd_song_id, ref = ref[3:], None
    if not nd_song_id and not ref:
        raise HTTPException(422, "Не указан трек")
    ref, nd_song_id = await Ids(s).complete(ref, nd_song_id)
    # Ids.complete can open a read transaction. Do not hold its connection
    # while the upstream catalog uses its own connection to populate its cache.
    await s.commit()
    jobs = []
    if nd_song_id:
        jobs.append(_library_credits(user, nd_song_id))
    if ref:
        # Optional enrichment must not prevent browsing the local library when
        # the online catalog is throttled. Surface the partial result explicitly.
        remote = _catalog_credits(ref)
        jobs.append(asyncio.wait_for(remote, timeout=10) if nd_song_id else remote)
    results = await asyncio.gather(*jobs, return_exceptions=True)
    errors = []
    lists = []
    for result in results:
        if isinstance(result, HTTPException):
            # Never mask authorization errors with cached names or catalog results.
            if result.status_code in (401, 403, 422):
                raise result
            errors.append(result)
            lists.append([])
        elif isinstance(result, TimeoutError):
            errors.append(HTTPException(504, "Не удалось вовремя получить участников из каталога"))
            lists.append([])
        elif isinstance(result, BaseException):
            raise result
        else:
            lists.append(result)
    artists = _merge(*lists) if len(lists) == 2 else lists[0]
    if errors and not artists:
        raise errors[0]
    return Credits(artists=artists, warning="; ".join(e.detail for e in errors) or None)


def _exact(name: str, candidates: list[Credit]) -> Credit | None:
    matches = {(c.nd_artist_id, c.ref): c for c in candidates if _name(c.name) == _name(name)}
    if len(matches) > 1:
        raise HTTPException(409, "Несколько одноимённых исполнителей: точная связь с треком неизвестна")
    return next(iter(matches.values()), None)


@router.get("/artist")
async def artist(scope: Literal["library", "catalog"], name: str = Query(min_length=1, max_length=512),
                 user: User = Depends(current_user)) -> Credit | None:
    """Cross to the other catalog only on a unique full name (never take the first search hit)."""
    if scope == "library":
        artists = await nd_call(user, lambda nd: nd.get_artists())
        return _exact(name, [Credit(name=a["name"], nd_artist_id=a["id"]) for a in artists if a.get("name") and a.get("id")])
    if not svc.deezer and not svc.hitmoz_catalog:
        raise HTTPException(503, "Каталог недоступен")
    error = None
    for source in (svc.deezer, svc.hitmoz_catalog):
        if not source:
            continue
        try:
            rows = ((await source.search(name, kinds=("artist",), limit=100)).artists if source is svc.deezer
                    else await source.search_artists(name))
        except Exception as e:
            error = e
            continue
        match = _exact(name, [Credit(name=a.name, ref=a.ref) for a in rows])
        if match:
            return match
    if error:
        raise HTTPException(502, "Не удалось проверить исполнителя в каталоге") from error
    return None
