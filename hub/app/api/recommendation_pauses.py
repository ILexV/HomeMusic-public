"""Temporary, user-scoped recommendation restrictions; explicit playback is unaffected."""

from datetime import UTC
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_user
from app.db import get_session
from app.models import User, utcnow
from app.recommendation_models import RecommendationPause
from app.recommendation_policy import PolicyError, expires_at, resolve_target

router = APIRouter(prefix="/api/recommendations/pauses", tags=["recommendations"])


class PauseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["today", "week", "less_artist"]
    ref: str | None = Field(None, min_length=1, max_length=512)
    nd_song_id: str | None = Field(None, min_length=1, max_length=64)


def item(row: RecommendationPause) -> dict:
    return {"id": row.id, "action": row.action, "label": row.label,
            "expires_at": row.expires_at.replace(tzinfo=UTC).isoformat(), "ref": row.ref,
            "nd_song_id": row.nd_song_id, "artist": row.artist}


@router.get("")
async def list_pauses(user: User = Depends(current_user),
                      s: AsyncSession = Depends(get_session, scope="function")):
    rows = (await s.execute(select(RecommendationPause).where(
        RecommendationPause.user_id == user.id, RecommendationPause.expires_at > utcnow())
        .order_by(RecommendationPause.expires_at, RecommendationPause.id))).scalars()
    return {"items": [item(row) for row in rows]}


@router.post("", status_code=201)
async def create_pause(body: PauseIn, user: User = Depends(current_user),
                       s: AsyncSession = Depends(get_session, scope="function")):
    try:
        target = await resolve_target(s, user, body.ref, body.nd_song_id)
        expiry = expires_at(body.action)
    except PolicyError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    if body.action == "less_artist":
        target["label"] = target["artist"]
    active = (await s.execute(select(RecommendationPause).where(
        RecommendationPause.user_id == user.id, RecommendationPause.action == body.action,
        RecommendationPause.expires_at > utcnow()))).scalars()
    for existing in active:
        same = (existing.artist_key == target["artist_key"] if body.action == "less_artist" else
                bool(set(existing.aliases or []) & set(target["aliases"])))
        if same:
            return item(existing)
    row = RecommendationPause(user_id=user.id, action=body.action, expires_at=expiry, **target)
    s.add(row)
    await s.flush()
    return item(row)


@router.delete("/{pause_id}", status_code=204)
async def delete_pause(pause_id: int, user: User = Depends(current_user),
                       s: AsyncSession = Depends(get_session, scope="function")):
    row = await s.get(RecommendationPause, pause_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(404, "Ограничение не найдено")
    await s.delete(row)
    return Response(status_code=204)
