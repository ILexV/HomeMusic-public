"""Listening to catalog tracks before downloading them (see app/listen.py)."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from app import listen, recording_feedback
from app.api.schemas import ListenOut, ListenPrefetchIn, ListenPrefetchOut
from app.auth import current_user
from app.models import User

router = APIRouter(prefix="/api/listen", tags=["listen"])


async def _choose(ref: str, *, full: bool = False, user_id: int | None = None) -> listen.Choice:
    try:
        return await listen.choose(ref, full_only=full, user_id=user_id)
    except listen.ListenError as e:
        raise HTTPException(e.status, str(e)) from e


@router.get("", response_model=ListenOut)
async def resolve(ref: str, full: bool = False, user: User = Depends(current_user)):
    """Picks the file (may take a while: providers are searched), then the client plays `stream_url`."""
    c = await _choose(ref, full=full, user_id=user.id)
    binding = await recording_feedback.bind_choice(user.id, c)
    cand = c.candidate
    return ListenOut(
        ref=c.ref,
        kind=c.kind,
        artist=c.artist,
        title=c.title,
        duration_s=c.duration_s,
        provider=cand.provider if cand else None,
        bitrate_kbps=cand.bitrate_kbps if cand else None,
        page_url=cand.page_url if cand else None,
        nd_song_id=c.nd_song_id,
        recording_binding=binding,
        stream_url=f"/api/listen/stream?ref={quote(c.ref, safe='')}&binding={quote(binding, safe='')}",
    )


@router.post("/prefetch", response_model=ListenPrefetchOut)
async def prefetch(body: ListenPrefetchIn, user: User = Depends(current_user)):
    """The tracks on the user's screen, top first: the Hub picks their files in the background (replaces the
    previous list)."""
    return ListenPrefetchOut(queued=listen.prefetch(user.id, body.refs))


@router.get("/stream")
async def stream(ref: str, request: Request, q: str | None = None, full: bool = False,
                 binding: str | None = None, user: User = Depends(current_user)):
    """Audio of the chosen file (Range → 206): the library file, the server copy of the picked file (read while
    it downloads), the copy of a file of a source without plain HTTP audio, or a preview proxied from its source.
    `q=mobile`: the light variant for a metered network (Opus copy of a heavy file, `app.light`); the answer says
    which variant it is in `X-HomeMusic-Variant`."""
    rng = request.headers.get("range") or ""
    # the app loads the next tracks of its queue ahead (`X-HomeMusic-Prefetch`): that is no listen yet, its
    # `start` event is (app/api/plays.py), and its server copy waits for those of played tracks
    ahead = request.headers.get("x-homemusic-prefetch") == "1"
    try:
        m = await listen.open_media(ref, rng or None, ahead=ahead, quality=q, full_only=full, user_id=user.id)
    except listen.ListenError as e:
        raise HTTPException(e.status, str(e)) from e
    if not ahead and (m.path is not None or m.status in (200, 206)):
        ready = listen._full_results.get(ref)
        verified = (True if ready and listen._full_status(ref) == "ready"
                    and ready.choice is not None
                    and listen._file_ident(ready.choice) == listen._file_ident(m.choice) else None)
        await recording_feedback.record_stream(user.id, m.choice, binding_id=binding, full_verified=verified)
    if m.choice.kind == "full" and not ahead and (not rng or rng.startswith("bytes=0-")):
        from app.imports.core import listened

        await listened(user.id, ref)  # a track of an economy import is downloaded once listened to
    variant = {"X-HomeMusic-Listen-Kind": m.choice.kind}
    if m.variant:
        variant["X-HomeMusic-Variant"] = m.variant
    if m.path is not None:
        return FileResponse(m.path, media_type=m.media_type, headers={"Cache-Control": "no-store", **variant})
    headers = {**m.headers, "accept-ranges": "bytes", "cache-control": "no-store", **variant}
    if m.body is None:
        return Response(status_code=m.status, headers=headers)
    return StreamingResponse(m.body, status_code=m.status, headers=headers, media_type=m.media_type,
                             background=BackgroundTask(m.close) if m.close else None)
