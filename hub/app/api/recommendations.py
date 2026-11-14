"""Private quality and recorded-decision access (never administrator-only)."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import recommendations
from app.auth import current_user
from app.db import get_session
from app.models import Mix, MixItem, SmartItem, SmartSession, User, WaveItem, WaveSession

router = APIRouter(prefix="/api/recommendations", tags=["recommendations"])


@router.get("/quality")
async def quality(days: int = Query(30), user: User = Depends(current_user),
                  s: AsyncSession = Depends(get_session, scope="function")):
    if days not in (7, 30, 90):
        raise HTTPException(422, "Период должен быть 7, 30 или 90 дней")
    return await recommendations.quality(s, user.id, days)


@router.get("/explain")
async def explain(context: str = Query(..., max_length=128), ref: str | None = Query(None, max_length=64),
                  nd_song_id: str | None = Query(None, max_length=64), user: User = Depends(current_user),
                  s: AsyncSession = Depends(get_session, scope="function")):
    if ref and ref.startswith("nd:"):
        song_id = ref[3:]
        if not song_id or (nd_song_id and nd_song_id != song_id):
            raise HTTPException(404, "Решение не найдено")
        nd_song_id, ref = song_id, None
    kind, _, ident = context.partition(":")
    if kind == "mix" and ident.isdigit():
        owner = await s.get(Mix, int(ident))
        model, condition = MixItem, MixItem.mix_id == int(ident)
    elif kind in ("wave", "smart") and ident:
        session, model = (WaveSession, WaveItem) if kind == "wave" else (SmartSession, SmartItem)
        owner = await s.get(session, ident)
        condition = model.session_id == ident
    else:
        raise HTTPException(404, "Решение не найдено")
    if owner is None or owner.user_id != user.id or not (ref or nd_song_id):
        raise HTTPException(404, "Решение не найдено")
    query = select(model).where(condition)
    if model is not MixItem:
        query = query.where(model.user_id == user.id)
    # When both identities are supplied they must identify the same recorded row.
    if ref:
        query = query.where(model.ref == ref)
    if nd_song_id:
        query = query.where(model.nd_song_id == nd_song_id)
    item = (await s.execute(query.order_by(model.id.desc()).limit(1))).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "Решение не найдено")
    data = item.features_json if model is MixItem else item.decision_json
    return {"reasons": recommendations.stored_reasons(data)}
