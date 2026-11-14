import asyncio
import json
import os
import shutil
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from yt_dlp.utils import DownloadError

import app.providers.ytdlp as ytdlp_mod
from app.config import YtdlpCfg
from app.providers.base import Candidate, Provider, ProviderError, TrackQuery
from app.providers.ytdlp import (
    YTM_API_URL,
    YTM_SONGS_PARAMS,
    YtdlpProvider,
    clean_artist,
    entry_to_candidate,
    listen_format,
    search_text,
    ytm_item_to_candidate,
    ytsearch_query,
)

NETWORK = os.environ.get("HUB_NETWORK_TESTS") == "1"

FLAT_TOPIC = {
    "_type": "url", "ie_key": "Youtube", "id": "abc123def45", "url": "https://www.youtube.com/watch?v=abc123def45",
    "title": "Группа крови", "duration": 284.0, "channel": "Кино - Topic", "channel_id": "UC1",
    "view_count": 1000, "thumbnails": [{"url": "https://i.ytimg.com/a.jpg"}, {"url": "https://i.ytimg.com/b.jpg"}],
}
FLAT_DASHED = {
    "_type": "url", "ie_key": "Youtube", "id": "zzz", "title": "Кино - Группа крови (Official Audio)",
    "duration": 285, "channel": "Rock Archive", "uploader": "Rock Archive",
}
FULL_MUSIC = {
    "id": "ytm1", "title": "Группа крови", "track": "Группа крови", "artists": ["Кино"], "artist": "Кино",
    "album": "Группа крови", "release_year": 1988, "release_date": "19880105", "upload_date": "20150301",
    "duration": 284, "abr": 129.5, "acodec": "opus", "channel": "Кино - Topic",
    "thumbnail": "https://i.ytimg.com/vi/ytm1/maxresdefault.jpg",
}


# --------------------------------------------------------------------------- pure mapping


def test_clean_artist() -> None:
    assert clean_artist("Кино - Topic") == "Кино"
    assert clean_artist(" Artist -Topic ") == "Artist"
    assert clean_artist(None) == ""
    assert clean_artist("Topicals") == "Topicals"


def test_entry_topic_channel() -> None:
    c = entry_to_candidate(FLAT_TOPIC)
    assert c is not None
    assert (c.provider, c.provider_id) == ("ytdlp", "abc123def45")
    assert c.artist == "Кино" and c.title == "Группа крови"
    assert c.duration_s == 284
    assert c.page_url == "https://www.youtube.com/watch?v=abc123def45"
    assert c.cover_url == "https://i.ytimg.com/b.jpg"
    assert c.upload_date is None and c.bitrate_kbps is None and c.explicit is None
    assert c.extra["source"] == "ytsearch" and c.extra["channel"] == "Кино - Topic"


def test_entry_dashed_title() -> None:
    c = entry_to_candidate(FLAT_DASHED)
    assert c is not None
    assert c.artist == "Кино" and c.title == "Группа крови (Official Audio)"
    assert c.extra["original_title"] == "Кино - Группа крови (Official Audio)"


def test_entry_plain_channel() -> None:
    c = entry_to_candidate({"id": "q", "title": "Song", "channel": "ArtistVEVO", "duration": 10.6})
    assert c is not None and c.artist == "Artist" and c.title == "Song" and c.duration_s == 11


def test_entry_full_music_metadata() -> None:
    c = entry_to_candidate(FULL_MUSIC, source="ytmusic")
    assert c is not None
    assert c.artist == "Кино" and c.title == "Группа крови"
    assert c.album == "Группа крови" and c.year == 1988
    assert c.upload_date == date(2015, 3, 1)
    assert c.bitrate_kbps == 130
    assert c.extra["source"] == "ytmusic" and c.extra["acodec"] == "opus"


def test_entry_multiple_artists_and_formats_abr() -> None:
    e = {
        "id": "m", "track": "Duet", "title": "Duet (Official)", "artists": ["A", "B"],
        "formats": [
            {"format_id": "140", "acodec": "mp4a.40.2", "vcodec": "none", "abr": 129.0},
            {"format_id": "251", "acodec": "opus", "vcodec": "none", "abr": 141.2},
            {"format_id": "18", "acodec": "mp4a.40.2", "vcodec": "avc1", "abr": 96},
        ],
    }
    c = entry_to_candidate(e)
    assert c is not None and c.artist == "A, B" and c.title == "Duet" and c.bitrate_kbps == 141


@pytest.mark.parametrize(
    "entry",
    [
        {"title": "no id"},
        {"id": "x", "title": ""},
        {"id": "live", "title": "Stream", "live_status": "is_live"},
        {"_type": "url", "ie_key": "YoutubeTab", "id": "UCxyz", "title": "Some channel"},
    ],
)
def test_entry_rejected(entry: dict) -> None:
    assert entry_to_candidate(entry) is None


def test_bad_upload_date_ignored() -> None:
    c = entry_to_candidate({**FULL_MUSIC, "upload_date": "2015xx01"})
    assert c is not None and c.upload_date is None


def test_queries() -> None:
    assert ytsearch_query("ytsearch5:", "a - b", 10) == "ytsearch10:a - b"
    assert ytsearch_query("ytsearchdate3:", "x", 2) == "ytsearchdate2:x"
    assert ytsearch_query("garbage", "x", 0) == "ytsearch1:x"
    assert search_text(TrackQuery(artist="Кино", title="Кукушка", version="live")) == "Кино - Кукушка live"
    assert search_text(TrackQuery(artist="кино группа крови", title="")) == "кино группа крови"


def test_protocol() -> None:
    assert isinstance(YtdlpProvider(YtdlpCfg()), Provider)


def _link(text: str, page_type: str) -> dict:
    return {"text": text, "navigationEndpoint": {"browseEndpoint": {
        "browseId": "x", "browseEndpointContextSupportedConfigs": {
            "browseEndpointContextMusicConfig": {"pageType": f"MUSIC_PAGE_TYPE_{page_type}"}}}}}


def ytm_row(vid: str | None, title: str, artists: list[str], album: str | None, duration: str,
            explicit: bool = False) -> dict:
    """A row of the YouTube Music "Songs" search, as its web API returns it."""
    runs: list[dict] = []
    for i, a in enumerate(artists):
        if i:
            runs.append({"text": " & "})
        runs.append(_link(a, "ARTIST"))
    if album:
        runs += [{"text": " • "}, _link(album, "ALBUM")]
    runs += [{"text": " • "}, {"text": duration}]
    row: dict = {
        "thumbnail": {"musicThumbnailRenderer": {"thumbnail": {"thumbnails": [
            {"url": "https://lh3.test/s60"}, {"url": "https://lh3.test/s120"}]}}},
        "flexColumns": [
            {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": title}]}}},
            {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": runs}}},
            {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "1.7B plays"}]}}},
        ],
        "playlistItemData": {"videoId": vid} if vid else {},
    }
    if explicit:
        row["badges"] = [{"musicInlineBadgeRenderer": {"icon": {"iconType": "MUSIC_EXPLICIT_BADGE"}}}]
    return {"musicResponsiveListItemRenderer": row}


def ytm_response(*rows: dict) -> dict:
    return {"contents": {"tabbedSearchResultsRenderer": {"tabs": [{"tabRenderer": {"content": {
        "sectionListRenderer": {"contents": [{"musicShelfRenderer": {"contents": list(rows)}}]}}}}]}}}


def test_ytm_row_mapping() -> None:
    c = ytm_item_to_candidate(ytm_row("ytm1", "Группа крови", ["Кино"], "Группа крови", "4:45")[
        "musicResponsiveListItemRenderer"])
    assert c is not None
    assert (c.provider, c.provider_id, c.page_url) == ("ytdlp", "ytm1", "https://www.youtube.com/watch?v=ytm1")
    assert (c.artist, c.title, c.album, c.duration_s) == ("Кино", "Группа крови", "Группа крови", 285)
    assert c.explicit is None and not c.enriched and c.bitrate_kbps is None
    assert c.cover_url == "https://lh3.test/s120" and c.extra["source"] == "ytmusic"

    c = ytm_item_to_candidate(ytm_row("e", "Without Me", ["Eminem", "Dido"], None, "1:04:51", explicit=True)[
        "musicResponsiveListItemRenderer"])
    assert c is not None
    assert (c.artist, c.album, c.duration_s, c.explicit) == ("Eminem, Dido", None, 3891, True)
    # artists without links: the segment's text
    row = ytm_row("u", "Song", [], "LP", "3:00")["musicResponsiveListItemRenderer"]
    row["flexColumns"][1]["musicResponsiveListItemFlexColumnRenderer"]["text"]["runs"].insert(0, {"text": "Someone"})
    c = ytm_item_to_candidate(row)
    assert c is not None and c.artist == "Someone" and c.album == "LP"
    # the variant without links: "artists • album • duration" as plain text (a type label may lead)
    row = ytm_row("p", "Get Lucky", [], None, "6:09")["musicResponsiveListItemRenderer"]
    row["flexColumns"][1]["musicResponsiveListItemFlexColumnRenderer"]["text"]["runs"] = [
        {"text": t} for t in ("Song", " • ", "Daft Punk, Pharrell Williams & Nile Rodgers", " • ", "Random Access Memories",
                              " • ", "6:09")]
    c = ytm_item_to_candidate(row)
    assert c is not None
    assert (c.artist, c.album, c.duration_s) == ("Daft Punk, Pharrell Williams & Nile Rodgers", "Random Access Memories", 369)
    assert ytm_item_to_candidate(ytm_row(None, "No id", ["A"], None, "3:00")["musicResponsiveListItemRenderer"]) is None


# --------------------------------------------------------------------------- search / fetch with a fake yt-dlp


def music_api_down(monkeypatch: pytest.MonkeyPatch, p: YtdlpProvider) -> None:
    async def down(text: str, limit: int) -> list[Candidate]:
        raise httpx.ConnectError("music.youtube.com unreachable")

    monkeypatch.setattr(p, "_search_music_api", down)


@respx.mock
async def test_search_music_api(monkeypatch: pytest.MonkeyPatch) -> None:
    p = YtdlpProvider(YtdlpCfg())
    rows = [ytm_row(f"v{i}", "Группа крови", ["Кино"], "Группа крови", "4:45") for i in range(6)]
    route = respx.post(YTM_API_URL).mock(return_value=httpx.Response(200, json=ytm_response(*rows)))
    extracted: list[str] = []

    def fake_extract(url: str, **opts: Any) -> dict:
        extracted.append(url)
        return {**FULL_MUSIC, "id": url.rsplit("=", 1)[1]}

    monkeypatch.setattr(p, "_extract", fake_extract)
    res = await p.search(TrackQuery(artist="Кино", title="Группа крови", version="live"), limit=4)
    assert [c.provider_id for c in res] == ["v0", "v1", "v2", "v3"]
    assert extracted == []  # one request, no lookup per hit
    sent = json.loads(route.calls.last.request.content)
    assert sent["query"] == "Кино Группа крови live" and sent["params"] == YTM_SONGS_PARAMS
    assert sent["context"]["client"]["clientName"] == "WEB_REMIX"
    # a download looks up its top hits
    c = await p.enrich(res[0])
    assert c.enriched and c.bitrate_kbps == 130 and extracted == ["https://www.youtube.com/watch?v=v0"]
    await p.aclose()


@respx.mock
async def test_search_music_api_trouble_falls_back_to_ytdlp(monkeypatch: pytest.MonkeyPatch) -> None:
    p = YtdlpProvider(YtdlpCfg())
    monkeypatch.setattr(p, "_extract", lambda url, **o: (
        {"entries": [{"id": "ytm1", "title": "Группа крови"}]} if "music.youtube.com/search" in url else FULL_MUSIC))
    respx.post(YTM_API_URL).mock(side_effect=[httpx.Response(403), httpx.Response(200, json={"error": "changed"})])
    for _ in range(2):  # blocked, then a response of another shape
        res = await p.search(TrackQuery(artist="Кино", title="Группа крови"), limit=4)
        assert [c.provider_id for c in res] == ["ytm1"] and res[0].enriched
    await p.aclose()


async def test_search_music_then_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    p = YtdlpProvider(YtdlpCfg())
    calls: list[str] = []

    def fake_extract(url: str, **opts: Any) -> dict:
        calls.append(url)
        if url.startswith("https://music.youtube.com/search"):
            assert "#songs" in url and opts["extract_flat"] == "in_playlist"
            return {"entries": [{"id": "ytm1", "title": "Группа крови"}, {"id": "bad", "title": "x"}]}
        if url.endswith("ytm1"):
            return FULL_MUSIC
        raise DownloadError("ERROR: video unavailable")

    monkeypatch.setattr(p, "_extract", fake_extract)
    music_api_down(monkeypatch, p)
    res = await p.search(TrackQuery(artist="Кино", title="Группа крови"), limit=5)
    assert [c.provider_id for c in res] == ["ytm1"]  # the failed metadata lookup is skipped
    assert res[0].artist == "Кино" and res[0].extra["source"] == "ytmusic"
    assert res[0].page_url == "https://www.youtube.com/watch?v=ytm1"
    assert not any(u.startswith("ytsearch") for u in calls)
    # the hit already had a full metadata lookup: enriching it doesn't extract again
    n = len(calls)
    assert res[0].enriched and await p.enrich(res[0]) == res[0] and len(calls) == n


def test_listen_format_prefers_m4a_audio() -> None:
    fmts = [
        {"format_id": "251", "ext": "webm", "acodec": "opus", "vcodec": "none", "abr": 160, "url": "https://g/251"},
        {"format_id": "140", "ext": "m4a", "acodec": "mp4a", "vcodec": "none", "abr": 129, "url": "https://g/140"},
        {"format_id": "139", "ext": "m4a", "acodec": "mp4a", "vcodec": "none", "abr": 48, "url": "https://g/139"},
        {"format_id": "18", "ext": "mp4", "acodec": "mp4a", "vcodec": "avc1", "url": "https://g/18"},
        {"format_id": "hls", "ext": "m4a", "acodec": "mp4a", "vcodec": "none", "abr": 256, "url": "https://g/m3u8",
         "protocol": "m3u8_native"},
    ]
    assert listen_format({"formats": fmts})["format_id"] == "140"
    assert listen_format({"formats": fmts[:1]})["format_id"] == "251"
    assert listen_format({"formats": fmts[3:4]}) is None


async def test_stream_reuses_the_search_lookup_once(monkeypatch: pytest.MonkeyPatch) -> None:
    p = YtdlpProvider(YtdlpCfg())
    calls: list[dict] = []
    audio = {"format_id": "140", "ext": "m4a", "acodec": "mp4a", "vcodec": "none", "abr": 129,
             "url": "https://g.test/140", "http_headers": {"User-Agent": "UA"}}

    def fake_extract(url: str, **opts: Any) -> dict:
        calls.append(opts)
        if url.startswith("https://music.youtube.com/search"):
            return {"entries": [{"id": "ytm1", "title": "Группа крови"}]}
        if opts.get("format") == ytdlp_mod.LISTEN_FORMAT:
            return {"url": "https://g.test/fresh", "ext": "m4a"}
        return {**FULL_MUSIC, "formats": [audio]}

    monkeypatch.setattr(p, "_extract", fake_extract)
    music_api_down(monkeypatch, p)
    [c] = await p.search(TrackQuery(artist="Кино", title="Группа крови"), limit=4)
    n = len(calls)
    src = await p.stream(c)
    assert src.url == "https://g.test/140" and src.headers == {"User-Agent": "UA"} and src.media_type == "audio/mp4"
    assert len(calls) == n  # no second extraction
    src = await p.stream(c)  # a renewal (the link stopped working) extracts afresh
    assert src.url == "https://g.test/fresh" and len(calls) == n + 1
    await p.aclose()


YT_FILE = Candidate(provider="ytdlp", provider_id="vid1", artist="Кино", title="Группа крови",
                    page_url="https://www.youtube.com/watch?v=vid1")


@pytest.mark.parametrize(("ahead_s", "waits"), [(5, [5.0]), (0, []), (-3, [])],
                         ids=["ad-before-the-video", "no-ad", "ad-over"])
async def test_stream_waits_until_the_ad_could_be_skipped(monkeypatch: pytest.MonkeyPatch, ahead_s: int,
                                                          waits: list[float]) -> None:
    # googlevideo answers 403 until `available_at`: a stream must not hand the URL out earlier
    p = YtdlpProvider(YtdlpCfg())
    now = 1_800_000_000.0
    monkeypatch.setattr(ytdlp_mod.time, "time", lambda: now)
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr(ytdlp_mod, "_sleep", fake_sleep)
    monkeypatch.setattr(p, "_extract", lambda url, **opts: {
        "url": "https://g.test/140", "ext": "m4a", "available_at": int(now) + ahead_s})
    src = await p.stream(YT_FILE)
    assert src.url == "https://g.test/140" and slept == waits
    await p.aclose()


async def test_stream_gives_up_on_a_long_ad(monkeypatch: pytest.MonkeyPatch) -> None:
    p = YtdlpProvider(YtdlpCfg())
    now = 1_800_000_000.0
    monkeypatch.setattr(ytdlp_mod.time, "time", lambda: now)
    monkeypatch.setattr(p, "_extract", lambda url, **opts: {
        "url": "https://g.test/140", "available_at": now + ytdlp_mod.MAX_AD_WAIT_S + 1})
    with pytest.raises(ProviderError, match="реклама"):
        await p.stream(YT_FILE)
    await p.aclose()


async def test_search_falls_back_to_ytsearch(monkeypatch: pytest.MonkeyPatch) -> None:
    p = YtdlpProvider(YtdlpCfg(search_prefix="ytsearch5:"))
    seen: list[str] = []

    def fake_extract(url: str, **opts: Any) -> dict:
        seen.append(url)
        if url.startswith("https://music.youtube.com/"):
            raise DownloadError("ERROR: music search broken")
        return {"entries": [FLAT_TOPIC, FLAT_DASHED, None]}

    monkeypatch.setattr(p, "_extract", fake_extract)
    music_api_down(monkeypatch, p)
    res = await p.search(TrackQuery(artist="Кино", title="Группа крови"), limit=3)
    assert seen[-1] == "ytsearch3:Кино - Группа крови"
    assert [c.provider_id for c in res] == ["abc123def45", "zzz"]


async def test_search_errors_raise_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    p = YtdlpProvider(YtdlpCfg(), use_music_search=False)

    def boom(url: str, **opts: Any) -> dict:
        raise DownloadError("ERROR: network down")

    monkeypatch.setattr(p, "_extract", boom)
    with pytest.raises(ProviderError):
        await p.search(TrackQuery(artist="a", title="b"))


@pytest.mark.parametrize("apostrophe", ["'", "’"])
@pytest.mark.parametrize("operation", ["stream", "fetch"])
async def test_bot_check_explains_source_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                apostrophe: str, operation: str) -> None:
    def blocked(*args, **kwargs):
        raise DownloadError(
            f"ERROR: [youtube] Ykzk5erqQnM: Sign in to confirm you{apostrophe}re not a bot. "
            "Use --cookies-from-browser or --cookies for the authentication. See https://github.com/yt-dlp/yt-dlp"
        )

    monkeypatch.setattr(ytdlp_mod.yt_dlp.YoutubeDL, "extract_info", blocked)
    p = YtdlpProvider(YtdlpCfg())
    c = entry_to_candidate(FLAT_TOPIC)
    with pytest.raises(ProviderError, match="YouTube запросил проверку «не робот»") as error:
        if operation == "fetch":
            await p.fetch(c, tmp_path)
        else:
            await p.stream(c)
    assert "другой источник" in str(error.value)
    assert "--cookies" not in str(error.value) and "https://" not in str(error.value)


@pytest.mark.parametrize("operation", ["metadata", "stream", "fetch"])
async def test_youtube_cookies_are_reloaded_without_rewriting_export(monkeypatch: pytest.MonkeyPatch,
                                                                    tmp_path: Path, operation: str) -> None:
    cookie_path = tmp_path / "cookies.txt"
    monkeypatch.setenv("YTDLP_COOKIES_FILE", str(cookie_path))
    received = []

    def extract(ydl, url, **opts):
        received.append(ydl.cookiejar.get_cookie_header("https://www.youtube.com/watch?v=abc123def45"))
        assert ydl.cookiejar.get_cookie_header("https://example.com/") is None
        assert ydl.params.get("cookiefile") is None
        return {"id": "abc123def45", "url": "https://g.test/audio", "ext": "m4a"}

    def process(ydl, info, **opts):
        path = tmp_path / "result.m4a"
        path.write_bytes(b"audio")
        return {"requested_downloads": [{"filepath": str(path)}]}

    monkeypatch.setattr(ytdlp_mod.yt_dlp.YoutubeDL, "extract_info", extract)
    monkeypatch.setattr(ytdlp_mod.yt_dlp.YoutubeDL, "process_ie_result", process)
    p = YtdlpProvider(YtdlpCfg())
    c = entry_to_candidate(FLAT_TOPIC)
    for value in ("first", "refreshed"):
        exported = ("# Netscape HTTP Cookie File\n"
                    f"#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tSID\t{value}\n"
                    ".example.com\tTRUE\t/\tTRUE\t0\tOTHER\tprivate\n")
        cookie_path.write_text(exported, encoding="utf-8")
        before = cookie_path.stat().st_mtime_ns
        if operation == "metadata":
            await p.enrich(c)
        elif operation == "stream":
            await p.stream(c)
        else:
            await p.fetch(c, tmp_path)
        assert cookie_path.read_text(encoding="utf-8") == exported
        assert cookie_path.stat().st_mtime_ns == before
    assert received == ["SID=first", "SID=refreshed"]
    await p.aclose()


@pytest.mark.parametrize("contents", [None, "secret invalid cookies", "# Netscape HTTP Cookie File\ninvalid secret line"])
async def test_unreadable_cookies_fail_without_exposing_contents(monkeypatch: pytest.MonkeyPatch,
                                                               tmp_path: Path, contents: str | None) -> None:
    path = tmp_path / "cookies.txt"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")
    monkeypatch.setenv("YTDLP_COOKIES_FILE", str(path))
    p = YtdlpProvider(YtdlpCfg())
    with pytest.raises(ProviderError, match="Не удалось прочитать cookies YouTube") as error:
        await p.enrich(entry_to_candidate(FLAT_TOPIC))
    assert "secret" not in str(error.value)


async def test_soundcloud_does_not_load_youtube_cookies(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.providers.reuploads import SoundcloudProvider

    monkeypatch.setenv("YTDLP_COOKIES_FILE", str(tmp_path / "missing.txt"))

    def extract(ydl, url, **opts):
        assert not list(ydl.cookiejar)
        return {"id": "123"}

    monkeypatch.setattr(ytdlp_mod.yt_dlp.YoutubeDL, "extract_info", extract)
    p = SoundcloudProvider(YtdlpCfg())
    assert (await p._run(p._extract, "https://soundcloud.com/artist/track"))["id"] == "123"


async def test_enrich(monkeypatch: pytest.MonkeyPatch) -> None:
    p = YtdlpProvider(YtdlpCfg())
    flat = entry_to_candidate(FLAT_DASHED)
    assert flat is not None
    monkeypatch.setattr(p, "_extract", lambda url, **o: {**FULL_MUSIC, "id": "zzz"})
    c = await p.enrich(flat)
    assert c.enriched is True and flat.enriched is False
    assert c.bitrate_kbps == 130 and c.upload_date == date(2015, 3, 1)
    assert c.title == "Группа крови" and c.artist == "Кино"
    assert c.extra["source"] == "ytsearch"


class FakeYDL(ytdlp_mod.yt_dlp.YoutubeDL):
    """Minimal stand-in for yt_dlp.YoutubeDL used by fetch()."""

    last_opts: dict = {}

    def __init__(self, opts: dict) -> None:
        super().__init__(opts)
        self.opts = opts
        FakeYDL.last_opts = opts

    def __enter__(self) -> "FakeYDL":
        return self

    def __exit__(self, *a: object) -> None:
        return super().__exit__(*a)

    formats: list[dict] = []  # what the extraction finds
    processed: dict = {}  # the extraction handed to `process_ie_result`

    def extract_info(self, url: str, download: bool = False, process: bool = True) -> dict:
        assert not download and not process  # fetch looks at the formats before downloading
        return {"id": url.rsplit("=", 1)[1], "formats": [dict(f) for f in FakeYDL.formats]}

    def process_ie_result(self, info: dict, download: bool = False) -> dict:
        assert download
        FakeYDL.processed = info
        vid = info["id"]
        out = Path(self.prepare_filename({**info, "ext": "opus"}))
        for hook in self.opts["progress_hooks"]:
            hook({"status": "downloading", "downloaded_bytes": 0, "total_bytes": 100})
            hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100})
            hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes_estimate": 100})
            hook({"status": "finished"})
        out.write_bytes(b"OggS fake")
        Path(str(out) + ".part").write_bytes(b"")  # junk that must not be picked
        return {"id": vid, "requested_downloads": [{"filepath": str(out)}]}


@pytest.mark.parametrize("sync_cb", [True, False])
async def test_fetch_with_fake_ydl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sync_cb: bool) -> None:
    monkeypatch.setattr(ytdlp_mod.yt_dlp, "YoutubeDL", FakeYDL)
    p = YtdlpProvider(YtdlpCfg(prefer_original_codec=True))
    c = entry_to_candidate(FLAT_TOPIC)
    assert c is not None
    progress: list[float] = []
    if sync_cb:
        def cb(f: float) -> None:
            progress.append(f)
    else:
        async def cb(f: float) -> None:  # type: ignore[misc]
            progress.append(f)

    path = await p.fetch(c, tmp_path / "staging", on_progress=cb)
    await asyncio.sleep(0.05)  # let call_soon_threadsafe / coroutine callbacks run
    assert path.parent == tmp_path / "staging" and path.name.startswith("audio_") and path.suffix == ".opus"
    assert path.read_bytes() == b"OggS fake"
    assert progress == [0.0, 0.5, 1.0]
    opts = FakeYDL.last_opts
    assert opts["format"] == "bestaudio[protocol=https]/bestaudio[protocol=http]/bestaudio[protocol=m3u8_native]"
    assert opts["external_downloader"] == "native"
    assert opts["external_downloader_args"]["ffmpeg_i"] == ["-protocol_whitelist", "file,crypto,data"]
    assert opts["postprocessors"] == [{"key": "FFmpegExtractAudio", "preferredcodec": "best"}]


async def test_fetch_mp3_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ytdlp_mod.yt_dlp, "YoutubeDL", FakeYDL)
    p = YtdlpProvider(YtdlpCfg(prefer_original_codec=False))
    c = entry_to_candidate(FLAT_TOPIC)
    assert c is not None
    await p.fetch(c, tmp_path)
    assert FakeYDL.last_opts["postprocessors"][0]["preferredcodec"] == "mp3"
    assert FakeYDL.last_opts["postprocessors"][0]["preferredquality"] == "320"
    await p.aclose()


GV = "https://rr7---sn-isp1.googlevideo.com/videoplayback?expire=1&mn=sn-isp1%2Csn-far2&mm=31%2C29&fvip=1&sig=x"


def media_servers(monkeypatch: pytest.MonkeyPatch, *answering: str) -> list[str]:
    """Only the `answering` googlevideo hosts complete a TLS handshake; returns the hosts probed."""
    probed: list[str] = []

    def answers(host: str) -> bool:
        probed.append(host)
        return host in answering

    monkeypatch.setattr(ytdlp_mod, "_media_host_answers", answers)
    return probed


def test_media_mirrors_are_the_videos_other_cache_servers() -> None:
    assert ytdlp_mod.media_mirrors(GV) == [GV.replace("rr7---sn-isp1", "rr1---sn-far2")]
    assert ytdlp_mod.media_mirrors("https://g.test/140?mn=sn-a") == []  # not a YouTube media server


@pytest.mark.parametrize(("answering", "expected"), [
    (("rr7---sn-isp1.googlevideo.com",), GV),
    (("rr1---sn-far2.googlevideo.com",), GV.replace("rr7---sn-isp1", "rr1---sn-far2")),
    ((), GV),  # nothing answers: yt-dlp tries the original and reports its error
], ids=["own-server-answers", "own-server-hangs", "none-answers"])
def test_answering_url(monkeypatch: pytest.MonkeyPatch, answering: tuple[str, ...], expected: str) -> None:
    media_servers(monkeypatch, *answering)
    assert ytdlp_mod.answering_url(GV) == expected


async def test_fetch_downloads_from_a_cache_server_that_answers(monkeypatch: pytest.MonkeyPatch,
                                                                tmp_path: Path) -> None:
    monkeypatch.setattr(ytdlp_mod.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(FakeYDL, "formats", [{"format_id": "251", "url": GV}, {"format_id": "hls", "url": None}])
    probed = media_servers(monkeypatch, "rr1---sn-far2.googlevideo.com")
    c = entry_to_candidate(FLAT_TOPIC)
    assert c is not None
    await YtdlpProvider(YtdlpCfg()).fetch(c, tmp_path)
    assert [f["url"] for f in FakeYDL.processed["formats"]] == [GV.replace("rr7---sn-isp1", "rr1---sn-far2"), None]
    assert probed == ["rr7---sn-isp1.googlevideo.com", "rr1---sn-far2.googlevideo.com"]


async def test_stream_from_a_cache_server_that_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    p = YtdlpProvider(YtdlpCfg())
    monkeypatch.setattr(p, "_extract", lambda url, **o: {"url": GV, "ext": "m4a"})
    media_servers(monkeypatch, "rr1---sn-far2.googlevideo.com")
    c = entry_to_candidate(FLAT_TOPIC)
    assert c is not None
    src = await p.stream(c)
    assert src.url == GV.replace("rr7---sn-isp1", "rr1---sn-far2")
    await p.aclose()


# --------------------------------------------------------------------------- real network (opt-in)


@pytest.mark.network
@pytest.mark.skipif(not NETWORK, reason="set HUB_NETWORK_TESTS=1 to run network tests")
async def test_network_search_fetch_probe(tmp_path: Path) -> None:
    from app.library.probe import probe

    p = YtdlpProvider(YtdlpCfg())
    q = TrackQuery(artist="Кино", title="Группа крови", duration_s=285)
    res = await p.search(q, limit=5)
    assert res, "no results"
    best = next((c for c in res if "кино" in c.artist.lower()), res[0])
    best = await p.enrich(best)
    assert best.enriched
    path = await p.fetch(best, tmp_path)
    assert path.is_file() and path.stat().st_size > 100_000
    if shutil.which("ffprobe"):
        info = await probe(path)
        assert info.duration_s > 60


@pytest.mark.network
@pytest.mark.skipif(not NETWORK, reason="set HUB_NETWORK_TESTS=1 to run network tests")
async def test_network_ytsearch_fallback_only() -> None:
    p = YtdlpProvider(YtdlpCfg(), use_music_search=False)
    res = await p.search(TrackQuery(artist="Кино", title="Группа крови"), limit=3)
    assert res and all(isinstance(c, Candidate) for c in res)


async def test_provider_id_cannot_choose_download_path(monkeypatch, tmp_path):
    monkeypatch.setattr(ytdlp_mod.yt_dlp, "YoutubeDL", FakeYDL)
    provider = YtdlpProvider(YtdlpCfg())
    candidate = Candidate(provider="ytdlp", provider_id="../../outside", artist="A", title="T",
                          page_url="https://www.youtube.com/watch?v=abc123def45")
    path = await provider.fetch(candidate, tmp_path)
    assert path.parent == tmp_path and path.name.startswith("audio_")
    await provider.aclose()


async def test_downloader_cannot_return_file_outside_staging(monkeypatch, tmp_path):
    outside = tmp_path / "outside.opus"
    outside.write_bytes(b"OggS")
    monkeypatch.setattr(ytdlp_mod.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(FakeYDL, "process_ie_result", lambda *a, **kw:
                        {"requested_downloads": [{"filepath": str(outside)}]})
    provider = YtdlpProvider(YtdlpCfg())
    with pytest.raises(ProviderError, match="staging"):
        await provider.fetch(YT_FILE, tmp_path / "staging")
    await provider.aclose()
