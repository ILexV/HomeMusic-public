"""yt-dlp provider: YouTube Music / YouTube search and audio download (PLAN.md 3.4, 8.2).

yt-dlp is synchronous, so every call runs in a worker thread with its own `YoutubeDL` instance
(instances are not thread-safe). YouTube needs a JS runtime (Deno 2.3+ / Node 22+) for its player
challenges since mid-2026; without one, yt-dlp still works partially (metadata, some formats) and we log a hint.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
import os
import re
import shutil
import ssl
import threading
import time
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import date
from http.cookiejar import LoadError, MozillaCookieJar
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlsplit

import httpcore
import httpx
import yt_dlp
from yt_dlp.utils import DownloadCancelled, DownloadError, ExtractorError

from app.config import YtdlpCfg
from app.external import PublicSyncBackend
from app.net import PrioritySemaphore, make_client
from app.providers.base import Candidate, ProviderError, StreamSource, TrackQuery, contained_path, staging_path
from app.providers.external_ytdlp import PublicRH

log = logging.getLogger(__name__)

WATCH_URL = "https://www.youtube.com/watch?v={id}"
YTM_SEARCH_URL = "https://music.youtube.com/search?q={q}#songs"
# The YouTube Music web app's own search request ("Songs" filter): one request gives artist, album and duration,
# where yt-dlp's flat search gives only titles and needs a full extraction per hit (~2 s each)
YTM_API_URL = "https://music.youtube.com/youtubei/v1/search?prettyPrint=false"
YTM_SONGS_PARAMS = "EgWKAQIIAWoKEAkQBRAKEAMQBA=="
YTM_CLIENT_VERSION = "1.20260707.12.00"  # used when yt-dlp's own (kept current by its updates) can't be read
_DURATION_RE = re.compile(r"^\d+(?::\d{2}){1,2}$")
_YTM_ROW_TYPES = {"Song", "Video"}  # a leading type label (unfiltered results)
_TOPIC_SUFFIX = re.compile(r"\s*-\s*Topic$", re.IGNORECASE)
_VEVO_SUFFIX = re.compile(r"VEVO$")
_PREFIX_RE = re.compile(r"^([a-z]+?)(\d+|all)?:$")
_AUDIO_EXTS = {".opus", ".m4a", ".mp3", ".ogg", ".flac", ".wav", ".aac", ".webm", ".mka"}
LISTEN_FORMAT = "bestaudio[ext=m4a]/bestaudio/best"
_MEDIA_TYPES = {"m4a": "audio/mp4", "mp4": "audio/mp4", "webm": "audio/webm", "mp3": "audio/mpeg", "ogg": "audio/ogg"}
STREAM_URL_TTL_S = 20 * 60  # audio URLs from a metadata lookup, reused by `stream` (googlevideo links live ~6 h)
MEDIA_HOST_SUFFIX = ".googlevideo.com"
MEDIA_PROBE_TIMEOUT_S = 5.0
MEDIA_PROBE_TTL_S = 10 * 60  # how long a media server's probe result is trusted
# YouTube may put an ad before the video: its audio URL answers 403 until the ad could be skipped (yt-dlp's
# `available_at`). A stream waits that long, up to this; a longer ad makes the file count as not opening.
MAX_AD_WAIT_S = 40
METADATA_TTL_S = 10 * 60
BOT_MESSAGE = (
    "YouTube запросил проверку «не робот» и не отдал файл. "
    "Попробуйте позже или выберите другой источник. "
    "Если ошибка повторяется, сообщите владельцу сервера."
)
COOKIES_MESSAGE = (
    "Сессия YouTube истекла или была отозвана. Владельцу сервера нужно заменить файл cookies. "
    "Пока выберите другой источник."
)
RATE_MESSAGE = "YouTube временно ограничил запросы. Попробуйте позже или выберите другой источник."

ProgressCb = Callable[[float], Any]


class _YoutubeRefusal(ProviderError):
    """A session/IP refusal, rather than an unavailable individual video."""


def _refusal_message(msg: str) -> str | None:
    text = msg.lower().replace("’", "'")
    if "cookies" in text and ("no longer valid" in text or "rotated" in text):
        return COOKIES_MESSAGE
    if "confirm you're not a bot" in text:
        return BOT_MESSAGE
    if "http error 429" in text or "too many requests" in text or "content isn't available, try again later" in text:
        return RATE_MESSAGE
    return None


def _cookie_revision() -> tuple:
    """Only file metadata; never read credentials to detect an operator's replacement."""
    path = os.environ.get("YTDLP_COOKIES_FILE", "")
    try:
        st = Path(path).stat() if path else None
    except OSError:
        st = None
    return (path, st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino) if st else (path,)


class YoutubeAccess:
    """Shared by regular YouTube and re-uploads, owned by one Services lifespan (no global state)."""

    def __init__(self, cfg: YtdlpCfg, *, max_parallel: int | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.sem = PrioritySemaphore(max_parallel or cfg.max_parallel)
        self.clock = clock
        self.cooldown_s = cfg.refusal_cooldown_s
        self._revision = _cookie_revision()
        self._blocked_until = 0.0
        self._message = ""
        self.metadata: dict[str, tuple[float, dict]] = {}
        self.pending: dict[str, asyncio.Task] = {}

    def refresh(self) -> tuple:
        revision = _cookie_revision()
        if revision != self._revision:
            self._revision = revision
            self._blocked_until = 0.0
            self.metadata.clear()
            self.pending.clear()
        return revision

    def check(self) -> tuple:
        revision = self.refresh()
        if self.clock() < self._blocked_until:
            raise _YoutubeRefusal(self._message)
        return revision

    def block(self, message: str, revision: tuple) -> None:
        # A worker using an older export must not disable a freshly replaced session.
        if self.refresh() == revision:
            self._blocked_until = self.clock() + self.cooldown_s
            self._message = message


_media_hosts: dict[str, tuple[float, bool]] = {}  # googlevideo host -> (monotonic time, answers)
_media_lock = threading.Lock()


def _media_host_answers(host: str) -> bool:
    """A TLS handshake with the media server within `MEDIA_PROBE_TIMEOUT_S` (remembered a while)."""
    now = time.monotonic()
    with _media_lock:
        hit = _media_hosts.get(host)
    if hit and now - hit[0] < MEDIA_PROBE_TTL_S:
        return hit[1]
    try:
        stream = PublicSyncBackend().connect_tcp(host, 443, timeout=MEDIA_PROBE_TIMEOUT_S)
        try:
            tls = stream.start_tls(ssl.create_default_context(), server_hostname=host, timeout=MEDIA_PROBE_TIMEOUT_S)
            tls.close()
            ok = True
        finally:
            stream.close()
    except (OSError, httpcore.NetworkError, httpcore.TimeoutException):
        ok = False
    with _media_lock:
        _media_hosts[host] = (now, ok)
    return ok


def media_mirrors(url: str) -> list[str]:
    """The same googlevideo file on the video's other cache servers (`mn`, YouTube's own list, in its order)."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if not host.endswith(MEDIA_HOST_SUFFIX):
        return []
    qs = parse_qs(parts.query)
    fvip = (qs.get("fvip") or ["1"])[0]
    servers = [s for s in (qs.get("mn") or [""])[0].split(",") if s and f"---{s}." not in host]
    return [parts._replace(netloc=f"rr{fvip}---{s}{MEDIA_HOST_SUFFIX}").geturl() for s in servers]


def answering_url(url: str) -> str:
    """`url`, or the same file on another of the video's cache servers when its own doesn't answer. YouTube may
    send a client to a cache inside its ISP that hangs on the TLS handshake for some videos; yt-dlp has no
    fallback and gives up after its retries."""
    host = urlsplit(url).hostname or ""
    if not host.endswith(MEDIA_HOST_SUFFIX) or _media_host_answers(host):
        return url
    return next((alt for alt in media_mirrors(url) if _media_host_answers(urlsplit(alt).hostname or "")), url)


def ad_wait_s(fmt: dict, now: float) -> float:
    """Seconds until the format's URL serves the file (0: it does already)."""
    available_at = fmt.get("available_at")
    return max(float(available_at) - now, 0.0) if isinstance(available_at, (int, float)) else 0.0


async def _sleep(seconds: float) -> None:  # replaced in tests
    await asyncio.sleep(seconds)


def _log_switch(old: str, new: str) -> None:
    if old != new:
        log.info("youtube media server %s doesn't answer, using %s", urlsplit(old).hostname, urlsplit(new).hostname)


def on_answering_servers(info: dict) -> dict:
    """An extraction whose formats on a media server that doesn't answer point to one that does."""
    switched: dict[str | None, str] = {}
    for f in info.get("formats") or []:
        if isinstance(url := f.get("url"), str):
            f["url"] = answering_url(url)
            switched.setdefault(urlsplit(url).hostname, f["url"])
    for host, url in switched.items():
        _log_switch(f"https://{host}/", url)
    return info


class _YdlLogger:
    """Route yt-dlp output to logging (it calls debug() for both [debug] and info lines)."""

    def __init__(self, *, youtube: bool = True) -> None:
        self.youtube = youtube

    def debug(self, msg: str) -> None:
        # Plugin trace messages can contain PO tokens. Never persist diagnostic traces.
        if not msg.startswith("[debug]"):
            log.debug("%s", msg)

    def info(self, msg: str) -> None:
        log.debug("%s", msg)

    def warning(self, msg: str) -> None:
        if self.youtube and (message := _refusal_message(msg)):
            raise _YoutubeRefusal(message)
        log.warning("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.error("yt-dlp: %s", msg)


def detect_js_runtimes() -> dict[str, dict]:
    """JS runtimes available on PATH, in yt-dlp `js_runtimes` format (deno preferred)."""
    found: dict[str, dict] = {}
    for name in ("deno", "node", "bun"):
        if exe := shutil.which(name):
            found[name] = {"path": exe}
    return found


def clean_artist(name: str | None) -> str:
    """Strip YouTube auto-channel decorations: 'Кино - Topic' -> 'Кино'."""
    name = (name or "").strip()
    name = _TOPIC_SUFFIX.sub("", name)
    return name.strip()


def _parse_upload_date(v: Any) -> date | None:
    if not v:
        return None
    s = str(v)
    try:
        return date(int(s[0:4]), int(s[4:6]), int(s[6:8])) if len(s) == 8 and s.isdigit() else date.fromisoformat(s)
    except ValueError:
        return None


def _best_abr(info: dict) -> int | None:
    if info.get("abr"):
        return round(float(info["abr"]))
    for rf in info.get("requested_formats") or []:
        if rf.get("acodec") not in (None, "none") and rf.get("abr"):
            return round(float(rf["abr"]))
    abrs = [
        float(f["abr"]) for f in info.get("formats") or []
        if f.get("abr") and f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
    ]
    return round(max(abrs)) if abrs else None


def entry_to_candidate(entry: dict, source: str = "ytsearch") -> Candidate | None:
    """Map a yt-dlp info dict (flat search entry or full extraction) to a Candidate. None if unusable."""
    vid = entry.get("id")
    if not vid or (entry.get("_type") == "url" and entry.get("ie_key") not in (None, "Youtube")):
        return None
    if entry.get("live_status") in {"is_live", "is_upcoming"}:
        return None

    raw_title = (entry.get("title") or "").strip()
    track = (entry.get("track") or "").strip()
    artists = [a for a in (entry.get("artists") or []) if a]
    artist = ", ".join(artists) if artists else (entry.get("artist") or entry.get("creator") or "").strip()
    channel = entry.get("channel") or entry.get("uploader") or ""
    title = track or raw_title
    if not artist:
        if _TOPIC_SUFFIX.search(channel):
            artist = clean_artist(channel)  # auto-generated "Artist - Topic" upload: title is the bare track title
        elif not track and " - " in raw_title:
            # regular upload "Artist - Title": trust the title over a random channel name
            artist, title = (p.strip() for p in raw_title.split(" - ", 1))
        else:
            artist = _VEVO_SUFFIX.sub("", clean_artist(channel)).strip()
    if not title:
        return None

    duration = entry.get("duration")
    thumbs = entry.get("thumbnails") or []
    cover = entry.get("thumbnail") or (thumbs[-1].get("url") if thumbs else None)
    year = entry.get("release_year")
    extra = {
        "source": source,
        "channel": channel or None,
        "channel_id": entry.get("channel_id"),
        "original_title": raw_title or None,
        "view_count": entry.get("view_count"),
        "channel_is_verified": entry.get("channel_is_verified"),
        "acodec": entry.get("acodec") if entry.get("acodec") not in (None, "none") else None,
        "release_date": entry.get("release_date"),
    }
    return Candidate(
        provider="ytdlp",
        provider_id=str(vid),
        artist=artist,
        title=title,
        duration_s=round(float(duration)) if duration else None,
        bitrate_kbps=_best_abr(entry),
        explicit=None,  # YouTube doesn't expose the explicit flag
        upload_date=_parse_upload_date(entry.get("upload_date")),
        page_url=WATCH_URL.format(id=vid),
        download_url=None,
        cover_url=cover,
        album=entry.get("album") or None,
        year=int(year) if year else None,
        extra={k: v for k, v in extra.items() if v is not None},
    )


def listen_format(info: dict) -> dict | None:
    """The format `LISTEN_FORMAT` would pick among an extraction's formats: audio-only m4a with the best bitrate,
    else any audio-only format (plain HTTP(S), not HLS/DASH manifests)."""
    audio = [
        f for f in info.get("formats") or []
        if f.get("url") and f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
        and f.get("protocol", "https") in ("http", "https")
    ]
    m4a = [f for f in audio if f.get("ext") == "m4a"]
    pool = m4a or audio
    return max(pool, key=lambda f: f.get("abr") or f.get("tbr") or 0) if pool else None


def ytm_client_version() -> str:
    try:
        from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS

        return str(INNERTUBE_CLIENTS["web_music"]["INNERTUBE_CONTEXT"]["client"]["clientVersion"])
    except Exception:
        return YTM_CLIENT_VERSION


def _ytm_items(o: Any) -> list[dict]:
    """The song rows (`musicResponsiveListItemRenderer`) of a YouTube Music search response, in order."""
    out: list[dict] = []
    stack = [o]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if isinstance(item := cur.get("musicResponsiveListItemRenderer"), dict):
                out.append(item)
                continue
            stack.extend(reversed(list(cur.values())))
        elif isinstance(cur, list):
            stack.extend(reversed(cur))
    return out


def _parse_duration(s: str) -> int | None:
    if not _DURATION_RE.match(s):
        return None
    total = 0
    for part in s.split(":"):
        total = total * 60 + int(part)
    return total


def ytm_item_to_candidate(item: dict) -> Candidate | None:
    """Map a YouTube Music search row to a Candidate: title in the first column, "artists • album • duration"
    in the second, an Explicit badge. None if it isn't a playable song."""
    vid = (item.get("playlistItemData") or {}).get("videoId")
    cols = [
        ((c.get("musicResponsiveListItemFlexColumnRenderer") or {}).get("text") or {}).get("runs") or []
        for c in item.get("flexColumns") or []
    ]
    if not vid or len(cols) < 2:
        return None
    title = "".join(r.get("text", "") for r in cols[0]).strip()
    segments: list[list[dict]] = [[]]
    for r in cols[1]:
        if r.get("text", "").strip() == "•":
            segments.append([])
        else:
            segments[-1].append(r)

    def page_type(r: dict) -> str:
        cfg = ((r.get("navigationEndpoint") or {}).get("browseEndpoint") or {}).get(
            "browseEndpointContextSupportedConfigs") or {}
        return (cfg.get("browseEndpointContextMusicConfig") or {}).get("pageType", "")

    def text(seg: list[dict]) -> str:
        return "".join(r.get("text", "") for r in seg).strip()

    duration = _parse_duration(text(segments[-1])) if len(segments) > 1 else None
    if duration is not None:
        segments = segments[:-1]
    if len(segments) > 1 and text(segments[0]) in _YTM_ROW_TYPES:
        segments = segments[1:]
    # the runs link to artist and album pages, or (another variant of the response) are plain text in that order
    artists = [text([r]) for seg in segments for r in seg if page_type(r) == "MUSIC_PAGE_TYPE_ARTIST"]
    albums = [text([r]) for seg in segments for r in seg if page_type(r) == "MUSIC_PAGE_TYPE_ALBUM"]
    if artists or albums:
        artist = ", ".join(artists) or text(segments[0])
        album = albums[0] if albums else None
    else:
        artist = text(segments[0])
        album = text(segments[1]) if len(segments) > 1 else None
    if not title or not artist:
        return None
    badges = [((b.get("musicInlineBadgeRenderer") or {}).get("icon") or {}).get("iconType")
              for b in item.get("badges") or []]
    thumbs = (((item.get("thumbnail") or {}).get("musicThumbnailRenderer") or {}).get("thumbnail") or {}).get(
        "thumbnails") or []
    return Candidate(
        provider="ytdlp",
        provider_id=str(vid),
        artist=artist,
        title=title,
        duration_s=duration,
        explicit=True if "MUSIC_EXPLICIT_BADGE" in badges else None,  # no badge doesn't mean a clean version
        page_url=WATCH_URL.format(id=vid),
        cover_url=thumbs[-1].get("url") if thumbs else None,
        album=album,
        extra={"source": "ytmusic"},
    )


def search_text(q: TrackQuery) -> str:
    parts = [q.artist, "-", q.title] if q.artist and q.title else [q.artist or q.title]
    if q.version:
        parts.append(q.version)
    return " ".join(p for p in parts if p).strip()


def ytsearch_query(prefix: str, text: str, limit: int) -> str:
    """`'ytsearch5:'` + limit 10 -> `'ytsearch10:<text>'` (keeps the configured search key)."""
    m = _PREFIX_RE.match(prefix.strip())
    key = m.group(1) if m else "ytsearch"
    return f"{key}{max(1, limit)}:{text}"


class YtdlpProvider:
    name = "ytdlp"

    def __init__(self, cfg: YtdlpCfg, *, use_music_search: bool = True, max_parallel: int | None = None,
                 access: YoutubeAccess | None = None) -> None:
        self.cfg = cfg
        self.use_music_search = use_music_search
        self._youtube = (access or YoutubeAccess(cfg, max_parallel=max_parallel)) if self.name != "soundcloud" else None
        self._sem = self._youtube.sem if self._youtube else PrioritySemaphore(max_parallel or 2)
        self._js = detect_js_runtimes()
        self._remote_ejs = importlib.util.find_spec("yt_dlp_ejs") is None
        self._http: httpx.AsyncClient | None = None  # proxies listening streams
        self._api: httpx.AsyncClient | None = None  # YouTube Music search
        self._stream_urls: dict[str, tuple[float, dict]] = {}  # video id -> (monotonic time, format) for `stream`
        if not self._js:
            log.warning("no JS runtime (deno/node) on PATH: YouTube downloads may fail or be limited")

    # ------------------------------------------------------------------ yt-dlp plumbing

    def _base_opts(self) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": False,
            "noprogress": True,
            "noplaylist": True,
            "logger": _YdlLogger(youtube=self._youtube is not None),
            "socket_timeout": 30,
            "retries": 3,
            "fragment_retries": 3,
            "proxy": "",  # never inherit an environment proxy that resolves media hosts remotely
            "hls_prefer_native": True,
            "external_downloader": "native",
            "external_downloader_args": {"ffmpeg_i": ["-protocol_whitelist", "file,crypto,data"]},
        }
        if self._youtube:
            opts.update({
                "extractor_args": {"youtube": {"player_client": ["mweb"]}},
                "retries": 1,
                "fragment_retries": 1,
                "extractor_retries": 1,
                "concurrent_fragment_downloads": 1,
                "sleep_interval_requests": self.cfg.sleep_requests_s,
                "sleep_interval": self.cfg.sleep_downloads_s,
            })
            if base_url := os.environ.get("YTDLP_POT_BASE_URL"):
                opts["extractor_args"]["youtubepot-bgutilhttp"] = {"base_url": [base_url]}
        if cache_dir := os.environ.get("YTDLP_CACHE_DIR"):
            opts["cachedir"] = cache_dir
        if self._js:
            opts["js_runtimes"] = self._js
            if self._remote_ejs:
                # challenge solver scripts aren't installed (yt-dlp-ejs); let yt-dlp fetch them from its GitHub
                opts["remote_components"] = ["ejs:github"]
        ffmpeg = os.environ.get("FFMPEG_BIN")
        if ffmpeg and os.path.sep in ffmpeg:
            opts["ffmpeg_location"] = ffmpeg
        return opts

    def _extract(self, url: str, **opts: Any) -> dict:
        with self._youtube_dl(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return ydl.sanitize_info(info) or {}

    @contextmanager
    def _youtube_dl(self, opts: dict[str, Any]) -> Iterator[yt_dlp.YoutubeDL]:
        # Load a fresh, private jar for each worker. Never let yt-dlp rewrite the exported file:
        # simultaneous downloads would race, and a read-only secret mount must work too.
        cookies = MozillaCookieJar()
        if self.name != "soundcloud" and (path := os.environ.get("YTDLP_COOKIES_FILE")):
            try:
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", category=UserWarning, module="http.cookiejar")
                    cookies.load(path, ignore_discard=True, ignore_expires=True)
            except (OSError, LoadError, ValueError):
                # Cookie parser exceptions may contain credentials from the offending line.
                raise ProviderError("Не удалось прочитать cookies YouTube. Владельцу сервера нужно проверить файл cookies.") from None
        with yt_dlp.YoutubeDL({**self._base_opts(), **opts}) as ydl:
            ydl._request_director.close()
            ydl._request_director = ydl.build_request_director([PublicRH])
            if self._youtube and (base_url := os.environ.get("YTDLP_POT_BASE_URL")):
                ydl._request_director.handlers[PublicRH.RH_KEY].token_service = base_url.rstrip("/")
            for cookie in cookies:
                if cookie.domain.lstrip(".") == "youtube.com" or cookie.domain.endswith(".youtube.com"):
                    # Netscape exports use 0 for session cookies; the stdlib treats it as already expired.
                    if cookie.expires == 0:
                        cookie.expires, cookie.discard = None, True
                    ydl.cookiejar.set_cookie(cookie)
            yield ydl

    async def _run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._youtube:
            self._youtube.check()
        async with self._sem:
            revision = self._youtube.check() if self._youtube else ()
            try:
                return await asyncio.to_thread(fn, *args, **kwargs)
            except _YoutubeRefusal as e:
                if self._youtube:
                    self._youtube.block(str(e), revision)
                raise
            except (DownloadError, ExtractorError) as e:
                msg = str(e)
                if self._youtube and (message := _refusal_message(msg)):
                    self._youtube.block(message, revision)
                    raise _YoutubeRefusal(message) from None
                if not self._js and ("challenge" in msg.lower() or "format is not available" in msg):
                    msg += " (no JS runtime found: install Deno 2.3+ or Node 22+)"
                raise ProviderError(f"yt-dlp: {msg}") from e

    # ------------------------------------------------------------------ Provider API

    async def search(self, q: TrackQuery, limit: int = 10) -> list[Candidate]:
        if self._youtube:
            self._youtube.check()
        results: list[Candidate] = []
        if self.use_music_search:
            try:
                results = await self._search_music(q, limit)
            except _YoutubeRefusal:
                raise
            except ProviderError as e:
                log.info("YouTube Music search failed, falling back to ytsearch: %s", e)
        if not results:
            results = await self._search_youtube(q, limit)
        return results[:limit]

    async def _search_music(self, q: TrackQuery, limit: int) -> list[Candidate]:
        """YouTube Music 'Songs' tab through its web API; when that fails, through yt-dlp."""
        text = " ".join(p for p in (q.artist, q.title, q.version) if p)
        try:
            return await self._search_music_api(text, limit)
        except _YoutubeRefusal:
            raise
        except Exception as e:
            log.info("YouTube Music API search failed, searching through yt-dlp: %r", e)
        return await self._search_music_ytdlp(text, limit)

    async def _search_music_api(self, text: str, limit: int) -> list[Candidate]:
        """One request; the hits aren't enriched (no bitrate/upload date): a download looks up its top ones."""
        async with self._sem:
            revision = self._youtube.check() if self._youtube else ()
            try:
                return await self._music_api_request(text, limit)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 and self._youtube:
                    self._youtube.block(RATE_MESSAGE, revision)
                    raise _YoutubeRefusal(RATE_MESSAGE) from None
                raise

    async def _music_api_request(self, text: str, limit: int) -> list[Candidate]:
        if self._api is None:
            self._api = make_client(timeout=15, headers={"Origin": "https://music.youtube.com"})
        version = ytm_client_version()
        resp = await self._api.post(
            YTM_API_URL,
            json={"context": {"client": {"clientName": "WEB_REMIX", "clientVersion": version, "hl": "en"}},
                  "query": text, "params": YTM_SONGS_PARAMS},
            headers={"X-YouTube-Client-Name": "67", "X-YouTube-Client-Version": version},
        )
        resp.raise_for_status()
        data = resp.json()
        if "contents" not in data:
            raise ProviderError("YouTube Music: unexpected search response")
        out: list[Candidate] = []
        for item in _ytm_items(data["contents"]):
            if c := ytm_item_to_candidate(item):
                out.append(c)
                if len(out) >= limit:
                    break
        return out

    async def _search_music_ytdlp(self, text: str, limit: int) -> list[Candidate]:
        """Flat entries only carry id + title, so each hit gets a metadata lookup."""
        url = YTM_SEARCH_URL.format(q=quote_plus(text))
        info = await self._run(self._extract, url, extract_flat="in_playlist", playlistend=limit, noplaylist=False)
        ids = [e["id"] for e in (info.get("entries") or []) if e and e.get("id")][:limit]
        if not ids:
            return []
        infos = await asyncio.gather(*(self._metadata(i) for i in ids), return_exceptions=True)
        out: list[Candidate] = []
        for i, res in zip(ids, infos, strict=True):
            if isinstance(res, BaseException):
                log.info("YouTube Music: metadata for %s failed: %s", i, res)
                continue
            if c := entry_to_candidate(res, source="ytmusic"):
                # already a full metadata lookup: `enrich` would repeat it
                out.append(c.model_copy(update={"page_url": WATCH_URL.format(id=i), "enriched": True}))
        return out

    async def _metadata(self, video_id: str) -> dict:
        info = await self._metadata_info(video_id)
        if fmt := listen_format(info):
            # Cache/pending hits need the same stream shortcut as a fresh lookup, for each provider.
            now = time.monotonic()
            self._stream_urls = {k: v for k, v in self._stream_urls.items() if now - v[0] < STREAM_URL_TTL_S}
            self._stream_urls[video_id] = (now, fmt)
        return info

    async def _metadata_info(self, video_id: str) -> dict:
        access = self._youtube
        revision = access.refresh() if access else ()
        if access:
            cached = access.metadata.get(video_id)
            if cached and access.clock() - cached[0] < METADATA_TTL_S:
                return deepcopy(cached[1])
            if pending := access.pending.get(video_id):
                return deepcopy(await asyncio.shield(pending))

        async def lookup() -> dict:
            info = await self._lookup_metadata(video_id)
            if access and access.refresh() == revision:
                now = access.clock()
                access.metadata = {k: v for k, v in access.metadata.items() if now - v[0] < METADATA_TTL_S}
                if len(access.metadata) >= 256:
                    access.metadata.pop(next(iter(access.metadata)))
                access.metadata[video_id] = (now, deepcopy(info))
            return info

        if access is None:
            return await lookup()
        task = asyncio.create_task(lookup())
        access.pending[video_id] = task
        try:
            return await asyncio.shield(task)
        finally:
            # Keep coalescing if this caller was cancelled before its worker finished.
            def done(t: asyncio.Task) -> None:
                if access.pending.get(video_id) is t:
                    access.pending.pop(video_id, None)
                if not t.cancelled():
                    t.exception()  # retrieve a failure even if all callers were cancelled
            task.add_done_callback(done)

    async def _lookup_metadata(self, video_id: str) -> dict:
        # formats aren't needed here, so don't fail when they can't be resolved (e.g. no JS runtime)
        return await self._run(
            self._extract, WATCH_URL.format(id=video_id),
            ignore_no_formats_error=True, format="bestaudio/best", check_formats=False,
        )

    async def _search_youtube(self, q: TrackQuery, limit: int) -> list[Candidate]:
        query = ytsearch_query(self.cfg.search_prefix, search_text(q), limit)
        info = await self._run(self._extract, query, extract_flat="in_playlist", noplaylist=False)
        out: list[Candidate] = []
        for e in info.get("entries") or []:
            if e and (c := entry_to_candidate(e, source="ytsearch")):
                out.append(c)
        return out

    async def enrich(self, c: Candidate) -> Candidate:
        if c.enriched:
            return c
        info = await self._metadata(c.provider_id)
        full = entry_to_candidate(info, source=c.extra.get("source", "ytsearch"))
        if full is None:
            return c.model_copy(update={"enriched": True})
        update: dict[str, Any] = {
            "enriched": True,
            "bitrate_kbps": full.bitrate_kbps or c.bitrate_kbps,
            "upload_date": full.upload_date or c.upload_date,
            "duration_s": full.duration_s or c.duration_s,
            "cover_url": full.cover_url or c.cover_url,
            "album": full.album or c.album,
            "year": full.year or c.year,
            "extra": {**c.extra, **full.extra, "source": c.extra.get("source", full.extra.get("source"))},
        }
        # real music metadata (artist/track fields) beats names guessed from the channel/title
        if info.get("track") or info.get("artists") or info.get("artist"):
            update["artist"] = full.artist
            update["title"] = full.title
        return c.model_copy(update=update)

    def media_url(self, c: Candidate) -> str:
        """The page yt-dlp downloads the candidate from."""
        return WATCH_URL.format(id=c.provider_id)

    async def fetch(self, c: Candidate, dest_dir: Path, on_progress: ProgressCb | None = None) -> Path:
        dest_dir = Path(dest_dir).resolve()
        dest = staging_path(dest_dir)
        loop = asyncio.get_running_loop()
        cancelled = threading.Event()
        last = [-1.0]

        def emit(fraction: float) -> None:
            if on_progress is None or (fraction - last[0] < 0.01 and fraction < 1.0):
                return
            last[0] = fraction
            if inspect.iscoroutinefunction(on_progress):
                asyncio.run_coroutine_threadsafe(on_progress(fraction), loop)
            else:
                loop.call_soon_threadsafe(on_progress, fraction)

        def hook(d: dict) -> None:
            if cancelled.is_set():
                raise DownloadCancelled("cancelled")
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                if total:
                    emit(min(0.99, d.get("downloaded_bytes", 0) / total))
            elif d.get("status") == "finished":
                emit(1.0)

        if self.cfg.prefer_original_codec:
            pp = {"key": "FFmpegExtractAudio", "preferredcodec": "best"}  # remux only: opus -> .opus, aac -> .m4a
        else:
            pp = {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"}
        opts = {
            # Only native HTTP/HLS downloaders: ffmpeg must never fetch an unchecked remote URL.
            "format": "bestaudio[protocol=https]/bestaudio[protocol=http]/bestaudio[protocol=m3u8_native]",
            "outtmpl": str(dest) + ".%(ext)s",
            "postprocessors": [pp],
            "progress_hooks": [hook],
            "overwrites": True,
            "writethumbnail": False,
        }

        def download() -> Path:
            with self._youtube_dl(opts) as ydl:
                info = ydl.extract_info(self.media_url(c), download=False, process=False)
                # Token calls belong to extraction; media, fragments and keys are public-only.
                ydl._request_director.handlers[PublicRH.RH_KEY].token_service = None
                info = ydl.process_ie_result(on_answering_servers(info), download=True)
            for rd in (info or {}).get("requested_downloads") or []:
                if fp := rd.get("filepath"):
                    path = contained_path(dest_dir, Path(fp))
                    if path.is_file():
                        return path
            files = [
                contained_path(dest_dir, p) for p in dest_dir.glob(f"{dest.name}.*")
                if p.suffix.lower() in _AUDIO_EXTS and p.is_file()
            ]
            if not files:
                raise ProviderError(f"yt-dlp finished but no file for {c.provider_id} in {dest_dir}")
            return max(files, key=lambda p: p.stat().st_mtime)

        try:
            return await self._run(download)
        except asyncio.CancelledError:
            cancelled.set()  # the worker thread stops at the next progress hook
            raise

    async def stream(self, c: Candidate) -> StreamSource:
        """Direct audio URL of the same video `fetch` would save. googlevideo links are bound to the IP that
        extracted them, so the Hub proxies them (an AAC/m4a format first: every browser plays it).
        A format found by the search's metadata lookup is used once; a renewal extracts afresh. A media server
        that doesn't answer gives way to another of the video's (`answering_url`). A URL behind an ad is
        returned once the ad could be skipped (`MAX_AD_WAIT_S`)."""
        cached = self._stream_urls.pop(c.provider_id, None)
        if cached and time.monotonic() - cached[0] < STREAM_URL_TTL_S:
            info = cached[1]
        else:
            info = await self._run(self._extract, WATCH_URL.format(id=c.provider_id), format=LISTEN_FORMAT)
        url = info.get("url")
        if not url:
            raise ProviderError(f"yt-dlp: нет прямой ссылки на аудио для {c.provider_id}")
        if wait := ad_wait_s(info, time.time()):
            if wait > MAX_AD_WAIT_S:
                raise ProviderError(f"YouTube: перед {c.provider_id} реклама на {wait:.0f} с")
            log.info("youtube %s: waiting %.1f s for the ad before it", c.provider_id, wait)
            await _sleep(wait)
        url, original = await asyncio.to_thread(answering_url, url), url
        _log_switch(original, url)
        if self._http is None:
            self._http = make_client(timeout=60)
        headers = {k: v for k, v in (info.get("http_headers") or {}).items() if isinstance(v, str)}
        return StreamSource(url=url, client=self._http, headers=headers,
                            media_type=_MEDIA_TYPES.get(info.get("ext") or "", "audio/mp4"))

    async def aclose(self) -> None:
        for client in (self._http, self._api):
            if client is not None:
                await client.aclose()
        self._http = self._api = None
