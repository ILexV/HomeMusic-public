"""Account-owned saved rules; evaluation always uses the caller's live Navidrome library."""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_user
from app.db import get_session
from app.library.navidrome import SubsonicError
from app.models import User, utcnow
from app.smart_playlists import Definition, Evaluation, PlaylistInput, PlaylistOut, SmartPlaylist, evaluate

router = APIRouter(prefix="/api/smart-playlists", tags=["smart-playlists"])


def output(row: SmartPlaylist) -> PlaylistOut:
    return PlaylistOut(id=row.id, name=row.name, definition=Definition.model_validate(row.definition),
                       created_at=row.created_at, updated_at=row.updated_at)


async def owned(s: AsyncSession, user_id: int, playlist_id: str) -> SmartPlaylist:
    row = (await s.execute(select(SmartPlaylist).where(
        SmartPlaylist.id == playlist_id, SmartPlaylist.user_id == user_id,
    ))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Плейлист не найден")
    return row


async def evaluated(s: AsyncSession, user: User, definition: Definition) -> Evaluation:
    try:
        return await evaluate(s, user, definition)
    except TimeoutError as exc:
        raise HTTPException(504, "Медиатека не ответила вовремя; повторите запрос") from exc
    except (httpx.HTTPError, SubsonicError, ValueError) as exc:
        # Do not expose upstream request URLs (which contain Subsonic credentials).
        raise HTTPException(502, "Не удалось полностью прочитать медиатеку") from exc


@router.get("", response_model=list[PlaylistOut])
async def list_playlists(user: User = Depends(current_user),
                         s: AsyncSession = Depends(get_session, scope="function")):
    rows = (await s.execute(select(SmartPlaylist).where(SmartPlaylist.user_id == user.id)
                           .order_by(SmartPlaylist.name, SmartPlaylist.id))).scalars()
    return [output(row) for row in rows]


@router.post("", response_model=PlaylistOut, status_code=201)
async def create(body: PlaylistInput, user: User = Depends(current_user),
                 s: AsyncSession = Depends(get_session, scope="function")):
    row = SmartPlaylist(user_id=user.id, name=body.name, definition=body.definition.model_dump())
    s.add(row)
    await s.flush()
    return output(row)


@router.post("/preview", response_model=Evaluation)
async def preview(body: Definition, user: User = Depends(current_user),
                  s: AsyncSession = Depends(get_session, scope="function")):
    return await evaluated(s, user, body)


@router.get("/{playlist_id}", response_model=PlaylistOut)
async def get(playlist_id: str, user: User = Depends(current_user),
              s: AsyncSession = Depends(get_session, scope="function")):
    return output(await owned(s, user.id, playlist_id))


@router.put("/{playlist_id}", response_model=PlaylistOut)
async def update(playlist_id: str, body: PlaylistInput, user: User = Depends(current_user),
                 s: AsyncSession = Depends(get_session, scope="function")):
    row = await owned(s, user.id, playlist_id)
    row.name = body.name
    row.definition = body.definition.model_dump()
    row.updated_at = utcnow()
    await s.flush()
    return output(row)


@router.delete("/{playlist_id}")
async def delete(playlist_id: str, user: User = Depends(current_user),
                 s: AsyncSession = Depends(get_session, scope="function")):
    await s.delete(await owned(s, user.id, playlist_id))
    await s.flush()
    return {"ok": True}


@router.post("/{playlist_id}/evaluate", response_model=Evaluation)
async def saved_evaluation(playlist_id: str, user: User = Depends(current_user),
                           s: AsyncSession = Depends(get_session, scope="function")):
    row = await owned(s, user.id, playlist_id)
    return await evaluated(s, user, Definition.model_validate(row.definition))
