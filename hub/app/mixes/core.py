"""«Микс дня» in the Hub (MIXES.md 6): the daily build, quiet downloads, the Navidrome playlist, the replacement.

Job kinds (background: below the user's downloads, `PRIORITY_MIX`):
- `mix_build` — profile → `build.build` → a new mix: its songs, downloads of those the library lacks, the playlist;
  the user's previous mix is closed and its songs downloaded only for it and never listened to go (6.3);
- `mix_sync`  — one mix after downloads finished or failed: a song that did not download gives its place to the
  best of the reserve, the playlist gets the songs that are in the library now.

Downloads of a mix are the Hub's own (`query_json["mix"]`, `library.owned.quiet`): not in «Загрузки», no
notifications, background network priority. The playlist «Микс дня» is one per user (`MixSettings.nd_playlist_id`):
its songs are replaced, its id stays, so the app and «Keep synced» follow it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog.models import CatalogTrack, split_ref
from app.config import get_settings
from app.db import session_scope
from app.jobs.queue import PRIORITY_IMPORT, enqueue, handler, notify
from app.library.owned import ACTIVE_STATUSES, quiet
from app.mixes import build as builder
from app.mixes.taste import build_profile
from app.models import (
    DownloadBatch,
    DownloadItem,
    ImportItem,
    Job,
    Mix,
    MixItem,
    MixSettings,
    PlayEvent,
    Track,
    User,
    utcnow,
)

log = logging.getLogger(__name__)

PRIORITY_MIX = PRIORITY_IMPORT
PLAYLIST_NAME = "Микс дня"
SYNC_DELAY_S = 20  # downloads coming in one after another ask for one sync
CHECK_EVERY_S = 300
LISTENED_S = 30
FAILED = ("failed", "needs_review", "cancelled")
_sync_lock = asyncio.Lock()


# --- settings ---------------------------------------------------------------------------------------------------


async def settings_of(s: AsyncSession, user_id: int) -> MixSettings:
    row = await s.get(MixSettings, user_id)
    if row is None:
        row = MixSettings(user_id=user_id, mode="normal", mix_hour=get_settings().cfg.mixes.default_hour,
                          artists_json=[])
        s.add(row)
        await s.flush()
    return row


async def current_mix(s: AsyncSession, user_id: int) -> Mix | None:
    return (await s.execute(select(Mix).where(Mix.user_id == user_id, Mix.status == "active")
                            .order_by(Mix.id.desc()).limit(1))).scalar_one_or_none()


async def enqueue_build(s: AsyncSession, user_id: int, *, priority: int = PRIORITY_MIX) -> bool:
    """One queued or running build per user. False: one is under way."""
    busy = (await s.execute(select(Job.id).where(
        Job.kind == "mix_build", Job.status.in_(("queued", "running")),
        func.json_extract(Job.payload_json, "$.user_id") == user_id).limit(1))).scalar_one_or_none()
    if busy is not None:
        return False
    await enqueue(s, "mix_build", {"user_id": user_id}, priority=priority, max_attempts=3)
    return True


async def enqueue_sync(s: AsyncSession, mix_id: int, *, delay_s: float = SYNC_DELAY_S) -> None:
    pending = (await s.execute(select(Job.id).where(
        Job.kind == "mix_sync", Job.status == "queued",
        func.json_extract(Job.payload_json, "$.mix_id") == mix_id).limit(1))).scalar_one_or_none()
    if pending is None:
        await enqueue(s, "mix_sync", {"mix_id": mix_id}, delay_s=delay_s, priority=PRIORITY_MIX)


async def downloads_changed(s: AsyncSession, items: list[DownloadItem]) -> None:
    """Downloads of mixes finished or failed (`jobs.downloads`): their mixes are synced soon."""
    for mix_id in {int(m["id"]) for i in items if (m := (i.query_json or {}).get("mix"))}:
        await enqueue_sync(s, mix_id)


# --- the build --------------------------------------------------------------------------------------------------


@handler("mix_build")
async def handle_build(job: Job) -> None:
    await build_for(int(job.payload_json["user_id"]))


async def build_for(user_id: int) -> Mix | None:
    """Builds the user's mix of today and puts it in place of the previous one. None: nothing to build it from."""
    from app.plays import import_scrobbles

    async with session_scope() as s:
        user = await s.get(User, user_id)
        if user is None:
            return None
        mode = (await settings_of(s, user_id)).mode
    try:
        await import_scrobbles(user)  # the freshest history (Navidrome's scrobbles come hourly otherwise)
    except Exception as e:
        log.info("mix %s: scrobbles not synced: %r", user_id, e)
    cfg = get_settings().cfg.mixes
    shadow_snapshot = None
    shadow_error = None
    shadow_now = utcnow() if cfg.taste_shadow else None
    if cfg.taste_shadow:
        try:
            from app.mixes.taste_snapshot import load_snapshot
            async with session_scope() as s:
                shadow_snapshot = await load_snapshot(s, user.id, now=shadow_now)
        except Exception:
            shadow_error = "snapshot_failed"
            log.warning("mix shadow snapshot failed", exc_info=True)
    async with session_scope() as s:
        profile = await build_profile(s, user, model=cfg.taste_model)
    if profile.empty:
        log.info("mix: user %s has no history and no picked artists yet", user_id)
        return None
    pool = []
    built = await builder.build(profile, mode, shadow_pool=pool) if shadow_snapshot is not None else await builder.build(profile, mode)
    if cfg.taste_shadow:
        if shadow_snapshot is not None:
            try:
                from app.mixes.shadow import compare, sound_diff
                built.stats["taste_shadow"] = compare(shadow_snapshot, pool, now=shadow_now,
                                                      timezone=get_settings().timezone, cfg=cfg)
                built.stats["taste_shadow"]["active_seed_keys"] = built.stats.get("seed_keys", [])[:100]
                built.stats["taste_shadow"]["pool_complete"] = built.stats.get("candidates", 0) == len(pool)
                try:
                    await sound_diff(built.stats["taste_shadow"], shadow_snapshot, pool, now=shadow_now,
                                     timezone=get_settings().timezone, cfg=cfg, mode=mode)
                except Exception:
                    built.stats["taste_shadow"]["sound_status"] = "unavailable"
                    log.warning("mix shadow sound comparison failed", exc_info=True)
            except Exception:
                shadow_error = "comparison_failed"
                log.warning("mix shadow comparison failed", exc_info=True)
        if shadow_error:
            built.stats["taste_shadow"] = {"schema": 1, "error": shadow_error}
    if not built.active:
        log.warning("mix: user %s: no songs (%s)", user_id, built.stats.get("generators"))
        return None
    async with session_scope() as s:
        mix = await save(s, user, built)
        mix_id = mix.id
        await enqueue_sync(s, mix_id, delay_s=0)
    try:
        await sync(mix_id)
    except Exception:
        # The local mix is already committed; its durable sync job retries publication, not the build.
        log.warning("mix %s: publication deferred", mix_id, exc_info=True)
    await close_previous(user_id, mix_id)
    log.info("mix %s of user %s: %s songs, %s", mix_id, user_id, len(built.active), built.stats)
    async with session_scope() as s:
        return await s.get(Mix, mix_id)


async def save(s: AsyncSession, user: User, built: builder.Built) -> Mix:
    batch = DownloadBatch(user_id=user.id, origin="mix", title=PLAYLIST_NAME)
    s.add(batch)
    await s.flush()
    mix = Mix(user_id=user.id, title=built.title, description=built.description, mode=built.stats.get("mode", "normal"),
              status="active", batch_id=batch.id, stats_json=built.stats, created_at=utcnow())
    s.add(mix)
    await s.flush()
    for pos, c in enumerate(built.active + built.reserve):
        s.add(MixItem(mix_id=mix.id, position=pos, state="active" if pos < len(built.active) else "reserve",
                      familiar=c.familiar, ref=c.track.ref if c.track.source == "dz" else None,
                      nd_song_id=c.nd_song_id, track_id=c.library_track_id, generator=c.generator,
                      score=round(c.score, 4), features_json=c.features()))
    await s.flush()
    return mix


async def queue_download(s: AsyncSession, mix: Mix, it: MixItem) -> None:
    """A song of the mix the library lacks: joins a download under way (an import's, another mix's), else a quiet
    download of its own (`downloaded_for_mix`)."""
    if it.track_id or it.download_item_id or not it.ref:
        return
    active = (await s.execute(select(DownloadItem).where(
        DownloadItem.status.in_(ACTIVE_STATUSES),
        func.json_extract(DownloadItem.query_json, "$.meta.track_ref") == it.ref).limit(1))).scalar_one_or_none()
    if active is not None:
        it.download_item_id = active.id
        it.downloaded_for_mix = bool((active.query_json or {}).get("mix")) and quiet(active.query_json)
        return
    t = CatalogTrack.model_validate((it.features_json or {}).get("track") or {})
    from app.jobs.downloads import query_from_track

    meta: dict[str, Any] = {"track_ref": it.ref, "artists": t.artists or [t.artist]}
    src, id_ = split_ref(it.ref)
    if src == "dz":
        meta["deezer_track_id"] = id_
    item = DownloadItem(batch_id=mix.batch_id, user_id=mix.user_id, query_json={
        "query": query_from_track(t).model_dump(), "meta": meta, "mix": {"id": mix.id, "item": it.id}})
    s.add(item)
    await s.flush()
    await enqueue(s, "download_item", {"item_id": item.id}, priority=PRIORITY_MIX)
    it.download_item_id, it.downloaded_for_mix = item.id, True


@handler("mix_sync")
async def handle_sync(job: Job) -> None:
    await sync(int(job.payload_json["mix_id"]), schedule=False)


async def sync(mix_id: int, *, schedule: bool = True) -> None:
    async with _sync_lock:
        await _sync(mix_id, schedule=schedule)


async def _sync(mix_id: int, *, schedule: bool) -> None:
    """Commit the local composition and outbox before any Navidrome write."""
    async with session_scope() as s:
        mix = await s.get(Mix, mix_id)
        if mix is None or mix.status != "active":
            return
        items = list((await s.execute(select(MixItem).where(MixItem.mix_id == mix_id)
                                      .order_by(MixItem.position))).scalars())
        dl_ids = [i.download_item_id for i in items if i.download_item_id]
        dls = {d.id: d for d in (await s.execute(select(DownloadItem).where(DownloadItem.id.in_(dl_ids)))).scalars()}
        for it in items:
            dl = dls.get(it.download_item_id) if it.download_item_id else None
            if dl is not None and dl.status == "done" and dl.track_id:
                it.track_id = dl.track_id
            if it.track_id and not it.nd_song_id:
                it.nd_song_id = (await s.execute(select(Track.nd_song_id).where(Track.id == it.track_id))
                                 ).scalar_one_or_none()
        reserve = [i for i in items if i.state == "reserve"]
        for it in items:
            dl = dls.get(it.download_item_id) if it.download_item_id else None
            dead = it.state == "active" and not it.track_id and (
                (dl is not None and dl.status in FAILED) or (dl is None and not it.ref))
            if not dead:
                continue
            it.state = "failed"
            sub = next((r for r in reserve if r.familiar == it.familiar), reserve[0] if reserve else None)
            if sub is None:
                continue
            reserve.remove(sub)
            sub.state, it.position, sub.position = "active", sub.position, it.position
        for it in items:
            if it.state == "active":
                await queue_download(s, mix, it)
        await s.flush()
        songs = [i.nd_song_id for i in sorted(items, key=lambda i: i.position) if i.state == "active" and i.nd_song_id]
        user = await s.get(User, mix.user_id)
        settings = await settings_of(s, mix.user_id)
        publish = user is not None and (
            songs != (mix.stats_json or {}).get("playlist_songs")
            or (mix.stats_json or {}).get("playlist_status") in ("pending", "error"))
        playlist_id = settings.nd_playlist_id
        if publish:
            mix.stats_json = {**(mix.stats_json or {}), "playlist_status": "pending", "playlist_error": None,
                              "playlist_desired_songs": songs}
            if schedule:
                await enqueue_sync(s, mix_id, delay_s=0)
    # the light copies for a metered network (app.light), made now so a tap finds them ready
    from app import listen

    listen.warm_light([f"nd:{song}" for song in songs])
    notify()
    if not publish:
        return
    try:
        pid = await write_playlist(user, playlist_id, songs, mix)
    except Exception as exc:
        async with session_scope() as s:
            current = await s.get(Mix, mix_id)
            if current is not None:
                current.stats_json = {**(current.stats_json or {}), "playlist_status": "error",
                                      "playlist_error": str(exc)[:500]}
        raise
    async with session_scope() as s:
        current = await s.get(Mix, mix_id)
        if current is not None:
            current.nd_playlist_id = pid
            current.stats_json = {**(current.stats_json or {}), "playlist_songs": songs,
                                  "playlist_status": "published", "playlist_error": None}


async def write_playlist(user: User, playlist_id: str | None, songs: list[str], mix: Mix) -> str:
    """Publish a committed mix. A lost create response is reconciled by its durable, unique temporary name."""
    from app.auth import user_navidrome

    nd = user_navidrome(user)
    comment = f"{mix.title}. {mix.description}".strip(". ")
    marker = f"{PLAYLIST_NAME} [HomeMusic:{user.id}]"
    try:
        playlists = await nd.get_playlists()
        known = {p.get("id") for p in playlists}
        if playlist_id not in known:
            playlist_id = None
        if not playlist_id:
            matching = [p["id"] for p in playlists if p.get("name") == marker and p.get("id")]
            if len(matching) > 1:
                raise RuntimeError("Найдено несколько плейлистов незавершённой публикации")
            if matching:
                playlist_id = matching[0]
            else:
                async with session_scope() as s:
                    pending = (await s.execute(select(Mix.id).where(
                        Mix.user_id == user.id,
                        func.json_extract(Mix.stats_json, "$.playlist_creating") == True,
                    ).limit(1))).scalar_one_or_none()
                    if pending is not None:
                        raise RuntimeError("Создание плейлиста не подтверждено; ожидается сверка с Navidrome")
                    current = await s.get(Mix, mix.id)
                    current.stats_json = {**(current.stats_json or {}), "playlist_creating": True}
                # The intent was committed. If this response is lost, never blindly create a second playlist.
                playlist_id = (await nd.create_playlist(marker)).get("id")
                if not playlist_id:
                    raise RuntimeError("Navidrome не подтвердил создание плейлиста")
        # Save the remote identity before changing songs/name; every later operation can safely be repeated.
        async with session_scope() as s:
            settings = await settings_of(s, user.id)
            settings.nd_playlist_id = playlist_id
            current = await s.get(Mix, mix.id)
            current.nd_playlist_id = playlist_id
            pending_mixes = (await s.execute(select(Mix).where(
                Mix.user_id == user.id, func.json_extract(Mix.stats_json, "$.playlist_creating") == True,
            ))).scalars()
            for pending_mix in pending_mixes:
                pending_mix.stats_json = {**(pending_mix.stats_json or {}), "playlist_creating": False}
        await nd.set_playlist_songs(playlist_id, songs)
        await nd.update_playlist(playlist_id, name=PLAYLIST_NAME, comment=comment[:500])
        return playlist_id
    finally:
        await nd.aclose()


# --- the replacement (6.3) --------------------------------------------------------------------------------------


async def close_previous(user_id: int, new_mix_id: int) -> None:
    """The user's older mixes close: their downloads not started yet are cancelled, and the songs downloaded only
    for closed mixes are checked for removal (a download that was still running is checked at the next closing)."""
    async with session_scope() as s:
        old = list((await s.execute(select(Mix).where(Mix.user_id == user_id, Mix.status == "active",
                                                      Mix.id != new_mix_id))).scalars())
        for m in old:
            m.status, m.closed_at = "closed", utcnow()
        if old:
            queued = (await s.execute(select(DownloadItem).where(
                DownloadItem.status == "queued",
                func.json_extract(DownloadItem.query_json, "$.mix.id").in_([m.id for m in old])))).scalars()
            for d in queued:
                if quiet(d.query_json):
                    d.status = "cancelled"
    await remove_unheard(await pending_cleanup(user_id))


async def pending_cleanup(user_id: int) -> list[int]:
    """Closed mixes of the user with songs downloaded for them not checked for removal yet."""
    async with session_scope() as s:
        return list((await s.execute(select(Mix.id).join(MixItem, MixItem.mix_id == Mix.id).where(
            Mix.user_id == user_id, Mix.status == "closed", MixItem.downloaded_for_mix.is_(True),
            func.json_extract(MixItem.features_json, "$.cleanup").is_(None)).distinct())).scalars())


async def _users_keep(users: list[User]) -> tuple[set[str], set[str]] | None:
    """Navidrome songs any user has starred or put in a playlist of their own (the mixes' playlists not counted).
    None: some user's Navidrome did not answer — nothing may be deleted then."""
    from app.auth import user_navidrome

    async with session_scope() as s:
        mix_playlists = set((await s.execute(select(MixSettings.nd_playlist_id)
                                             .where(MixSettings.nd_playlist_id.is_not(None)))).scalars())
    starred: set[str] = set()
    listed: set[str] = set()
    for u in users:
        nd = user_navidrome(u)
        try:
            starred.update(x["id"] for x in (await nd.get_starred2()).get("song") or [] if x.get("id"))
            for pl in await nd.get_playlists():
                if pl.get("id") in mix_playlists:
                    continue
                listed.update(x["id"] for x in (await nd.get_playlist(pl["id"])).get("entry") or [] if x.get("id"))
        except Exception as e:
            log.warning("mix cleanup: Navidrome of user %s did not answer (%r): nothing is deleted", u.id, e)
            return None
        finally:
            await nd.aclose()
    return starred, listed


async def kept_reasons(s: AsyncSession, it: MixItem, track: Track, starred: set[str], listed: set[str]) -> list[str]:
    """Why a song downloaded for a mix stays in the library (6.3); empty — it goes."""
    why: list[str] = []
    song = track.nd_song_id
    refs = [r for r in {it.ref, f"dz:{track.deezer_track_id}" if track.deezer_track_id else None} if r]
    ids = or_(*([PlayEvent.nd_song_id == song] if song else []), *([PlayEvent.ref.in_(refs)] if refs else []))
    # by anyone: the app's and the PWA's events and Navidrome's scrobbles (imported as `finish`)
    listened = (await s.execute(select(PlayEvent.id).where(
        ids, or_(PlayEvent.kind == "finish", (PlayEvent.kind == "skip") & (PlayEvent.position_s >= LISTENED_S)))
        .limit(1))).scalar_one_or_none()
    if listened is not None:
        why.append("listened")
    if song and song in starred:
        why.append("starred")
    if song and song in listed:
        why.append("playlist")
    explicit = [d for d in (await s.execute(select(DownloadItem).where(DownloadItem.track_id == track.id))).scalars()
                if not quiet(d.query_json)]
    if explicit:
        why.append("requested")
    other_mix = (await s.execute(select(MixItem.id).join(Mix, Mix.id == MixItem.mix_id).where(
        Mix.status == "active", MixItem.state == "active", MixItem.id != it.id,
        or_(MixItem.track_id == track.id, *([MixItem.ref.in_(refs)] if refs else []))).limit(1))).scalar_one_or_none()
    if other_mix is not None:
        why.append("mix")
    imported = (await s.execute(select(ImportItem.id).where(
        or_(ImportItem.track_id == track.id, *([ImportItem.ref.in_(refs)] if refs else []))).limit(1))
    ).scalar_one_or_none()
    if imported is not None:
        why.append("import")
    return why


async def remove_unheard(mix_ids: list[int]) -> int:
    """Songs of closed mixes downloaded only for them that nobody listened to, starred, put in a playlist, asked to
    download, and that no active mix or import holds: to the trash (as the assistant deletes). Each song is decided
    once (`features_json.cleanup`: "trashed" or why it stays); one still downloading waits. Returns how many went."""
    from app.jobs.downloads import enqueue_scan
    from app.library import editor

    if not mix_ids:
        return 0
    async with session_scope() as s:
        users = list((await s.execute(select(User))).scalars())
    keep = await _users_keep(users)
    if keep is None:
        return 0
    starred, listed = keep
    removed = 0
    async with session_scope() as s:
        items = list((await s.execute(select(MixItem).where(
            MixItem.mix_id.in_(mix_ids), MixItem.downloaded_for_mix.is_(True)))).scalars())
        for it in items:
            if (it.features_json or {}).get("cleanup"):
                continue
            dl = await s.get(DownloadItem, it.download_item_id) if it.download_item_id else None
            if dl is not None and dl.status in ACTIVE_STATUSES:
                continue  # still downloading: decided at the next closing
            if not it.track_id and dl is not None and dl.status == "done":
                it.track_id = dl.track_id
            track = await s.get(Track, it.track_id) if it.track_id else None
            if track is None:
                verdict = "nothing"  # did not download, or deleted already
            elif why := await kept_reasons(s, it, track, starred, listed):
                log.info("mix cleanup: keep %s — %s (%s)", track.artist, track.title, ", ".join(why))
                if why == ["mix"]:
                    continue  # another mix holds it for now: decided when that one closes
                verdict = "kept: " + ", ".join(why)
            else:
                log.info("mix cleanup: trash %s — %s", track.artist, track.title)
                await editor.trash_track(s, track)
                verdict = "trashed"
                removed += 1
            it.features_json = {**(it.features_json or {}), "cleanup": verdict}
        if removed:
            await enqueue_scan(s)
    if removed:
        notify()
    return removed


# --- schedule ---------------------------------------------------------------------------------------------------


async def has_history(s: AsyncSession, user_id: int) -> bool:
    ev = (await s.execute(select(PlayEvent.id).where(PlayEvent.user_id == user_id).limit(1))).scalar_one_or_none()
    if ev is not None:
        return True
    dl = (await s.execute(select(DownloadItem.id).where(DownloadItem.user_id == user_id,
                                                        DownloadItem.status == "done").limit(1))).scalar_one_or_none()
    return dl is not None


async def due_users(now: datetime | None = None) -> list[int]:
    """Users whose mix of today is due: past their hour (local time), not attempted today, with history or picked
    artists. Marks them attempted."""
    local = builder.local_now(now)
    today: date = local.date()
    out: list[int] = []
    async with session_scope() as s:
        for u in (await s.execute(select(User).order_by(User.id))).scalars():
            st = await settings_of(s, u.id)
            if st.last_build_on == today or local.hour < st.mix_hour:
                continue
            if not (st.artists_json or await has_history(s, u.id)):
                continue
            st.last_build_on = today
            if await enqueue_build(s, u.id):
                out.append(u.id)
    if out:
        notify()
    return out


async def schedule_loop() -> None:
    await asyncio.sleep(60)
    while True:
        try:
            users = await due_users()
            if users:
                log.info("mix of the day queued for users %s", users)
        except Exception:
            log.exception("mix schedule failed")
        await asyncio.sleep(CHECK_EVERY_S)


def next_build_at(mix_hour: int, now: datetime | None = None) -> datetime:
    """Local time of the next scheduled build."""
    local = builder.local_now(now)
    nxt = local.replace(hour=mix_hour, minute=0, second=0, microsecond=0)
    return nxt if nxt > local else nxt + timedelta(days=1)


__all__ = ["PLAYLIST_NAME", "build_for", "current_mix", "downloads_changed", "due_users", "enqueue_build",
           "remove_unheard", "schedule_loop", "settings_of", "sync"]
