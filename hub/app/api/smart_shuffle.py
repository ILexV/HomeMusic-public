"""«Умное перемешивание» (MIXES.md 8): see app/mixes/smart.py."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import DownloadIn, PlayEventsOut, SmartAddOut, SmartNextIn, SmartOut, SmartRecOut, SmartStartIn, SmartTrackIn
from app.api.wave import track_outs
from app.auth import current_user, user_navidrome
from app.db import get_session
from app.mixes import smart
from app.models import User

router = APIRouter(prefix="/api/smart-shuffle", tags=["smart-shuffle"])


async def plan_out(s: AsyncSession, plan: smart.Plan, user_id: int) -> SmartOut:
    tracks = await track_outs(s, [c for _, _, c in plan.recs], user_id)
    recs = [SmartRecOut(**{**t.model_dump(), "explanation": list(c.explanation)}, after=after, slot=slot)
            for t, (slot, after, c) in zip(tracks, plan.recs, strict=True)]
    recorded = None
    if plan.recorded is not None:
        r = plan.recorded
        recorded = PlayEventsOut(accepted=r.accepted, duplicates=r.duplicates, rejected=r.rejected)
    return SmartOut(session_id=plan.session_id, context=plan.context, title=plan.title, own=plan.own, recs=recs,
                    pending=plan.pending, own_per_rec=plan.per_rec, recorded=recorded)


@router.post("/start", response_model=SmartOut)
async def start(body: SmartStartIn, user: User = Depends(current_user),
                s: AsyncSession = Depends(get_session, scope="function")):
    """A new run: the order of the context's own songs and the first recommendations between them."""
    try:
        plan = await smart.start(user, body.context, body.songs, body.current, title=body.title, origin=body.origin,
                                 client=body.client)
    except smart.SmartError as e:
        raise HTTPException(e.status, str(e)) from e
    return await plan_out(s, plan, user.id)


@router.post("/{session_id}/next", response_model=SmartOut)
async def next_(session_id: str, body: SmartNextIn, user: User = Depends(current_user),
                s: AsyncSession = Depends(get_session, scope="function")):
    """The events since the last call and where the player is; the recommendations for the places ahead that are
    not filled yet (`recs`, each with the own song it goes after)."""
    try:
        plan = await smart.next_batch(user, session_id, body.played, body.at)
    except smart.SmartError as e:
        raise HTTPException(e.status, str(e)) from e
    return await plan_out(s, plan, user.id)


@router.post("/{session_id}/hide")
async def hide(session_id: str, body: SmartTrackIn, user: User = Depends(current_user)):
    """«−»: do not recommend this track in this playlist («Понравившиеся») any more."""
    try:
        await smart.hide(user, session_id, body.ref, body.nd_song_id)
    except smart.SmartError as e:
        raise HTTPException(e.status, str(e)) from e
    return {"ok": True}


@router.post("/{session_id}/add", response_model=SmartAddOut)
async def add(session_id: str, body: SmartTrackIn, user: User = Depends(current_user),
              s: AsyncSession = Depends(get_session, scope="function")):
    """«+»: put the recommendation in the playlist (not in the library yet: download it, then put it in). In
    «Понравившиеся» the client likes the track itself (its own ♥ path); the Hub only notes it."""
    from app.api.downloads import create_batch
    from app.jobs.queue import notify

    try:
        item, playlist_id = await smart.accept(user, session_id, body.ref, body.nd_song_id)
    except smart.SmartError as e:
        raise HTTPException(e.status, str(e)) from e
    if playlist_id is None:
        return SmartAddOut(status="liked")
    nd_id = body.nd_song_id or item.nd_song_id
    if nd_id:
        nd = user_navidrome(user)
        try:
            await nd.update_playlist(playlist_id, add=[nd_id])
        finally:
            await nd.aclose()
        return SmartAddOut(status="added", playlist_id=playlist_id)
    await create_batch(s, user, DownloadIn(track_refs=[item.ref], playlist_id=playlist_id), origin="smart-shuffle")
    await s.commit()
    notify()
    return SmartAddOut(status="downloading", playlist_id=playlist_id)
