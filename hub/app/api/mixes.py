"""«Микс дня» and the taste onboarding (MIXES.md 6, 8): see app/mixes/."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    MixOut,
    MixRebuildIn,
    MixSettingsIn,
    MixSettingsOut,
    MixTodayOut,
    MixTrackOut,
    TasteArtistsIn,
    TasteOnboardingOut,
)
from app.auth import current_user
from app.catalog.models import CatalogArtist, CatalogTrack, split_ref
from app.config import get_settings
from app.db import get_session
from app.jobs.queue import notify
from app.library.owned import annotate_tracks
from app.mixes import core
from app.mixes.build import GENERATOR_TITLES, MODE_TITLES
from app.models import Job, Mix, MixItem, Track, User
from app.recommendations import stored_reasons
from app.services import svc

router = APIRouter(tags=["mixes"])


async def mix_out(s: AsyncSession, mix: Mix, user_id: int) -> MixOut:
    items = list((await s.execute(select(MixItem).where(MixItem.mix_id == mix.id, MixItem.state == "active")
                                  .order_by(MixItem.position))).scalars())
    tracks = [CatalogTrack.model_validate((i.features_json or {}).get("track")) for i in items]
    annotated = await annotate_tracks(s, [t for t in tracks if t.source != "nd"], user_id)
    by_ref = {t.ref: t for t in annotated}
    out: list[MixTrackOut] = []
    for i, t in zip(items, tracks, strict=True):
        base = by_ref.get(t.ref)
        d = base.model_dump() if base is not None else {**t.model_dump(), "owned": bool(i.nd_song_id),
                                                        "library_track_id": i.track_id}
        if i.nd_song_id:
            d.update(owned=True, nd_song_id=i.nd_song_id, library_track_id=i.track_id or d.get("library_track_id"))
        f = i.features_json or {}
        out.append(MixTrackOut(**d, position=i.position, generator=i.generator,
                               generator_title=GENERATOR_TITLES.get(i.generator, i.generator),
                               generators=list((f.get("generators") or {}).keys()), familiar=i.familiar,
                               score=i.score, sim=f.get("sim"), explanation=stored_reasons(f)))
    return MixOut(id=mix.id, title=mix.title, description=mix.description, mode=mix.mode,  # type: ignore[arg-type]
                  mode_title=MODE_TITLES.get(mix.mode, mix.mode), created_at=mix.created_at,
                  nd_playlist_id=mix.nd_playlist_id, ready=sum(1 for t in out if t.nd_song_id), total=len(out),
                  tracks=out, generators=(mix.stats_json or {}).get("generators") or {},
                  playlist_status=(mix.stats_json or {}).get("playlist_status",
                                                              "published" if mix.nd_playlist_id else "pending"),
                  playlist_error=(mix.stats_json or {}).get("playlist_error"))


async def settings_out(s: AsyncSession, user_id: int) -> MixSettingsOut:
    st = await core.settings_of(s, user_id)
    return MixSettingsOut(mode=st.mode, mix_hour=st.mix_hour, timezone=get_settings().timezone,  # type: ignore[arg-type]
                          next_build_at=core.next_build_at(st.mix_hour), nd_playlist_id=st.nd_playlist_id,
                          artists=list(st.artists_json or []))


async def _building(s: AsyncSession, user_id: int) -> bool:
    return (await s.execute(select(Job.id).where(
        Job.kind == "mix_build", Job.status.in_(("queued", "running")),
        func.json_extract(Job.payload_json, "$.user_id") == user_id).limit(1))).scalar_one_or_none() is not None


@router.get("/api/mixes/today", response_model=MixTodayOut)
async def today(user: User = Depends(current_user), s: AsyncSession = Depends(get_session, scope="function")):
    """The user's current mix (lives until the next one), or None with the settings and whether to onboard."""
    mix = await core.current_mix(s, user.id)
    st = await settings_out(s, user.id)
    onboarding = mix is None and not st.artists and not await core.has_history(s, user.id)
    return MixTodayOut(mix=await mix_out(s, mix, user.id) if mix else None, building=await _building(s, user.id),
                       settings=st, onboarding=onboarding)


@router.get("/api/mixes/settings", response_model=MixSettingsOut)
async def get_settings_(user: User = Depends(current_user), s: AsyncSession = Depends(get_session, scope="function")):
    return await settings_out(s, user.id)


@router.put("/api/mixes/settings", response_model=MixSettingsOut)
async def put_settings(body: MixSettingsIn, user: User = Depends(current_user),
                       s: AsyncSession = Depends(get_session, scope="function")):
    """The mode applies from the next mix (`POST /api/mixes/rebuild` — now); a new hour — from the next day."""
    st = await core.settings_of(s, user.id)
    if body.mode is not None:
        st.mode = body.mode
    if body.mix_hour is not None:
        st.mix_hour = body.mix_hour
    await s.flush()
    return await settings_out(s, user.id)


@router.post("/api/mixes/rebuild")
async def rebuild(body: MixRebuildIn | None = None, user: User = Depends(current_user),
                  s: AsyncSession = Depends(get_session, scope="function")):
    """Build the mix again now (admin, debugging): it replaces the current one when ready."""
    if not user.is_admin:
        raise HTTPException(403, "Только для администратора")
    user_id = (body.user_id if body else None) or user.id
    if await s.get(User, user_id) is None:
        raise HTTPException(404, "Пользователь не найден")
    queued = await core.enqueue_build(s, user_id, priority=0)
    await s.commit()
    notify()
    return {"queued": queued}


@router.get("/api/mixes/{mix_id}", response_model=MixOut)
async def get_mix(mix_id: int, user: User = Depends(current_user),
                  s: AsyncSession = Depends(get_session, scope="function")):
    mix = await s.get(Mix, mix_id)
    if mix is None or (mix.user_id != user.id and not user.is_admin):
        raise HTTPException(404, "Микс не найден")
    return await mix_out(s, mix, mix.user_id)


async def library_artists(s: AsyncSession, limit: int = 20) -> list[CatalogArtist]:
    """The artists the library has most songs of (everyone's music), as catalog artists."""
    rows = (await s.execute(select(Track.artist, func.max(Track.deezer_track_id)).where(
        Track.deezer_track_id.is_not(None)).group_by(Track.artist).order_by(func.count(Track.id).desc())
        .limit(limit))).all()
    assert svc.deezer is not None

    async def one(track_id: str) -> CatalogArtist | None:
        try:
            t = await svc.deezer.get_track(track_id)
            return await svc.deezer.get_artist(split_ref(t.artist_ref)[1]) if t.artist_ref else None
        except Exception:
            return None

    found = await asyncio.gather(*(one(tid) for _, tid in rows))
    return [a for a in found if a is not None]


@router.get("/api/taste/onboarding", response_model=TasteOnboardingOut)
async def onboarding(q: str | None = Query(None, max_length=100), user: User = Depends(current_user),
                     s: AsyncSession = Depends(get_session, scope="function")):
    """«Что вы слушаете?»: artists to pick from — the library's most present ones and the chart, or found by `q`."""
    st = await core.settings_of(s, user.id)
    suggestions = []
    if svc.deezer is not None:
        try:
            if q and q.strip():
                suggestions = (await svc.deezer.search(q.strip(), kinds=("artist",), limit=20)).artists
            else:
                found = await library_artists(s) + await svc.deezer.chart_artists(30)
                suggestions = list({a.ref: a for a in found}.values())  # an artist once, at its first place
        except Exception as e:
            raise HTTPException(502, f"Каталог не ответил: {e}") from e
    needed = not st.artists_json and not await core.has_history(s, user.id)
    return TasteOnboardingOut(needed=needed, selected=list(st.artists_json or []), suggestions=suggestions)


@router.post("/api/taste/artists", response_model=MixSettingsOut)
async def pick_artists(body: TasteArtistsIn, user: User = Depends(current_user),
                       s: AsyncSession = Depends(get_session, scope="function")):
    """The artists a new user listens to (Deezer refs): the mix starts from them; the first one is built now."""
    if svc.deezer is None:
        raise HTTPException(503, "Каталог недоступен")
    picked = []
    for ref in dict.fromkeys(body.artist_refs):
        try:
            src, id_ = split_ref(ref)
        except ValueError as e:
            raise HTTPException(400, f"Неверный исполнитель: {ref}") from e
        if src != "dz":
            raise HTTPException(400, f"Неверный исполнитель: {ref}")
        try:
            a = await svc.deezer.get_artist(id_)
        except Exception as e:
            raise HTTPException(404, f"Исполнитель не найден: {ref}") from e
        picked.append({"ref": a.ref, "name": a.name})
    st = await core.settings_of(s, user.id)
    first = not st.artists_json and await core.current_mix(s, user.id) is None
    st.artists_json = picked
    if first:
        await core.enqueue_build(s, user.id, priority=0)
    await s.commit()
    notify()
    return await settings_out(s, user.id)
