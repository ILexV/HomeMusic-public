"""Import an audio file into MUSIC_ROOT/library: album row, tags, atomic move, Track row (PLAN 8.4).

Navidrome is not notified here: callers enqueue an `nd_scan` job (coalesced) that scans and links nd ids.
"""

from __future__ import annotations

import asyncio
import base64
import errno
import hashlib
import json
import logging
import os
import shutil
import uuid
from datetime import date, datetime
from pathlib import Path, PurePosixPath

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.library import tagger
from app.library.layout import album_dirname, next_version_path, sanitize_component, track_filename, unique_path
from app.matching.normalize import fingerprint, normalize_artist, normalize_title
from app.models import Album, Track, utcnow

log = logging.getLogger(__name__)

# one import / library edit at a time: they touch the same folders and album rows
library_lock = asyncio.Lock()


class ImportSpec(BaseModel):
    artist: str
    title: str
    artists: list[str] = []
    album: str | None = None
    album_artist: str | None = None
    album_id: int | None = None  # existing Hub album (uploads, editor)
    year: int | None = None
    release_date: date | None = None
    release_type: str | None = None
    track_no: int | None = None
    track_total: int | None = None
    disc_no: int | None = None
    genre: str | None = None
    isrc: str | None = None
    explicit: bool | None = None
    deezer_track_id: str | None = None
    deezer_album_id: str | None = None
    upc: str | None = None
    cover: bytes | None = None
    source: str | None = None
    source_url: str | None = None
    source_upload_date: date | None = None
    duration_s: int | None = None
    bitrate_kbps: int | None = None
    codec: str | None = None
    warnings: list[str] = []
    added_by: int | None = None
    force: bool = False  # explicit re-download: keep the old file, add a "[vN]" version next to it


def album_reldir(album: Album) -> PurePosixPath:
    return PurePosixPath(sanitize_component(album.album_artist), album_dirname(album.year, album.title))


def library_abs(rel: str | PurePosixPath) -> Path:
    return get_settings().library_dir / Path(*PurePosixPath(rel).parts)


async def find_album(s: AsyncSession, *, title: str, album_artist: str, deezer_album_id: str | None) -> Album | None:
    if deezer_album_id:
        a = (
            await s.execute(select(Album).where(Album.deezer_album_id == str(deezer_album_id)).limit(1))
        ).scalar_one_or_none()
        if a:
            return a
    nt, na = normalize_title(title).text, normalize_artist(album_artist).text
    rows = (await s.execute(select(Album).where(Album.title == title))).scalars().all()
    if not rows:
        # same album typed with a different case/ё/punctuation
        rows = (await s.execute(select(Album))).scalars().all()
    for a in rows:
        if normalize_title(a.title).text == nt and normalize_artist(a.album_artist).text == na:
            if deezer_album_id and a.deezer_album_id and a.deezer_album_id != str(deezer_album_id):
                continue  # same name, different release (e.g. deluxe)
            return a
    return None


async def get_or_create_album(s: AsyncSession, spec: ImportSpec) -> Album:
    if spec.album_id:
        a = await s.get(Album, spec.album_id)
        if a is None:
            raise ValueError(f"album {spec.album_id} not found")
        return a
    title = spec.album or spec.title
    album_artist = spec.album_artist or spec.artist
    a = await find_album(s, title=title, album_artist=album_artist, deezer_album_id=spec.deezer_album_id)
    if a is None:
        a = Album(
            uid=str(uuid.uuid4()),
            title=title,
            album_artist=album_artist,
            year=spec.year or (spec.release_date.year if spec.release_date else None),
            release_type=spec.release_type or ("single" if not spec.album else "album"),
            deezer_album_id=spec.deezer_album_id,
            upc=spec.upc,
            genre=spec.genre,
            created_by=spec.added_by,
        )
        s.add(a)
        await s.flush()
    elif spec.deezer_album_id and not a.deezer_album_id:
        a.deezer_album_id = spec.deezer_album_id
    return a


def tagdata_for(track: Track, album: Album, *, cover: bytes | None = None, artists: list[str] | None = None,
                track_total: int | None = None) -> tagger.TagData:
    return tagger.TagData(
        title=track.title,
        artist=track.artist,
        artists=artists or [],
        album=album.title,
        album_artist=album.album_artist,
        track_no=track.track_no,
        track_total=track_total,
        disc_no=track.disc_no,
        date=str(album.year) if album.year else None,
        genre=track.genre or album.genre,
        isrc=track.isrc,
        explicit=track.explicit,
        release_type=album.release_type,
        track_uid=track.uid,
        album_uid=album.uid,
        source=track.source,
        source_url=track.source_url,
        source_date=track.source_upload_date,
        acquired=track.acquired_at,
        deezer_track_id=track.deezer_track_id,
        deezer_album_id=album.deezer_album_id,
        cover=cover,
    )


def _move(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dest)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        # A crash while copying across mounts must leave either the staging file or the complete destination.
        tmp = dest.with_name(f".{dest.name}.part")
        shutil.copyfile(src, tmp)
        with tmp.open("rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp, dest)
        src.unlink()


def _write_cover_file(album_dir: Path, cover: bytes) -> None:
    album_dir.mkdir(parents=True, exist_ok=True)
    tmp = album_dir / ".cover.jpg.part"
    tmp.write_bytes(cover)
    os.replace(tmp, album_dir / "cover.jpg")


def is_original(source_date: date | None, explicit: bool | None) -> bool:
    """Pre-censorship upload or explicit version: never replaced automatically (PLAN 8.6)."""
    cutoff = get_settings().cfg.matching.original_before
    return bool(explicit) or (source_date is not None and source_date < cutoff)


def _save_import(journal: Path, record: dict) -> None:
    tmp = journal.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, journal)


def prepare_import(journal: Path, src: Path, spec: ImportSpec) -> None:
    """Persist the intent before tagging or moving bytes; keep it until the caller's DB commit."""
    _save_import(journal, {
        "src": str(src), "uid": str(uuid.uuid4()), "spec": spec.model_dump(mode="json", exclude={"cover"}),
        "cover": base64.b64encode(spec.cover).decode("ascii") if spec.cover else None,
    })


def load_import(journal: Path) -> tuple[Path, ImportSpec]:
    record = json.loads(journal.read_text(encoding="utf-8"))
    spec = ImportSpec(**record["spec"], cover=base64.b64decode(record["cover"]) if record["cover"] else None)
    return Path(record["src"]), spec


def _digest(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


async def import_file(s: AsyncSession, src: Path, spec: ImportSpec, *, journal: Path | None = None) -> Track:
    """Import once; a download's durable journal also recovers a move whose DB transaction rolled back."""
    async with library_lock:
        record = json.loads(journal.read_text(encoding="utf-8")) if journal else {}
        if record:
            existing = (await s.execute(select(Track).where(Track.uid == record["uid"]))).scalar_one_or_none()
            if existing is not None:
                return existing
        album = await get_or_create_album(s, spec)
        if record.get("album_uid") and album.uid != record["album_uid"]:
            has_tracks = (await s.execute(select(Track.id).where(Track.album_id == album.id).limit(1))).scalar()
            if has_tracks is None:
                album.uid = record["album_uid"]
        cover = tagger.prepare_cover(spec.cover) if spec.cover else None
        album_dir_rel = album_reldir(album)
        album_dir = library_abs(album_dir_rel)
        if cover is None and (album_dir / "cover.jpg").is_file():
            cover = (album_dir / "cover.jpg").read_bytes()

        track = Track(
            uid=record.get("uid") or str(uuid.uuid4()),
            album_id=album.id,
            path="",
            artist=spec.artist,
            title=spec.title,
            album=album.title,
            album_artist=album.album_artist,
            track_no=spec.track_no,
            disc_no=spec.disc_no,
            year=album.year,
            genre=spec.genre or album.genre,
            duration_s=spec.duration_s,
            bitrate_kbps=spec.bitrate_kbps,
            codec=spec.codec,
            isrc=spec.isrc.upper() if spec.isrc else None,
            deezer_track_id=str(spec.deezer_track_id) if spec.deezer_track_id else None,
            explicit=spec.explicit,
            source=spec.source,
            source_url=spec.source_url,
            source_upload_date=spec.source_upload_date,
            acquired_at=utcnow(),
            locked=is_original(spec.source_upload_date, spec.explicit),
            fingerprint=fingerprint(spec.artist, spec.title),
            warnings=list(spec.warnings),
            added_by=spec.added_by,
        )

        if record.get("dest"):
            dest = library_abs(record["dest"])
        else:
            ext = src.suffix.lstrip(".").lower() or "mp3"
            name = track_filename(spec.disc_no, spec.track_no, spec.title, ext)
            dest = album_dir / name
            if dest.exists():
                dest = next_version_path(dest) if spec.force else unique_path(album_dir, name)
            if journal:
                record.update(dest=PurePosixPath(album_dir_rel, dest.name).as_posix(), album_uid=album.uid)
                await asyncio.to_thread(_save_import, journal, record)
        if record.get("digest") and dest.is_file():
            if await asyncio.to_thread(_digest, dest) != record["digest"]:
                raise ValueError("import destination changed; refusing to adopt another file")
        else:
            if dest.exists():
                raise ValueError("import destination is occupied")
            await asyncio.to_thread(
                tagger.write_tags, src, tagdata_for(track, album, cover=cover, artists=spec.artists,
                                                  track_total=spec.track_total)
            )
            if journal:
                record["digest"] = await asyncio.to_thread(_digest, src)
                await asyncio.to_thread(_save_import, journal, record)
            await asyncio.to_thread(_move, src, dest)
        if cover:
            if not (album_dir / "cover.jpg").is_file():
                await asyncio.to_thread(_write_cover_file, album_dir, cover)
            album.cover_path = (album_dir_rel / "cover.jpg").as_posix()

        track.path = dest.relative_to(get_settings().library_dir).as_posix()
        s.add(track)
        await s.flush()
        log.info("imported %s", track.path, extra={"user_id": spec.added_by})
        return track


def staging_dir_for(kind: str, key: str | int) -> Path:
    d = get_settings().staging_dir / kind / str(key)
    d.mkdir(parents=True, exist_ok=True)
    return d


def cleanup_dir(d: Path) -> None:
    shutil.rmtree(d, ignore_errors=True)


def now_local_date() -> date:
    return datetime.now().date()
