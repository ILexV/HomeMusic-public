"""ru.hitmoz.org provider (PLAN.md 3.3): server-rendered HTML, needs `Accept-Language: ru`, 302 to a CDN for files."""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import os
import random
import re
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode as Node

from app.config import HitmozCfg
from app.net import RETRY_STATUSES, RateLimiter, make_client, request_with_backoff
from app.providers.base import Candidate, ProviderError, ProviderUnavailable, StreamSource, TrackQuery, staging_path

log = logging.getLogger(__name__)

PAGE_SIZE = 48
MAX_SEARCH_PAGES = 2
HEALTH_TTL_S = 600
MIN_AUDIO_BYTES = 100 * 1024
EMPTY_SEARCH_TEXT = "По вашему запросу ничего не найдено"

_SONG_RE = re.compile(r"/song/(\d+)")
_ARTIST_RE = re.compile(r"/artist/(\d+)")
_ALBUM_RE = re.compile(r"/album/(\d+)")
_UPLOAD_RE = re.compile(r"/get/music/(\d{8})/")
_BITRATE_RE = re.compile(r"(\d{2,4})\s*kbps", re.IGNORECASE)
_SIZE_RE = re.compile(r"([\d.,]+)\s*(?:Мб|MB)", re.IGNORECASE)
_TIME_RE = re.compile(r"(?<![\d:])(\d{1,2}(?::\d{2}){1,2})\s*$")
_BG_URL_RE = re.compile(r"url\(['\"]?([^'\")]+)['\"]?\)")
_TITLE_YEAR_RE = re.compile(r"^(.*?)\s*\((\d{4})\)\s*$", re.DOTALL)
_YANDEX_SIZE_RE = re.compile(r"/\d+x\d+$")
_ARTIST_H1_SUFFIX_RE = re.compile(r"\s*-\s*слушать все песни\s*$", re.IGNORECASE)

ProgressCb = Callable[[float], None]


# ---------------------------------------------------------------- helpers


def _text(node: Node | None) -> str:
    return " ".join(node.text(separator=" ").split()) if node is not None else ""


def parse_duration(s: str | None) -> int | None:
    """'04:44' / '1:02:03' -> seconds."""
    if not s:
        return None
    m = _TIME_RE.search(s.strip())
    if not m:
        return None
    total = 0
    for part in m.group(1).split(":"):
        total = total * 60 + int(part)
    return total


def upload_date_from_url(url: str | None) -> date | None:
    """`/get/music/{YYYYMMDD}/...` -> date (hitmoz upload date)."""
    m = _UPLOAD_RE.search(url or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def abs_url(url: str | None, base_url: str) -> str | None:
    """Absolute URL (handles relative and protocol-relative `//host/...`)."""
    if not url:
        return None
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    return urljoin(base_url.rstrip("/") + "/", url)


def bg_image(node: Node | None, base_url: str) -> str | None:
    """URL from `style="background-image: url('...')"`."""
    if node is None:
        return None
    m = _BG_URL_RE.search(node.attributes.get("style") or "")
    return clean_image_url(abs_url(m.group(1), base_url)) if m else None


def clean_image_url(url: str | None) -> str | None:
    """Drop placeholder images (Deezer URLs with an empty hash)."""
    if not url or "/images/artist//" in url or "/images/cover//" in url:
        return None
    return url


def yandex_size(url: str | None, size: str) -> str | None:
    """Resize an avatars.yandex.net URL (`.../150x150` -> `.../{size}`); other hosts unchanged."""
    if url and "avatars.yandex.net" in url:
        return _YANDEX_SIZE_RE.sub(f"/{size}", url)
    return url


def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def _parse_meta(raw: str | None) -> dict:
    """`data-musmeta` JSON (selectolax already unescapes HTML entities)."""
    if not raw:
        return {}
    for candidate in (raw, html_lib.unescape(raw)):
        try:
            data = json.loads(candidate)
            return data if isinstance(data, dict) else {}
        except ValueError:
            continue
    return {}


def _is_full_download(url: str | None) -> bool:
    return bool(url) and "/get/music/" in url and "/get/cuts/" not in url


# ---------------------------------------------------------------- pure parsers


def detect_canonical(html: str) -> str | None:
    """href of `<link rel="canonical">` (the real current host of a mirror)."""
    node = HTMLParser(html).css_first('link[rel="canonical"]')
    href = node.attributes.get("href") if node is not None else None
    return href.strip() if href else None


def is_empty_search(html: str) -> bool:
    return EMPTY_SEARCH_TEXT in html


def parse_track_items(tree: HTMLParser | Node, base_url: str) -> list[Candidate]:
    """All `li.tracks__item` / `.tracks__item[data-musmeta]` rows under `tree` as Candidates."""
    out: list[Candidate] = []
    seen: set[str] = set()
    for li in tree.css(".tracks__item[data-musmeta]"):
        meta = _parse_meta(li.attributes.get("data-musmeta"))
        link = li.css_first('a.track__info-l[href^="/song/"]')
        song_id = None
        if link is not None:
            m = _SONG_RE.search(link.attributes.get("href") or "")
            song_id = m.group(1) if m else None
        if song_id is None:
            raw_id = str(meta.get("id") or li.attributes.get("data-musid") or "")
            song_id = raw_id.removeprefix("track-id-") or None
        if not song_id or not song_id.isdigit() or song_id in seen:
            continue
        seen.add(song_id)
        title = _text(link.css_first(".track__title")) if link is not None else ""
        artist = _text(link.css_first(".track__desc")) if link is not None else ""
        title = title or str(meta.get("title") or "").strip()
        artist = artist or str(meta.get("artist") or "").strip()
        dl = li.css_first('a.track__download-btn[href^="/get/music/"]')
        download_url = abs_url(dl.attributes.get("href"), base_url) if dl is not None else None
        if download_url is None and _is_full_download(meta.get("url")):
            download_url = abs_url(meta["url"], base_url)
        cover = clean_image_url(abs_url(meta.get("img"), base_url)) or bg_image(li.css_first(".track__img"), base_url)
        out.append(
            Candidate(
                provider="hitmoz",
                provider_id=song_id,
                artist=artist,
                title=title,
                duration_s=parse_duration(_text(li.css_first(".track__fulltime"))),
                bitrate_kbps=None,
                explicit=None,
                upload_date=upload_date_from_url(download_url),
                page_url=abs_url(f"/song/{song_id}", base_url) or "",
                download_url=download_url,
                cover_url=cover,
            )
        )
    return out


def parse_search(html: str, base_url: str) -> list[Candidate]:
    """Tracks of a search results page (empty list for «ничего не найдено»)."""
    if is_empty_search(html):
        return []
    return parse_track_items(HTMLParser(html), base_url)


def has_next_page(html: str, offset: int) -> bool:
    """True if the pagination links to `/start/{offset + 48}`."""
    return f"/start/{offset + PAGE_SIZE}" in html


def parse_search_artists(html: str, base_url: str) -> list[dict]:
    """Artist block («Исполнители») of a search page: [{id, name, picture_url}]."""
    out: list[dict] = []
    for a in HTMLParser(html).css('ul.singers-list a[href^="/artist/"]'):
        m = _ARTIST_RE.search(a.attributes.get("href") or "")
        name = _text(a.css_first(".album-title"))
        if m and name:
            out.append({"id": m.group(1), "name": name, "picture_url": bg_image(a.css_first(".album-image"), base_url)})
    return out


def parse_search_albums(html: str, base_url: str) -> list[dict]:
    """Album block («Альбомы») of a search page: [{id, title, year, artist, cover_url}]."""
    out: list[dict] = []
    for a in HTMLParser(html).css('ul.album-list a[href^="/album/"]'):
        m = _ALBUM_RE.search(a.attributes.get("href") or "")
        if not m:
            continue
        title, year = split_title_year(_text(a.css_first(".album-title")))
        out.append(
            {
                "id": m.group(1),
                "title": title,
                "year": year,
                "artist": _text(a.css_first(".album-singer")) or None,
                "cover_url": bg_image(a.css_first(".album-image"), base_url),
            }
        )
    return out


def split_title_year(s: str) -> tuple[str, int | None]:
    """'Акустический альбом (1998)' -> ('Акустический альбом', 1998)."""
    s = " ".join(s.split())
    m = _TITLE_YEAR_RE.match(s)
    if m and m.group(1):
        return m.group(1).strip(), int(m.group(2))
    return s, None


def _explicit_flag(full_title: str) -> bool | None:
    parts = re.findall(r"[(\[]([^()\[\]]*)", full_title)
    words = " ".join(parts).lower()
    if re.search(r"\bexplicit\b", words):
        return True
    if re.search(r"\bclean\b", words):
        return False
    return None


def parse_track_page(html: str, base_url: str) -> dict:
    """`/song/{id}`: full h1 title, explicit flag, quality options, best `/get/music/` link, duration.

    Returns keys: id, full_title, heading, version, explicit, options [{url, bitrate_kbps, size_mb}],
    download_url, bitrate_kbps, size_mb, duration_s, upload_date, artist_id, artist, title, cover_url.
    """
    tree = HTMLParser(html)
    h1 = tree.css_first("h1.p-track-title")
    lines = [" ".join(ln.split()) for ln in (h1.text() if h1 is not None else "").splitlines()]
    lines = [ln for ln in lines if ln]
    full_title = " ".join(lines)
    heading = lines[0] if lines else ""
    version = " ".join(lines[1:]) or None
    if version and version.startswith("(") and version.endswith(")"):
        version = version[1:-1].strip()

    options: list[dict] = []
    for opt in tree.css("select.p-track-select option"):
        url = abs_url(opt.attributes.get("value"), base_url)
        label = _text(opt).replace("\xa0", " ")
        br = _BITRATE_RE.search(label)
        size = _SIZE_RE.search(label)
        options.append(
            {
                "url": url,
                "bitrate_kbps": int(br.group(1)) if br else None,
                "size_mb": float(size.group(1).replace(",", ".")) if size else None,
            }
        )
    full = [o for o in options if _is_full_download(o["url"])]
    best = max(full, key=lambda o: o["bitrate_kbps"] or 0) if full else None
    download_url = best["url"] if best else None
    if download_url is None:
        btn = tree.css_first("a.p-track-download__btn")
        href = abs_url(btn.attributes.get("href"), base_url) if btn is not None else None
        download_url = href if _is_full_download(href) else None

    info = tree.css_first(".p-track-info__text")
    info_text = _text(info).replace("\xa0", " ")
    info_br = _BITRATE_RE.search(info_text)
    info_size = _SIZE_RE.search(info_text)
    bitrate = (best or {}).get("bitrate_kbps") or (int(info_br.group(1)) if info_br else None)
    size_mb = (best or {}).get("size_mb") or (float(info_size.group(1).replace(",", ".")) if info_size else None)

    artist_id = None
    artist_link = info.css_first('a[href^="/artist/"]') if info is not None else None
    if artist_link is not None:
        m = _ARTIST_RE.search(artist_link.attributes.get("href") or "")
        artist_id = m.group(1) if m else None

    item = tree.css_first(".p-track .tracks__item[data-musmeta]") or tree.css_first(".tracks__item[data-musmeta]")
    meta = _parse_meta(item.attributes.get("data-musmeta")) if item is not None else {}
    song_id = None
    canonical = detect_canonical(html)
    for src in (canonical, str(meta.get("id") or "")):
        m = _SONG_RE.search(src or "") or re.search(r"track-id-(\d+)", src or "")
        if m:
            song_id = m.group(1)
            break

    return {
        "id": song_id,
        "full_title": full_title,
        "heading": heading,
        "version": version,
        "explicit": _explicit_flag(full_title),
        "options": options,
        "download_url": download_url,
        "bitrate_kbps": bitrate,
        "size_mb": size_mb,
        "duration_s": parse_duration(info_text),
        "upload_date": upload_date_from_url(download_url),
        "artist_id": artist_id,
        "artist": _text(artist_link) or str(meta.get("artist") or "").strip() or None,
        "title": str(meta.get("title") or "").strip() or None,
        "cover_url": clean_image_url(abs_url(meta.get("img"), base_url)),
    }


def parse_artist_page(html: str, base_url: str) -> dict:
    """`/artist/{id}[/start/{n}]`: {id, name, picture_url, tracks: [Candidate], max_offset}.

    The artist page lists tracks only (no albums, no artist picture as of 2026-10).
    """
    tree = HTMLParser(html)
    canonical = detect_canonical(html) or ""
    m = _ARTIST_RE.search(canonical)
    artist_id = m.group(1) if m else None
    name = _ARTIST_H1_SUFFIX_RE.sub("", _text(tree.css_first("h1.p-info-title")))
    if not name:
        crumbs = tree.css(".breadcrumb .breadcrumb-item")
        name = _text(crumbs[-1]) if crumbs else ""
    picture = None
    if artist_id:  # the artist may be in the «топ исполнители» sidebar
        a = tree.css_first(f'.top-singers a[href="/artist/{artist_id}"]')
        picture = bg_image(a.css_first(".top-singer-image"), base_url) if a is not None else None
    content = tree.css_first("ul.tracks__list") or tree
    id_pat = re.escape(artist_id) if artist_id else r"\d+"
    offsets = [int(x) for x in re.findall(rf"/artist/{id_pat}/start/(\d+)", html)]
    return {
        "id": artist_id,
        "name": name,
        "picture_url": picture,
        "tracks": parse_track_items(content, base_url),
        "max_offset": max(offsets, default=0),
    }


def parse_album_page(html: str, base_url: str) -> dict:
    """`/album/{id}`: {id, title, year, artist, artist_id, artists, genre, cover_url, tracks: [Candidate]}."""
    tree = HTMLParser(html)
    m = _ALBUM_RE.search(detect_canonical(html) or "")
    title, year = split_title_year(_text(tree.css_first("h1.p-info-title")))
    artist_links = tree.css('.album--info a[href^="/artist/"]')
    artists = [_text(a) for a in artist_links if _text(a)]
    artist_id = None
    if artist_links:
        am = _ARTIST_RE.search(artist_links[0].attributes.get("href") or "")
        artist_id = am.group(1) if am else None
    genre = tree.css_first('.album--info a[href^="/genre/"]')
    cover = bg_image(tree.css_first(".p-info--image"), base_url)
    if cover is None:
        og = tree.css_first('meta[property="og:image"]')
        cover = clean_image_url(abs_url(og.attributes.get("content"), base_url)) if og is not None else None
    content = tree.css_first(".p-info ul.tracks__list") or tree.css_first("ul.tracks__list") or tree
    return {
        "id": m.group(1) if m else None,
        "title": title,
        "year": year,
        "artist": artists[0] if artists else None,
        "artists": artists,
        "artist_id": artist_id,
        "genre": _text(genre) or None,
        "cover_url": cover,
        "tracks": parse_track_items(content, base_url),
    }


# ---------------------------------------------------------------- download


async def download_audio(
    client: httpx.AsyncClient,
    url: str,
    dest: Path,
    *,
    headers: dict[str, str] | None = None,
    limiter: RateLimiter | None = None,
    on_progress: ProgressCb | None = None,
    retries: int = 3,
    base_delay: float = 1.5,
    label: str = "provider",
) -> Path:
    """Stream `url` (following redirects) to `dest` via `dest.part`; verify audio content type and size."""
    part = dest.with_name(dest.name + ".part")
    resp: httpx.Response | None = None
    try:
        for attempt in range(retries + 1):
            if limiter:
                await limiter.wait()
            try:
                req = client.build_request("GET", url, headers=headers)
                resp = await client.send(req, stream=True, follow_redirects=True)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                if attempt >= retries:
                    raise ProviderError(f"{label}: ошибка сети при скачивании: {e!r}") from e
                await asyncio.sleep(base_delay * 2**attempt + random.uniform(0, 0.5))
                continue
            if resp.status_code in RETRY_STATUSES and attempt < retries:
                await resp.aclose()
                log.warning("%s download %s -> %s, retrying", label, url, resp.status_code)
                await asyncio.sleep(base_delay * 2**attempt + random.uniform(0, 0.5))
                continue
            break
        assert resp is not None
        if resp.status_code != 200:
            raise ProviderError(f"{label}: файл недоступен (HTTP {resp.status_code})")
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if not (ctype.startswith("audio/") or ctype == "application/octet-stream"):
            raise ProviderError(f"{label}: вместо аудио пришёл {ctype or 'неизвестный тип'}")
        total = int(resp.headers.get("content-length") or 0) or None
        if total is not None and total < MIN_AUDIO_BYTES:
            raise ProviderError(f"{label}: файл слишком мал ({total} байт)")
        dest.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        buf = bytearray()
        f = await asyncio.to_thread(open, part, "xb")
        try:
            async for chunk in resp.aiter_bytes(64 * 1024):
                buf += chunk
                size += len(chunk)
                if len(buf) >= 1024 * 1024:
                    await asyncio.to_thread(f.write, bytes(buf))
                    buf.clear()
                if on_progress and total:
                    on_progress(min(size / total, 1.0))
            if buf:
                await asyncio.to_thread(f.write, bytes(buf))
        finally:
            await asyncio.to_thread(f.close)
        if size < MIN_AUDIO_BYTES:
            raise ProviderError(f"{label}: файл слишком мал ({size} байт)")
        if total is not None and size != total:
            raise ProviderError(f"{label}: файл скачан не полностью ({size} из {total} байт)")
        os.replace(part, dest)
        if on_progress:
            on_progress(1.0)
        return dest
    except httpx.HTTPError as e:
        raise ProviderError(f"{label}: ошибка при скачивании: {e!r}") from e
    finally:
        if resp is not None:
            await resp.aclose()
        if part.exists():
            part.unlink(missing_ok=True)


# ---------------------------------------------------------------- provider


class HitmozProvider:
    """ru.hitmoz.org: search, track page enrichment, polite downloads with domain fallback."""

    name = "hitmoz"

    def __init__(self, cfg: HitmozCfg, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.headers = {"Accept-Language": cfg.accept_language}
        self._own_client = client is None
        self.client = client or make_client(headers=self.headers)
        self.limiter = RateLimiter(cfg.rate_per_sec)
        self.download_sem = asyncio.Semaphore(max(1, cfg.max_parallel_downloads))
        self.retries = 3
        self.base_delay = 1.5
        self.health_ttl_s = HEALTH_TTL_S
        self._base_url: str | None = None
        self._checked_at = 0.0
        self._health: dict = {"provider": self.name, "domain": None, "ok": False, "latency_ms": None}
        self._base_lock = asyncio.Lock()

    # -- domains / health

    async def _check_domains(self) -> str:
        failed: set[str] = set()
        errors: list[str] = []
        for domain in self.cfg.domains:
            url = domain if "://" in domain else f"https://{domain}/"
            t0 = time.monotonic()
            try:
                resp = await request_with_backoff(
                    self.client, "GET", url, limiter=self.limiter, headers=self.headers, retries=0
                )
            except httpx.HTTPError as e:
                failed.add(urlsplit(url).netloc)
                errors.append(f"{domain}: {e!r}")
                continue
            latency = round((time.monotonic() - t0) * 1000, 1)
            if resp.status_code != 200:
                failed.add(urlsplit(url).netloc)
                errors.append(f"{domain}: HTTP {resp.status_code}")
                continue
            base = _origin(str(resp.url))
            canonical = detect_canonical(resp.text)
            if canonical and canonical.startswith("http"):
                canon_base = _origin(canonical)
                if urlsplit(canon_base).netloc not in failed:
                    base = canon_base
            self._health = {
                "provider": self.name,
                "domain": urlsplit(base).netloc,
                "base_url": base,
                "configured": domain,
                "ok": True,
                "latency_ms": latency,
                "checked_at": datetime.now(UTC).isoformat(),
                "error": None,
            }
            log.info("hitmoz: using %s (via %s, %.0f ms)", base, domain, latency)
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
        raise ProviderUnavailable(f"hitmoz: ни один домен не отвечает ({'; '.join(errors)})")

    async def base_url(self, force: bool = False) -> str:
        """Healthy base URL (`https://host`), re-checked every ~10 min or after a failure."""
        async with self._base_lock:
            fresh = time.monotonic() - self._checked_at < self.health_ttl_s
            if self._base_url and fresh and not force:
                return self._base_url
            self._base_url = None
            self._checked_at = time.monotonic()
            self._base_url = await self._check_domains()
            return self._base_url

    def invalidate(self) -> None:
        """Forget the cached base URL (the next request re-runs the health check)."""
        self._base_url = None
        self._checked_at = 0.0

    async def health(self, force: bool = False) -> dict:
        """{provider, domain, base_url, ok, latency_ms, checked_at, error} for the health endpoint."""
        try:
            await self.base_url(force=force)
        except ProviderUnavailable:
            pass
        return dict(self._health)

    # -- pages

    async def fetch_page(self, path: str, params: dict | None = None) -> tuple[str, str]:
        """GET a site page (rate-limited, backoff, domain fallback). Returns (html, base_url)."""
        last_error: Exception | None = None
        for attempt in range(2):
            base = await self.base_url()
            url = base + path
            try:
                resp = await request_with_backoff(
                    self.client,
                    "GET",
                    url,
                    limiter=self.limiter,
                    headers=self.headers,
                    params=params,
                    retries=self.retries,
                    base_delay=self.base_delay,
                )
            except httpx.HTTPError as e:
                last_error = ProviderError(f"hitmoz: ошибка сети: {e!r}")
                self.invalidate()
                continue
            if resp.status_code == 200:
                return resp.text, _origin(str(resp.url))
            if resp.status_code == 404:
                raise ProviderError(f"hitmoz: страница не найдена: {path}")
            last_error = ProviderError(f"hitmoz: HTTP {resp.status_code} на {path}")
            self.invalidate()
            if attempt == 0:
                log.warning("hitmoz: %s -> %s, re-checking domains", url, resp.status_code)
        assert last_error is not None
        raise last_error

    # -- Provider API

    async def search(self, q: TrackQuery, limit: int = 10) -> list[Candidate]:
        """Search `"artist title"`; reads a second page (`/search/start/48`) only if needed."""
        query = " ".join(f"{q.artist} {q.title}".split())
        if not query:
            return []
        results: list[Candidate] = []
        seen: set[str] = set()
        for page in range(MAX_SEARCH_PAGES):
            offset = page * PAGE_SIZE
            path = "/search" if page == 0 else f"/search/start/{offset}"
            html, base = await self.fetch_page(path, params={"q": query})
            if is_empty_search(html):
                break
            items = parse_search(html, base)
            if page == 0 and not items and "tracks__item" not in html:
                log.warning("hitmoz: search page without tracks and without empty marker (layout change?)")
            for c in items:
                if c.provider_id not in seen:
                    seen.add(c.provider_id)
                    results.append(c)
            if len(results) >= limit or len(items) < PAGE_SIZE or not has_next_page(html, offset):
                break
        return results[:limit]

    async def enrich(self, c: Candidate) -> Candidate:
        """Track page: full h1 title (version, Explicit/Clean), best `/get/music/` link and bitrate."""
        if not re.fullmatch(r"[0-9]+", c.provider_id):
            raise ProviderError("hitmoz: неверный идентификатор трека")
        html, base = await self.fetch_page(f"/song/{c.provider_id}")
        d = parse_track_page(html, base)
        if not d["full_title"] and not d["options"]:
            raise ProviderError("hitmoz: не удалось разобрать страницу трека (вёрстка изменилась?)")
        extra = dict(c.extra)
        extra.update(
            full_title=d["full_title"],
            version=d["version"],
            options=d["options"],
            artist_id=d["artist_id"],
            size_mb=d["size_mb"],
        )
        update: dict = {"enriched": True, "extra": extra, "page_url": abs_url(f"/song/{c.provider_id}", base)}
        if d["explicit"] is not None:
            update["explicit"] = d["explicit"]
        if d["download_url"]:
            update["download_url"] = d["download_url"]
            update["upload_date"] = d["upload_date"] or c.upload_date
        if d["bitrate_kbps"]:
            update["bitrate_kbps"] = d["bitrate_kbps"]
        if d["duration_s"]:
            update["duration_s"] = d["duration_s"]
        if not c.cover_url and d["cover_url"]:
            update["cover_url"] = d["cover_url"]
        return c.model_copy(update=update)

    async def fetch(self, c: Candidate, dest_dir: Path, on_progress: ProgressCb | None = None) -> Path:
        """Download the 320 kbps `/get/music/` file (302 -> CDN) to a server-generated staging name."""
        if not _is_full_download(c.download_url):
            c = await self.enrich(c)
        if not _is_full_download(c.download_url):
            raise ProviderError("hitmoz: нет ссылки на полный MP3")
        assert c.download_url is not None
        url = c.download_url
        if url.startswith("/"):
            url = (await self.base_url()) + url
        dest = staging_path(dest_dir, ".mp3")
        async with self.download_sem:
            return await download_audio(
                self.client,
                url,
                dest,
                headers=self.headers,
                limiter=self.limiter,
                on_progress=on_progress,
                retries=self.retries,
                base_delay=self.base_delay,
                label="hitmoz",
            )

    async def stream(self, c: Candidate) -> StreamSource:
        """The same 320 kbps `/get/music/` file as `fetch` (302 -> CDN), for listening through the Hub."""
        if not _is_full_download(c.download_url):
            c = await self.enrich(c)
        if not _is_full_download(c.download_url):
            raise ProviderError("hitmoz: нет ссылки на полный MP3")
        assert c.download_url is not None
        url = c.download_url
        if url.startswith("/"):
            url = (await self.base_url()) + url
        return StreamSource(url=url, client=self.client, headers=dict(self.headers))

    async def aclose(self) -> None:
        if self._own_client:
            await self.client.aclose()
