"""Listening before downloading (GET /api/listen): the file a download of a catalog track would pick, streamed
through the Hub with Range support.

The file is chosen the way the download pipeline does it (catalog meta → Cyrillic original → provider order →
search + rank). Picking it is what takes seconds, so it is kept off the tap:
- a pick is remembered for weeks (`catalog_cache`, key `listen:{ref}`): only the CDN link behind it expires, and
  that is renewed on the fly;
- the PWA names the tracks on the user's screen (`prefetch`) and the Hub picks them in the background, checking
  that the file opens. Background work yields to taps and downloads (`app.net.Priority`): a hitmoz request or a
  yt-dlp slot goes to a user request first;
- a tap that finds nothing ready searches all sources at once and takes the first one in source order that has
  the track. The stream itself opens the file; when it does not open, the next candidate takes over.
Ranking skips the track page (hitmoz: Explicit/Clean, real bitrate). Once the user has listened, the Hub checks
the chosen file in the background; a download of that ref tries the heard file first if the check passed.
Owned tracks play from the library; when no source has the track, Deezer's 30 s preview plays (`kind="preview"`).

A provider's file found in the Files tab of the search is listened to the same way, by its ref "file:<provider>:<id>"
(`remember_files`): no pick, the file itself, prefetched while on screen. A source that has no plain HTTP audio
(`streams = False`: SoundCloud, HLS) is listened to from a copy the Hub downloads first, kept for a few hours.

The stream of a picked file is served from its server copy (`_Copy`): the Hub downloads the whole file to its disk at
its own speed and the player reads the copy as it grows, so a CDN that drops the connection midway (googlevideo does)
is resumed by the Hub instead of reaching the player, and seeks and the next requests are served from disk. A copy for
the track being played goes first: copies loaded ahead (the app's precache, `X-HomeMusic-Prefetch`) wait while one
for a played track downloads.

For a metered network the stream can be asked for the light variant (`quality="mobile"`, `app.light`): the Opus copy
of the file when it is heavy (the copy of a catalog track is made from its server copy). Copies are made ahead of
time (`warm_light`: a mix's songs, a wave's tracks); a stream that finds none ready waits a moment, then serves the
original and says so (`Media.variant`) while the copy is finished in the background.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import shutil
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import httpx
from sqlalchemy import select

from app import light
from app.catalog.cache import cache_delete, cache_get, cache_set, cache_set_many
from app.catalog.deezer import CatalogNotFound
from app.catalog.models import split_ref
from app.catalog.yandex import russian_original
from app.config import get_settings
from app.db import session_scope
from app.jobs.downloads import (
    MAX_TRIES_PER_PROVIDER,
    complete_meta,
    meta_from_catalog,
    query_from_track,
    search_provider,
)
from app.library.owned import find_existing
from app.library.probe import ProbeError, probe
from app.models import Track
from app.matching.select import rank_candidates
from app.net import Priority, in_background, make_client, priority_context
from app.providers.base import FILE_REF, Candidate, StreamSource, TrackQuery, file_ref
from app.services import svc

log = logging.getLogger(__name__)

MEM_TTL_S = 3 * 3600  # a pick in memory (with the upstream link it opened), since its last use
MEM_MAX = 2000
KEEP_S = 30 * 86400  # a remembered pick
KEEP_PREVIEW_S = 86400  # "no source has it" is looked at again the next day
MISS_TTL_S = 30 * 60  # the prefetch skips a track that was not found for this long
# A source failed (network, site down): a preview picked meanwhile is not remembered and is kept in memory this long
TRANSIENT_TTL_S = 60
PREVIEW_S = 30
# Fewer search results than a download's 15 (when its API fails, YouTube Music looks up each one) and no track pages
SEARCH_LIMIT = 4
PROBE_RANGE = "bytes=0-0"
HEARD_CHECK_WAIT_S = 10  # a download waits this long for the check of the file just heard
PREFETCH_WORKERS = 3
PREFETCH_MAX = 40  # tracks per user
FULL_NEGATIVE_TTL_S = 60  # recommendation-only rejection; never a dislike or a permanent source verdict
FULL_RETRY_TTL_S = 2  # a transient failure can be retried by the next wave request
FULL_MAX_JOBS = PREFETCH_MAX * PREFETCH_WORKERS
FULL_PROBE_TIMEOUT_S = 15
_PLAYABLE_TYPES = ("audio/", "video/")  # googlevideo may label audio-only formats video/*
LOCAL_DIR = "listen"  # under staging: server copies, and copies of files of sources without plain HTTP audio
LOCAL_KEEP_S = MEM_TTL_S  # a copy, since its last use
LOCAL_MAX_BYTES = 500 * 2**20
LOCAL_MEDIA_TYPES = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".mp4": "audio/mp4", ".opus": "audio/ogg",
                     ".ogg": "audio/ogg", ".webm": "audio/webm", ".flac": "audio/flac"}
COPY_NAME = "stream"  # a server copy, in the folder of its ref under LOCAL_DIR
COPY_CHUNK = 64 * 1024  # what a stream reads from a server copy at once
COPY_RESUMES = 4  # a dropped upstream connection is resumed this many times in a row (with no progress between)
# A request this far past what a server copy has waits for it; a seek farther ahead goes to the upstream itself
SEEK_WAIT_BYTES = 2 * 2**20
CALM_RECHECK_S = 1.0  # a copy loaded ahead that waits looks again this often whether it still has to

Kind = Literal["full", "preview", "library"]


class ListenError(Exception):
    """Nothing to play; `status` is the HTTP status for the API."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class _FullUnavailable(ListenError):
    """No full source was found, rather than a temporary upstream failure."""


@dataclass
class Choice:
    ref: str
    kind: Kind
    artist: str
    title: str
    duration_s: int | None = None
    candidate: Candidate | None = None  # kind "full"
    alternates: list[Candidate] = field(default_factory=list)  # "full": next files when `candidate` doesn't open
    query: dict | None = None  # catalog query + deezer_track_id (the library check of a remembered pick)
    pquery: dict | None = None  # the query the providers were searched with (Cyrillic original)
    path: Path | None = None  # kind "library"
    nd_song_id: str | None = None  # kind "library", once Navidrome has scanned the file
    source: StreamSource | None = None  # upstream that opened last (URL after redirects); renewed when it fails
    local: Path | None = None  # "full" from a source without plain HTTP audio: the copy the Hub downloaded
    heard: bool = False  # the user has listened to it
    verified: bool = False  # "full": the candidate passed the track-page check (or needs none)
    checking: asyncio.Task | None = None  # the check after the first listen
    transient: bool = False  # picked while a source failed: not remembered, soon looked at again
    at: float = field(default_factory=time.monotonic)

    def fresh(self) -> bool:
        return time.monotonic() - self.at < (TRANSIENT_TTL_S if self.transient else MEM_TTL_S)


@dataclass
class _Job:
    task: asyncio.Task[Choice]
    prio: Priority


@dataclass(frozen=True)
class _Representation:
    candidate: tuple[str, str] | None
    url: str
    size: int | None
    etag: str | None


@dataclass(eq=False)
class _Copy:
    """The server copy of a picked file: downloaded to the Hub's disk, read by streams while it grows."""

    cand: Candidate  # the file it is a copy of
    path: Path
    prio: Priority  # background: loaded ahead; it yields to copies of played tracks
    media_type: str = "audio/mpeg"
    size: int | None = None  # from the first upstream response
    have: int = 0  # bytes on disk
    done: bool = False
    failed: ListenError | None = None
    readers: int = 0  # streams of a played track reading it
    task: asyncio.Task | None = None
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)
    representation: _Representation | None = None

    def settled(self) -> bool:
        return self.done or self.failed is not None

    async def until(self, ready: Callable[[], bool]) -> None:
        async with self.cond:
            await self.cond.wait_for(ready)

    async def changed(self) -> None:
        async with self.cond:
            self.cond.notify_all()


@dataclass
class Media:
    """What a stream request gets: a file on disk (`path`), or a `body` (a growing server copy or the upstream)
    with its status and headers. Neither: an empty answer with `status` (416)."""

    choice: Choice
    path: Path | None = None
    body: AsyncIterator[bytes] | None = None
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    media_type: str | None = None  # None: by the file's name
    variant: str | None = None  # `light.LIGHT`/`ORIGINAL`/`PENDING` for a stream asked for the light variant
    close: Callable[[], Awaitable[None]] | None = None  # after the answer (the body may never have been read)


@dataclass
class _FullResult:
    status: str
    until: float
    choice: Choice | None = None
    path: Path | None = None
    signature: tuple[str, int | None, str] | None = None


_choices: dict[str, Choice] = {}
_user_choices: dict[tuple[int, str], Choice] = {}  # only accounts with a recording rejection
_inflight: dict[str, _Job] = {}
_fetching: dict[str, _Job] = {}  # ref -> download of a local copy (`_predownload`)
_copies: dict[str, _Copy] = {}  # ref -> server copy of its picked file
_calm: asyncio.Event | None = None  # set while no copy of a played track downloads (`_stir`)
_missed: dict[str, float] = {}  # ref -> monotonic time until which the prefetch skips it
_wanted: dict[int, list[str]] = {}  # user id -> refs on their screen still to pick, top first
_wake: asyncio.Event | None = None
_workers: list[asyncio.Task] = []
_full_wanted: dict[int, list[str]] = {}
_full_jobs: dict[str, asyncio.Future[None]] = {}
_full_active: set[str] = set()
_full_results: dict[str, _FullResult] = {}
_tasks: set[asyncio.Task] = set()  # fire-and-forget work (kept referenced until done)
_http: httpx.AsyncClient | None = None  # Deezer preview CDN
# Audio is proxied over HTTP/1.1: on a shared HTTP/2 connection a stream the player abandons (track switch, seek)
# leaves its unread data counted against the connection window, and after a couple of them the next stream gets
# headers but no data (seen with the hitmoz CDN).
_media: httpx.AsyncClient | None = None


async def choose(ref: str, *, background: bool = False, stale: Choice | None = None, full_only: bool = False,
                 user_id: int | None = None) -> Choice:
    if user_id is None:
        return await _choose_shared(ref, background=background, stale=stale, full_only=full_only)
    from app.recording_feedback import library_candidate, rejected_candidates
    rejected = await rejected_candidates(user_id, ref)
    private = _user_choices.get((user_id, ref))
    if rejected and private is not None and private is not stale and private.fresh() and _usable(private):
        if private.candidate and (private.candidate.provider, private.candidate.provider_id) not in rejected:
            private.alternates = [c for c in private.alternates if (c.provider, c.provider_id) not in rejected]
            return private
    choice = await _choose_shared(ref, background=background, stale=stale, full_only=full_only)
    blocked = choice.candidate and (choice.candidate.provider, choice.candidate.provider_id) in rejected
    if rejected and choice.kind == "library" and choice.path:
        blocked = await library_candidate(choice.path, choice.nd_song_id) in rejected
    if blocked:
        src, ident = split_ref(ref)
        if src == "nd":
            async with session_scope() as s:
                catalog_id = (await s.execute(select(Track.deezer_track_id).where(
                    Track.nd_song_id == ident, Track.deezer_track_id.is_not(None)).limit(1))).scalar_one_or_none()
            if catalog_id:
                src, ident = "dz", catalog_id
        if src != "dz":
            raise ListenError(409, "Вы отклонили эту запись. Выберите другой файл или отмените сообщение.")
        choice = await _resolve_deezer(ref, ident, background, full_only=True, rejected=rejected)
        if choice.kind == "preview":
            raise ListenError(404, "Другой полной записи пока нет. Попробуйте другой источник.")
    # A private copy prevents another request's fallback from changing this user's binding.
    if rejected:
        choice = replace(choice, alternates=[c for c in choice.alternates
                         if (c.provider, c.provider_id) not in rejected])
        if len(_user_choices) >= MEM_MAX:
            _user_choices.pop(next(iter(_user_choices)))
        _user_choices[user_id, ref] = choice
    return choice


async def _choose_shared(ref: str, *, background: bool = False, stale: Choice | None = None,
                         full_only: bool = False) -> Choice:
    """The pick for `ref`: in memory, remembered or found now. Concurrent callers share one resolution, which
    also survives the caller going away (the next tap finds it ready). `background`: a prefetch, which yields to
    user requests (a user request joining it lifts it). `stale`: this pick did not open, find another."""
    hit = _choices.get(ref)
    if hit and hit is not stale and hit.fresh() and _usable(hit) and not (full_only and hit.kind == "preview"):
        hit.at = time.monotonic()
        return hit
    job = _inflight.get(ref)
    if job is None:
        prio = Priority(background)
        coro = _resolve(ref, probe=background or stale is not None, remembered=stale is None, full_only=full_only)
        job = _Job(asyncio.create_task(coro, context=priority_context(prio)), prio)
        _inflight[ref] = job

        def done(t: asyncio.Task[Choice], _ref: str = ref) -> None:
            _inflight.pop(_ref, None)
            if not t.cancelled():
                t.exception()  # retrieved here: nobody may be awaiting it any more

        job.task.add_done_callback(done)
    elif not background:
        job.prio.foreground()
    choice = await asyncio.shield(job.task)
    if full_only and choice.kind == "preview":
        # A foreground request may have joined an older browser/prefetch resolution.
        return await choose(ref, background=background, stale=choice, full_only=True)
    return choice


def _usable(c: Choice) -> bool:
    # an owned file Navidrome hadn't scanned yet is looked up again: its song id makes ♥ work
    return c.path is None or (c.path.is_file() and bool(c.nd_song_id))


MOBILE = "mobile"  # the quality of a stream for a metered network (`light`)


async def open_media(ref: str, range_header: str | None, *, ahead: bool = False, quality: str | None = None,
                     full_only: bool = False, user_id: int | None = None) -> Media:
    """The audio for the client's Range: the library file, a local copy, the server copy of the picked file (a
    preview: the upstream). `ahead`: the app loads a track of its queue ahead; its server copy waits for those of
    played tracks. `quality` "mobile": the light variant when the file is heavy (`app.light`). A pick none of whose
    files opens is replaced by a new one (once)."""
    c = await choose(ref, background=ahead, full_only=full_only, user_id=user_id)
    lite = quality == MOBILE
    for retry in (False, True):
        if c.kind == "library":
            assert c.path is not None
            return await _lighten(c, Media(c, path=c.path), c.path, _library_ident(c.path), ahead) if lite                 else Media(c, path=c.path)
        if _downloads_only(c):
            await _predownload(c)
            _heard(c)
            assert c.local is not None
            media = Media(c, path=c.local, media_type=local_media_type(c.local))
            return await _lighten(c, media, c.local, _file_ident(c), ahead) if lite else media
        if c.kind != "full":
            break
        known = await light.cached(_file_ident(c)) if lite else None
        if known is not None and known[0] is not None:  # the light copy is on disk: no need for the file at all
            _heard(c)
            return _variant(c, known)
        cp = _server_copy(c, background=ahead)
        await cp.until(lambda cp=cp: cp.size is not None or cp.settled())
        from app.recording_feedback import rejected_candidates
        rejected = await rejected_candidates(user_id, ref)
        if (cp.cand.provider, cp.cand.provider_id) in rejected:
            if retry:
                raise ListenError(409, "Вы отклонили эту запись. Выберите другой источник.")
            c = await choose(ref, background=ahead, stale=c, full_only=full_only, user_id=user_id)
            continue
        if cp.failed is None or cp.have:
            if not _same_file(c.candidate, cp.cand):
                c = replace(c, candidate=cp.cand, source=None)
            _heard(c)
            variant = known[1] if known else light.PENDING
            if lite and known is None:
                media, variant = await _lighten_copy(c, cp, ahead)
                if media is not None:
                    return media
            if media := await _from_copy(c, cp, range_header, foreground=not ahead):
                return replace(media, variant=variant) if lite else media
            break
        if retry:
            raise cp.failed
        c = await choose(ref, background=ahead, stale=c, full_only=full_only, user_id=user_id)
    resp, src = await open_stream(c, range_header)
    _heard(c)
    media = _upstream_media(c, resp, src)
    if lite:
        media.variant = light.ORIGINAL if c.kind == "preview" else light.PENDING
    return media


def _library_ident(path: Path) -> str:
    st = path.stat()
    return f"lib|{path}|{st.st_size}|{st.st_mtime_ns}"


def _file_ident(c: Choice) -> str:
    """What identifies the bytes of the picked file (the light copy is made from its server copy)."""
    cand = c.candidate
    return f"{c.ref}|{cand.provider}:{cand.provider_id}" if cand else f"{c.ref}|{c.kind}"


def _variant(c: Choice, hit: tuple[Path | None, str], original: Media | None = None) -> Media:
    path, variant = hit
    if path is not None:
        return Media(c, path=path, media_type=light.MEDIA_TYPE, variant=variant)
    assert original is not None
    return replace(original, variant=variant)


async def _lighten(c: Choice, media: Media, src: Path, ident: str, ahead: bool) -> Media:
    """The answer for a light request when the file is on disk: its Opus copy, or `media` itself (light enough as
    it is, or the copy is not ready in time)."""
    hit = await light.get(src, ident, wait_s=light.WAIT_AHEAD_S if ahead else light.WAIT_S)
    return _variant(c, hit, media)


async def _lighten_copy(c: Choice, cp: _Copy, ahead: bool) -> tuple[Media | None, str]:
    """The same for a catalog track, whose file is the server copy: the copy is waited for, then the encode. No
    media: the original plays (the variant to say), and the light copy is finished in the background."""
    ident = _file_ident(c)
    wait_s = light.WAIT_AHEAD_S if ahead else light.WAIT_S
    t0 = time.monotonic()
    try:
        await asyncio.wait_for(cp.until(cp.settled), wait_s)
    except TimeoutError:
        pass
    if cp.done:
        hit = await light.get(cp.path, ident, wait_s=max(wait_s - (time.monotonic() - t0), 0.2))
        if hit[0] is not None:
            return _variant(c, hit), light.LIGHT
        return None, hit[1]
    light.served_original(ident)
    if not cp.settled():
        _spawn(_make_light(c, cp), background=True)
    return None, light.PENDING


async def _make_light(c: Choice, cp: _Copy) -> None:
    """Finish the light copy of a catalog track once its server copy is complete."""
    await cp.until(cp.settled)
    if cp.done and (task := light.ensure(cp.path, _file_ident(c))) is not None:
        await task


async def _light_source(c: Choice) -> tuple[Path, str] | None:
    """The file the light copy of a pick is made from, ready to be read (a catalog track: its server copy is loaded
    in the background first); None: nothing to make (a preview, a copy that did not load)."""
    if c.kind == "library" and c.path is not None:
        return c.path, _library_ident(c.path)
    if c.kind != "full":
        return None
    if _downloads_only(c):
        await _predownload(c)
        return (c.local, _file_ident(c)) if c.local is not None else None
    cp = _server_copy(c, background=True)
    await cp.until(cp.settled)
    return (cp.path, _file_ident(c)) if cp.done else None


def warm_light(refs: list[str]) -> None:
    """Make the light copies of these tracks in the background (a mix's songs, a wave's tracks), in order, so a
    stream asked for the light variant finds them ready. Songs of the library are `nd:<song>`."""
    _spawn(_warm_light(list(dict.fromkeys(refs))), background=True)


async def _warm_light(refs: list[str]) -> None:
    for ref in refs:
        try:
            c = await choose(ref, background=True)
            if c.kind == "full" and await light.cached(_file_ident(c)) is not None:
                continue  # made already (or light enough as it is)
            if (src := await _light_source(c)) is not None and (task := light.ensure(*src)) is not None:
                await task
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.info("listen %s: no light copy: %r", ref, e)


def _heard(c: Choice) -> None:
    if c.kind == "full" and not c.heard:
        c.heard = True
        c.checking = _spawn(_after_heard(c), background=True)


def local_media_type(path: Path) -> str:
    return LOCAL_MEDIA_TYPES.get(path.suffix.lower(), "audio/mpeg")


async def listened_candidate(ref: str | None, *, user_id: int | None = None) -> Candidate | None:
    """The file the user listened to for this catalog ref, if it passed the check (a download tries it first)."""
    if not ref:
        return None
    c = _user_choices.get((user_id, ref)) if user_id is not None else None
    c = c or _choices.get(ref)
    if c is None or not c.fresh():
        c = await _remembered(ref, library=False)
    if c is None or c.kind != "full" or not c.heard:
        return None
    if c.checking is not None and not c.checking.done():
        await asyncio.wait([c.checking], timeout=HEARD_CHECK_WAIT_S)
    from app.recording_feedback import rejected_candidates
    if c.candidate and (c.candidate.provider, c.candidate.provider_id) in await rejected_candidates(user_id, ref):
        return None
    return c.candidate if c.verified else None


async def forget(ref: str) -> None:
    """Drop the pick (the track has just been imported: the library file plays from now on). A provider's file
    stays: it is that file the user asks for."""
    if is_file_ref(ref):
        return
    _choices.pop(ref, None)
    for key in [key for key in _user_choices if key[1] == ref]:
        del _user_choices[key]
    _full_results.pop(ref, None)
    await cache_delete(_key(ref))


def is_file_ref(ref: str) -> bool:
    return ref.startswith(FILE_REF + ":")


async def remember_files(cands: list[Candidate]) -> None:
    """Files the search has just shown (Files tab): each can be listened to and downloaded by its `file_ref`."""
    fresh: list[Choice] = []
    for cand in cands:
        ref = file_ref(cand.provider, cand.provider_id)
        hit = _choices.get(ref)
        if hit and hit.fresh() and hit.candidate == cand:
            continue  # the same file, maybe opened already
        c = Choice(ref, "full", cand.artist, cand.title, cand.duration_s, candidate=cand, verified=True)
        _choices[ref] = c
        fresh.append(c)
    if fresh:
        _prune()
        await cache_set_many({_key(c.ref): _payload(c) for c in fresh}, KEEP_S)


async def file_candidate(ref: str) -> Candidate | None:
    """The provider's file behind a `file_ref` (a download of it from the player), if the search showed it."""
    c = _choices.get(ref)
    if c is None or not c.fresh():
        c = await _remembered(ref, library=False)
    return c.candidate if c is not None else None


async def open_stream(choice: Choice, range_header: str | None, *,
                      resume: _Representation | None = None) -> tuple[httpx.Response, StreamSource]:
    """Upstream response (not read yet) for the client's Range. A link that stopped working (expired CDN URL,
    dead mirror) is renewed from the candidate once; a candidate that still doesn't open gives way to the next."""
    last = ""
    missing = False
    while True:
        if resume is not None and _candidate_key(choice) != resume.candidate:
            raise ListenError(502, "Файл трека сменился")
        cand = choice.candidate
        for _ in range(2):
            try:
                src = choice.source or await _upstream(choice)
            except ListenError as e:
                last = str(e)
                missing = e.status in (400, 404, 410)
                break
            headers = httpx.Headers(src.client.headers)  # the provider's own (hitmoz: Accept-Language)
            headers.update(src.headers)
            headers["Accept-Encoding"] = "identity"
            if range_header:
                headers["Range"] = range_header
            if resume is not None:
                if not resume.etag:
                    raise ListenError(502, "Источник не подтверждает неизменность файла")
                headers["If-Range"] = resume.etag
            media = _media_client()
            try:
                resp = await media.send(media.build_request("GET", src.url, headers=headers), stream=True,
                                        follow_redirects=True)
            except httpx.HTTPError as e:
                last = repr(e)
                missing = False
                choice.source = None
                continue
            ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            if resp.status_code == 416 or (
                resp.status_code in (200, 206)
                and (ctype.startswith(_PLAYABLE_TYPES) or ctype in ("", "application/octet-stream"))
            ):
                if ((cand.provider, cand.provider_id) if cand else None) != _candidate_key(choice):
                    await resp.aclose()
                    raise ListenError(502, "Файл трека сменился")
                choice.source = replace(src, url=str(resp.url))  # later Range requests skip the redirects
                try:
                    _validate_response(resp, range_header)
                    if resume is not None:
                        _validate_resume(choice, resp, range_header, resume)
                except BaseException:
                    await resp.aclose()
                    raise
                return resp, src
            last = f"HTTP {resp.status_code} {ctype}".strip()
            missing = resp.status_code in (404, 410)
            await resp.aclose()
            choice.source = None
        if choice.candidate is cand:  # (a concurrent request may have moved on already)
            if resume is not None or choice.kind != "full" or not choice.alternates:
                error = _FullUnavailable if missing else ListenError
                raise error(502, f"Источник не отдаёт файл ({last})")
            log.info("listen %s: %s does not open (%s), next file", choice.ref, cand.page_url if cand else "", last)
            choice.candidate = choice.alternates.pop(0)
            choice.source = None
            choice.verified = choice.candidate.enriched
            _spawn(_save(choice))


def _media_client() -> httpx.AsyncClient:
    global _media
    if _media is None:
        _media = make_client(timeout=30, http2=False)
    return _media


# --- prefetch --------------------------------------------------------------------------------------------------


def prefetch(user_id: int, refs: list[str]) -> int:
    """The catalog tracks on this user's screen, top first: their files are picked in the background. Replaces
    the user's previous list (tracks that scrolled away are dropped). Returns how many are still to pick."""
    _start_prefetch()
    todo = [r for r in dict.fromkeys(refs) if not _ready(r)][:PREFETCH_MAX]
    if todo:
        _wanted[user_id] = todo
        _wake.set()
    else:
        _wanted.pop(user_id, None)
    return len(todo)


def _start_prefetch() -> None:
    global _wake
    if _wake is None:
        _wake = asyncio.Event()
    if not _workers:
        for _ in range(PREFETCH_WORKERS):
            _workers.append(asyncio.create_task(_worker(), context=priority_context(Priority(True))))


async def prepare_full(user_id: int, refs: list[str], *, timeout_s: float = 2.0) -> dict[str, str]:
    """Prepare a bounded recommendation reserve, using the ordinary prefetch workers and shared copies.

    A ready result means the complete file has an audio stream and its measured duration passes the library's
    duration tolerance. Short recordings are valid; a 30-second file of a three-minute song is not. Timeout or
    temporary source/probe trouble is pending. Waiting never cancels shared resolution, copying or verification.
    No playback/taste event is emitted and ordinary manual playback remains unchanged.
    """
    refs = list(dict.fromkeys(refs))
    can_queue = user_id in _full_wanted or len(_full_wanted) < MEM_MAX
    _prune_full()
    # Replace this user's obsolete queued reserve; another user's interest and work already started survive.
    if can_queue:
        _full_wanted[user_id] = refs[:PREFETCH_MAX]
    _drop_full_unwanted()
    queued: list[str] = []
    waiting: set[asyncio.Future[None]] = set()
    for ref in refs[:PREFETCH_MAX]:
        if _full_status(ref) is not None:
            continue
        future = _full_jobs.get(ref)
        if future is None:
            if not can_queue or len(_full_jobs) >= FULL_MAX_JOBS:
                continue
            future = asyncio.get_running_loop().create_future()
            _full_jobs[ref] = future
        waiting.add(future)
        if ref not in _full_active:
            queued.append(ref)
    if queued and can_queue:
        _full_wanted[user_id] = queued
        _start_prefetch()
        assert _wake is not None
        _wake.set()
    else:
        _full_wanted.pop(user_id, None)
    if waiting and timeout_s > 0:
        await asyncio.wait(waiting, timeout=timeout_s)  # unlike wait_for/gather, cancellation leaves futures intact
    return {ref: _full_status(ref) or "pending" for ref in refs}


def _full_expected(c: Choice) -> int | None:
    return ((c.query or {}).get("duration_s") or c.duration_s
            or (c.candidate.duration_s if c.candidate else None))


def _full_signature(c: Choice, path: Path) -> tuple[str, int | None, str]:
    return _file_ident(c), _full_expected(c), _library_ident(path)


def _full_status(ref: str) -> str | None:
    result = _full_results.get(ref)
    if result is None:
        return None
    valid = result.until > time.monotonic()
    if valid and result.status == "ready":
        c = result.choice
        valid = c is not None and _choices.get(ref) is c and c.fresh() and result.path is not None
        if valid:
            try:
                valid = result.signature == _full_signature(c, result.path)
            except OSError:
                valid = False
        if valid:
            c.at = time.monotonic()
    if not valid:
        _full_results.pop(ref, None)
        return None
    return result.status


def _prune_full() -> None:
    now = time.monotonic()
    for ref in [ref for ref, result in _full_results.items() if result.until <= now]:
        del _full_results[ref]
    while len(_full_results) > MEM_MAX:
        del _full_results[next(iter(_full_results))]


def _drop_full_unwanted() -> None:
    wanted = {ref for refs in _full_wanted.values() for ref in refs} | _full_active
    for ref in [ref for ref in _full_jobs if ref not in wanted]:
        future = _full_jobs.pop(ref)
        if not future.done():
            future.set_result(None)


def _next_full_wanted() -> str | None:
    while _full_wanted:
        uid = next(iter(_full_wanted))
        refs = _full_wanted.pop(uid)
        while refs:
            ref = refs.pop(0)
            if ref in _full_jobs and ref not in _full_active:
                if refs:
                    _full_wanted[uid] = refs
                _full_active.add(ref)
                return ref
    return None


async def _prepare_recording(ref: str) -> _FullResult:
    c = await choose(ref, background=True, full_only=True)
    if c.kind == "preview":
        return _FullResult("pending" if c.transient else "unavailable", 0)
    if c.kind == "library":
        path = c.path
    elif _downloads_only(c):
        path = await _predownload(c)
    else:
        cp = _server_copy(c, background=True)
        await cp.until(cp.settled)
        if cp.failed is not None:
            raise cp.failed
        path = cp.path
    if path is None or not path.is_file():
        return _FullResult("unavailable", 0)
    expected = _full_expected(c)
    if c.kind != "library" and not expected:
        # With no reference duration a complete HTTP response could still be a catalog preview.
        return _FullResult("unavailable", 0)
    signature = _full_signature(c, path)
    info = await probe(path, timeout=FULL_PROBE_TIMEOUT_S)
    if signature != _full_signature(c, path):
        return _FullResult("pending", 0)
    tolerance = get_settings().cfg.matching.duration_tolerance_s
    if (not math.isfinite(info.duration_s) or info.duration_s <= 0
            or (expected and abs(info.duration_s - expected) > tolerance)):
        return _FullResult("unavailable", 0)
    return _FullResult("ready", 0, c, path, signature)


async def _warm_full(ref: str) -> None:
    try:
        try:
            result = await _prepare_recording(ref)
        except ListenError as e:
            status = "unavailable" if e.status in (400, 404, 410) or isinstance(e, _FullUnavailable) else "pending"
            result = _FullResult(status, 0)
        except ProbeError as e:
            # Missing ffprobe is a service problem, not evidence of a bad recording.
            result = _FullResult("pending" if isinstance(e.__cause__, FileNotFoundError) else "unavailable", 0)
        except (OSError, TimeoutError):
            result = _FullResult("pending", 0)
        except Exception:
            log.exception("listen full readiness %s", ref)
            result = _FullResult("pending", 0)
        ttl = MEM_TTL_S if result.status == "ready" else (
            FULL_NEGATIVE_TTL_S if result.status == "unavailable" else FULL_RETRY_TTL_S)
        result.until = time.monotonic() + ttl
        _full_results.pop(ref, None)
        _full_results[ref] = result
        _prune_full()
    finally:
        _full_active.discard(ref)
        future = _full_jobs.pop(ref, None)
        if future is not None and not future.done():
            future.set_result(None)


def _ready(ref: str) -> bool:
    """Picked and opened already, being picked, or recently not found."""
    if ref in _inflight or _missed.get(ref, 0) > time.monotonic():
        return True
    hit = _choices.get(ref)
    return bool(hit and hit.fresh() and _usable(hit)
                and (hit.kind != "full" or hit.source is not None or hit.local is not None))


def _next_wanted() -> str | None:
    """The next ref to pick; users take turns."""
    while _wanted:
        uid = next(iter(_wanted))
        refs = _wanted.pop(uid)
        while refs:
            ref = refs.pop(0)
            if not _ready(ref):
                if refs:
                    _wanted[uid] = refs
                return ref
    return None


async def _worker() -> None:
    assert _wake is not None
    prefer_full = True
    while True:
        full = prefer_full
        ref = _next_full_wanted() if full else _next_wanted()
        if ref is None:
            full = not full
            ref = _next_full_wanted() if full else _next_wanted()
        prefer_full = not full
        if ref is None:
            _wake.clear()
            await _wake.wait()
            continue
        try:
            if full:
                await _warm_full(ref)
            else:
                await _warm(ref)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("listen prefetch %s", ref)


async def _warm(ref: str) -> None:
    """Pick the file and open it once, so a tap streams at once."""
    try:
        c = await choose(ref, background=True)
        if c.kind == "full" and c.source is None and not await _opens(c):
            await choose(ref, background=True, stale=c)
    except ListenError as e:
        log.info("listen prefetch %s: %s", ref, e)
        _missed[ref] = time.monotonic() + (MISS_TTL_S if e.status == 404 else TRANSIENT_TTL_S)


async def reset() -> None:
    """Forget picks in memory, stop the prefetch and server copies, close the clients (app shutdown, tests)."""
    global _http, _media, _wake, _calm
    tasks = set([*_workers, *_tasks, *(j.task for j in [*_inflight.values(), *_fetching.values()])])
    for t in tasks:
        t.cancel()
    # Resolution may still be rolling back its DB session. Finish cancellation before
    # disposing the engine/closing clients; a second event-loop cancellation interrupts that cleanup.
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _workers.clear()
    _tasks.clear()
    _inflight.clear()
    _fetching.clear()
    _copies.clear()
    _choices.clear()
    _user_choices.clear()
    _missed.clear()
    _wanted.clear()
    _full_wanted.clear()
    for future in _full_jobs.values():
        if not future.done():
            future.set_result(None)
    _full_jobs.clear()
    _full_active.clear()
    _full_results.clear()
    _wake = _calm = None
    light.reset()
    for client in (_http, _media):
        if client is not None:
            await client.aclose()
    _http = _media = None


def _spawn(coro: Coroutine[Any, Any, Any], *, background: bool = False, prio: Priority | None = None) -> asyncio.Task:
    if prio is None and background:
        prio = Priority(True)
    t = asyncio.create_task(coro, context=priority_context(prio) if prio else None)
    _tasks.add(t)

    def done(t: asyncio.Task) -> None:
        _tasks.discard(t)
        if not t.cancelled() and (e := t.exception()):
            log.warning("listen: background work failed: %r", e)

    t.add_done_callback(done)
    return t


# --- resolution ------------------------------------------------------------------------------------------------


async def _resolve(ref: str, *, probe: bool, remembered: bool, full_only: bool = False) -> Choice:
    """`probe`: sources one by one, each pick opened before it is taken (background, or a pick that failed);
    otherwise all sources at once and nothing opened (the stream opens it). `remembered`: a remembered pick
    may be used."""
    t0 = time.monotonic()
    try:
        src, id_ = split_ref(ref)
    except ValueError as e:
        raise ListenError(400, "Неверная ссылка на трек") from e
    if src == FILE_REF:
        return await _resolve_file(ref, probe=probe, remembered=remembered)
    if src == "nd":
        return await _resolve_song(ref, id_)
    if not (src == "dz" and svc.deezer is not None or src == "hm" and "hitmoz" in svc.providers):
        raise ListenError(400, "Этот трек нельзя послушать")
    choice = await _remembered(ref) if remembered else None
    if choice is not None and full_only and choice.kind == "preview":
        choice = None
    how = "remembered"
    if choice is not None and probe and choice.kind == "full" and not await _opens_bounded(choice):
        choice = None  # the remembered file is gone
    if choice is None:
        if not remembered:
            await cache_delete(_key(ref))
        choice = await (_resolve_deezer(ref, id_, probe, full_only=full_only) if src == "dz" else _resolve_hitmoz(ref, id_, probe))
        how = "probed" if probe else "searched"
        if full_only and choice.kind == "preview":
            error = ListenError if choice.transient else _FullUnavailable
            raise error(503, "Полная версия пока недоступна. Повторите позже или выберите другой источник.")
        await _save(choice)
    _prune()
    _choices[ref] = choice
    what = choice.candidate.page_url if choice.candidate else choice.path or "deezer preview"
    log.info("listen %s: %s %s (%s, %.1f s)", ref, choice.kind, what, how, time.monotonic() - t0)
    return choice


def _prune() -> None:
    for k in [k for k, c in _choices.items() if not c.fresh()]:
        del _choices[k]
    if len(_choices) >= MEM_MAX:
        for k, _ in sorted(_choices.items(), key=lambda kc: kc[1].at)[: len(_choices) - MEM_MAX + 1]:
            del _choices[k]
    now = time.monotonic()
    for k in [k for k, until in _missed.items() if until <= now]:
        del _missed[k]


async def _resolve_file(ref: str, *, probe: bool, remembered: bool) -> Choice:
    """A provider's file: nothing to pick, only to find it among the files the search showed."""
    if not remembered:  # it did not open: there is no other file to try
        raise ListenError(502, "Источник не отдаёт этот файл")
    choice = await _remembered(ref, library=False)
    if choice is None:
        raise ListenError(404, "Файл не найден: повторите поиск")
    if probe and not await _opens(choice):
        raise ListenError(502, "Источник не отдаёт этот файл")
    _choices[ref] = choice
    return choice


async def _resolve_song(ref: str, song_id: str) -> Choice:
    """A song only the library has (`nd:<song>`, in a mix): its file."""
    async with session_scope() as s:
        t = (await s.execute(select(Track).where(Track.nd_song_id == song_id).limit(1))).scalar_one_or_none()
        found = (t.path, t.artist, t.title, t.duration_s) if t else None
    path = get_settings().library_dir / found[0] if found else None
    if not found or not path or not path.is_file():
        raise ListenError(404, "Песни нет в библиотеке")
    choice = Choice(ref, "library", found[1], found[2], found[3], path=path, nd_song_id=song_id)
    _choices[ref] = choice
    return choice


async def _resolve_deezer(ref: str, id_: str, probe: bool, *, full_only: bool = False,
                          rejected: set[tuple[str, str]] | frozenset = frozenset()) -> Choice:
    from app.recording_feedback import library_candidate
    assert svc.deezer is not None
    try:
        t = await svc.deezer.get_track(id_)
    except CatalogNotFound as e:
        raise ListenError(404, "Трек не найден в каталоге") from e
    except Exception as e:
        raise ListenError(502, f"Каталог недоступен: {e}") from e
    q = query_from_track(t)
    query = {**q.model_dump(), "deezer_track_id": t.id}
    # An owned song never waits for optional album/name metadata from external services.
    path, nd_song_id = await _library_file(query)
    if path and (not rejected or await library_candidate(path, nd_song_id) not in rejected):
        return Choice(ref, "library", q.artist, q.title, q.duration_s, path=path, nd_song_id=nd_song_id, query=query)

    # the same steps as handle_download_item: Cyrillic original, source order, ranking
    try:
        meta = await asyncio.wait_for(complete_meta(meta_from_catalog(t)), META_TIMEOUT_S)
    except TimeoutError:
        meta = {}
    try:
        q, pq = await asyncio.wait_for(russian_original(q, meta.get("upc")), NAME_TIMEOUT_S)
    except TimeoutError:
        from app.catalog.yandex import cis_release
        pq = q.model_copy(update={"lang": "ru"}) if cis_release(meta.get("upc"), q.isrc) else q
    providers = svc.provider_order(pq)

    def full(cands: list[Candidate]) -> Choice:
        return Choice(ref, "full", q.artist, q.title, q.duration_s or cands[0].duration_s, candidate=cands[0],
                      alternates=cands[1:], query=query, pquery=pq.model_dump(), verified=cands[0].enriched)

    trouble: list[str] = []  # sources that failed, files that didn't open: "not found" may not be true
    if probe:
        for p in providers:
            if cands := await _search(ref, p, pq, trouble, rejected=rejected):
                choice = full(cands)
                if await _opens_bounded(choice):
                    return choice
                trouble.append(f"{p.name}: файлы не открываются")
    elif cands := await _search_all(ref, providers, pq, trouble, rejected=rejected):
        return full(cands)

    # A failed regular source must not silently turn a full-song request into a short preview.
    regular = {p.name for p in providers}
    for p in svc.provider_order(pq, for_download=True):
        if p.name in regular:
            continue
        if cands := await _search(ref, p, pq, trouble, timeout_s=FALLBACK_TIMEOUT_S, rejected=rejected):
            choice = full(cands)
            if not probe or await _opens_bounded(choice):
                return choice
            trouble.append(f"{p.name}: файлы не открываются")

    # A queued heart may have finished importing while these sources were being searched.
    path, nd_song_id = await _library_file(query)
    if path and (not rejected or await library_candidate(path, nd_song_id) not in rejected):
        return Choice(ref, "library", q.artist, q.title, q.duration_s, path=path, nd_song_id=nd_song_id, query=query)
    choice = Choice(ref, "preview", q.artist, q.title, PREVIEW_S, query=query, transient=bool(trouble))
    if t.preview_url and (full_only or not probe or await _opens_bounded(choice)):
        return choice
    if trouble:
        raise ListenError(502, "Источники недоступны: " + "; ".join(trouble))
    raise ListenError(404, "Трек не нашёлся ни в одном источнике")


META_TIMEOUT_S = 1.5
NAME_TIMEOUT_S = 3.0
SEARCH_TIMEOUT_S = 6.0
FALLBACK_TIMEOUT_S = 8.0
# A YouTube file takes ~8 s to extract, then its URL may wait for an ad before the video (`ytdlp.MAX_AD_WAIT_S`)
OPEN_TIMEOUT_S = 25.0


async def _opens_bounded(choice: Choice) -> bool:
    try:
        return await asyncio.wait_for(_opens(choice), OPEN_TIMEOUT_S)
    except TimeoutError:
        return False


async def _search(ref: str, p, pq: TrackQuery, trouble: list[str], *, timeout_s: float | None = None,
                  rejected: set[tuple[str, str]] | frozenset = frozenset()) -> list[Candidate]:
    try:
        cands, err = await asyncio.wait_for(search_provider(p, pq, 0, limit=SEARCH_LIMIT, enrich_top_n=0),
                                          SEARCH_TIMEOUT_S if timeout_s is None else timeout_s)
    except TimeoutError:
        cands, err = [], f"{p.name}: превышено время ожидания поиска"
    if err:
        log.info("listen %s: %s", ref, err)
        trouble.append(err)
    return [c for c in cands if (c.provider, c.provider_id) not in rejected][:MAX_TRIES_PER_PROVIDER]


async def _search_all(ref: str, providers: list, pq: TrackQuery, trouble: list[str], *,
                      rejected: set[tuple[str, str]] | frozenset = frozenset()) -> list[Candidate]:
    """All sources at once; the first in source order that has the track wins (the rest are cancelled)."""
    tasks = [asyncio.create_task(_search(ref, p, pq, trouble, rejected=rejected)) for p in providers]
    try:
        for t in tasks:
            if cands := await t:
                return cands
        return []
    finally:
        for t in tasks:
            t.cancel()


async def _resolve_hitmoz(ref: str, id_: str, probe: bool) -> Choice:
    """hitmoz catalog track (Deezer was unavailable): the ref is the hitmoz song itself."""
    cand = Candidate(provider="hitmoz", provider_id=id_, artist="", title="", page_url=f"/song/{id_}")
    try:
        cand = await svc.providers["hitmoz"].enrich(cand)
    except Exception as e:
        raise ListenError(502, f"hitmoz: {e}") from e
    choice = Choice(ref, "full", "", str(cand.extra.get("full_title") or ""), cand.duration_s, candidate=cand,
                    verified=True)
    if probe and not await _opens(choice):
        raise ListenError(404, "Файл трека недоступен")
    return choice


async def _library_file(query: dict) -> tuple[Path | None, str | None]:
    """The owned file of this track and its Navidrome song id."""
    async with session_scope() as s:
        t = await find_existing(s, artist=query["artist"], title=query["title"], duration_s=query.get("duration_s"),
                                isrc=query.get("isrc"), deezer_track_id=query.get("deezer_track_id"))
        rel, nd_song_id = (t.path, t.nd_song_id) if t else (None, None)
    path = get_settings().library_dir / rel if rel else None
    return (path, nd_song_id) if path and path.is_file() else (None, None)


async def _opens(choice: Choice) -> bool:
    """Ask the upstream for one byte: a dead link or an HTML page instead of audio moves on to the next file. A
    source without plain HTTP audio: download the copy that will play."""
    if _downloads_only(choice):
        try:
            await _predownload(choice)
        except Exception as e:
            log.info("listen %s: no local copy: %s", choice.ref, e)
            return False
        return True
    try:
        resp, _ = await open_stream(choice, PROBE_RANGE)
    except Exception as e:
        log.info("listen %s: %s does not open: %s", choice.ref,
                 choice.candidate.page_url if choice.candidate else choice.kind, e)
        return False
    await resp.aclose()
    return resp.status_code in (200, 206)


async def _upstream(choice: Choice) -> StreamSource:
    global _http
    if choice.kind == "full":
        assert choice.candidate is not None
        provider = svc.providers.get(choice.candidate.provider)
        if provider is None:
            raise ListenError(404, "Источник отключён")
        try:
            return await provider.stream(choice.candidate)
        except Exception as e:
            raise ListenError(502, str(e)) from e
    if choice.kind == "preview" and svc.deezer is not None:
        try:
            url = await svc.deezer.preview_url(split_ref(choice.ref)[1])
        except Exception as e:
            raise ListenError(502, f"Каталог недоступен: {e}") from e
        if url:
            if _http is None:
                _http = make_client(timeout=30)
            return StreamSource(url=url, client=_http)
    raise ListenError(404, "Нечего воспроизвести")


# --- local copies ----------------------------------------------------------------------------------------------


def _downloads_only(c: Choice) -> bool:
    provider = svc.providers.get(c.candidate.provider) if c.kind == "full" and c.candidate else None
    return provider is not None and not getattr(provider, "streams", True)


async def _predownload(c: Choice) -> Path:
    """The local copy of the file (downloaded now, or already). Concurrent callers share one download; a user
    request joining a background one lifts it."""
    if c.local is not None and c.local.is_file():
        await asyncio.to_thread(os.utime, c.local)  # in use: pruned last
        return c.local
    key = _file_ident(c)
    job = _fetching.get(key)
    if job is None:
        assert c.candidate is not None
        prio = Priority(in_background())
        job = _Job(asyncio.create_task(_fetch_local(key, c.candidate), context=priority_context(prio)), prio)
        _fetching[key] = job

        def done(t: asyncio.Task, _ref: str = key) -> None:
            _fetching.pop(_ref, None)
            if not t.cancelled():
                t.exception()  # retrieved here: nobody may be awaiting it any more

        job.task.add_done_callback(done)
    elif not in_background():
        job.prio.foreground()
    c.local = await asyncio.shield(job.task)
    return c.local


async def _fetch_local(ref: str, cand: Candidate) -> Path:
    provider = svc.providers.get(cand.provider)
    if provider is None:
        raise ListenError(404, "Источник отключён")
    t0 = time.monotonic()
    root = get_settings().staging_dir / LOCAL_DIR
    await asyncio.to_thread(prune_local, root, _busy_dirs() - {_local_name(ref)})
    dest = root / _local_name(ref)
    try:
        await asyncio.to_thread(dest.mkdir, parents=True, exist_ok=True)
        path = await provider.fetch(cand, dest)
    except Exception as e:
        await asyncio.to_thread(shutil.rmtree, dest, True)
        raise ListenError(502, f"{cand.provider}: {e}") from e
    log.info("listen %s: local copy %s (%.1f s)", ref, path.name, time.monotonic() - t0)
    return path


def _local_name(ref: str) -> str:
    return hashlib.sha1(ref.encode()).hexdigest()[:16]


def prune_local(root: Path, busy: set[str], now: float | None = None) -> None:
    """Copies unused for `LOCAL_KEEP_S` go, and the least recently used ones while all of them take more than
    `LOCAL_MAX_BYTES`. Copies being downloaded (`busy` folder names) stay."""
    if not root.is_dir():
        return
    now = time.time() if now is None else now
    dirs: list[tuple[float, int, Path]] = []
    for d in root.iterdir():
        if d.name in busy or not d.is_dir():
            continue
        files = [f.stat() for f in d.iterdir() if f.is_file()]
        dirs.append((max((st.st_mtime for st in files), default=0.0), sum(st.st_size for st in files), d))
    total = sum(size for _, size, _ in dirs)
    for used, size, d in sorted(dirs, key=lambda x: x[0]):
        if now - used < LOCAL_KEEP_S and total <= LOCAL_MAX_BYTES:
            break
        shutil.rmtree(d, ignore_errors=True)
        total -= size


# --- server copies ---------------------------------------------------------------------------------------------


def _same_file(a: Candidate | None, b: Candidate | None) -> bool:
    # the heard file's track page check replaces the candidate with an enriched one: still the same file
    return a is not None and b is not None and (a.provider, a.provider_id) == (b.provider, b.provider_id)


def _server_copy(c: Choice, *, background: bool) -> _Copy:
    """The server copy of the picked file: being downloaded or on disk, else started now. A played track's request
    lifts a copy loaded ahead."""
    cp = _copies.get(c.ref)
    if cp is not None and _same_file(cp.cand, c.candidate) and cp.failed is None and (
            not cp.done or cp.path.is_file()):
        if not background:
            cp.prio.foreground()
            _stir()
        return cp
    if cp is not None and cp.task is not None and _same_file(cp.cand, c.candidate):
        cp.task.cancel()
    for ref in [r for r, old in _copies.items() if old.failed or (old.done and not old.path.is_file())]:
        del _copies[ref]
    assert c.candidate is not None
    cp = _Copy(c.candidate, get_settings().staging_dir / LOCAL_DIR / _local_name(c.ref) / f"{COPY_NAME}-{time.monotonic_ns()}",
               Priority(background))
    cp.task = _spawn(_download_copy(c, cp), prio=cp.prio)
    _copies[c.ref] = cp
    _stir()
    return cp


def _urgent() -> bool:
    """A copy of a played track is downloading."""
    return any(not cp.prio.background and not cp.settled() for cp in _copies.values())


def _stir() -> None:
    """Copies changed: wake those loaded ahead once no copy of a played track downloads."""
    global _calm
    if _calm is None:
        _calm = asyncio.Event()
    if _urgent():
        _calm.clear()
    else:
        _calm.set()


async def _yield_to_played(cp: _Copy) -> None:
    """A copy loaded ahead waits while a copy of a played track downloads."""
    while cp.prio.background and _urgent():
        _stir()
        assert _calm is not None
        try:  # re-checked now and then: the copy may have been lifted meanwhile
            await asyncio.wait_for(_calm.wait(), CALM_RECHECK_S)
        except TimeoutError:
            pass


def _busy_dirs() -> set[str]:
    return ({_local_name(r) for r in _fetching}
            | {cp.path.parent.name for cp in _copies.values() if not cp.settled()}
            | {_local_name(ref) for ref in _full_active})


async def _download_copy(c: Choice, cp: _Copy) -> None:
    """Append only validated bytes; never truncate a copy that a reader may already have consumed."""
    t0 = time.monotonic()
    f = None
    resp = None
    try:
        await asyncio.to_thread(prune_local, cp.path.parent.parent, _busy_dirs())
        await asyncio.to_thread(cp.path.parent.mkdir, parents=True, exist_ok=True)
        f = await asyncio.to_thread(open, cp.path, "wb", buffering=0)
        resp, src = await open_stream(c, "bytes=0-")
        if resp.status_code == 416:
            raise ListenError(502, "Источник не отдаёт файл (HTTP 416)")
        cp.cand = c.candidate
        cp.representation = _representation(c, resp)
        cp.size = cp.representation.size
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        cp.media_type = ctype if ctype.startswith("audio/") else src.media_type
        await cp.changed()
        async for chunk in _relay(c, resp, representation=cp.representation):
            await _yield_to_played(cp)
            await asyncio.to_thread(_write_all, f, chunk)
            cp.have += len(chunk)
            await cp.changed()
        cp.size = cp.have
        cp.done = True
        log.info("listen %s: server copy %d KB (%.1f s%s)", c.ref, cp.have // 1024, time.monotonic() - t0,
                 ", ahead" if cp.prio.background else "")
    except ListenError as e:
        cp.failed = e
    except Exception as e:
        log.warning("listen %s: server copy failed: %r", c.ref, e)
        cp.failed = ListenError(502, f"Не удалось скачать файл: {e}")
    finally:
        if resp is not None:
            await resp.aclose()
        if f is not None:
            f.close()
        if not cp.done and cp.failed is None:
            cp.failed = ListenError(503, "Загрузка файла прервана")
        _stir()
        await asyncio.shield(cp.changed())


def _write_all(f, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[f.write(view):]


def _total_size(resp: httpx.Response) -> int | None:
    """The whole file's size: from Content-Range of a 206, from Content-Length of a 200."""
    if resp.status_code == 206:
        parsed = _content_range(resp)
        return parsed[2] if parsed else None
    length = resp.headers.get("content-length", "")
    return int(length) if resp.status_code == 200 and length.isdigit() else None


def _byte_range(header: str | None, size: int) -> tuple[int, int] | Literal[False] | None:
    """The client's single `bytes=` range as (first, last); None: the whole file; False: not satisfiable."""
    spec = (header or "").strip()
    if not spec.startswith("bytes=") or "," in spec:
        return None
    a, sep, b = spec.removeprefix("bytes=").strip().partition("-")
    try:
        if not sep:
            return None
        if not a:  # the last `b` bytes
            n = int(b)
            return (max(size - n, 0), size - 1) if n > 0 and size else False
        first, last = int(a), int(b) if b else size - 1
    except ValueError:
        return None
    if first >= size or last < first:
        return False
    return first, min(last, size - 1)


async def _from_copy(c: Choice, cp: _Copy, range_header: str | None, *, foreground: bool) -> Media | None:
    """The answer from the server copy; None when the range lies far past what it has (the upstream serves it)."""
    if cp.done:
        await asyncio.to_thread(os.utime, cp.path)  # in use: pruned last
        return Media(c, path=cp.path, media_type=cp.media_type)
    if cp.size is None:
        return None
    rng = _byte_range(range_header, cp.size)
    if rng is False:
        return Media(c, status=416, headers={"content-range": f"bytes */{cp.size}"})
    first, last = rng or (0, cp.size - 1)
    if first > cp.have + SEEK_WAIT_BYTES:
        return None
    headers = {"content-length": str(last - first + 1)}
    if rng:
        headers["content-range"] = f"bytes {first}-{last}/{cp.size}"
    return Media(c, body=_read_copy(c, cp, first, last, foreground=foreground), status=206 if rng else 200,
                 headers=headers, media_type=cp.media_type)


async def _read_copy(c: Choice, cp: _Copy, first: int, last: int, *, foreground: bool) -> AsyncIterator[bytes]:
    """Bytes `first`..`last` of the server copy as they arrive; when the copy fails, the rest from the upstream."""
    pos = first
    if foreground:
        cp.readers += 1
    f = None
    try:
        f = await asyncio.to_thread(open, cp.path, "rb")
        while pos <= last:
            if pos >= cp.have:
                await cp.until(lambda pos=pos: cp.have > pos or cp.settled())
                if pos >= cp.have:
                    break
            data = await asyncio.to_thread(_read_at, f, pos, min(COPY_CHUNK, cp.have - pos, last + 1 - pos))
            if not data:
                break
            yield data
            pos += len(data)
    finally:
        if f is not None:
            f.close()
        if foreground:
            cp.readers -= 1
            if not cp.readers and not cp.settled():
                cp.prio.background = True  # nobody plays it any more (a skip): a played track's copy goes first
                _stir()
    if pos <= last:
        if cp.representation is None:
            raise ListenError(502, "Источник не подтверждает неизменность файла")
        resp, _ = await open_stream(c, f"bytes={pos}-{last}", resume=cp.representation)
        async for chunk in _relay(c, resp, representation=cp.representation):
            yield chunk


def _read_at(f, pos: int, n: int) -> bytes:
    f.seek(pos)
    return f.read(n)


def _upstream_media(c: Choice, resp: httpx.Response, src: StreamSource) -> Media:
    headers = {k: resp.headers[k] for k in ("content-length", "content-range") if k in resp.headers}
    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    return Media(c, body=_relay(c, resp), status=resp.status_code, headers=headers,
                 media_type=ctype if ctype.startswith("audio/") else src.media_type, close=resp.aclose)


async def _relay(c: Choice, resp: httpx.Response, *,
                 representation: _Representation | None = None) -> AsyncIterator[bytes]:
    """Resume only the exact remaining range of a validator-confirmed representation."""
    span = _span(resp)
    rep = representation or _representation(c, resp)
    pos = span[0] if span else 0
    drops = 0
    try:
        while True:
            try:
                async for chunk in resp.aiter_raw():
                    if span and pos + len(chunk) > span[1] + 1:
                        raise ListenError(502, "Источник передал больше байтов, чем объявлено")
                    pos += len(chunk)
                    drops = 0
                    yield chunk
                if span is None or pos == span[1] + 1:
                    return
                raise httpx.RemoteProtocolError("upstream ended before Content-Length")
            except httpx.TransportError:
                if span and pos == span[1] + 1:
                    return
                drops += 1
                if span is None or drops > COPY_RESUMES:
                    raise
            await resp.aclose()
            resp, _ = await open_stream(c, f"bytes={pos}-{span[1]}", resume=rep)
    finally:
        await resp.aclose()


def _candidate_key(c: Choice) -> tuple[str, str] | None:
    return (c.candidate.provider, c.candidate.provider_id) if c.candidate else None


def _representation(c: Choice, resp: httpx.Response) -> _Representation:
    etag = resp.headers.get("etag", "")
    # Weak validators and dates alone cannot prove byte-for-byte identity.
    strong = etag if len(etag) >= 2 and etag.startswith('"') and etag.endswith('"') else None
    return _Representation(_candidate_key(c), str(resp.url), _total_size(resp), strong)


def _content_range(resp: httpx.Response) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", resp.headers.get("content-range", ""))
    if match is None:
        return None
    first, last, size = map(int, match.groups())
    return (first, last, size) if first <= last < size else None


def _validate_response(resp: httpx.Response, requested: str | None) -> None:
    if resp.headers.get("content-encoding", "identity").lower() != "identity":
        raise ListenError(502, "Источник вернул сжатый диапазон")
    length = resp.headers.get("content-length")
    if length is not None and not length.isdigit():
        raise ListenError(502, "Источник вернул неверный Content-Length")
    if resp.status_code == 206:
        parsed = _content_range(resp)
        if parsed is None:
            raise ListenError(502, "Источник вернул неверный Content-Range")
        first, last, size = parsed
        if length is not None and int(length) != last - first + 1:
            raise ListenError(502, "Размер диапазона не совпадает с Content-Length")
        if _byte_range(requested, size) != (first, last):
            raise ListenError(502, "Источник вернул другой диапазон")


def _validate_resume(c: Choice, resp: httpx.Response, requested: str | None, rep: _Representation) -> None:
    current = _representation(c, resp)
    if (resp.status_code != 206 or rep.size is None or not rep.etag or current != rep
            or _span(resp) != _byte_range(requested, rep.size)):
        raise ListenError(502, "Источник не подтвердил диапазон того же файла")


def _span(resp: httpx.Response) -> tuple[int, int] | None:
    if resp.status_code == 206:
        parsed = _content_range(resp)
        return parsed[:2] if parsed else None
    length = resp.headers.get("content-length", "")
    return (0, int(length) - 1) if resp.status_code == 200 and length.isdigit() else None


# --- after listening -------------------------------------------------------------------------------------------


async def _after_heard(c: Choice) -> None:
    """Check the heard file on its track page (hitmoz: Explicit/Clean, version, real bitrate): a download takes
    it only if it still fits. One that doesn't is replaced, for the next listen, by a download-grade pick."""
    if not c.verified and c.candidate is not None:
        cand = c.candidate
        provider = svc.providers.get(cand.provider)
        try:
            enriched = await provider.enrich(cand) if provider else None
        except Exception as e:
            log.info("listen %s: track page check failed: %r", c.ref, e)
            enriched = None
        if enriched is not None and c.candidate is cand:
            if _still_fits(c, enriched):
                c.candidate, c.verified = enriched, True
            else:
                log.info("listen %s: %s does not fit after its track page, picking again", c.ref, cand.page_url)
                await _repick(c, provider)
                return
    await _save(c)


def _still_fits(c: Choice, enriched: Candidate) -> bool:
    if not c.pquery:
        return True
    q = TrackQuery(**c.pquery)
    if q.explicit and enriched.explicit is False:
        return False  # the censored copy of an explicit track
    return bool(rank_candidates(q, [enriched], get_settings().cfg.matching))


async def _repick(c: Choice, provider) -> None:
    """A download-grade search (track pages of the top results) of the source the heard file came from; the best
    file is remembered for the next listen (the one playing now stays: a seek must hit the same file)."""
    assert c.pquery is not None
    cands, err = await search_provider(provider, TrackQuery(**c.pquery), 0, limit=SEARCH_LIMIT)
    if err or not cands:
        await cache_delete(_key(c.ref))
        return
    best = cands[0]
    await _save(replace(c, candidate=best, alternates=cands[1:MAX_TRIES_PER_PROVIDER], source=None, heard=False,
                        verified=best.enriched, checking=None))


# --- remembered picks ------------------------------------------------------------------------------------------


def _key(ref: str) -> str:
    return f"listen:{ref}"


async def _save(c: Choice) -> None:
    if c.kind == "library" or c.transient:
        return
    await cache_set(_key(c.ref), _payload(c), KEEP_S if c.kind == "full" else KEEP_PREVIEW_S)


def _payload(c: Choice) -> dict:
    return {
        "kind": c.kind,
        "artist": c.artist,
        "title": c.title,
        "duration_s": c.duration_s,
        "candidate": c.candidate.model_dump(mode="json") if c.candidate else None,
        "alternates": [a.model_dump(mode="json") for a in c.alternates],
        "query": c.query,
        "pquery": c.pquery,
        "heard": c.heard,
        "verified": c.verified,
        "sources": _source_names(c.pquery),
    }


def _source_names(pquery: dict | None) -> list[str]:
    """The sources a pick of this query is searched in, in order (with the download fallback)."""
    return [p.name for p in svc.provider_order(TrackQuery(**pquery), for_download=True)] if pquery else []


def _outranked(provider: str, pquery: dict, sources: list[str]) -> bool:
    """The source order has changed since the pick: a source now ahead of the picked file's was not ahead of it
    then (the first source in order that has the track wins, those behind it were not asked)."""
    order = _source_names(pquery)
    ahead = order[: order.index(provider)] if provider in order else order
    asked = sources[: sources.index(provider)] if provider in sources else sources
    return not set(ahead) <= set(asked)


async def _remembered(ref: str, *, library: bool = True) -> Choice | None:
    """The remembered pick (without an opened link); an owned track plays from the library instead."""
    try:
        d = await cache_get(_key(ref))
        if not d:
            return None
        cand = Candidate(**d["candidate"]) if d.get("candidate") else None
        c = Choice(ref, d["kind"], d["artist"], d["title"], d.get("duration_s"), candidate=cand,
                   alternates=[Candidate(**a) for a in d.get("alternates") or []], query=d.get("query"),
                   pquery=d.get("pquery"), heard=bool(d.get("heard")), verified=bool(d.get("verified")))
    except Exception as e:
        log.warning("listen %s: remembered pick unreadable: %r", ref, e)
        return None
    if c.kind == "full" and c.candidate is None:
        return None
    if (c.kind == "full" and c.candidate is not None and c.pquery
            and _outranked(c.candidate.provider, c.pquery, d.get("sources") or [])):
        return None  # picked before a preferred source was added (YouTube after SoundCloud for Russian tracks)
    if library and c.query:
        path, nd_song_id = await _library_file(c.query)
        if path:
            return Choice(ref, "library", c.artist, c.title, c.duration_s, path=path, nd_song_id=nd_song_id,
                          query=c.query)
    return c
