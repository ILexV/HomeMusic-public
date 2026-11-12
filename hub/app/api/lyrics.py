"""Account-authenticated LRCLIB lookup; never infer timing from plain lyrics."""
from __future__ import annotations

import asyncio
import math
import re
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from time import monotonic
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.auth import current_user, user_navidrome
from app.models import User

router = APIRouter(prefix="/api/lyrics", tags=["lyrics"])
PROVIDER_URL = "https://lrclib.net/api/get"
MAX_TEXT = 128_000
MAX_LINES = 3000


class LyricsIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    ref: str | None = Field(default=None, min_length=3, max_length=256)
    nd_song_id: str | None = Field(default=None, min_length=1, max_length=128)
    title: str = Field(default="", max_length=512)
    artist: str = Field(default="", max_length=512)
    album: str = Field(default="", max_length=512)
    duration_s: float | None = Field(default=None, gt=0, le=86400, allow_inf_nan=False)

    @model_validator(mode="after")
    def target(self):
        if self.ref and self.nd_song_id:
            raise ValueError("Укажите один идентификатор трека")
        if self.ref:
            if not re.fullmatch(r"(?:dz|hm|mb|nd|file):[\w:.-]+", self.ref):
                raise ValueError("Неверная ссылка на трек")
        if not (self.ref or self.nd_song_id) and not (self.title and self.artist):
            raise ValueError("Для локального файла нужны название и исполнитель")
        return self


class LyricLine(BaseModel):
    text: str
    time_s: float | None = None


class LyricsOut(BaseModel):
    status: Literal["synced", "plain", "instrumental", "unavailable"]
    lines: list[LyricLine] = Field(default_factory=list)
    source: str = "LRCLIB"


@dataclass(frozen=True)
class Metadata:
    title: str
    artist: str
    album: str
    duration_s: float | None


_STAMP = re.compile(r"\[(\d{1,3}):([0-5]\d)(?:[.:](\d{1,3}))?\]")
_OFFSET = re.compile(r"\[offset:\s*([+-]?\d+)\s*\]", re.IGNORECASE)


def parse_lrc(text: str) -> list[LyricLine]:
    """Expand repeated stamps; apply the global millisecond offset, then group simultaneous lines.

    LRC's positive offset makes text appear earlier. Empty timestamped lines are
    retained to clear the highlight during instrumental gaps.
    """
    if len(text) > MAX_TEXT:
        return []
    offsets = _OFFSET.findall(text)
    offset = int(offsets[-1]) / 1000 if offsets else 0.0
    timed: list[tuple[float, str]] = []
    expanded_size = 0
    for raw in text.splitlines():
        raw = raw.strip().lstrip("\ufeff")
        stamps = []
        rest = raw
        while (match := _STAMP.match(rest)) is not None:
            fraction = match[3] or ""
            seconds = int(match[1]) * 60 + int(match[2]) + (int(fraction) / 10 ** len(fraction) if fraction else 0)
            stamps.append(max(0.0, seconds - offset))
            rest = rest[match.end():].lstrip()
        for stamp in stamps:
            expanded_size += len(rest) + 1
            timed.append((stamp, rest))
            if len(timed) > MAX_LINES or expanded_size > MAX_TEXT:
                return []
    timed.sort(key=lambda line: line[0])  # stable: translations keep provider order
    lines: list[LyricLine] = []
    for stamp, text in timed:
        if lines and lines[-1].time_s == stamp:
            if text:
                lines[-1].text = "\n".join(filter(None, (lines[-1].text, text)))
        else:
            lines.append(LyricLine(text=text, time_s=stamp))
    return lines


def normalized(text: str) -> str:
    # Keep every word, including live/remix/edit/remaster qualifiers. Do not
    # fuzzy-match a different recording just because the base title is similar.
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(re.findall(r"\w+", text))


def version_matches(meta: Metadata, record: dict) -> bool:
    if not normalized(meta.title) or not normalized(meta.artist):
        return False
    if normalized(meta.title) != normalized(str(record.get("trackName") or "")):
        return False
    if normalized(meta.artist) != normalized(str(record.get("artistName") or "")):
        return False
    if meta.album and normalized(meta.album) != normalized(str(record.get("albumName") or "")):
        return False
    try:
        duration = float(record.get("duration"))
    except (ValueError, TypeError):
        return False
    if not math.isfinite(duration) or duration <= 0:
        return False
    # With no duration and no album there is insufficient evidence of the version.
    if meta.duration_s is None:
        return bool(meta.album)
    return abs(duration - meta.duration_s) <= 2.0


async def resolve_metadata(body: LyricsIn, user: User) -> Metadata | None:
    """Never trust supplied titles when an authoritative ID is available."""
    song_id = body.nd_song_id
    if body.ref and body.ref.startswith("nd:"):
        song_id = body.ref[3:]
    if song_id:
        nd = user_navidrome(user)
        try:
            song = await nd.get_song(song_id)
        finally:
            await nd.aclose()
        if not song or str(song.get("id")) != song_id:
            return None
        return Metadata(song.get("title") or "", song.get("artist") or "", song.get("album") or "",
                        float(song["duration"]) if song.get("duration") else None)
    if body.ref:
        from app.mixes.resolve import catalog_track
        track = await catalog_track(body.ref)
        if track:
            return Metadata(track.title, track.artist, track.album_title or "", track.duration_s)
        from app.listen import file_candidate
        from app.providers.base import Candidate
        from app.services import svc
        candidate = None
        if body.ref.startswith("file:"):
            candidate = await file_candidate(body.ref)
        elif body.ref.startswith("hm:") and "hitmoz" in svc.providers:
            ident = body.ref[3:]
            candidate = await svc.providers["hitmoz"].enrich(
                Candidate(provider="hitmoz", provider_id=ident, artist="", title="", page_url=f"/song/{ident}"))
        if candidate:
            return Metadata(candidate.title, candidate.artist, "", candidate.duration_s)
        return None
    return Metadata(body.title, body.artist, body.album, body.duration_s)


# Public provider content only, bounded both by entry count and accepted text size.
_cache: OrderedDict[Metadata, tuple[float, LyricsOut]] = OrderedDict()


async def fetch_lyrics(meta: Metadata) -> LyricsOut:
    cached = _cache.get(meta)
    if cached and cached[0] > monotonic():
        _cache.move_to_end(meta)
        return cached[1]
    params = {"track_name": meta.title, "artist_name": meta.artist}
    if meta.album:
        params["album_name"] = meta.album
    if meta.duration_s:
        params["duration"] = str(meta.duration_s)
    result = LyricsOut(status="unavailable")
    async with httpx.AsyncClient(timeout=8.0, follow_redirects=False,
                                 headers={"User-Agent": "HomeMusic/1.0 (desktop synchronized lyrics)"}) as client:
        async with client.stream("GET", PROVIDER_URL, params=params) as response:
            if response.status_code == 404:
                record = None
            else:
                response.raise_for_status()
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 1024 * 1024:
                        return result
                import json
                record = json.loads(content)
    if isinstance(record, dict) and version_matches(meta, record):
        if record.get("instrumental") is True:
            result = LyricsOut(status="instrumental")
        else:
            synced = record.get("syncedLyrics")
            plain = record.get("plainLyrics")
            lines = parse_lrc(synced) if isinstance(synced, str) else []
            if lines:
                result = LyricsOut(status="synced", lines=lines)
            elif isinstance(plain, str) and len(plain) <= MAX_TEXT and plain.strip():
                result = LyricsOut(status="plain", lines=[LyricLine(text=line) for line in plain.splitlines()[:MAX_LINES]])
    _cache[meta] = (monotonic() + (86400 if result.status != "unavailable" else 300), result)
    _cache.move_to_end(meta)
    while len(_cache) > 128:
        _cache.popitem(last=False)
    return result


@router.post("", response_model=LyricsOut)
async def lyrics(body: LyricsIn, user: User = Depends(current_user)):
    try:
        async with asyncio.timeout(12):
            meta = await resolve_metadata(body, user)
            if meta is None or not meta.title or not meta.artist:
                return LyricsOut(status="unavailable")
            return await fetch_lyrics(meta)
    except HTTPException:
        raise
    except Exception:
        # Provider adapters also have their own error classes. Lyrics availability
        # must not break playback, and upstream details must not leak to clients.
        return LyricsOut(status="unavailable")
