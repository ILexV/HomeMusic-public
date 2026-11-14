"""Download pipeline (PLAN 8.3, 8.4): catalog meta → dedupe → providers → fetch → postcheck → tag → import.

Job kinds:
- `expand_batch`  — album/artist/track refs → download items
- `download_item` — one track end to end, ends in `importing` (or `done` if already owned)
- `nd_scan`       — coalesced Navidrome scan, links nd_song_id, completes items, fills playlists
"""

from __future__ import annotations

import asyncio
import logging
import sys
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import DownloadItemOut
from app.catalog.deezer import CatalogNotFound
from app.catalog.models import CatalogAlbum, CatalogTrack, split_ref
from app.catalog.yandex import russian_original
from app.config import get_settings
from app.db import session_scope
from app.events import bus
from app.jobs.queue import PermanentError, enqueue, handler, notify, on_final_failure
from app.library import probe
from app.library.importer import ImportSpec, cleanup_dir, import_file, library_abs, load_import, prepare_import, staging_dir_for
from app.library.navidrome import admin_client
from app.library.owned import find_existing, quiet, star_only
from app.matching.select import (
    classify,
    match_catalog,
    needs_uncensored,
    rank_candidates,
    trusted_original,
    uploaded_in_time,
)
from app.models import DownloadBatch, DownloadItem, Job, PendingPlaylistAdd, Track, User
from app.net import make_client
from app.providers.base import Candidate, ProviderError, TrackQuery, file_ref
from app.services import svc

log = logging.getLogger(__name__)

TERMINAL = ("done", "failed", "cancelled", "needs_review")
FAILED = ("failed", "needs_review")
USER_FLAGS = ("star", "requested", "star_event_id", "star_cancelled", "star_failed", "favorite_client", "favorite_context")
MAX_TRIES_PER_PROVIDER = 3
MAX_SHOWN = 15  # candidates kept on the item for the user to pick from
MAX_SHOWN_REUPLOADS = 5
SCAN_PASSES = 3  # more Navidrome scans after one that left tracks unlinked
MAYBE_CENSORED = "Возможна цензура: копий без цензуры не нашлось"


# --- item state + events ------------------------------------------------------------------


async def item_links(s: AsyncSession, items: list[DownloadItem]) -> dict[int, tuple[str | None, str | None]]:
    """item id -> (Navidrome song id, batch target playlist id) for `item_out`."""
    track_ids = {i.track_id for i in items if i.track_id}
    batch_ids = {i.batch_id for i in items}
    songs = dict((await s.execute(select(Track.id, Track.nd_song_id).where(Track.id.in_(track_ids)))).all())         if track_ids else {}
    playlists = dict((await s.execute(
        select(DownloadBatch.id, DownloadBatch.target_playlist_id).where(DownloadBatch.id.in_(batch_ids)))).all())         if batch_ids else {}
    return {i.id: (songs.get(i.track_id), playlists.get(i.batch_id)) for i in items}


def item_out(item: DownloadItem, *, detail: bool = False,
             links: tuple[str | None, str | None] = (None, None)) -> DownloadItemOut:
    q = (item.query_json or {}).get("query", {})
    meta = (item.query_json or {}).get("meta") or {}
    return DownloadItemOut(
        id=item.id,
        batch_id=item.batch_id,
        status=item.status,
        progress=item.progress or 0,
        artist=q.get("artist", ""),
        title=q.get("title", ""),
        album=meta.get("album") or q.get("album"),
        provider=item.provider,
        error=item.error,
        warnings=item.warnings or [],
        track_id=item.track_id,
        nd_song_id=links[0],
        playlist_id=links[1],
        track_ref=item_ref(item),
        star=quiet(item.query_json),
        liked=bool((item.query_json or {}).get("star")) and not bool((item.query_json or {}).get("star_failed")),
        import_id=((item.query_json or {}).get("import") or {}).get("id"),
        candidates=[Candidate(**c) for c in item.candidates_json or []] if detail else None,
        chosen=Candidate(**item.chosen_candidate_json) if item.chosen_candidate_json else None,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def item_ref(item: DownloadItem) -> str | None:
    """The catalog track the item was made from, or the provider's file it downloads (a `file_ref`)."""
    if ref := ((item.query_json or {}).get("meta") or {}).get("track_ref"):
        return ref
    c = item.chosen_candidate_json
    return file_ref(c["provider"], c["provider_id"]) if c else None


async def update_item(item_id: int, **fields: Any) -> DownloadItem | None:
    async with session_scope() as s:
        item = await s.get(DownloadItem, item_id)
        if item is None:
            return None
        if "query_json" in fields:
            # ♥ / an explicit download may have joined this item meanwhile (`api.downloads.join_active`): the job's
            # copy of query_json predates it
            cur = item.query_json or {}
            fields["query_json"] = {**fields["query_json"], **{k: cur[k] for k in USER_FLAGS if k in cur}}
        for k, v in fields.items():
            setattr(item, k, v)
        if item.status in FAILED and star_only(item.query_json):
            # a download queued only for ♥ that failed is shown to the user as a download from now on (Downloads,
            # notifications): the ♥ doesn't just vanish. It still stars the song once a file is picked.
            item.query_json = {**item.query_json, "requested": True}
        if item.status in (*FAILED, "cancelled"):
            from app.favorites import download_failed
            await download_failed(s, item)
        await s.flush()
        await s.refresh(item)
        if "status" in fields and item.status in TERMINAL:
            await mixes_done(s, [item])
        links = (await item_links(s, [item]))[item.id]
    bus.publish(item.user_id, "download", item_out(item, links=links).model_dump(mode="json"))
    return item


# --- catalog meta ----------------------------------------------------------------------------


def meta_from_catalog(t: CatalogTrack, album: CatalogAlbum | None = None) -> dict[str, Any]:
    m: dict[str, Any] = {
        "track_ref": t.ref,
        "artists": t.artists or [t.artist],
        "album": t.album_title,
        "album_artist": t.album_artist,
        "track_no": t.track_no,
        "disc_no": t.disc_no,
        "isrc": t.isrc,
        "explicit": t.explicit,
        "release_date": t.release_date.isoformat() if t.release_date else None,
        "cover_url": t.cover_url,
        "duration_s": t.duration_s,
    }
    if t.source == "dz":
        m["deezer_track_id"] = t.id
        if t.album_ref:
            m["deezer_album_id"] = split_ref(t.album_ref)[1]
    if album is not None:
        m.update(
            album=album.title,
            album_artist=album.artist,
            record_type=album.record_type,
            release_date=album.release_date.isoformat() if album.release_date else m.get("release_date"),
            year=album.year,
            cover_url=album.cover_url or m.get("cover_url"),
            genre=album.genre,
            upc=album.upc,
            track_total=album.nb_tracks or (len(album.tracks) if album.tracks else None),
        )
        if album.source == "dz":
            m["deezer_album_id"] = album.id
    return {k: v for k, v in m.items() if v is not None}


async def complete_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Fill album details (track number, cover, release type...) for a Deezer track known only by id."""
    if not meta.get("deezer_track_id") or (meta.get("track_no") and meta.get("record_type")) or svc.deezer is None:
        return meta
    try:
        t = await svc.deezer.get_track(meta["deezer_track_id"])
        album = await svc.deezer.get_album(split_ref(t.album_ref)[1]) if t.album_ref else None
    except Exception as e:
        log.warning("deezer meta for %s failed: %r", meta.get("deezer_track_id"), e)
        return meta
    if album and album.tracks:
        hit = next((x for x in album.tracks if x.id == t.id), None)
        if hit:
            t = t.model_copy(update={"track_no": hit.track_no or t.track_no, "disc_no": hit.disc_no or t.disc_no})
    merged = meta_from_catalog(t, album)
    merged.update({k: v for k, v in meta.items() if v is not None and k not in ("track_no", "disc_no")})
    for k in ("track_no", "disc_no"):
        merged[k] = merged.get(k) or meta.get(k)
    return merged


async def resolve_catalog_meta(q: TrackQuery) -> dict[str, Any] | None:
    """Find the track in Deezer; only a confident match is used (download needs a catalog record, PLAN 8.5.3)."""
    t = await resolve_catalog_track(q)
    return meta_from_catalog(t) if t else None


async def resolve_catalog_track(q: TrackQuery) -> CatalogTrack | None:
    if svc.deezer is None:
        return None
    try:
        found = await svc.deezer.search_tracks(q.artist, q.title, limit=10)
    except Exception as e:
        log.warning("deezer search failed: %r", e)
        return None
    if not found:
        return None
    scored = match_catalog(q.artist, q.title, found, album=q.album, duration=q.duration_s, version=q.version)
    cfg = get_settings().cfg.matching
    from app.matching.select import classify_scores

    if classify_scores([sc.total for _, sc in scored], cfg) != "auto":
        return None
    return scored[0][0]


# --- candidates ------------------------------------------------------------------------------


async def fetch_cover(url: str | None) -> bytes | None:
    if not url:
        return None
    try:
        async with make_client(timeout=30) as c:
            r = await c.get(url)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                return r.content
    except httpx.HTTPError as e:
        log.warning("cover download failed %s: %r", url, e)
    return None


def _parse_date(v: Any) -> date | None:
    if not v:
        return None
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


# --- handlers ----------------------------------------------------------------------------------


@handler("download_item")
async def handle_download_item(job: Job) -> None:
    item_id = int(job.payload_json["item_id"])
    async with session_scope() as s:
        item = await s.get(DownloadItem, item_id)
        if item is None or item.status == "cancelled":
            return
        qj = dict(item.query_json or {})
        chosen = item.chosen_candidate_json
        force = item.force
        user_id = item.user_id
        finished = item.status == "done"
        recovering = item.status == "importing" or item.track_id is not None
    journal = get_settings().staging_dir / "dl" / str(item_id) / ".import.json"
    if finished:
        if journal.is_file():
            if ref := (qj.get("meta") or {}).get("track_ref"):
                from app.listen import forget

                await forget(ref)
            cleanup_dir(journal.parent)
        return
    if recovering or journal.is_file():
        await resume_import(item_id, journal)
        return
    q = TrackQuery(**qj["query"])
    meta: dict[str, Any] = dict(qj.get("meta") or {})

    await update_item(item_id, status="searching", progress=0, error=None)

    # 1. catalog record (needed for tags; without it only a user-picked candidate may be downloaded). A picked
    # re-upload is looked up too: names from its title ("Трек – Исполнитель (без цензуры)") make poor tags.
    if not meta and not chosen:
        meta = await resolve_catalog_meta(q) or {}
    elif not meta and (chosen.get("extra") or {}).get("reupload"):
        if t := await resolve_catalog_track(q):
            q, meta = query_from_track(t), meta_from_catalog(t)
            qj["query"] = q.model_dump()
    meta = await complete_meta(meta) if meta else meta
    if meta:
        q = q.model_copy(update={
            "duration_s": q.duration_s or meta.get("duration_s"),
            "isrc": q.isrc or meta.get("isrc"),
            "explicit": q.explicit if q.explicit is not None else meta.get("explicit"),
        })
        qj["meta"] = meta
        qj["query"] = q.model_dump()
        await update_item(item_id, query_json=qj)

    # 2. already in the library?
    if not force:
        async with session_scope() as s:
            existing = await find_existing(
                s, artist=q.artist, title=q.title, duration_s=q.duration_s, isrc=q.isrc,
                deezer_track_id=meta.get("deezer_track_id"),
            )
            existing_id = existing.id if existing else None
            linked = bool(existing and existing.nd_song_id)
            other_file = bool(existing and chosen and existing.source_url != chosen.get("page_url"))
        if existing is not None:
            from app.recording_feedback import library_candidate, rejected_candidates

            rejected = await rejected_candidates(user_id, meta.get("track_ref"))
            if rejected:
                identity = await library_candidate(library_abs(existing.path), nd_song_id=existing.nd_song_id)
                other_file = other_file or identity in rejected
        if other_file:
            # A different user-picked file, or a recording the user rejected, needs a new version (PLAN 8.6).
            # Keep the old file for other users, and move this user's ♥ to the replacement.
            force = True
            qj["replaces"] = existing_id
            await update_item(item_id, query_json=qj)
        elif existing_id:
            item = await update_item(item_id, status="done" if linked else "importing", progress=1,
                                     track_id=existing_id, warnings=["Уже есть в библиотеке"])
            if linked and item is not None:
                async with session_scope() as s:
                    await star_liked(s, [item])
                    await imports_done(s, [item])
            await after_import(user_id, linked)
            return

    # 3. Russian tracks listed in Latin (Deezer translit): Cyrillic originals for the search, Russian source order
    pq = q
    if not chosen:
        q, pq = await russian_original(q, meta.get("upc"))
        if pq is not q:
            qj["query"], qj["provider_query"] = q.model_dump(), pq.model_dump()
            await update_item(item_id, query_json=qj)

    # 4. providers in order: search → rank → try the best files; the next provider is asked only if needed.
    # An explicit Russian track takes only copies known to be uncensored first (from the sources, then re-uploads
    # by small channels); a possibly censored copy is the last resort.
    stage = staging_dir_for("dl", item_id)
    errors: list[str] = []
    all_cands: list[Candidate] = []
    last_reason = ""
    providers = svc.provider_order(pq, for_download=True)
    mcfg = get_settings().cfg.matching
    guard = bool(meta) and not chosen and needs_uncensored(pq)
    held: list[Candidate] = []
    try:
        if chosen:
            ok, last_reason = await try_candidates(item_id, user_id, q, meta, [Candidate(**chosen)], stage, force)
            if ok:
                return
        else:
            from app.listen import listened_candidate

            # the file the user has just listened to (GET /api/listen) goes first: they get what they heard
            heard = await listened_candidate(meta.get("track_ref"), user_id=user_id)
            if heard and meta and (not guard or trusted_original(heard, mcfg)):
                ok, last_reason = await try_candidates(item_id, user_id, q, meta, [heard], stage, force)
                if ok:
                    return
            for p in providers:
                cands, err = await search_provider(p, pq, item_id)
                if err:
                    errors.append(err)
                    continue
                all_cands.extend(cands)
                await update_item(item_id, candidates_json=[c.model_dump(mode="json") for c in all_cands[:MAX_SHOWN]])
                if not meta or not cands:
                    continue  # without a catalog record we only collect candidates for the user (PLAN 8.5.3)
                tries = cands[:MAX_TRIES_PER_PROVIDER]
                if guard:
                    held += [c for c in tries if not trusted_original(c, mcfg)]
                    tries = [c for c in tries if trusted_original(c, mcfg)]
                    if not tries:
                        continue
                ok, reason = await try_candidates(item_id, user_id, q, meta, tries, stage, force)
                if ok:
                    return
                last_reason = reason or last_reason
            if guard:
                if reups := await search_reuploads(pq, item_id, searched=providers):
                    shown = all_cands[:MAX_SHOWN - min(MAX_SHOWN_REUPLOADS, len(reups))] + reups[:MAX_SHOWN_REUPLOADS]
                    all_cands.extend(reups)
                    await update_item(item_id, candidates_json=[c.model_dump(mode="json") for c in shown])
                    ok, reason = await try_candidates(item_id, user_id, q, meta, reups[:MAX_TRIES_PER_PROVIDER],
                                                      stage, force)
                    if ok:
                        return
                    last_reason = reason or last_reason
                if held:
                    ok, reason = await try_candidates(item_id, user_id, q, meta, held, stage, force,
                                                      warning=MAYBE_CENSORED)
                    if ok:
                        return
                    last_reason = reason or last_reason
    finally:
        # A failed DB commit may have already moved the file. Its intent must survive the retry.
        if sys.exc_info()[0] is None and not (stage / ".import.json").exists():
            cleanup_dir(stage)

    if chosen:
        await update_item(item_id, status="needs_review", error=f"Файл не подошёл ({last_reason})")
    elif not all_cands:
        if errors and len(errors) >= len(providers):
            raise RuntimeError("все источники недоступны: " + "; ".join(errors))
        await update_item(item_id, status="needs_review", error="Не найдено ни в одном источнике")
    elif not meta:
        sure = classify(all_cands, get_settings().cfg.matching) == "auto"
        await update_item(item_id, status="needs_review",
                          error="Нет записи в каталоге — " + ("подтвердите файл" if sure else "выберите файл"))
    else:
        await update_item(item_id, status="needs_review", error=f"Ни один кандидат не подошёл ({last_reason})")


async def search_provider(p, q: TrackQuery, item_id: int, *, limit: int = 15,
                          enrich_top_n: int | None = None) -> tuple[list[Candidate], str | None]:
    """Search one provider, rank its results and enrich the top ones (hitmoz: Explicit, real bitrate).
    Listening asks for fewer results and enrichments (`limit`, `enrich_top_n`): it has to start fast.
    Files uploaded after the source's cutoff (`sources.uploaded_before`) are dropped."""
    cfg = get_settings().cfg.matching
    cutoffs = get_settings().cfg.sources.uploaded_before
    n_enrich = cfg.enrich_top_n if enrich_top_n is None else enrich_top_n
    try:
        raw = await p.search(q, limit=limit)
    except ProviderError as e:
        return [], f"{p.name}: {e}"
    except Exception as e:
        log.warning("provider %s search failed: %r", p.name, e, extra={"item_id": item_id})
        return [], f"{p.name}: {e!r}"
    ranked = rank_candidates(q, [c for c in raw if uploaded_in_time(c, cutoffs)], cfg)
    if ranked and n_enrich:
        top = ranked[:n_enrich]
        enriched = await asyncio.gather(*(p.enrich(c) for c in top), return_exceptions=True)
        top = [e if isinstance(e, Candidate) else c for c, e in zip(top, enriched, strict=True)]
        # the track page may tell the upload date the search didn't
        ranked = rank_candidates(q, [c for c in top if uploaded_in_time(c, cutoffs)] + ranked[n_enrich:], cfg)
    return ranked, None


async def search_reuploads(q: TrackQuery, item_id: int, *, searched: list | None = None) -> list[Candidate]:
    """Re-uploads by small channels known to be uncensored (titled so, or uploaded before the law), best first.
    Sources in `searched` (the regular order already had them) are not asked again."""
    cfg = get_settings().cfg.matching
    providers = [p for p in svc.reupload_providers() if p not in (searched or [])]
    found = await asyncio.gather(*(search_provider(p, q, item_id) for p in providers))
    for _, err in found:
        if err:
            log.info("re-uploads: %s", err, extra={"item_id": item_id})
    return rank_candidates(q, [c for cands, _ in found for c in cands if trusted_original(c, cfg)], cfg)


async def try_candidates(item_id: int, user_id: int, q: TrackQuery, meta: dict[str, Any], cands: list[Candidate],
                         stage: Path, force: bool, *, warning: str | None = None) -> tuple[bool, str]:
    """Download candidates one by one until one passes the post-download check and is imported. `warning` goes
    to the imported track's warnings."""
    pcfg = get_settings().cfg
    last_reason = ""
    from app.recording_feedback import rejected_candidates

    rejected = await rejected_candidates(user_id, meta.get("track_ref") or "")
    for cand in cands:
        if (cand.provider, cand.provider_id) in rejected:
            last_reason = "Файл отмечен как неверная запись"
            continue
        provider = svc.providers.get(cand.provider)
        if provider is None:
            continue
        await update_item(item_id, status="downloading", provider=cand.provider,
                          chosen_candidate_json=cand.model_dump(mode="json"), progress=0)
        last_pub = [0.0]
        loop = asyncio.get_running_loop()

        def on_progress(frac: float, _last=last_pub, _loop=loop) -> None:
            # may be called from a worker thread (yt-dlp)
            if frac - _last[0] >= 0.05 or frac >= 1:
                _last[0] = frac
                _loop.call_soon_threadsafe(
                    lambda f=round(frac, 3): _loop.create_task(update_item(item_id, progress=f))
                )

        try:
            path = await provider.fetch(cand, stage, on_progress=on_progress)  # type: ignore[call-arg]
        except Exception as e:
            last_reason = f"{cand.provider}: {e}"
            log.warning("fetch failed %s: %r", cand.page_url, e, extra={"item_id": item_id})
            continue
        check = await probe.postcheck(path, q.duration_s or cand.duration_s, pcfg.matching.duration_tolerance_s,
                                      pcfg.postcheck.silence_warn_s)
        if not check.ok:
            last_reason = f"{cand.provider}: {check.reason}"
            path.unlink(missing_ok=True)
            continue
        rejected = await rejected_candidates(user_id, meta.get("track_ref"))
        if (cand.provider, cand.provider_id) in rejected:
            last_reason = "Файл отмечен как неверная запись"
            path.unlink(missing_ok=True)
            continue
        await import_downloaded(item_id, user_id, q, meta, cand, path, check, force, warning=warning)
        return True, ""
    return False, last_reason


async def import_downloaded(item_id: int, user_id: int, q: TrackQuery, meta: dict[str, Any], cand: Candidate,
                            path: Path, check: Any, force: bool, *, warning: str | None = None) -> None:
    await update_item(item_id, status="tagging", progress=1)
    cover = await fetch_cover(meta.get("cover_url") or cand.cover_url)
    release_date = _parse_date(meta.get("release_date"))
    single = not meta.get("album")
    spec = ImportSpec(
        artist=q.artist,
        title=q.title,
        artists=meta.get("artists") or [],
        album=meta.get("album") or q.title,
        album_artist=meta.get("album_artist") or q.artist,
        year=meta.get("year") or (release_date.year if release_date else cand.year),
        release_date=release_date,
        release_type={"compile": "compilation"}.get(meta.get("record_type", ""), meta.get("record_type"))
        or ("single" if single else "album"),
        track_no=meta.get("track_no") or (1 if single else None),
        track_total=meta.get("track_total"),
        disc_no=meta.get("disc_no") or 1,
        genre=meta.get("genre"),
        isrc=meta.get("isrc") or q.isrc,
        explicit=meta.get("explicit") if meta.get("explicit") is not None else cand.explicit,
        deezer_track_id=meta.get("deezer_track_id"),
        deezer_album_id=meta.get("deezer_album_id"),
        upc=meta.get("upc"),
        cover=cover,
        source=cand.provider,
        source_url=cand.page_url,
        source_upload_date=cand.upload_date,
        duration_s=round(check.info.duration_s) if check.info and check.info.duration_s else q.duration_s,
        bitrate_kbps=check.info.bitrate_kbps if check.info else cand.bitrate_kbps,
        codec=check.info.codec if check.info else None,
        warnings=list(check.warnings or []) + ([warning] if warning else []),
        added_by=user_id,
        force=force,
    )
    journal = staging_dir_for("dl", item_id) / ".import.json"
    await asyncio.to_thread(prepare_import, journal, path, spec)
    await update_item(item_id, status="importing")
    await resume_import(item_id, journal)


async def resume_import(item_id: int, journal: Path) -> None:
    """Commit the track, download link and scan intent together; replay filesystem work from its journal."""
    prepared = await asyncio.to_thread(load_import, journal) if journal.is_file() else None
    async with session_scope() as s:
        item = await s.get(DownloadItem, item_id)
        if item is None or item.status == "cancelled":
            return
        track = await s.get(Track, item.track_id) if item.track_id else None
        if track is None and prepared:
            path, spec = prepared
            track = await import_file(s, path, spec, journal=journal)
            item.warnings = spec.warnings
        if track is None:
            # Old (pre-journal) interrupted imports may already have committed their Track row.
            qj = item.query_json or {}
            q, meta = qj.get("query") or {}, qj.get("meta") or {}
            candidate = item.chosen_candidate_json or {}
            existing = await find_existing(s, artist=q.get("artist", ""), title=q.get("title", ""),
                                           duration_s=q.get("duration_s"), isrc=q.get("isrc"),
                                           deezer_track_id=meta.get("deezer_track_id"))
            if existing and existing.source_url == candidate.get("page_url") and not item.force:
                track = existing
        if track is None:
            # There is no reliable way to attribute a legacy orphan file to this download.
            missing = True
        else:
            missing = False
            item.track_id, item.progress, item.error = track.id, 1, None
            if item.status != "done":
                item.status = "importing"
                await enqueue_scan(s)
            await s.flush()
            track_id, user_id = track.id, item.user_id
            ref = ((item.query_json or {}).get("meta") or {}).get("track_ref")
            links = (await item_links(s, [item]))[item.id]
    if missing:
        await update_item(item_id, status="needs_review", error="Прерванный импорт без журнала: проверьте библиотеку")
        return
    notify()
    bus.publish(user_id, "download", item_out(item, links=links).model_dump(mode="json"))
    bus.publish(user_id, "library", {"kind": "imported", "track_id": track_id})
    if ref:
        from app.listen import forget

        await forget(ref)
    cleanup_dir(journal.parent)


async def after_import(user_id: int, linked: bool) -> None:
    async with session_scope() as s:
        if linked:
            await process_playlist_adds(s)
        else:
            await enqueue_scan(s)
    notify()


@on_final_failure("download_item")
async def _download_failed(job: Job, exc: Exception) -> None:
    item_id = int(job.payload_json["item_id"])
    async with session_scope() as s:
        item = await s.get(DownloadItem, item_id)
        if item is None or item.status in ("done", "cancelled"):
            return
        if item.track_id:
            # A notification/cache error after the import commit is not a failed download.
            item.status = "importing"
            await enqueue_scan(s)
            notify()
            return
    await update_item(item_id, status="failed", error=f"{exc}"[:500])
    async with session_scope() as s:
        await process_playlist_adds(s)


# --- scan + link + playlists ---------------------------------------------------------------------


async def enqueue_scan(s: AsyncSession) -> None:
    pending = (
        await s.execute(select(Job.id).where(Job.kind == "nd_scan", Job.status == "queued").limit(1))
    ).scalar_one_or_none()
    if pending is None:
        await enqueue(s, "nd_scan", {}, delay_s=1)


_scan_lock = asyncio.Lock()


@handler("nd_scan")
async def handle_scan(job: Job) -> None:
    async with _scan_lock:
        nd = admin_client()
        try:
            await nd.scan_and_wait(poll_s=get_settings().nd_scan_poll_s)
            linked = await link_unlinked(nd)
        finally:
            await nd.aclose()
    log.info("navidrome scan done, linked %s tracks", linked)
    async with session_scope() as s:
        # items whose track is now visible in Navidrome are done
        rows = (
            await s.execute(
                select(DownloadItem, Track.nd_song_id)
                .join(Track, Track.id == DownloadItem.track_id)
                .where(DownloadItem.status == "importing")
            )
        ).all()
        done_items = []
        for item, nd_id in rows:
            if nd_id:
                item.status = "done"
                done_items.append(item)
        await s.flush()
        links = await item_links(s, done_items)
        for item in done_items:
            bus.publish(item.user_id, "download", item_out(item, links=links[item.id]).model_dump(mode="json"))
        await star_liked(s, done_items)
        await imports_done(s, done_items)
        await mixes_done(s, done_items)
        await process_playlist_adds(s)
        still = (
            await s.execute(select(func.count()).select_from(Track).where(Track.nd_song_id.is_(None)))
        ).scalar_one()
    if not still:
        return
    # Navidrome may need another pass (file just landed while the scan was running) — a few, not forever: each pass is
    # a new job (its `attempts` start at 0), so the passes are counted in the payload. A file landing later enqueues
    # a scan of its own.
    passes = int((job.payload_json or {}).get("pass") or 0)
    if passes < SCAN_PASSES:
        async with session_scope() as s:
            await enqueue(s, "nd_scan", {"pass": passes + 1}, delay_s=10)
        notify()
    else:
        log.warning("navidrome scan: %s tracks still not found in Navidrome", still)


def _same_path(nd_path: str | None, rel: str) -> bool:
    """Navidrome reports the real path with its music folder prefix (e.g. `/music/Artist/...`)."""
    if not nd_path:
        return False
    nd_path, rel = _nfc(nd_path), _nfc(rel)
    return nd_path == rel or nd_path.endswith("/" + rel)


async def link_unlinked(nd) -> int:
    """Find Navidrome song ids for tracks that don't have one yet (search3 by title, match by real path)."""
    async with session_scope() as s:
        tracks = (await s.execute(select(Track).where(Track.nd_song_id.is_(None)).limit(500))).scalars().all()
        n = 0
        for t in tracks:
            hit = None
            # NFC: Navidrome finds nothing by a decomposed title («и» + U+0306 for «й»); its album and artist are
            # asked next for a title it still can't find
            for i, q in enumerate(dict.fromkeys(_nfc(x) for x in (t.title, t.album, t.artist) if x and x.strip())):
                try:
                    res = await nd.search3(q, song_count=100)
                except Exception as e:
                    log.warning("search3 failed for track %s: %r", t.id, e)
                    continue
                songs = res.get("song", [])
                hit = next((x for x in songs if _same_path(x.get("path"), t.path)), None)
                if hit is None and i == 0:
                    hit = next(
                        (
                            x for x in songs
                            if _nfc(x.get("title")) == _nfc(t.title) and _nfc(x.get("album")) == _nfc(t.album)
                            and (t.duration_s is None or abs((x.get("duration") or 0) - t.duration_s) <= 2)
                            and not any(o.nd_song_id == x["id"] for o in tracks)
                        ),
                        None,
                    )
                if hit:
                    break
            if hit:
                t.nd_song_id = hit["id"]
                n += 1
        return n


def _nfc(text: str | None) -> str:
    return unicodedata.normalize("NFC", text or "")


async def imports_done(s: AsyncSession, items: list[DownloadItem]) -> None:
    if any((i.query_json or {}).get("import") for i in items):
        from app.imports.core import downloads_done

        await downloads_done(s, items)


async def mixes_done(s: AsyncSession, items: list[DownloadItem]) -> None:
    if any((i.query_json or {}).get("mix") for i in items):
        from app.mixes.core import downloads_changed

        await downloads_changed(s, items)


async def star_liked(s: AsyncSession, items: list[DownloadItem]) -> None:
    """Items queued with ♥ (`DownloadIn.star`) are done: star their songs in Navidrome for the user who asked. A
    picked file imported next to an older version of the song (`replaces`) takes the ♥ off that version."""
    from app.auth import user_navidrome

    liked = [i for i in items if i.track_id]
    if not liked:
        return
    old = {i.id: i.query_json["replaces"] for i in liked if i.query_json.get("replaces")}
    songs = dict((await s.execute(select(Track.id, Track.nd_song_id).where(
        Track.id.in_({i.track_id for i in liked} | set(old.values()))))).all())
    by_user: dict[int, list[str]] = {}
    off_by_user: dict[int, list[str]] = {}
    for i in liked:
        if nd_id := songs.get(i.track_id):
            from app.favorites import record, remember, snapshot
            from app.api.schemas import FavoriteTarget
            from app.models import PlayEvent

            i = await s.get(DownloadItem, i.id, populate_existing=True)
            if i is None:
                continue
            if not i.query_json.get("star") or i.query_json.get("star_cancelled"):
                continue
            ref = item_ref(i)
            target = FavoriteTarget(ref=ref, nd_song_id=nd_id, target_data=i.query_json.get("query"))
            extra = [f"nd:{songs[old[i.id]]}"] if i.id in old and songs.get(old[i.id]) else []
            original = (await s.execute(select(PlayEvent).where(PlayEvent.user_id == i.user_id,
                PlayEvent.event_id == i.query_json.get("star_event_id")))).scalar_one_or_none()
            if original and original.ref:
                extra.append(f"ref:{original.ref}")  # a picked provider file may acquire a catalog ref later
            await remember(s, target, extra_aliases=extra)
            states = await snapshot(s, i.user_id)
            state = next((x for x in states.values() if x.target_type == "track" and
                          (x.nd_id == nd_id or ref and x.ref == ref)), None)
            if state and not state.liked:
                # A terminal failure rolls back the promise, but a successful manual retry may restore it.
                if original is None or state.event.event_id != original.event_id:
                    continue
                event = await record(s, i.user_id, target, True, operation=f"retry:{original.event_id}",
                                     client="hub", context="download:retry", mirror_pending=True)
                i.query_json = {**i.query_json, "star_event_id": event.event_id}
            i.query_json = {**i.query_json, "star_failed": False}
            by_user.setdefault(i.user_id, []).append(nd_id)
            if (off := songs.get(old.get(i.id))) and off != nd_id:
                off_by_user.setdefault(i.user_id, []).append(off)
    for user_id, song_ids in by_user.items():
        user = await s.get(User, user_id)
        if user is None:
            continue
        nd = user_navidrome(user)
        try:
            await nd.star(song_ids=song_ids)
            from app.favorites import snapshot
            for state in (await snapshot(s, user_id)).values():
                if state.target_type == "track" and state.nd_id in song_ids:
                    state.event.mirror_pending = False
            bus.publish(user_id, "library", {"kind": "starred", "song_ids": song_ids})
            if off := off_by_user.get(user_id):
                await nd.unstar(song_ids=off)
                bus.publish(user_id, "library", {"kind": "unstarred", "song_ids": off})
        except Exception as e:
            log.warning("star after download failed for user %s: %r", user_id, e)
        finally:
            await nd.aclose()


def insert_playlist_songs(current: list[str], entries: list[tuple[str | None, str]]) -> list[str]:
    """Insert newly ready songs before their next persisted anchor, preserving unrelated playlist entries."""
    songs = list(current)
    cursor = len(songs)
    for applied, song in reversed(entries):
        if applied:
            # Search backwards: the playlist may already have contained another copy of this song.
            cursor = next((i for i in range(cursor - 1, -1, -1) if songs[i] == applied), cursor)
        else:
            songs.insert(cursor, song)
    return songs


async def playlist_adds_loop() -> None:
    """Recover pending additions on startup and retry Navidrome failures without another download or scan."""
    while True:
        try:
            async with session_scope() as s:
                await process_playlist_adds(s)
        except Exception:
            log.exception("pending playlist additions failed")
        await asyncio.sleep(30)


async def process_playlist_adds(s: AsyncSession) -> None:
    """Wait for active downloads, pass manual review, and insert a reviewed song back into its requested place."""
    from app.auth import user_navidrome

    # Serialize readers through the transaction commit, including concurrent scan/download completions.
    # An asyncio lock released here would let the next caller see uncommitted additions and add them twice.
    await s.execute(update(PendingPlaylistAdd).where(PendingPlaylistAdd.id == -1).values(position=0))
    rows = (
        await s.execute(
            select(PendingPlaylistAdd, DownloadItem, Track)
            .join(DownloadItem, DownloadItem.id == PendingPlaylistAdd.download_item_id)
            .outerjoin(Track, Track.id == DownloadItem.track_id)
            .order_by(PendingPlaylistAdd.playlist_id, DownloadItem.batch_id,
                      PendingPlaylistAdd.position, PendingPlaylistAdd.id)
        )
    ).all()
    groups: dict[tuple[int, str], list[tuple[PendingPlaylistAdd, DownloadItem, Track | None]]] = {}
    for add, item, track in rows:
        groups.setdefault((add.user_id, add.playlist_id), []).append((add, item, track))
    for (user_id, playlist_id), entries in groups.items():
        ready: list[tuple[PendingPlaylistAdd, str]] = []
        for add, item, track in entries:
            if add.applied_song_id:
                ready.append((add, add.applied_song_id))
                continue
            if item.status in ("failed", "cancelled", "needs_review"):
                continue
            if track is not None and track.nd_song_id:
                ready.append((add, track.nd_song_id))
                continue
            break  # active downloads still finish in order
        new = [(add, song) for add, song in ready if not add.applied_song_id]
        if new:
            user = await s.get(User, user_id)
            if user is None:
                continue
            nd = user_navidrome(user)
            try:
                if any(add.applied_song_id for add, _ in ready):
                    current = [e["id"] for e in (await nd.get_playlist(playlist_id)).get("entry", [])]
                    songs = insert_playlist_songs(current, [(a.applied_song_id, song) for a, song in ready])
                    await nd.replace_playlist_songs(playlist_id, songs)
                else:
                    for k in range(0, len(new), 300):
                        await nd.update_playlist(playlist_id, add=[song for _, song in new[k:k + 300]])
                        # Flush each successful chunk so a later chunk's failure cannot duplicate it on retry.
                        for add, song in new[k:k + 300]:
                            add.applied_song_id = song
                        await s.flush()
            except Exception as e:
                log.warning("playlist %s add failed: %r", playlist_id, e)
                continue
            finally:
                await nd.aclose()
            for add, song in new:
                add.applied_song_id = song
        blocked = False
        for add, item, _ in entries:
            if item.status in ("failed", "cancelled") or (add.applied_song_id and not blocked):
                await s.delete(add)
            elif not add.applied_song_id:
                blocked = True
    await s.flush()


# --- batch expansion ------------------------------------------------------------------------------


async def create_items(
    s: AsyncSession,
    batch: DownloadBatch,
    entries: list[tuple[TrackQuery, dict[str, Any], Candidate | None]],
    *,
    force: bool = False,
    star: bool = False,
    start_position: int = 0,
    favorite_events: dict[str, str] | None = None,
) -> int:
    for i, (q, meta, cand) in enumerate(entries):
        query_json: dict[str, Any] = {"query": q.model_dump(), "meta": meta}
        if star:
            query_json["star"] = True  # ♥: star the song once it is in Navidrome (`star_liked`)
            if batch.origin == "assistant":
                query_json.update(favorite_client="assistant", favorite_context="assistant:bulk" if len(entries) > 1 else "assistant")
        item = DownloadItem(
            batch_id=batch.id,
            user_id=batch.user_id,
            query_json=query_json,
            chosen_candidate_json=cand.model_dump(mode="json") if cand else None,
            force=force,
        )
        s.add(item)
        await s.flush()
        if star:
            from app.favorites import download_requested, snapshot
            ref = item_ref(item)
            if favorite_events and ref in favorite_events:
                current = await snapshot(s, batch.user_id)
                state = next((x for x in current.values() if x.ref == ref and x.target_type == "track"), None)
                item.query_json = {**item.query_json, "star_event_id": favorite_events[ref],
                                   "star": bool(state and state.liked), "star_cancelled": not bool(state and state.liked)}
                if state is None or not state.liked:
                    # This quiet download was cancelled before expansion finished.
                    item.status = "cancelled"
            else:
                await download_requested(s, item)
        if batch.target_playlist_id:
            s.add(PendingPlaylistAdd(playlist_id=batch.target_playlist_id, user_id=batch.user_id,
                                     download_item_id=item.id, position=start_position + i))
        if item.status != "cancelled":
            await enqueue(s, "download_item", {"item_id": item.id})
    return len(entries)


def query_from_track(t: CatalogTrack) -> TrackQuery:
    return TrackQuery(artist=t.artist, title=t.title, album=t.album_title, duration_s=t.duration_s, isrc=t.isrc,
                      explicit=t.explicit)


async def get_catalog_album(ref: str) -> CatalogAlbum:
    src, id_ = split_ref(ref)
    if src == "dz" and svc.deezer:
        return await svc.deezer.get_album(id_)
    if src == "hm" and svc.hitmoz_catalog:
        return await svc.hitmoz_catalog.get_album(id_)
    raise PermanentError(f"unknown album ref {ref}")


@handler("expand_batch")
async def handle_expand_batch(job: Job) -> None:
    p = job.payload_json
    album_refs: list[str] = list(p.get("album_refs") or [])
    if p.get("artist_ref"):
        src, id_ = split_ref(p["artist_ref"])
        types = set(p.get("record_types") or ["album", "single", "ep"])
        page = await (svc.deezer.get_artist_page(id_) if src == "dz" else svc.hitmoz_catalog.get_artist(id_))
        # oldest first, so singles that are later included in an album don't win the album slot
        album_refs += [a.ref for a in sorted(page.albums, key=lambda a: a.release_date or date.min)
                       if a.record_type in types]
    entries: list[tuple[TrackQuery, dict[str, Any], Candidate | None]] = []
    seen: set[str] = set()
    for ref in album_refs:
        try:
            album = await get_catalog_album(ref)
        except (CatalogNotFound, PermanentError) as e:  # one dead ref must not sink the whole batch
            log.warning("expand_batch %s: skip album %s: %r", p["batch_id"], ref, e)
            continue
        for t in album.tracks or []:
            key = t.isrc or f"{t.artist}|{t.title}".casefold()
            if key in seen:
                continue
            seen.add(key)
            entries.append((query_from_track(t), meta_from_catalog(t, album), None))
    for ref in p.get("track_refs") or []:
        src, id_ = split_ref(ref)
        if src != "dz" or svc.deezer is None:
            continue
        try:
            t = await svc.deezer.get_track(id_)
        except CatalogNotFound as e:
            log.warning("expand_batch %s: skip track %s: %r", p["batch_id"], ref, e)
            continue
        entries.append((query_from_track(t), meta_from_catalog(t), None))
    async with session_scope() as s:
        batch = await s.get(DownloadBatch, int(p["batch_id"]))
        if batch is None:
            return
        await create_items(s, batch, entries, force=bool(p.get("force")), star=bool(p.get("star")),
                           start_position=int(p.get("position", 0)), favorite_events=p.get("favorite_events"))
    notify()
    bus.publish(batch.user_id, "batch", {"batch_id": batch.id, "queued": len(entries)})
