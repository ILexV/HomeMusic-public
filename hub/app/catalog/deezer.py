"""Deezer public API client (no key): search, artist pages, albums with full tracklists, tracks.

Quirks (verified on the live API):
- errors come as HTTP 200 with `{"error": {"code": ...}}`; code 4 = quota exceeded (retry), 700 = busy (retry),
  800 = no data (not found);
- `album/{id}` embeds at most 25 tracks and without `track_position`/`disk_number`/`isrc`, while
  `album/{id}/tracks` returns all three (paged, `limit` up to 100) -> an album costs 2 requests, not N+1;
- `artist/{id}/albums` items have no `artist` object; they are grouped by record type, not sorted by date;
- `search/track` with an `artist:"..."` filter currently returns nothing, hence the plain-query fallback;
- a mobile browser User-Agent gets a truncated, differently ranked search (3 hits instead of 12), so a neutral UA
  is sent instead of net.DEFAULT_UA.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from app.catalog.cache import cached
from app.catalog.models import (
    ArtistPage,
    CatalogAlbum,
    CatalogArtist,
    CatalogTrack,
    SearchResults,
    make_ref,
)
from app.config import CatalogCfg
from app.net import WindowLimiter, make_client, request_with_backoff

log = logging.getLogger(__name__)

BASE_URL = "https://api.deezer.com"
RECORD_TYPES = {"album", "single", "ep", "compile"}
PAGE_LIMIT = 100
QUOTA_CODES = {4, 700}  # Quota limit exceeded, Service busy
NOT_FOUND_CODE = 800
USER_AGENT = "HomeMusic/0.1"

M = TypeVar("M", bound=BaseModel)


class CatalogNotFound(Exception):
    """The requested catalog entity does not exist."""


class DeezerError(Exception):
    """Deezer API returned an error or an unexpected response."""


def _parse_date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:  # "0000-00-00" and friends
        return None


def _norm(s: str) -> str:
    return " ".join(s.split()).casefold()


def _key(prefix: str, *parts: Any) -> str:
    """Cache key; long free-text parts are hashed to stay under the 512-char column limit."""
    raw = "|".join(_norm(str(p)) for p in parts)
    if len(raw) > 200:
        raw = hashlib.sha1(raw.encode()).hexdigest()
    return f"{prefix}:{raw}"


def _quote(s: str) -> str:
    return s.replace('"', " ").strip()


def map_artist(d: dict[str, Any]) -> CatalogArtist:
    return CatalogArtist(
        ref=make_ref("dz", d["id"]),
        source="dz",
        id=str(d["id"]),
        name=d.get("name") or "",
        picture_url=d.get("picture_xl") or d.get("picture_big") or None,
        picture_small_url=d.get("picture_medium") or None,
        nb_albums=d.get("nb_album"),
        nb_fans=d.get("nb_fan"),
    )


def map_album(d: dict[str, Any], artist: dict[str, Any] | None = None) -> CatalogAlbum:
    """Map an album object; `artist` is used when the payload has no `artist` (artist/{id}/albums)."""
    art = d.get("artist") or artist or {}
    rd = _parse_date(d.get("release_date"))
    rt = d.get("record_type")
    genres = (d.get("genres") or {}).get("data") or []
    return CatalogAlbum(
        ref=make_ref("dz", d["id"]),
        source="dz",
        id=str(d["id"]),
        title=d.get("title") or "",
        artist=art.get("name") or "",
        artist_ref=make_ref("dz", art["id"]) if art.get("id") else None,
        record_type=rt if rt in RECORD_TYPES else "album",
        release_date=rd,
        year=rd.year if rd else None,
        cover_url=d.get("cover_xl") or d.get("cover_big") or None,
        cover_small_url=d.get("cover_medium") or None,
        nb_tracks=d.get("nb_tracks"),
        explicit=d.get("explicit_lyrics"),
        upc=d.get("upc") or None,
        genre=genres[0].get("name") if genres else None,
    )


def _artist_names(d: dict[str, Any]) -> tuple[str, list[str]]:
    """Main artist name and all credited names (track `artist` first, then other Main, then featured)."""
    main = (d.get("artist") or {}).get("name") or ""
    contributors = d.get("contributors") or []
    ordered = sorted(contributors, key=lambda c: c.get("role", "Main") != "Main")  # stable
    names = [main] if main else []
    for c in ordered:
        n = c.get("name")
        if n and n not in names:
            names.append(n)
    return main or (names[0] if names else ""), names


def map_track(d: dict[str, Any], album: CatalogAlbum | None = None) -> CatalogTrack:
    """Map a track object; `album` supplies album fields for album/{id}/tracks items."""
    main, names = _artist_names(d)
    alb = d.get("album") or {}
    art = d.get("artist") or {}
    album_id = alb.get("id") or (album.id if album else None)
    return CatalogTrack(
        ref=make_ref("dz", d["id"]),
        source="dz",
        id=str(d["id"]),
        title=d.get("title") or d.get("title_short") or "",
        artist=main,
        artists=names,
        artist_ref=make_ref("dz", art["id"]) if art.get("id") else None,
        album_ref=make_ref("dz", album_id) if album_id else None,
        album_title=alb.get("title") or (album.title if album else None),
        album_artist=album.artist if album else None,
        duration_s=d.get("duration"),
        track_no=d.get("track_position"),
        disc_no=d.get("disk_number"),
        isrc=d.get("isrc") or None,
        explicit=d.get("explicit_lyrics"),
        release_date=_parse_date(d.get("release_date") or alb.get("release_date"))
        or (album.release_date if album else None),
        cover_url=alb.get("cover_xl") or (album.cover_url if album else None),
        cover_small_url=alb.get("cover_medium") or (album.cover_small_url if album else None),
        preview_url=d.get("preview") or None,
        rank=d.get("rank") or None,
    )


def dedupe_and_sort(albums: list[CatalogAlbum]) -> list[CatalogAlbum]:
    """Drop clean/explicit twins (same title + record_type + year, explicit wins); newest first."""
    best: dict[tuple[str, str, int | None], CatalogAlbum] = {}
    for a in albums:
        k = (_norm(a.title), a.record_type, a.year)
        cur = best.get(k)
        if cur is None or (a.explicit and not cur.explicit):
            best[k] = a
    return sorted(best.values(), key=lambda a: (a.release_date or date.min, int(a.id)), reverse=True)


class DeezerClient:
    """Async Deezer catalog client with a shared rate limit and the `catalog_cache` TTL cache."""

    quota_retries: int = 5
    quota_delay_s: float = 5.0  # Deezer quota window is ~5 s

    def __init__(self, cfg: CatalogCfg, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self._own_client = client is None
        self._client = client or make_client(headers={"User-Agent": USER_AGENT})
        self._limiter = WindowLimiter(cfg.deezer_rate.requests, cfg.deezer_rate.per_seconds)

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    # --- transport -------------------------------------------------------------------------------------------

    async def _get(self, path_or_url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET a Deezer endpoint (path or absolute `next` URL); handle in-body errors."""
        url = path_or_url if path_or_url.startswith("http") else f"{BASE_URL}/{path_or_url.lstrip('/')}"
        for attempt in range(self.quota_retries + 1):
            resp = await request_with_backoff(self._client, "GET", url, limiter=self._limiter, params=params)
            if resp.status_code == 404:
                raise CatalogNotFound(url)
            if resp.status_code != 200:
                raise DeezerError(f"GET {url} -> HTTP {resp.status_code}")
            try:
                data = resp.json()
            except ValueError as e:
                raise DeezerError(f"GET {url}: invalid JSON") from e
            if not isinstance(data, dict):
                raise DeezerError(f"GET {url}: unexpected payload")
            err = data.get("error")
            if not err:
                return data
            code = err.get("code") if isinstance(err, dict) else None
            if code == NOT_FOUND_CODE:
                raise CatalogNotFound(f"{url}: {err.get('message')}")
            if code in QUOTA_CODES and attempt < self.quota_retries:
                delay = self.quota_delay_s * (attempt + 1)
                log.warning("deezer %s: %s (code %s), retry in %.1fs", url, err.get("message"), code, delay)
                await asyncio.sleep(delay)
                continue
            raise DeezerError(f"GET {url}: {err}")
        raise DeezerError(f"GET {url}: quota retries exhausted")  # pragma: no cover

    async def _get_all(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Return a complete list or fail before the caller caches a partial result."""
        data = await self._get(path, {**(params or {}), "limit": PAGE_LIMIT})
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        total = None
        while True:
            page = data.get("data")
            nxt = data.get("next")
            if not isinstance(page, list) or (not page and (seen or nxt)):
                raise DeezerError(f"{path}: missing page data")
            items.extend(page)
            total = data.get("total", total)
            if not nxt:
                if total is not None and len(items) != total:
                    raise DeezerError(f"{path}: expected {total} items, received {len(items)}")
                return items
            if nxt in seen:
                raise DeezerError(f"{path}: repeated next page")
            seen.add(nxt)
            data = await self._get(nxt)

    async def _cached_model(self, key: str, ttl_s: int, model: type[M], producer: Callable[[], Awaitable[M]]) -> M:
        async def produce() -> dict[str, Any]:
            return (await producer()).model_dump(mode="json")

        return model.model_validate(await cached(key, ttl_s, produce))

    async def _cached_list(
        self, key: str, ttl_s: int, model: type[M], producer: Callable[[], Awaitable[list[M]]]
    ) -> list[M]:
        async def produce() -> list[dict[str, Any]]:
            return [m.model_dump(mode="json") for m in await producer()]

        return [model.model_validate(x) for x in await cached(key, ttl_s, produce)]

    # --- search ----------------------------------------------------------------------------------------------

    async def _search_raw(self, kind: str, q: str, limit: int) -> list[dict[str, Any]]:
        data = await self._get(f"search/{kind}", {"q": q, "limit": limit})
        return list(data.get("data") or [])

    async def search(
        self, q: str, kinds: tuple[str, ...] | list[str] = ("artist", "album", "track"), limit: int = 10
    ) -> SearchResults:
        """Plain search over artists/albums/tracks in parallel."""
        ttl = self.cfg.ttl_search_s

        async def artists() -> list[CatalogArtist]:
            return [map_artist(x) for x in await self._search_raw("artist", q, limit)]

        async def albums() -> list[CatalogAlbum]:
            return [map_album(x) for x in await self._search_raw("album", q, limit)]

        async def tracks() -> list[CatalogTrack]:
            return [map_track(x) for x in await self._search_raw("track", q, limit)]

        async def none() -> list:
            return []

        def run(kind: str, model: type[M], producer: Callable[[], Awaitable[list[M]]]) -> Awaitable[list[M]]:
            if kind not in kinds:
                return none()
            return self._cached_list(_key(f"dz:search:{kind}", limit, q), ttl, model, producer)

        res = await asyncio.gather(
            run("artist", CatalogArtist, artists),
            run("album", CatalogAlbum, albums),
            run("track", CatalogTrack, tracks),
        )
        return SearchResults(artists=res[0], albums=res[1], tracks=res[2])

    async def search_track_page(self, q: str, offset: int = 0, limit: int = 25) -> tuple[list[CatalogTrack], int | None]:
        """One page of the plain track search: the tracks from `offset` (Deezer's `index`) and the offset of the
        next page (None at the end of the results)."""

        async def produce() -> dict[str, Any]:
            data = await self._get("search/track", {"q": q, "index": offset, "limit": limit})
            items = list(data.get("data") or [])
            more = bool(items) and (bool(data.get("next")) or len(items) >= limit)
            return {
                "tracks": [map_track(x).model_dump(mode="json") for x in items],
                "next": offset + len(items) if more else None,
            }

        page = await cached(_key("dz:search_page:track", offset, limit, q), self.cfg.ttl_search_s, produce)
        return [CatalogTrack.model_validate(x) for x in page["tracks"]], page["next"]

    async def search_tracks(self, artist: str, title: str, limit: int = 10) -> list[CatalogTrack]:
        """Advanced `artist:"..." track:"..."` search with a plain-query fallback."""

        async def produce() -> list[CatalogTrack]:
            items = await self._search_raw("track", f'artist:"{_quote(artist)}" track:"{_quote(title)}"', limit)
            if not items:
                items = await self._search_raw("track", f"{artist} {title}", limit)
            return [map_track(x) for x in items]

        return await self._cached_list(
            _key("dz:search_tracks", limit, artist, title), self.cfg.ttl_search_s, CatalogTrack, produce
        )

    async def search_albums(self, artist: str, title: str, limit: int = 5) -> list[CatalogAlbum]:
        """Advanced `artist:"..." album:"..."` search with a plain-query fallback."""

        async def produce() -> list[CatalogAlbum]:
            items = await self._search_raw("album", f'artist:"{_quote(artist)}" album:"{_quote(title)}"', limit)
            if not items:
                items = await self._search_raw("album", f"{artist} {title}", limit)
            return [map_album(x) for x in items]

        return await self._cached_list(
            _key("dz:search_albums", limit, artist, title), self.cfg.ttl_search_s, CatalogAlbum, produce
        )

    # --- artists ---------------------------------------------------------------------------------------------

    async def _artist_raw(self, id_: str | int) -> dict[str, Any]:
        return await self._get(f"artist/{id_}")

    async def get_artist(self, id_: str | int) -> CatalogArtist:
        async def produce() -> CatalogArtist:
            return map_artist(await self._artist_raw(id_))

        return await self._cached_model(f"dz:artist:{id_}", self.cfg.ttl_artist_s, CatalogArtist, produce)

    async def get_artist_top(self, id_: str | int, limit: int = 10) -> list[CatalogTrack]:
        async def produce() -> list[CatalogTrack]:
            data = await self._get(f"artist/{id_}/top", {"limit": limit})
            return [map_track(x) for x in data.get("data") or []]

        return await self._cached_list(f"dz:artist_top:{id_}:{limit}", self.cfg.ttl_artist_s, CatalogTrack, produce)

    async def get_artist_radio(self, id_: str | int) -> list[CatalogTrack]:
        """The artist's radio: ~50 tracks of the artist and similar ones (mixes)."""

        async def produce() -> list[CatalogTrack]:
            data = await self._get(f"artist/{id_}/radio", {"limit": 50})
            return [map_track(x) for x in data.get("data") or []]

        return await self._cached_list(f"dz:artist_radio:{id_}", self.cfg.ttl_artist_s, CatalogTrack, produce)

    async def get_artist_related(self, id_: str | int) -> list[CatalogArtist]:
        """Similar artists, closest first (mixes)."""

        async def produce() -> list[CatalogArtist]:
            data = await self._get(f"artist/{id_}/related", {"limit": 20})
            return [map_artist(x) for x in data.get("data") or []]

        return await self._cached_list(f"dz:artist_related:{id_}", self.cfg.ttl_artist_s, CatalogArtist, produce)

    async def chart_artists(self, limit: int = 30) -> list[CatalogArtist]:
        """The chart's artists (Deezer picks the country by the caller's address): onboarding of a new user."""

        async def produce() -> list[CatalogArtist]:
            data = await self._get("chart/0/artists", {"limit": limit})
            return [map_artist(x) for x in data.get("data") or []]

        return await self._cached_list(f"dz:chart_artists:{limit}", self.cfg.ttl_search_s, CatalogArtist, produce)

    async def get_artist_albums(self, id_: str | int) -> list[CatalogAlbum]:
        """All releases (every page), clean/explicit twins merged, newest first."""

        async def produce() -> list[CatalogAlbum]:
            artist, items = await asyncio.gather(self.get_artist(id_), self._get_all(f"artist/{id_}/albums"))
            art = {"id": artist.id, "name": artist.name}
            return dedupe_and_sort([map_album(x, art) for x in items])

        return await self._cached_list(f"dz:artist_albums:{id_}", self.cfg.ttl_artist_s, CatalogAlbum, produce)

    async def get_artist_page(self, id_: str | int) -> ArtistPage:
        artist, top, albums = await asyncio.gather(
            self.get_artist(id_), self.get_artist_top(id_), self.get_artist_albums(id_)
        )
        return ArtistPage(artist=artist, top_tracks=top, albums=albums)

    # --- albums and tracks -----------------------------------------------------------------------------------

    async def get_album(self, id_: str | int) -> CatalogAlbum:
        """Album with the full tracklist (positions, discs and ISRCs from album/{id}/tracks)."""

        async def produce() -> CatalogAlbum:
            raw, items = await asyncio.gather(self._get(f"album/{id_}"), self._get_all(f"album/{id_}/tracks"))
            album = map_album(raw)
            tracks = [map_track(x, album) for x in items]
            tracks.sort(key=lambda t: (t.disc_no or 1, t.track_no or 0))
            return album.model_copy(update={"tracks": tracks, "nb_tracks": album.nb_tracks or len(tracks)})

        return await self._cached_model(f"dz:album:{id_}", self.cfg.ttl_album_s, CatalogAlbum, produce)

    async def get_track(self, id_: str | int) -> CatalogTrack:
        async def produce() -> CatalogTrack:
            return map_track(await self._get(f"track/{id_}"))

        return await self._cached_model(f"dz:track:{id_}", self.cfg.ttl_album_s, CatalogTrack, produce)

    async def get_track_credits(self, id_: str) -> list[dict]:
        """Unlike the compact track record, keep the IDs of *all* named contributors."""
        async def produce() -> list[dict]:
            track = await self._get(f"track/{id_}")
            artists = [track.get("artist") or {}, *sorted(track.get("contributors") or [],
                       key=lambda a: a.get("role", "Main") != "Main")]
            out = []
            seen = set()
            for artist in artists:
                name = (artist.get("name") or "").strip()
                ref = make_ref("dz", artist["id"]) if artist.get("id") else None
                if name and (ref or name) not in seen:
                    out.append({"name": name, "ref": ref})
                    seen.add(ref or name)
            return out

        return await cached(f"dz:track_credits:{id_}", self.cfg.ttl_album_s, produce)

    async def preview_url(self, id_: str | int) -> str | None:
        """Fresh 30 s MP3 preview link (not cached: the links are signed and expire within hours)."""
        return (await self._get(f"track/{id_}")).get("preview") or None
