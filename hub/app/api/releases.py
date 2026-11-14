"""Personal release feed and reversible account-scoped read marks."""

from datetime import date, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import releases
from app.auth import current_user
from app.db import get_session
from app.models import User

router = APIRouter(prefix="/api/releases", tags=["releases"])


class ReleaseOut(BaseModel):
    ref: str
    title: str
    artist: str
    artist_ref: str | None
    cover_url: str | None
    release_date: date
    track_count: int | None
    seen: bool


class ReleasesOut(BaseModel):
    period: releases.Period
    generated_at: datetime
    partial: bool
    errors: list[str]
    items: list[ReleaseOut]


class ReleaseSeenIn(BaseModel):
    ref: str = Field(min_length=1, max_length=64)
    seen: bool


@router.get("", response_model=ReleasesOut)
async def listing(period: releases.Period = "week", unseen: bool = False,
                  user: User = Depends(current_user),
                  s: AsyncSession = Depends(get_session, scope="function")):
    return await releases.feed(s, user.id, period=period, unseen=unseen)


@router.put("/seen", response_model=ReleaseSeenIn)
async def mark(body: ReleaseSeenIn, user: User = Depends(current_user),
               s: AsyncSession = Depends(get_session, scope="function")):
    return await releases.mark_seen(s, user.id, body.ref, body.seen)
