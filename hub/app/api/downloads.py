"""Download queue API (FR-3)."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import listen
from app.api.schemas import DownloadBatchOut, DownloadIn, DownloadItemOut, DownloadTrackIn, RetryIn
from app.auth import current_user, user_navidrome
from app.catalog.models import split_ref
from app.db import get_session
from app.events import bus
from app.jobs.downloads import create_items, item_links, item_out, item_ref
from app.jobs.queue import enqueue, notify
from app.library.owned import ACTIVE_STATUSES, find_existing, quiet
from app.models import DownloadBatch, DownloadItem, Job, PendingPlaylistAdd, User
from app.providers.base import Candidate, TrackQuery
from app.services import svc

router = APIRouter(prefix="/api/downloads", tags=["downloads"])


async def resolve_playlist(user: User, playlist_id: str | None, new_name: str | None) -> str | None:
    if playlist_id:
        return playlist_id
    if not new_name:
        return None
    nd = user_navidrome(user)
    try:
        pl = await nd.create_playlist(new_name.strip())
    finally:
        await nd.aclose()
    return pl.get("id")


async def join_active(s: AsyncSession, user: User, refs: list[str], *, star: bool,
                      favorite_event_id: str | None = None) -> set[str]:
    """Catalog tracks this user is downloading already: no second download. ♥ marks the download to star the song
    after the import; an explicit download makes a quiet one (♥ only, an import's) a visible download, and one an
    import queued in the background goes first in the queue from then on. Returns the joined refs."""
    if not refs:
        return set()
    items = (await s.execute(
        select(DownloadItem).where(DownloadItem.user_id == user.id, DownloadItem.status.in_(ACTIVE_STATUSES))
    )).scalars().all()
    joined: set[str] = set()
    changed: list[DownloadItem] = []
    for item in items:
        qj = dict(item.query_json or {})
        ref = item_ref(item)
        if ref not in refs or ref in joined:
            continue
        joined.add(ref)
        if star and favorite_event_id:
            qj.update(star=True, star_event_id=favorite_event_id, star_cancelled=False, star_failed=False)
        elif star and not qj.get("star"):
            qj.update(star=True, requested=True)  # it was asked for explicitly: it stays a visible download
        elif not star and quiet(qj):
            qj["requested"] = True
        elif not (star and qj.get("import") and not qj.get("requested")):
            continue
        else:
            qj["requested"] = True  # ♥ on a track an import downloads quietly: the user wants it now
        item.query_json = qj
        if star and not favorite_event_id:
            from app.favorites import download_requested
            await download_requested(s, item)
        changed.append(item)
    if changed:
        await s.execute(update(Job).where(
            Job.kind == "download_item", Job.status == "queued", Job.priority < 0,
            func.json_extract(Job.payload_json, "$.item_id").in_([i.id for i in changed])).values(priority=0))
        await s.flush()
        links = await item_links(s, changed)
        for item in changed:
            bus.publish(user.id, "download", item_out(item, links=links[item.id]).model_dump(mode="json"))
    return joined


async def create_batch(
    s: AsyncSession, user: User, body: DownloadIn, *, origin: str, title: str | None = None
) -> DownloadBatchOut:
    favorite_events = {}
    if body.favorite_event_id:
        from app.api.schemas import FavoriteTarget
        from app.favorites import record, snapshot

        ref = body.track_refs[0]
        # Acquire the SQLite write lock before checking the idempotency marker.
        await record(s, user.id, FavoriteTarget(ref=ref), True, event_id=body.favorite_event_id,
                     ts=body.favorite_ts, client=body.favorite_client, context=body.favorite_context, mirror_pending=True)
        previous = (await s.execute(select(DownloadBatch).where(
            DownloadBatch.user_id == user.id, DownloadBatch.origin == "favorite-app",
            DownloadBatch.title == body.favorite_event_id))).scalar_one_or_none()
        if previous:
            return DownloadBatchOut(batch_id=previous.id, queued=0, already_owned=0)
        favorite_events[ref] = body.favorite_event_id
        states = await snapshot(s, user.id)
        current = next((x for x in states.values() if x.target_type == "track" and x.ref == ref), None)
        if current is None or not current.liked:
            batch = DownloadBatch(user_id=user.id, origin="favorite-app", title=body.favorite_event_id)
            s.add(batch)
            await s.flush()
            return DownloadBatchOut(batch_id=batch.id, queued=0, already_owned=0)
        origin, title = "favorite-app", body.favorite_event_id
    # ♥ downloads stay out of playlists: the song is in «Понравившиеся» anyway, and the app keeps a batch's playlist
    # on the phone
    playlist_id = None if body.star else await resolve_playlist(user, body.playlist_id, body.new_playlist_name)
    track_refs = list(body.track_refs)
    joined = set() if body.force else await join_active(s, user, track_refs, star=body.star,
                                                        favorite_event_id=body.favorite_event_id)
    track_refs = [r for r in track_refs if r not in joined]
    items = list(body.items)
    # a provider's file listened to in the player (Files tab of the search): the file itself, as if picked there
    for ref in [r for r in track_refs if listen.is_file_ref(r)]:
        c = await listen.file_candidate(ref)
        if c is None:
            raise HTTPException(404, "Файл не найден: повторите поиск")
        items.append(DownloadTrackIn(artist=c.artist, title=c.title, duration_s=c.duration_s, explicit=c.explicit,
                                     file_ref=ref))
    track_refs = [r for r in track_refs if not listen.is_file_ref(r)]
    batch = DownloadBatch(user_id=user.id, origin=origin, title=title or body.new_playlist_name,
                          target_playlist_id=playlist_id)
    s.add(batch)
    await s.flush()

    entries = []
    owned = 0
    for it in items:
        candidate = None
        if it.file_ref:
            if not listen.is_file_ref(it.file_ref):
                raise HTTPException(400, "Нужна ссылка file:<provider>:<id>")
            candidate = await listen.file_candidate(it.file_ref)
            if candidate is None:
                raise HTTPException(404, "Файл не найден: повторите поиск")
        q = TrackQuery(artist=it.artist, title=it.title, album=it.album, duration_s=it.duration_s, isrc=it.isrc,
                       explicit=it.explicit, version=it.version)
        meta: dict = {}
        if it.catalog_ref:
            src, id_ = split_ref(it.catalog_ref)
            meta["track_ref"] = it.catalog_ref
            if src == "dz":
                meta["deezer_track_id"] = id_
        have = None if body.force else await find_existing(
            s, artist=q.artist, title=q.title, duration_s=q.duration_s, isrc=q.isrc,
            deezer_track_id=meta.get("deezer_track_id"))
        # a picked file the library has not got is downloaded next to the song's older version (`jobs.downloads`)
        if have and (candidate is None or have.source_url == candidate.page_url):
            owned += 1
        entries.append((q, meta, candidate))
    queued = await create_items(s, batch, entries, force=body.force, star=body.star, favorite_events=favorite_events)

    expanding = bool(track_refs or body.album_ref or body.artist_ref)
    if expanding:
        if body.star and not body.favorite_event_id:
            from app.api.schemas import FavoriteTarget
            from app.favorites import record
            for ref in track_refs:
                event = await record(s, user.id, FavoriteTarget(ref=ref), True, mirror_pending=True,
                                     client="assistant" if origin == "assistant" else "hub",
                                     context="assistant:bulk" if origin == "assistant" else "download")
                favorite_events[ref] = event.event_id
        await enqueue(s, "expand_batch", {
            "batch_id": batch.id,
            "track_refs": track_refs,
            "album_refs": [body.album_ref] if body.album_ref else [],
            "artist_ref": body.artist_ref,
            "record_types": body.record_types,
            "force": body.force,
            "star": body.star,
            "position": len(entries),
            "favorite_events": favorite_events,
        })
    return DownloadBatchOut(batch_id=batch.id, queued=queued + len(joined), already_owned=owned, playlist_id=playlist_id,
                            expanding=expanding)


@router.post("", response_model=DownloadBatchOut)
async def create(body: DownloadIn, user: User = Depends(current_user), s: AsyncSession = Depends(get_session, scope="function")):
    if not (body.items or body.track_refs or body.album_ref or body.artist_ref):
        raise HTTPException(400, "Нечего скачивать")
    origin = "search" if (body.items and all(i.file_ref for i in body.items)) or (
        body.track_refs and all(listen.is_file_ref(ref) for ref in body.track_refs)) else "catalog"
    out = await create_batch(s, user, body, origin=origin)
    await s.commit()
    notify()
    return out


@router.get("", response_model=list[DownloadItemOut])
async def list_items(status: str | None = None, limit: int = Query(100, ge=1, le=500), user: User = Depends(current_user),
                     s: AsyncSession = Depends(get_session, scope="function"),
                     ref: str | None = Query(None, min_length=1, max_length=128)):
    stmt = select(DownloadItem).where(DownloadItem.user_id == user.id)
    if status:
        stmt = stmt.where(DownloadItem.status.in_([x.strip() for x in status.split(",") if x.strip()]))
    if ref is not None:
        matches = DownloadItem.query_json["meta"]["track_ref"].as_string() == ref
        if listen.is_file_ref(ref):
            candidate = await listen.file_candidate(ref)
            if candidate is not None:
                matches = or_(matches, and_(
                    DownloadItem.chosen_candidate_json["provider"].as_string() == candidate.provider,
                    DownloadItem.chosen_candidate_json["provider_id"].as_string() == candidate.provider_id,
                ))
        stmt = stmt.where(matches)
    rows = (await s.execute(stmt.order_by(DownloadItem.id.desc()).limit(limit))).scalars().all()
    links = await item_links(s, list(rows))
    return [item_out(r, links=links[r.id]) for r in rows]


async def _own_item(s: AsyncSession, user: User, item_id: int) -> DownloadItem:
    item = await s.get(DownloadItem, item_id)
    if item is None or item.user_id != user.id:
        raise HTTPException(404, "Загрузка не найдена")
    return item


@router.get("/{item_id}", response_model=DownloadItemOut)
async def get_item(item_id: int, user: User = Depends(current_user), s: AsyncSession = Depends(get_session, scope="function")):
    item = await _own_item(s, user, item_id)
    return item_out(item, detail=True, links=(await item_links(s, [item]))[item.id])


HITMOZ_SONG = re.compile(r"https?://[^/]*hitm[a-z]*\.[a-z]+/song/(\d+)", re.I)
HITMOS_FM_SONG = re.compile(r"https?://[^/]*hitmos\.fm/song/([\w-]+)", re.I)
YOUTUBE = re.compile(r"(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/|music\.youtube\.com/watch\?v=)([\w-]{11})", re.I)
SOUNDCLOUD = re.compile(r"^https?://(?:www\.|m\.)?soundcloud\.com/([\w-]+/[\w-]+)/?(?:[?#].*)?$", re.I)


async def candidate_from_url(url: str, q: TrackQuery) -> Candidate:
    url = url.strip()
    if m := HITMOS_FM_SONG.search(url):
        cand = Candidate(provider="hitmos_fm", provider_id=m.group(1), artist=q.artist, title=q.title, page_url=url)
    elif m := HITMOZ_SONG.search(url):
        cand = Candidate(provider="hitmoz", provider_id=m.group(1), artist=q.artist, title=q.title, page_url=url)
    elif m := YOUTUBE.search(url):
        cand = Candidate(provider="ytdlp", provider_id=m.group(1), artist=q.artist, title=q.title,
                         page_url=f"https://www.youtube.com/watch?v={m.group(1)}")
    elif m := SOUNDCLOUD.search(url):
        # the numeric track id comes with `enrich`; the id is a part of the downloaded file's name
        cand = Candidate(provider="soundcloud", provider_id=m.group(1).replace("/", "-"), artist=q.artist, title=q.title,
                         page_url=f"https://soundcloud.com/{m.group(1)}")
    else:
        raise HTTPException(400, "Ссылка не поддерживается: нужна страница трека hitmoz / hitmos.fm, YouTube "
                                 "или SoundCloud")
    provider = svc.providers.get(cand.provider)
    if provider is None:
        raise HTTPException(400, "Источник отключён")
    try:
        enriched = await provider.enrich(cand)
        if cand.provider == "hitmos_fm" and not enriched.download_url:
            # the song page carries the file link in its RSC payload
            from app.providers.hitmos_fm import parse_rsc_tracks

            html, _ = await provider.fetch_page(f"/song/{cand.provider_id}")  # type: ignore[attr-defined]
            obj = parse_rsc_tracks(html).get(cand.provider_id) or {}
            enriched = enriched.model_copy(update={
                "download_url": obj.get("download"),
                "artist": obj.get("artist") or enriched.artist,
                "title": obj.get("title") or enriched.title,
            })
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Не удалось открыть ссылку: {e}") from e
    if cand.provider not in ("ytdlp", "soundcloud") and not enriched.download_url:
        raise HTTPException(400, "На странице не нашлась ссылка на файл")
    return enriched


@router.post("/items/{item_id}/retry", response_model=DownloadItemOut)
async def retry(item_id: int, body: RetryIn, user: User = Depends(current_user),
                s: AsyncSession = Depends(get_session, scope="function")):
    item = await _own_item(s, user, item_id)
    if item.status in ("searching", "downloading", "tagging", "importing"):
        raise HTTPException(409, "Загрузка ещё идёт")
    if body.candidate_index is not None:
        cands = item.candidates_json or []
        if not 0 <= body.candidate_index < len(cands):
            raise HTTPException(400, "Нет такого варианта")
        item.chosen_candidate_json = cands[body.candidate_index]
    elif body.url:
        q = TrackQuery(**item.query_json["query"])
        item.chosen_candidate_json = (await candidate_from_url(body.url, q)).model_dump(mode="json")
    else:
        item.chosen_candidate_json = None
    item.status = "queued"
    item.error = None
    item.progress = 0
    await enqueue(s, "download_item", {"item_id": item.id})
    await s.commit()
    notify()
    out = item_out(item)
    bus.publish(user.id, "download", out.model_dump(mode="json"))
    return out


@router.post("/items/{item_id}/cancel", response_model=DownloadItemOut)
async def cancel(item_id: int, user: User = Depends(current_user), s: AsyncSession = Depends(get_session, scope="function")):
    item = await _own_item(s, user, item_id)
    if item.status != "done":
        item.status = "cancelled"
        from app.favorites import download_failed
        await download_failed(s, item)
    await s.flush()
    out = item_out(item)
    bus.publish(user.id, "download", out.model_dump(mode="json"))
    return out


@router.post("/clear")
async def clear(user: User = Depends(current_user), s: AsyncSession = Depends(get_session, scope="function")):
    res = await s.execute(
        delete(DownloadItem).where(DownloadItem.user_id == user.id, DownloadItem.status.in_(("done", "cancelled")))
        # Keep the anchors needed to put a reviewed song back in order. They can be cleared after it is resolved.
        .where(~DownloadItem.id.in_(select(PendingPlaylistAdd.download_item_id)))
    )
    return {"removed": res.rowcount or 0}
