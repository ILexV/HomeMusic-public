"""rus.hitmos.fm provider (PLAN.md 3.3, fallback): Next.js site, MP3 links live in the RSC payload.

Track objects in the `self.__next_f.push([1, "..."])` payload look like
`{"id": 9355210, "title", "artist", "duration": 287, "play": "https://plc1.hitmos.fm/<b64>.mp3",
"download": "https://dwc1.hitmos.fm/<b64>.mp3", "image", "artistId", "artists"}` where `<b64>` encodes
`/mp3/{file_id}/Artist - Title (hitmos.fm).mp3` (download) or `/mp3/{file_id}/track.mp3` (play).
`id` is the song id of `/song/{id}`; `file_id` is a different internal id.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from app.config import HitmosFmCfg
from app.net import RateLimiter, make_client, request_with_backoff
from app.providers.base import Candidate, ProviderError, ProviderUnavailable, StreamSource, TrackQuery, staging_path
from app.providers.hitmoz import HEALTH_TTL_S, ProgressCb, abs_url, download_audio, parse_duration

log = logging.getLogger(__name__)

MAX_PARALLEL_DOWNLOADS = 2
_PUSH_RE = re.compile(r"self\.__next_f\.push\(")
_SONG_RE = re.compile(r"/song/(\d+)")
_ARTIST_RE = re.compile(r"/artist/(\d+)")
_TRACK_OBJ_RE = re.compile(r'\{"id":\d+,')


def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def rsc_payload(html: str) -> str:
    """Concatenated string chunks of `self.__next_f.push([1, "..."])` scripts."""
    dec = json.JSONDecoder()
    out: list[str] = []
    for m in _PUSH_RE.finditer(html):
        try:
            arr, _ = dec.raw_decode(html, m.end())
        except ValueError:
            continue
        if isinstance(arr, list) and len(arr) > 1 and isinstance(arr[1], str):
            out.append(arr[1])
    return "".join(out)


def parse_rsc_tracks(html: str) -> dict[str, dict]:
    """Track objects with a `download` link from the RSC payload, keyed by song id (str)."""
    payload = rsc_payload(html)
    dec = json.JSONDecoder()
    tracks: dict[str, dict] = {}
    for m in _TRACK_OBJ_RE.finditer(payload):
        try:
            obj, _ = dec.raw_decode(payload, m.start())
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("download"), str) and obj["download"]:
            tracks.setdefault(str(obj["id"]), obj)
    return tracks


def decode_file_path(url: str | None) -> str | None:
    """`https://dwc1.hitmos.fm/<b64>.mp3` -> `/mp3/{file_id}/Artist - Title (hitmos.fm).mp3`."""
    if not url:
        return None
    token = unquote(urlsplit(url).path.lstrip("/")).removesuffix(".mp3")
    try:
        return base64.b64decode(token + "=" * (-len(token) % 4), validate=False).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


def is_empty_search(html: str) -> bool:
    return not HTMLParser(html).css_first('a.track-item__title[href^="/song/"]') and not parse_rsc_tracks(html)


def parse_search(html: str, base_url: str) -> list[Candidate]:
    """Search page -> Candidates (HTML rows joined with RSC download links by song id)."""
    rsc = parse_rsc_tracks(html)
    out: list[Candidate] = []
    seen: set[str] = set()

    def make(song_id: str, artist: str, title: str, duration: int | None, artist_id: str | None) -> Candidate:
        obj = rsc.get(song_id, {})
        download = obj.get("download")
        file_path = decode_file_path(download)
        fm = re.match(r"/mp3/(\d+)/", file_path or "")
        return Candidate(
            provider="hitmos_fm",
            provider_id=song_id,
            artist=artist or str(obj.get("artist") or "").strip(),
            title=title or str(obj.get("title") or "").strip(),
            duration_s=duration or (int(obj["duration"]) if isinstance(obj.get("duration"), int) else None),
            page_url=abs_url(f"/song/{song_id}", base_url) or "",
            download_url=download,
            cover_url=obj.get("image") or None,
            extra={
                "play_url": obj.get("play"),
                "file_id": fm.group(1) if fm else None,
                "file_name": file_path.rsplit("/", 1)[-1] if file_path else None,
                "artist_id": artist_id or (str(obj["artistId"]) if obj.get("artistId") else None),
            },
        )

    for item in HTMLParser(html).css(".track-item"):
        link = item.css_first('a.track-item__title[href^="/song/"]')
        if link is None:
            continue
        m = _SONG_RE.search(link.attributes.get("href") or "")
        if not m or m.group(1) in seen:
            continue
        song_id = m.group(1)
        seen.add(song_id)
        artist_a = item.css_first("a.track-item__artist")
        am = _ARTIST_RE.search(artist_a.attributes.get("href") or "") if artist_a is not None else None
        dur = item.css_first(".track-item__duration")
        out.append(
            make(
                song_id,
                " ".join(artist_a.text().split()) if artist_a is not None else "",
                " ".join(link.text().split()),
                parse_duration(dur.text() if dur is not None else None),
                am.group(1) if am else None,
            )
        )
    # rows only present in the payload (e.g. markup changed): keep them too
    for song_id in rsc:
        if song_id not in seen:
            seen.add(song_id)
            out.append(make(song_id, "", "", None, None))
    return out


class HitmosFmProvider:
    """rus.hitmos.fm: search with direct dwc1 MP3 links (~320 kbps, no token)."""

    name = "hitmos_fm"

    def __init__(self, cfg: HitmosFmCfg, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self._own_client = client is None
        self.client = client or make_client()
        self.limiter = RateLimiter(cfg.rate_per_sec)
        self.download_sem = asyncio.Semaphore(MAX_PARALLEL_DOWNLOADS)
        self.retries = 3
        self.base_delay = 1.5
        self.health_ttl_s = HEALTH_TTL_S
        self._base_url: str | None = None
        self._checked_at = 0.0
        self._health: dict = {"provider": self.name, "domain": None, "ok": False, "latency_ms": None}
        self._base_lock = asyncio.Lock()

    async def _check_domains(self) -> str:
        errors: list[str] = []
        for domain in self.cfg.domains:
            url = domain if "://" in domain else f"https://{domain}/"
            t0 = time.monotonic()
            try:
                resp = await request_with_backoff(self.client, "GET", url, limiter=self.limiter, retries=0)
            except httpx.HTTPError as e:
                errors.append(f"{domain}: {e!r}")
                continue
            if resp.status_code != 200:
                errors.append(f"{domain}: HTTP {resp.status_code}")
                continue
            base = _origin(str(resp.url))
            self._health = {
                "provider": self.name,
                "domain": urlsplit(base).netloc,
                "base_url": base,
                "configured": domain,
                "ok": True,
                "latency_ms": round((time.monotonic() - t0) * 1000, 1),
                "checked_at": datetime.now(UTC).isoformat(),
                "error": None,
            }
            return base
        self._health = {
            "provider": self.name,
            "domain": None,
            "base_url": None,
            "ok": False,
            "latency_ms": None,
            "checked_at": datetime.now(UTC).isoformat(),
            "error": "; ".join(errors) or "no domains configured",
        }
        raise ProviderUnavailable(f"hitmos.fm: ни один домен не отвечает ({'; '.join(errors)})")

    async def base_url(self, force: bool = False) -> str:
        async with self._base_lock:
            if self._base_url and not force and time.monotonic() - self._checked_at < self.health_ttl_s:
                return self._base_url
            self._base_url = None
            self._checked_at = time.monotonic()
            self._base_url = await self._check_domains()
            return self._base_url

    def invalidate(self) -> None:
        self._base_url = None
        self._checked_at = 0.0

    async def health(self, force: bool = False) -> dict:
        """{provider, domain, base_url, ok, latency_ms, checked_at, error}."""
        try:
            await self.base_url(force=force)
        except ProviderUnavailable:
            pass
        return dict(self._health)

    async def fetch_page(self, path: str, params: dict | None = None) -> tuple[str, str]:
        """GET a site page (rate-limited, backoff). Returns (html, base_url)."""
        base = await self.base_url()
        try:
            resp = await request_with_backoff(
                self.client,
                "GET",
                base + path,
                limiter=self.limiter,
                params=params,
                retries=self.retries,
                base_delay=self.base_delay,
            )
        except httpx.HTTPError as e:
            self.invalidate()
            raise ProviderError(f"hitmos.fm: ошибка сети: {e!r}") from e
        if resp.status_code != 200:
            self.invalidate()
            raise ProviderError(f"hitmos.fm: HTTP {resp.status_code} на {path}")
        return resp.text, _origin(str(resp.url))

    async def search(self, q: TrackQuery, limit: int = 10) -> list[Candidate]:
        query = " ".join(f"{q.artist} {q.title}".split())
        if not query:
            return []
        html, base = await self.fetch_page("/search", params={"q": query})
        return parse_search(html, base)[:limit]

    async def enrich(self, c: Candidate) -> Candidate:
        """No extra details on hitmos.fm: returns a copy marked enriched."""
        return c.model_copy(update={"enriched": True})

    async def fetch(self, c: Candidate, dest_dir: Path, on_progress: ProgressCb | None = None) -> Path:
        """Download the dwc1 MP3 to a server-generated staging name."""
        if not c.download_url:
            raise ProviderError("hitmos.fm: нет ссылки на MP3")
        dest = staging_path(dest_dir, ".mp3")
        async with self.download_sem:
            return await download_audio(
                self.client,
                c.download_url,
                dest,
                limiter=self.limiter,
                on_progress=on_progress,
                retries=self.retries,
                base_delay=self.base_delay,
                label="hitmos.fm",
            )

    async def stream(self, c: Candidate) -> StreamSource:
        """The same dwc1 MP3 as `fetch`, for listening through the Hub."""
        if not c.download_url:
            raise ProviderError("hitmos.fm: нет ссылки на MP3")
        return StreamSource(url=c.download_url, client=self.client)

    async def aclose(self) -> None:
        if self._own_client:
            await self.client.aclose()
