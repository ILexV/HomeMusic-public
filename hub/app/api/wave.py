"""«Моя волна» (MIXES.md 7): see app/mixes/wave.py."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import PlayEventsOut, WaveNextIn, WaveOut, WaveStartIn, WaveTrackOut
from app.auth import current_user
from app.db import get_session
from app.library.owned import annotate_tracks
from app.mixes import wave
from app.models import User

router = APIRouter(prefix="/api/wave", tags=["wave"])


async def track_outs(s: AsyncSession, cands: list, user_id: int) -> list[WaveTrackOut]:
    """The candidates as the API's tracks: the catalog record with the library state, the generator, the sound."""
    annotated = await annotate_tracks(s, [c.track for c in cands if c.track.source not in ("nd", "file")], user_id)
    by_ref = {t.ref: t for t in annotated}
    tracks: list[WaveTrackOut] = []
    for c in cands:
        base = by_ref.get(c.track.ref)
        d = base.model_dump() if base is not None else c.track.model_dump()
        if c.nd_song_id:
            d.update(owned=True, nd_song_id=c.nd_song_id, library_track_id=c.library_track_id or d.get("library_track_id"))
        g = wave.top_generator(c)
        tracks.append(WaveTrackOut(**d, generator=g, generator_title=wave.generator_title(g), familiar=c.familiar,
                                   sim=c.sim, explanation=c.explanation))
    return tracks


async def wave_out(s: AsyncSession, b: wave.Batch, user_id: int) -> WaveOut:
    tracks = await track_outs(s, b.tracks, user_id)
    recorded = None
    if b.recorded is not None:
        r = b.recorded
        recorded = PlayEventsOut(accepted=r.accepted, duplicates=r.duplicates, rejected=r.rejected)
    return WaveOut(session_id=b.session_id, title=b.title, mode=b.mode,  # type: ignore[arg-type]
                   seed=b.seed, tracks=tracks, pending=b.pending, recorded=recorded,
                   preferences=b.preferences, preferences_status=b.preferences_status, preparation=b.preparation)


@router.post("/start", response_model=WaveOut)
async def start(body: WaveStartIn | None = None, user: User = Depends(current_user),
                s: AsyncSession = Depends(get_session, scope="function")):
    """A new wave: the user's own, or from a track / an artist. The first 5 tracks (the seed first)."""
    body = body or WaveStartIn()
    try:
        b = await wave.start(user, body.seed.model_dump(exclude_none=True) if body.seed else None, body.mode,
                             origin=body.origin, client=body.client, restart_of=body.restart_of,
                             request=body.model_dump(mode="json", exclude_none=True),
                             preferences=body.preferences.model_dump() if body.preferences is not None else None)
    except wave.WaveError as e:
        raise HTTPException(e.status, str(e)) from e
    return await wave_out(s, b, user.id)


@router.post("/{session_id}/next", response_model=WaveOut)
async def next_(session_id: str, body: WaveNextIn, user: User = Depends(current_user),
                s: AsyncSession = Depends(get_session, scope="function")):
    """The events since the last call; the next 5 tracks, picked again with them (they replace `queued`)."""
    try:
        b = await wave.next_batch(user, session_id, body.played, body.queued,
                                 preferences=body.preferences.model_dump() if body.preferences is not None else None)
    except wave.WaveError as e:
        raise HTTPException(e.status, str(e)) from e
    return await wave_out(s, b, user.id)
