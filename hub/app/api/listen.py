"""Listening to catalog tracks before downloading them (see app/listen.py)."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.types import Receive, Scope, Send

from app import listen, recording_feedback
from app.api.schemas import ListenOut, ListenPrefetchIn, ListenPrefetchOut
from app.auth import current_user
from app.models import User, utcnow

router = APIRouter(prefix="/api/listen", tags=["listen"])


class _ListenFileResponse(FileResponse):
    """Observe Starlette's actual descriptor after Range validation, not a stat of a path it may never open."""

    def __init__(self, media, *, on_open, whole_prefetch, headers):
        super().__init__(media.path, media_type=media.media_type, headers=headers)
        self.on_open = on_open
        self.whole_prefetch = whole_prefetch

    @asynccontextmanager
    async def _open_file(self):
        async with super()._open_file() as f:
            st = os.fstat(f.fileno())
            delivery = await self.on_open(st)
            yield f
            # The ASGI server can cancel this task as soon as the final body leaves. No suspension between a
            # successful full send and recording it; tell() only inspects this already open local descriptor.
            if (self.whole_prefetch and delivery and delivery.recording and not delivery.precached_at
                    and f.wrapped.tell() == st.st_size):
                delivery.precached_at = utcnow()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # A pathsend bypasses _open_file, so it couldn't establish the file's identity.
        if "http.response.pathsend" in scope.get("extensions", {}):
            extensions = dict(scope["extensions"])
            del extensions["http.response.pathsend"]
            scope = {**scope, "extensions": extensions}
        await super().__call__(scope, receive, send)


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
        stream_url=(f"/api/listen/stream?ref={quote(c.ref, safe='')}&binding={quote(binding, safe='')}"
                    + ("&full=1" if full else "")),
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
    epoch = listen._rejection_epoch
    try:
        m = await listen.open_media(ref, rng or None, ahead=ahead, quality=q, full_only=full, user_id=user.id)
    except listen.ListenError as e:
        raise HTTPException(e.status, str(e)) from e

    async def opened(st: os.stat_result | None = None):
        recording = listen._verified_media(m, st)
        # Persist the existing reporting binding before marking this response as successfully opened.
        if not ahead:
            await recording_feedback.record_stream(user.id, m.choice, binding_id=binding,
                                                   full_verified=True if recording else None)
        return listen._observe_wave_stream(user.id, m, full=full, ahead=ahead, epoch=epoch, recording=recording)

    if m.choice.kind == "full" and not ahead and (not rng or rng.startswith("bytes=0-")):
        from app.imports.core import listened

        await listened(user.id, ref)  # a track of an economy import is downloaded once listened to
    variant = {"X-HomeMusic-Listen-Kind": m.choice.kind}
    if m.variant:
        variant["X-HomeMusic-Variant"] = m.variant
    if m.path is not None:
        return _ListenFileResponse(m, on_open=opened, whole_prefetch=ahead and rng in ("", "bytes=0-"),
                                   headers={"Cache-Control": "no-store", **variant})
    if m.body is not None and m.status in (200, 206):
        await opened()
    headers = {**m.headers, "accept-ranges": "bytes", "cache-control": "no-store", **variant}
    if m.body is None:
        return Response(status_code=m.status, headers=headers)
    return StreamingResponse(m.body, status_code=m.status, headers=headers, media_type=m.media_type,
                             background=BackgroundTask(m.close) if m.close else None)
