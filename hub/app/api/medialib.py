"""The Navidrome library as the user's player sees it: artists, albums, «Понравившиеся», covers. The Android app
has these as native screens (Tempus); the PWA in a plain browser shows them from here (`web/src/pages/Home.tsx`,
`MediaLibrary.tsx`). Everything is asked of Navidrome as the user, so their stars and play counts are the ones shown.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.catalog import library_album_tracks, match_album_tracks
from app.api.schemas import NdAlbumOut, NdArtistOut, NdArtistPageOut, NdHomeOut, NdLikedOut, NdSongOut, TrackOut
from app.auth import current_user, user_navidrome
from app.db import get_session
from app.library.navidrome import NavidromeClient, SubsonicError
from app.mixes.resolve import library_track
from app.models import Album, CatalogCache, ImportItem, Track, User

router = APIRouter(prefix="/api/nd", tags=["medialib"])

ALBUM_LISTS = ("recent", "frequent", "newest", "starred", "alphabeticalByName", "alphabeticalByArtist", "random")
HOME_ROW = 15
HOME_RADIO = 30
SMALL_COVER = 300
BIG_COVER = 1000


def cover_url(cover_id: str | None, size: int = SMALL_COVER) -> str | None:
    return f"/api/nd/cover/{quote(cover_id, safe='')}?size={size}" if cover_id else None


def album_out(a: dict) -> NdAlbumOut:
    return NdAlbumOut(id=a["id"], title=a.get("name") or a.get("title") or "", artist=a.get("artist") or "",
                      artist_id=a.get("artistId"), year=a.get("year") or None, song_count=a.get("songCount") or 0,
                      duration_s=a.get("duration") or 0, cover_url=cover_url(a.get("coverArt")),
                      starred=bool(a.get("starred")))


def artist_out(a: dict) -> NdArtistOut:
    return NdArtistOut(id=a["id"], name=a.get("name") or "", album_count=a.get("albumCount") or 0,
                       cover_url=cover_url(a.get("coverArt")), starred=bool(a.get("starred")))


async def songs_out(s: AsyncSession, songs: list[dict], *, album_refs: bool = False) -> list[NdSongOut]:
    """Navidrome songs as tracks: the Hub's own record of the file gives the Deezer ref (mixes, «Моя волна» and
    listening data know the song by it), the rest are `nd:<song>`."""
    ids = [x["id"] for x in songs if x.get("id")]
    rows: dict[str, Track] = {}
    album_ids: dict[str, str] = {}
    for i in range(0, len(ids), 500):
        q = select(Track).where(Track.nd_song_id.in_(ids[i:i + 500]))
        if album_refs:
            for t, album_id in await s.execute(q.outerjoin(Album, Track.album_id == Album.id).add_columns(Album.deezer_album_id)):
                rows[t.nd_song_id] = t
                if album_id:
                    album_ids[t.nd_song_id] = album_id
        else:
            rows.update((t.nd_song_id, t) for t in (await s.execute(q)).scalars())
    out = []
    for x in songs:
        sid = x.get("id")
        if not sid:
            continue
        t = rows.get(sid)
        ref = library_track(t).ref if t is not None else f"nd:{sid}"
        artist = x.get("artist") or ""
        names = [a["name"] for a in x.get("artists") or [] if a.get("name")] or ([artist] if artist else [])
        isrc = x.get("isrc")
        if isinstance(isrc, list):
            isrc = next((value for value in isrc if value), None)
        out.append(NdSongOut(
            ref=ref, source="dz" if ref.startswith("dz:") else "nd", id=ref.split(":", 1)[1], title=x.get("title") or "",
            artist=artist, artists=names, album_title=x.get("album"), album_artist=x.get("displayAlbumArtist"),
            duration_s=x.get("duration"), track_no=x.get("track"), disc_no=x.get("discNumber"),
            album_ref=f"dz:{album_ids[sid]}" if sid in album_ids else None,
            isrc=isrc or (t.isrc if t is not None else None),
            explicit=t.explicit if t is not None else None,
            cover_url=cover_url(x.get("coverArt"), BIG_COVER), cover_small_url=cover_url(x.get("coverArt")),
            owned=True, library_track_id=t.id if t is not None else None, nd_song_id=sid,
            nd_album_id=x.get("albumId"), nd_artist_id=x.get("artistId"),
        ))
    return out


async def nd_call(user: User, fn, not_found: str = "Не найдено в библиотеке"):
    """Runs `fn(nd)` as the user and turns Navidrome failures into HTTP errors: «not found» (70) → 404,
    «not authorized» (50, e.g. not the playlist's owner) → 403, any other Subsonic error or no answer → 502."""
    nd = user_navidrome(user)
    try:
        return await fn(nd)
    except SubsonicError as e:
        if e.code == 70:  # not found
            raise HTTPException(404, not_found) from e
        if e.code == 50:  # not authorized
            raise HTTPException(403, "Нет прав на это в библиотеке") from e
        raise HTTPException(502, f"Navidrome: {e.message}") from e
    except httpx.HTTPError as e:
        raise HTTPException(502, "Navidrome не отвечает") from e
    finally:
        await nd.aclose()


def _radio(artists: list[dict]) -> list[NdArtistOut]:
    """«Радиостанции»: every artist, the liked ones first, then those with more albums."""
    ranked = sorted(artists, key=lambda a: (not a.get("starred"), -(a.get("albumCount") or 0), (a.get("name") or "").casefold()))
    return [artist_out(a) for a in ranked]


@router.get("/home", response_model=NdHomeOut)
async def home(user: User = Depends(current_user)):
    async def load(nd: NavidromeClient):
        return await asyncio.gather(*(nd.get_album_list2(t, size=HOME_ROW) for t in ("recent", "frequent", "newest", "starred")),
                                    nd.get_artists())

    recent, frequent, newest, starred, artists = await nd_call(user, load)
    return NdHomeOut(recent=[album_out(a) for a in recent], frequent=[album_out(a) for a in frequent],
                     newest=[album_out(a) for a in newest], starred=[album_out(a) for a in starred],
                     radio=_radio(artists)[:HOME_RADIO])


@router.get("/albums", response_model=list[NdAlbumOut])
async def albums(type: str = "alphabeticalByName", offset: int = Query(0, ge=0), size: int = Query(50, ge=1, le=500),
                 user: User = Depends(current_user)):
    if type not in ALBUM_LISTS:
        raise HTTPException(422, f"type: one of {', '.join(ALBUM_LISTS)}")
    return [album_out(a) for a in await nd_call(user, lambda nd: nd.get_album_list2(type, size=size, offset=offset))]


def complete_tracklist(local: list[NdSongOut], release: list[TrackOut]) -> list[NdSongOut]:
    """Keep Navidrome identities (and local-only bonus tracks), in catalog order."""
    matches = match_album_tracks(local, release)
    used = set(matches.values())
    song_ids = {t.nd_song_id for t in local if t.nd_song_id}
    rows = []
    for j, track in enumerate(release):
        index = matches.get(j)
        if index is None:
            if track.nd_song_id in song_ids:
                # Global ownership also uses a version-blind fingerprint. Don't mark another
                # version as this local file if album matching rejected it.
                track = track.model_copy(update={"owned": False, "nd_song_id": None, "library_track_id": None})
            rows.append(NdSongOut(**track.model_dump()))
        else:
            # Keep the local identity used by the player, not an external copy of the song.
            song = local[index]
            update = {"album_ref": track.album_ref, "track_no": track.track_no or song.track_no,
                      "disc_no": track.disc_no or song.disc_no}
            if song.source == "nd":
                update.update(ref=track.ref, id=track.id, source=track.source)
            rows.append(song.model_copy(update=update))
    rows.extend(t for i, t in enumerate(local) if i not in used)
    return rows


@router.get("/albums/{album_id}", response_model=NdAlbumOut)
async def album(album_id: str, catalog: bool = False, user: User = Depends(current_user),
                s: AsyncSession = Depends(get_session, scope="function")):
    a = await nd_call(user, lambda nd: nd.get_album(album_id))
    if not a.get("id"):
        raise HTTPException(404, "Альбом не найден")
    out = album_out(a)
    out.tracks = await songs_out(s, a.get("song") or [], album_refs=catalog)
    if catalog:
        album_ids = {t.album_ref.split(":", 1)[1] for t in out.tracks if t.album_ref}
        if len(album_ids) > 1:
            out.catalog_status = "ambiguous"
            return out
        out.catalog_status, release = await library_album_tracks(
            s, user.id, out.artist, out.title, out.year, next(iter(album_ids), None), out.tracks,
        )
        if release:
            out.tracks = complete_tracklist(out.tracks, release)
            out.song_count = len(out.tracks)
            out.duration_s = sum(t.duration_s or 0 for t in out.tracks)
    return out


@router.get("/artists", response_model=list[NdArtistOut])
async def artists(user: User = Depends(current_user)):
    return [artist_out(a) for a in await nd_call(user, lambda nd: nd.get_artists())]


@router.get("/artists/{artist_id}", response_model=NdArtistPageOut)
async def artist(artist_id: str, user: User = Depends(current_user)):
    a = await nd_call(user, lambda nd: nd.get_artist(artist_id))
    if not a.get("id"):
        raise HTTPException(404, "Исполнитель не найден")
    # newest year first, albums without a year last, ties by title
    albums_ = sorted(a.get("album") or [], key=lambda x: (not x.get("year"), -(x.get("year") or 0),
                                                          (x.get("name") or x.get("title") or "").casefold(), x.get("id") or ""))
    return NdArtistPageOut(artist=artist_out(a), albums=[album_out(x) for x in albums_])


@router.get("/liked", response_model=NdLikedOut)
async def liked(user: User = Depends(current_user), s: AsyncSession = Depends(get_session, scope="function")):
    from app.favorites import parse_time, snapshot
    from app.imports.apply import _user_likes

    body = await nd_call(user, lambda nd: nd.get_starred2())
    states = await snapshot(s, user.id)
    decisions = {(st.target_type, alias): st for st in states.values() for alias in st.aliases}

    def state(typ, *, ref=None, nd_id=None):
        return decisions.get((typ, f"ref:{ref}")) or decisions.get((typ, f"nd:{nd_id}"))

    # The same own decisions as /api/starred override delayed ND/import snapshots.
    ranked = []
    seen = set()
    songs = body.get("song") or []
    for obj, song in zip([x for x in songs if x.get("id")], await songs_out(s, songs), strict=True):
        st = state("track", ref=song.ref, nd_id=song.nd_song_id)
        if st is not None and not st.liked:
            continue
        seen.update([f"ref:{song.ref}", f"nd:{song.nd_song_id}", *(st.aliases if st else [])])
        ranked.append((st.since if st else parse_time(obj.get("starred")), song))

    pending = {}
    for item in (await s.execute(_user_likes(user.id).where(ImportItem.status.in_(("waiting", "queued"))))).scalars():
        st = state("track", ref=item.ref)
        if st is None:
            pending.setdefault(item.ref, (item.raw_json or {}, None, None, []))
    for st in states.values():
        if st.target_type == "track" and st.liked:
            ref = st.ref or (f"nd:{st.nd_id}" if st.nd_id else None)
            if ref:
                pending[ref] = (st.data or {}, st.since, st.nd_id, st.aliases)

    # Read stored metadata in batches. Listing likes must never depend on a catalog
    # request or start a download; old likes can recover names from playback caches.
    cache_keys = {key for ref in pending for key in (f"listen:{ref}", f"mix:track:{ref}",
                  f"dz:track:{ref[3:]}" if ref.startswith("dz:") else "") if key}
    cached = {}
    keys = sorted(cache_keys)
    for start in range(0, len(keys), 500):
        for row in (await s.execute(select(CatalogCache).where(CatalogCache.key.in_(keys[start:start + 500])))).scalars():
            cached[row.key] = row.payload_json.get("v") or {}
    for ref, (data, since, nd_id, links) in pending.items():
        aliases = {f"ref:{ref}", *links, *([f"nd:{nd_id}"] if nd_id else [])}
        if seen.intersection(aliases):
            continue
        meta = {**cached.get(f"listen:{ref}", {}), **cached.get(f"mix:track:{ref}", {}),
                **cached.get(f"dz:track:{ref[3:]}", {}), **data}
        src, _, ident = ref.partition(":")
        if src not in ("dz", "hm", "mb", "nd", "file") or not ident:
            continue
        song = NdSongOut(ref=ref, source=src, id=ident, title=meta.get("title") or ref,
                         artist=meta.get("artist") or "", duration_s=meta.get("duration_s") or meta.get("duration"),
                         album_title=meta.get("album_title"), artist_ref=meta.get("artist_ref"),
                         album_ref=meta.get("album_ref"), cover_url=meta.get("cover_url"),
                         cover_small_url=meta.get("cover_small_url"), nd_song_id=nd_id, owned=bool(nd_id))
        ranked.append((since or parse_time(meta.get("liked_at")), song))
        seen.update(aliases)

    def visible(typ, obj):
        st = state(typ, nd_id=obj.get("id"))
        return st is None or st.liked

    ranked.sort(key=lambda pair: pair[0] or datetime.min, reverse=True)
    return NdLikedOut(songs=[song for _, song in ranked],
                      albums=[album_out(a) for a in sorted(body.get("album") or [], key=lambda x: x.get("starred") or "", reverse=True)
                              if visible("album", a)],
                      artists=[artist_out(a) for a in body.get("artist") or [] if visible("artist", a)])


@router.get("/cover/{cover_id}")
async def cover(cover_id: str, size: int | None = Query(None, ge=32, le=1500), user: User = Depends(current_user)):
    """A Navidrome cover (getCoverArt), so the page needs no Navidrome credentials."""
    nd = user_navidrome(user)
    try:
        url, params = nd.cover_art_url_params(cover_id, size)
        r = await nd.http.get(url, params=params)
    except httpx.HTTPError as e:
        raise HTTPException(502, "Navidrome не отвечает") from e
    finally:
        await nd.aclose()
    kind = r.headers.get("content-type", "")
    if r.status_code != 200 or not kind.startswith("image/"):
        raise HTTPException(404, "Нет обложки")
    return Response(r.content, media_type=kind, headers={"Cache-Control": "private, max-age=86400"})
