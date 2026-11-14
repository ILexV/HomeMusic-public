"""Download provider interface (PLAN.md 8.2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import uuid4

import httpx
from pydantic import BaseModel


class TrackQuery(BaseModel):
    artist: str
    title: str
    album: str | None = None
    duration_s: int | None = None
    isrc: str | None = None
    explicit: bool | None = None
    version: str | None = None  # remix/live/... only if explicitly requested
    lang: str | None = None  # "ru" forces the Russian source order


class Candidate(BaseModel):
    provider: str
    provider_id: str
    artist: str
    title: str
    duration_s: int | None = None
    bitrate_kbps: int | None = None
    explicit: bool | None = None
    upload_date: date | None = None
    page_url: str
    download_url: str | None = None
    cover_url: str | None = None
    album: str | None = None
    year: int | None = None
    score: float = 0
    enriched: bool = False
    # free-form provider data (e.g. full h1 title with version, quality options)
    extra: dict = {}


FILE_REF = "file"


def file_ref(provider: str, provider_id: str) -> str:
    """A provider's file named like a catalog track ("file:hitmoz:123"): listened to and downloaded by this ref
    (the Files tab of the search, the app's player)."""
    return f"{FILE_REF}:{provider}:{provider_id}"


@dataclass
class StreamSource:
    """Where a candidate's audio can be streamed from (listening before download): GET `url` with `headers`."""

    url: str
    client: httpx.AsyncClient
    headers: dict[str, str] = field(default_factory=dict)
    media_type: str = "audio/mpeg"  # used when the upstream sends a generic content type


class ProviderError(Exception):
    """Provider failed (site down, layout changed, blocked). The resolver moves on to the next provider."""


class ProviderUnavailable(ProviderError):
    """No healthy domain / service unreachable."""


def staging_path(dest_dir: Path, suffix: str = "") -> Path:
    """A server-generated file name, independent of any provider identifiers or titles."""
    root = Path(dest_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return contained_path(root, root / f"audio_{uuid4().hex}{suffix}")


def contained_path(dest_dir: Path, path: Path) -> Path:
    root, path = Path(dest_dir).resolve(), Path(path).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ProviderError("Provider output escaped its staging directory")
    return path


@runtime_checkable
class Provider(Protocol):
    name: str

    async def search(self, q: TrackQuery, limit: int = 10) -> list[Candidate]:
        """Find candidates for the query. Raw results: filtering/scoring is done by app.matching."""
        ...

    async def enrich(self, c: Candidate) -> Candidate:
        """Fetch extra details (e.g. hitmoz track page: h1 with Explicit, real bitrate). Returns a copy."""
        ...

    async def fetch(self, c: Candidate, dest_dir: Path) -> Path:
        """Download the audio file into dest_dir (streaming) and return its path."""
        ...

    async def stream(self, c: Candidate) -> StreamSource:
        """Upstream of the file `fetch` would save, for listening through the Hub without downloading it."""
        ...

    async def aclose(self) -> None: ...
