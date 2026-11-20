"""Second catalog backed by ru.hitmoz.org (Russian artists missing in Deezer). Refs are "hm:<id>".

The site has artist/album pages but no artist discography: `get_artist` returns tracks only, albums come
from album search results. Pictures/covers are avatars.yandex.net URLs (resized to larger variants).
"""

from __future__ import annotations

import logging

from app.catalog.cache import cached
from app.catalog.models import ArtistPage, CatalogAlbum, CatalogArtist, CatalogTrack, make_ref
from app.providers.base import Candidate, ProviderError
from app.providers.hitmoz import (
    PAGE_SIZE,
    HitmozProvider,
    parse_album_page,
    parse_artist_page,
    parse_search_albums,
    parse_search_artists,
    parse_track_page,
    yandex_size,
)

log = logging.getLogger(__name__)

TTL_S = 7 * 86400
SEARCH_TTL_S = 86400
MAX_ARTIST_PAGES = 3
PICTURE_SIZE = "400x400"
COVER_SIZE = "1000x1000"
COVER_SMALL_SIZE = "200x200"


def _split_artists(s: str) -> list[str]:
    return [p.strip() for p in s.replace(" feat. ", ", ").replace(" & ", ", ").split(",") if p.strip()]


def track_from_candidate(
    c: Candidate,
    *,
    artist_ref: str | None = None,
    album: CatalogAlbum | None = None,
    track_no: int | None = None,
) -> CatalogTrack:
    """hitmoz Candidate (track row) -> CatalogTrack; the downloader re-searches by artist/title."""
    artists = _split_artists(c.artist) or [c.artist]
    return CatalogTrack(
        ref=make_ref("hm", c.provider_id),
        source="hm",
        id=c.provider_id,
        title=c.title,
        artist=artists[0],
        artists=artists,
        artist_ref=artist_ref,
        album_ref=album.ref if album else None,
        album_title=album.title if album else None,
        album_artist=album.artist if album else None,
        duration_s=c.duration_s,
        track_no=track_no,
        cover_url=(album.cover_url if album and album.cover_url else c.cover_url),
        cover_small_url=album.cover_small_url if album and album.cover_url else None,
    )


def _artist(d: dict) -> CatalogArtist:
    return CatalogArtist(
        ref=make_ref("hm", d["id"]),
        source="hm",
        id=d["id"],
        name=d["name"],
        picture_url=yandex_size(d.get("picture_url"), PICTURE_SIZE),
    )


class HitmozCatalog:
    """Artist/album browsing on hitmoz, sharing the provider's client, rate limiter and healthy domain."""

    def __init__(self, provider: HitmozProvider):
        self.provider = provider

    async def get_track(self, track_id: str) -> CatalogTrack | None:
        """Metadata of an exact track page; no audio URL is opened."""
        if not track_id.isascii() or not track_id.isdecimal():
            return None

        async def produce():
            html, base = await self.provider.fetch_page(f"/song/{track_id}")
            row = parse_track_page(html, base)
            artist, title = row["artist"], row["title"]
            if row["id"] != track_id or not artist or not title:
                return None
            return CatalogTrack(ref=make_ref("hm", track_id), source="hm", id=track_id, title=title,
                                artist=artist, artists=_split_artists(artist), duration_s=row["duration_s"],
                                artist_ref=make_ref("hm", row["artist_id"]) if row["artist_id"] else None,
                                explicit=row["explicit"], cover_url=row["cover_url"]).model_dump(mode="json")

        data = await cached(f"hm:track:{track_id}", TTL_S, produce)
        return CatalogTrack.model_validate(data) if data else None

    async def _search_html(self, q: str) -> tuple[str, str]:
        return await self.provider.fetch_page("/search", params={"q": " ".join(q.split())})

    async def search_artists(self, q: str) -> list[CatalogArtist]:
        """Artists block of the site search («Исполнители»)."""
        q = " ".join(q.split())
        if not q:
            return []

        async def produce() -> list[dict]:
            html, base = await self._search_html(q)
            return [_artist(d).model_dump(mode="json") for d in parse_search_artists(html, base)]

        data = await cached(f"hm:search_artists:{q.lower()}", SEARCH_TTL_S, produce)
        return [CatalogArtist.model_validate(x) for x in data or []]

    async def search_albums(self, q: str) -> list[CatalogAlbum]:
        """Albums block of the site search («Альбомы»); the artist is not shown there."""
        q = " ".join(q.split())
        if not q:
            return []

        async def produce() -> list[dict]:
            html, base = await self._search_html(q)
            return [
                CatalogAlbum(
                    ref=make_ref("hm", d["id"]),
                    source="hm",
                    id=d["id"],
                    title=d["title"],
                    artist=d["artist"] or "",
                    year=d["year"],
                    cover_url=yandex_size(d["cover_url"], COVER_SIZE),
                    cover_small_url=yandex_size(d["cover_url"], COVER_SMALL_SIZE),
                ).model_dump(mode="json")
                for d in parse_search_albums(html, base)
            ]

        data = await cached(f"hm:search_albums:{q.lower()}", SEARCH_TTL_S, produce)
        return [CatalogAlbum.model_validate(x) for x in data or []]

    async def _produce_artist(self, artist_id: str) -> dict:
        html, base = await self.provider.fetch_page(f"/artist/{artist_id}")
        first = parse_artist_page(html, base)
        if not first["name"]:
            raise ProviderError(f"hitmoz: не удалось разобрать страницу артиста {artist_id}")
        candidates: list[Candidate] = list(first["tracks"])
        offset = PAGE_SIZE
        for _ in range(MAX_ARTIST_PAGES - 1):
            if offset > first["max_offset"] or len(first["tracks"]) < PAGE_SIZE:
                break
            page_html, page_base = await self.provider.fetch_page(f"/artist/{artist_id}/start/{offset}")
            page = parse_artist_page(page_html, page_base)
            if not page["tracks"]:
                break
            candidates.extend(page["tracks"])
            offset += PAGE_SIZE
        picture = first["picture_url"]
        if picture is None:  # the artist page has no picture; the search artist block does
            try:
                s_html, s_base = await self._search_html(first["name"])
                picture = next(
                    (a["picture_url"] for a in parse_search_artists(s_html, s_base) if a["id"] == artist_id), None
                )
            except ProviderError as e:
                log.info("hitmoz: no picture for artist %s: %s", artist_id, e)
        artist = _artist({"id": artist_id, "name": first["name"], "picture_url": picture})
        seen: set[str] = set()
        tracks: list[CatalogTrack] = []
        for c in candidates:
            if c.provider_id not in seen:
                seen.add(c.provider_id)
                tracks.append(track_from_candidate(c, artist_ref=artist.ref))
        return ArtistPage(artist=artist, top_tracks=tracks, albums=[]).model_dump(mode="json")

    async def get_artist(self, artist_id: str) -> ArtistPage:
        """`/artist/{id}` (+ up to 2 more pages of 48 tracks). Albums are not listed on hitmoz artist pages."""
        artist_id = str(artist_id)
        data = await cached(f"hm:artist:{artist_id}", TTL_S, lambda: self._produce_artist(artist_id))
        return ArtistPage.model_validate(data)

    async def _produce_album(self, album_id: str) -> dict:
        html, base = await self.provider.fetch_page(f"/album/{album_id}")
        d = parse_album_page(html, base)
        if not d["title"]:
            raise ProviderError(f"hitmoz: не удалось разобрать страницу альбома {album_id}")
        artist_ref = make_ref("hm", d["artist_id"]) if d["artist_id"] else None
        album = CatalogAlbum(
            ref=make_ref("hm", album_id),
            source="hm",
            id=album_id,
            title=d["title"],
            artist=d["artist"] or (d["tracks"][0].artist if d["tracks"] else ""),
            artist_ref=artist_ref,
            year=d["year"],
            cover_url=yandex_size(d["cover_url"], COVER_SIZE),
            cover_small_url=yandex_size(d["cover_url"], COVER_SMALL_SIZE),
            nb_tracks=len(d["tracks"]),
            genre=d["genre"],
        )
        album.tracks = [
            track_from_candidate(c, artist_ref=artist_ref, album=album, track_no=i)
            for i, c in enumerate(d["tracks"], start=1)
        ]
        return album.model_dump(mode="json")

    async def get_album(self, album_id: str) -> CatalogAlbum:
        """`/album/{id}`: «Название (Год)», artist, genre, cover and tracks in order."""
        album_id = str(album_id)
        data = await cached(f"hm:album:{album_id}", TTL_S, lambda: self._produce_album(album_id))
        return CatalogAlbum.model_validate(data)
