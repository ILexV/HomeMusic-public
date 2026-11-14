"""Assistant pipeline (PLAN 8.5): recognize → resolve against library/catalog → confirmation card → downloads.

Side effects (downloads, playlists) happen only in `confirm_task`, called by the UI with the task's confirm_token.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app import listen
from app.api.schemas import (
    AssistantConfirmIn,
    AssistantRowOut,
    AssistantTaskOut,
    DownloadIn,
    DownloadTrackIn,
    TrackOut,
)
from app.catalog.models import CatalogAlbum, CatalogTrack, make_ref
from app.config import get_settings
from app.db import session_scope
from app.events import bus
from app.jobs.queue import PermanentError, handler, on_final_failure
from app.library.owned import annotate_tracks, find_existing
from app.matching.normalize import has_cyrillic, normalize_title
from app.matching.select import classify_scores, match_catalog
from app.models import AiUsage, AssistantTask, Job, Track, User
from app.providers.base import Candidate, TrackQuery, file_ref
from app.services import svc

log = logging.getLogger(__name__)

MAX_ROWS = 200


def task_dir(task_id: int) -> Path:
    d = get_settings().data_dir / "assistant" / str(task_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def task_out(t: AssistantTask, *, with_rows: bool = True) -> AssistantTaskOut:
    ex = t.extraction_json or {}
    res = t.resolution_json or {}
    return AssistantTaskOut(
        id=t.id,
        status=t.status,
        input_kind=t.input_kind,
        source_app=ex.get("source_app"),
        page_type=ex.get("page_type"),
        context=ex.get("context") or {},
        rows=[AssistantRowOut(**r) for r in res.get("rows", [])] if with_rows else [],
        suggested_playlist_name=res.get("suggested_playlist_name"),
        confirm_token=t.confirm_token if t.status == "awaiting_confirmation" else None,
        batch_id=t.batch_id,
        playlist_id=res.get("playlist_id"),
        error=t.error,
        cost_usd=t.cost_usd or 0,
        created_at=t.created_at,
    )


async def set_task(task_id: int, **fields: Any) -> AssistantTask | None:
    async with session_scope() as s:
        t = await s.get(AssistantTask, task_id)
        if t is None:
            return None
        for k, v in fields.items():
            setattr(t, k, v)
        await s.flush()
        await s.refresh(t)
    bus.publish(t.user_id, "assistant", {"id": t.id, "status": t.status})
    return t


# --- recognition ---------------------------------------------------------------------------------


async def recognize(task: AssistantTask) -> tuple[dict[str, Any], list[CatalogTrack] | None, float, int, int]:
    """Returns (extraction dict, ready catalog tracks for album/artist intents or None, cost, tokens in/out)."""
    from app.ai import extract as ex
    from app.ai import links
    from app.ai.llm import LLMClient, LLMError, configured

    ref = task.input_ref or {}
    if task.input_kind == "link":
        try:
            extraction = await links.extract_from_link(ref["url"])
        except links.UnsupportedLink as e:
            raise PermanentError(str(e) or "Ссылка не поддерживается") from e
        return extraction.model_dump(mode="json"), None, 0, 0, 0

    if task.input_kind == "text":
        text = ref.get("text", "")
        llm = LLMClient() if configured() else None
        if llm is None:
            # the fast path ("Artist - Title" lines) works without the model
            intent, usage = await ex.parse_text_request(text, None)  # type: ignore[arg-type]
        else:
            intent, usage = await ex.parse_text_request(text, llm)
        tracks = await tracks_for_intent(intent)
        extraction = ex.Extraction(
            source_app="other",
            page_type="other",
            context=ex.ExtractionContext(playlist_name=intent.playlist_name),
            items=intent.tracks if tracks is None else [],
        )
        return extraction.model_dump(mode="json"), tracks, usage.cost_usd, usage.tokens_in, usage.tokens_out

    if not configured():
        raise PermanentError("ИИ не настроен: добавьте LLM_API_KEY в .env")
    images = [p.read_bytes() for p in sorted(task_dir(task.id).iterdir()) if p.is_file()]
    if not images:
        raise PermanentError("Нет изображений")
    try:
        extraction, usage = await ex.extract_from_images(images, LLMClient())
    except LLMError as e:
        raise RuntimeError(f"ИИ недоступен: {e}") from e
    return extraction.model_dump(mode="json"), None, usage.cost_usd, usage.tokens_in, usage.tokens_out


async def tracks_for_intent(intent) -> list[CatalogTrack] | None:
    """Album / latest album / discography / top intents are answered straight from the catalog."""
    if intent.kind in ("tracks", "unknown") or not intent.artist or svc.deezer is None:
        return None
    found = await svc.deezer.search(intent.artist, kinds=("artist",), limit=5)
    if not found.artists:
        return None
    artist = found.artists[0]
    if intent.kind == "artist_top":
        return await svc.deezer.get_artist_top(artist.id, limit=10)
    page = await svc.deezer.get_artist_page(artist.id)
    albums: list[CatalogAlbum] = []
    if intent.kind == "album" and intent.album:
        nq = normalize_title(intent.album).text
        albums = [a for a in page.albums if normalize_title(a.title).text == nq] or [
            a for a in page.albums if nq in normalize_title(a.title).text
        ][:1]
    elif intent.kind == "latest_album":
        only_albums = [a for a in page.albums if a.record_type == "album"] or page.albums
        albums = sorted(only_albums, key=lambda a: a.release_date or datetime.min.date(), reverse=True)[:1]
    elif intent.kind == "discography":
        types = set(intent.record_types or ["album", "single", "ep"])
        albums = [a for a in page.albums if a.record_type in types]
    out: list[CatalogTrack] = []
    for a in albums[:30]:
        full = await svc.deezer.get_album(a.id)
        out += full.tracks or []
    return out or None


# --- resolution ------------------------------------------------------------------------------------


def _track_from_candidate(c: Candidate) -> TrackOut:
    return TrackOut(ref=make_ref("hm" if c.provider == "hitmoz" else c.provider, c.provider_id), source="hm",
                    id=c.provider_id, title=c.title, artist=c.artist, artists=[c.artist], duration_s=c.duration_s,
                    explicit=c.explicit, cover_url=c.cover_url, album_title=c.album)


async def _library_hit(s: AsyncSession, artist: str, title: str) -> Track | None:
    if not artist or not title:
        return None
    return await find_existing(s, artist=artist, title=title)


async def resolve_row(idx: int, item: dict[str, Any], album_tracks: list[CatalogTrack] | None,
                      sem: asyncio.Semaphore) -> tuple[AssistantRowOut, list[dict]]:
    """Library → album tracklist (album screenshots) → Deezer → hitmoz (needs confirmation)."""
    cfg = get_settings().cfg.matching
    artist = (item.get("artist_raw") or "").strip()
    title = (item.get("title_raw") or "").strip()
    truncated = bool(item.get("truncated"))
    guess = item.get("guess_full")
    row = AssistantRowOut(index=idx, artist_raw=artist, title_raw=title, album_raw=item.get("album_raw"),
                          duration=item.get("duration"), truncated=truncated, status="not_found")
    duration = _parse_duration(item.get("duration"))
    candidates_json: list[dict] = []

    async with sem:
        async with session_scope() as s:
            lib = None if truncated else await _library_hit(s, artist, title)
            if lib is not None:
                row.status, row.library_track_id, row.score = "owned", lib.id, 1.0
                row.best = TrackOut(ref=f"lib:{lib.id}", source="dz", id=str(lib.id), title=lib.title,
                                    artist=lib.artist, album_title=lib.album, duration_s=lib.duration_s,
                                    explicit=lib.explicit, owned=True, library_track_id=lib.id,
                                    nd_song_id=lib.nd_song_id)
                return row, candidates_json

            pool: list[CatalogTrack] = list(album_tracks or [])
            scored = match_catalog(artist, title, pool, duration=duration, truncated=truncated) if pool else []
            if not scored or scored[0][1].total < cfg.confirm_threshold:
                queries = [(artist, title)]
                if truncated and guess:
                    queries.insert(0, (artist, guess))
                queries.append((title, artist))  # the model sometimes swaps artist and title
                for qa, qt in queries:
                    try:
                        found = await svc.deezer.search_tracks(qa, qt, limit=10) if svc.deezer else []
                    except Exception as e:
                        log.warning("deezer search failed: %r", e)
                        found = []
                    if not found:
                        continue
                    sc = match_catalog(artist, title if not (truncated and guess) else guess, found,
                                       album=item.get("album_raw"), duration=duration,
                                       truncated=truncated and not guess)
                    if not scored or (sc and sc[0][1].total > scored[0][1].total):
                        scored = sc
                    if scored and scored[0][1].total >= cfg.auto_threshold:
                        break

            if scored:
                verdict = classify_scores([x[1].total for x in scored], cfg)
                top = [t for t, _ in scored[:3]]
                annotated = await annotate_tracks(s, top)
                row.score = round(scored[0][1].total, 3)
                if verdict != "not_found":
                    row.best = annotated[0]
                    row.options = annotated
                    row.status = "owned" if annotated[0].owned else verdict
                    if annotated[0].owned:
                        row.library_track_id = annotated[0].library_track_id
                    return row, candidates_json

        # not in the catalog: offer hitmoz files, never auto (PLAN 8.5.3)
        if has_cyrillic(artist + title) and "hitmoz" in svc.providers:
            try:
                cands = await svc.providers["hitmoz"].search(TrackQuery(artist=artist, title=guess or title), limit=5)
            except Exception as e:
                log.warning("hitmoz search failed: %r", e)
                cands = []
            from app.matching.select import rank_candidates

            ranked = rank_candidates(TrackQuery(artist=artist, title=guess or title), cands, cfg)[:3]
            if ranked:
                row.status = "confirm"
                row.options = [_track_from_candidate(c) for c in ranked]
                row.best = row.options[0]
                row.score = round(ranked[0].score, 3)
                candidates_json = [c.model_dump(mode="json") for c in ranked]
    return row, candidates_json


def _parse_duration(v: Any) -> int | None:
    if not v:
        return None
    try:
        parts = [int(x) for x in str(v).strip().split(":")]
    except ValueError:
        return None
    total = 0
    for p in parts:
        total = total * 60 + p
    return total or None


async def album_context_tracks(extraction: dict[str, Any]) -> list[CatalogTrack] | None:
    """Album screenshot: find the album first, then match rows against its tracklist (fixes truncated names)."""
    ctx = extraction.get("context") or {}
    if extraction.get("page_type") != "album" or not ctx.get("album_title") or svc.deezer is None:
        return None
    try:
        albums = await svc.deezer.search_albums(ctx.get("album_artist") or "", ctx["album_title"], limit=5)
    except Exception:
        return None
    nq = normalize_title(ctx["album_title"]).text
    for a in albums:
        if normalize_title(a.title).text == nq:
            return (await svc.deezer.get_album(a.id)).tracks
    return None


def suggested_name(extraction: dict[str, Any]) -> str:
    ctx = extraction.get("context") or {}
    if ctx.get("playlist_name"):
        return ctx["playlist_name"]
    if ctx.get("album_title"):
        return ctx["album_title"]
    return f"Со скриншота {datetime.now():%d.%m.%Y}"


@handler("assistant_task")
async def handle_assistant_task(job: Job) -> None:
    task_id = int(job.payload_json["task_id"])
    async with session_scope() as s:
        task = await s.get(AssistantTask, task_id)
        if task is None or task.status not in ("queued", "extracting", "resolving"):
            return
    await set_task(task_id, status="extracting", error=None)
    extraction, ready, cost, tin, tout = await recognize(task)
    if cost or tin or tout:
        async with session_scope() as s:
            s.add(AiUsage(user_id=task.user_id, kind=task.input_kind, tokens_in=tin, tokens_out=tout, cost_usd=cost))
    await set_task(task_id, status="resolving", extraction_json=extraction, cost_usd=(task.cost_usd or 0) + cost,
                   tokens_in=(task.tokens_in or 0) + tin, tokens_out=(task.tokens_out or 0) + tout)

    rows: list[AssistantRowOut] = []
    cands: dict[str, list[dict]] = {}
    if ready is not None:
        async with session_scope() as s:
            annotated = await annotate_tracks(s, ready[:MAX_ROWS])
        for i, t in enumerate(annotated):
            rows.append(AssistantRowOut(index=i, artist_raw=t.artist, title_raw=t.title, album_raw=t.album_title,
                                        status="owned" if t.owned else "auto", best=t, options=[t], score=1.0,
                                        library_track_id=t.library_track_id))
    else:
        items = (extraction.get("items") or [])[:MAX_ROWS]
        album_tracks = await album_context_tracks(extraction)
        sem = asyncio.Semaphore(4)
        results = await asyncio.gather(*(resolve_row(i, it, album_tracks, sem) for i, it in enumerate(items)))
        for row, cj in results:
            rows.append(row)
            if cj:
                cands[str(row.index)] = cj
    if not rows:
        await set_task(task_id, status="failed", error="Не удалось распознать ни одного трека")
        return
    resolution = {
        "rows": [r.model_dump(mode="json") for r in rows],
        "candidates": cands,
        "suggested_playlist_name": suggested_name(extraction),
    }
    log.info("assistant task %s: %s rows, cost $%.5f", task_id, len(rows), cost, extra={"task_id": task_id})
    await set_task(task_id, status="awaiting_confirmation", resolution_json=resolution,
                   confirm_token=secrets.token_urlsafe(16))


@on_final_failure("assistant_task")
async def _assistant_failed(job: Job, exc: Exception) -> None:
    await set_task(int(job.payload_json["task_id"]), status="failed", error=str(exc)[:500])


# --- confirmation ------------------------------------------------------------------------------------


async def confirm_task(s: AsyncSession, user: User, task: AssistantTask, body: AssistantConfirmIn) -> AssistantTask:
    from fastapi import HTTPException

    from app.api.downloads import create_batch

    if task.status != "awaiting_confirmation":
        raise HTTPException(409, "Задача уже подтверждена или ещё не готова")
    if not task.confirm_token or not secrets.compare_digest(task.confirm_token, body.confirm_token):
        raise HTTPException(403, "Неверный токен подтверждения")
    res = task.resolution_json or {}
    rows = {r["index"]: AssistantRowOut(**r) for r in res.get("rows", [])}
    cands = res.get("candidates", {})
    # Stored resolution candidates, not candidate JSON supplied by the confirming client.
    await listen.remember_files([Candidate(**candidate) for options in cands.values() for candidate in options])

    items: list[DownloadTrackIn] = []
    for r_in in sorted(body.rows, key=lambda r: r.index):
        row = rows.get(r_in.index)
        if row is None or not r_in.selected:
            continue
        ch = r_in.choice
        if ch is None or (ch.kind in ("catalog", "candidate") and not (ch.ref or ch.file_ref)):
            pick = row.best
            if pick is None:
                continue
            if row.status == "owned" and row.library_track_id:
                lib = await s.get(Track, row.library_track_id)
                if lib:
                    items.append(DownloadTrackIn(artist=lib.artist, title=lib.title, duration_s=lib.duration_s,
                                                 isrc=lib.isrc))
                continue
            items.append(_item_from_option(pick, cands.get(str(row.index), [])))
        elif ch.kind == "skip":
            continue
        elif ch.kind == "library" and ch.track_id:
            lib = await s.get(Track, ch.track_id)
            if lib:
                items.append(DownloadTrackIn(artist=lib.artist, title=lib.title, duration_s=lib.duration_s,
                                             isrc=lib.isrc))
        elif ch.kind == "candidate" and ch.file_ref:
            if not listen.is_file_ref(ch.file_ref) or (candidate := await listen.file_candidate(ch.file_ref)) is None:
                raise HTTPException(404, "Файл не найден: повторите поиск")
            items.append(DownloadTrackIn(artist=candidate.artist, title=candidate.title,
                                         duration_s=candidate.duration_s, file_ref=ch.file_ref))
        elif ch.kind == "catalog" and ch.ref:
            opt = next((o for o in row.options if o.ref == ch.ref), None)
            if opt is not None:
                items.append(_item_from_option(opt, cands.get(str(row.index), [])))
        elif ch.kind == "manual" and ch.artist and ch.title:
            items.append(DownloadTrackIn(artist=ch.artist.strip(), title=ch.title.strip()))
    if not items:
        raise HTTPException(400, "Ничего не выбрано")

    target = body.target
    req = DownloadIn(
        items=items,
        playlist_id=target.playlist_id if target.kind == "existing" else None,
        new_playlist_name=(target.name or res.get("suggested_playlist_name") or "Со скриншота")
        if target.kind == "new" else None,
    )
    out = await create_batch(s, user, req, origin="assistant", title=req.new_playlist_name)
    task.status = "confirmed"
    task.batch_id = out.batch_id
    task.confirm_token = None
    task.resolution_json = {**res, "playlist_id": out.playlist_id}
    await s.flush()
    return task


def _item_from_option(opt: TrackOut, cands: list[dict]) -> DownloadTrackIn:
    if opt.source == "dz":
        return DownloadTrackIn(artist=opt.artist, title=opt.title, album=opt.album_title, duration_s=opt.duration_s,
                               isrc=opt.isrc, explicit=opt.explicit, catalog_ref=opt.ref)
    # hitmoz file confirmed by the user
    cand = next((Candidate(**c) for c in cands if c.get("provider_id") == opt.id), None)
    return DownloadTrackIn(artist=opt.artist, title=opt.title, duration_s=opt.duration_s,
                           file_ref=file_ref(cand.provider, cand.provider_id) if cand else None)
