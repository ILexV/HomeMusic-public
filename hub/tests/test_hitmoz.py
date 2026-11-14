"""hitmoz provider: parsers on real saved pages + provider behaviour with respx mocks (no network)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from app.config import HitmozCfg
from app.providers.base import Candidate, ProviderError, ProviderUnavailable, TrackQuery
from app.providers.hitmoz import (
    HitmozProvider,
    detect_canonical,
    is_empty_search,
    parse_album_page,
    parse_artist_page,
    parse_duration,
    parse_search,
    parse_search_albums,
    parse_search_artists,
    parse_track_page,
)

FIX = Path(__file__).parent / "fixtures" / "hitmoz"
BASE = "https://ru.hitmoz.org"
MP3 = b"ID3\x03\x00" + b"\x00" * (200 * 1024)


def fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------- parsers


def test_parse_search():
    res = parse_search(fx("search_kino.html"), BASE)
    assert len(res) == 48
    c = res[0]
    assert c.provider == "hitmoz" and c.provider_id == "79436285"
    assert (c.artist, c.title, c.duration_s) == ("КИНО", "Группа крови", 284)
    assert c.page_url == f"{BASE}/song/79436285"
    assert c.download_url == f"{BASE}/get/music/20250814/KINO_-_Gruppa_krovi_79436285.mp3"
    assert c.upload_date == date(2025, 8, 14)
    assert c.cover_url == "https://statcore.hitmcdn.com/cover/8e4/70d/244792.jpg"
    assert c.bitrate_kbps is None and c.explicit is None
    assert all("/get/cuts/" not in (x.download_url or "") for x in res)
    assert len({x.provider_id for x in res}) == 48


def test_empty_search():
    html = fx("search_empty.html")
    assert is_empty_search(html)
    assert parse_search(html, BASE) == []
    assert not is_empty_search(fx("search_kino.html"))


def test_parse_track_page_plain():
    d = parse_track_page(fx("track_kino.html"), BASE)
    assert d["id"] == "79436285"
    assert d["full_title"] == "Кино - Группа крови"
    assert d["explicit"] is None
    assert d["bitrate_kbps"] == 320 and d["size_mb"] == 10.83 and d["duration_s"] == 284
    # protocol-relative option value made absolute; /get/cuts/ options are never chosen
    assert d["download_url"] == f"{BASE}/get/music/20250814/KINO_-_Gruppa_krovi_79436285.mp3"
    assert [o["bitrate_kbps"] for o in d["options"]] == [320, 128, 64]
    assert d["artist_id"] == "1286"


def test_parse_track_page_explicit():
    d = parse_track_page(fx("track_explicit.html"), BASE)
    assert d["full_title"] == "Eminem - Lose Yourself (Soundtrack Version (Explicit))"
    assert d["version"] == "Soundtrack Version (Explicit)"
    assert d["explicit"] is True
    assert d["bitrate_kbps"] == 320 and d["size_mb"] == 12.46 and d["duration_s"] == 326
    assert d["upload_date"] == date(2017, 8, 30)


def test_detect_canonical():
    assert detect_canonical(fx("album.html")) == f"{BASE}/album/488389"
    assert detect_canonical("<html><head></head></html>") is None


def test_parse_artist_page():
    d = parse_artist_page(fx("artist_kino.html"), BASE)
    assert d["id"] == "1286" and d["name"] == "Кино"
    assert len(d["tracks"]) == 48 and d["max_offset"] == 720
    assert d["tracks"][0].title == "Хочу перемен"


def test_parse_album_page():
    d = parse_album_page(fx("album.html"), BASE)
    assert (d["id"], d["title"], d["year"]) == ("488389", "Акустический альбом", 1998)
    assert d["artist"] == "Король и Шут" and d["artist_id"] == "1701" and d["genre"] == "Панк"
    assert d["cover_url"].startswith("https://avatars.yandex.net/")
    assert len(d["tracks"]) == 17  # an empty placeholder row is skipped
    assert d["tracks"][0].title == "Ели мясо мужики"


def test_parse_search_blocks():
    html = fx("search_artist_kino.html")
    artists = parse_search_artists(html, BASE)
    assert artists[0] == {
        "id": "1286",
        "name": "Кино",
        "picture_url": "https://avatars.yandex.net/get-music-content/49707/703d86d2.p.41075/150x150",
    }
    assert all(a["picture_url"] is None or "//250x250" not in a["picture_url"] for a in artists)
    albums = parse_search_albums(html, BASE)
    assert albums[0]["id"] == "66033" and albums[0]["title"] == "Музыка кино" and albums[0]["year"] == 2015


def test_parse_duration():
    assert parse_duration("04:44") == 284
    assert parse_duration("1:02:03") == 3723
    assert parse_duration("MP3 320Kbps / 12.46 Мб / 05:26") == 326
    assert parse_duration("") is None


# ---------------------------------------------------------------- provider (respx)


def _lang_guard(html: str):
    """Site behaviour: 403 with an empty body without `Accept-Language: ru`."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "ru" not in request.headers.get("accept-language", ""):
            return httpx.Response(403, content=b"")
        return httpx.Response(200, text=html)

    return handler


@pytest.fixture
def cfg() -> HitmozCfg:
    return HitmozCfg(rate_per_sec=1000)


@pytest.fixture
async def provider(cfg):
    client = httpx.AsyncClient(follow_redirects=True)  # bare client: the provider must add Accept-Language itself
    p = HitmozProvider(cfg, client=client)
    p.base_delay = 0.01
    yield p
    await client.aclose()


@respx.mock
async def test_search_sends_accept_language(provider):
    home = respx.get(f"{BASE}/").mock(side_effect=_lang_guard(fx("search_empty.html")))
    search = respx.get(f"{BASE}/search").mock(side_effect=_lang_guard(fx("search_kino.html")))
    res = await provider.search(TrackQuery(artist="Кино", title="Группа крови"), limit=5)
    assert len(res) == 5 and res[0].provider_id == "79436285"
    assert home.called and search.call_count == 1
    assert search.calls.last.request.url.params["q"] == "Кино Группа крови"
    async with httpx.AsyncClient() as bare:  # without the header the (mocked) site refuses
        assert (await bare.get(f"{BASE}/search")).status_code == 403


@respx.mock
async def test_search_reads_second_page_only_when_needed(provider):
    respx.get(f"{BASE}/").mock(return_value=httpx.Response(200, text=fx("search_empty.html")))
    p1 = respx.get(f"{BASE}/search").mock(return_value=httpx.Response(200, text=fx("search_kino.html")))
    p2 = respx.get(f"{BASE}/search/start/48").mock(return_value=httpx.Response(200, text=fx("search_empty.html")))
    await provider.search(TrackQuery(artist="Кино", title="Группа крови"), limit=10)
    assert p1.call_count == 1 and not p2.called
    res = await provider.search(TrackQuery(artist="Кино", title="Группа крови"), limit=60)
    assert p2.call_count == 1 and len(res) == 48


@respx.mock
async def test_domain_fallback_and_canonical(cfg):
    cfg = cfg.model_copy(update={"domains": ["ru.hitmoz.org", "hitmos.me"]})
    respx.get(f"{BASE}/").mock(side_effect=httpx.ConnectError("down"))
    respx.get("https://hitmos.me/").mock(
        return_value=httpx.Response(200, text='<link rel="canonical" href="https://ru.hitmoz.org/"/>')
    )
    p = HitmozProvider(cfg)
    try:
        # canonical points to a host that just failed -> stay on the mirror that answered
        assert await p.base_url() == "https://hitmos.me"
        h = await p.health()
        assert h["ok"] and h["domain"] == "hitmos.me" and h["latency_ms"] is not None
    finally:
        await p.aclose()


@respx.mock
async def test_mirror_canonical_detects_real_host(cfg):
    cfg = cfg.model_copy(update={"domains": ["rus.hitmotop.com"]})
    respx.get("https://rus.hitmotop.com/").mock(
        return_value=httpx.Response(200, text='<link rel="canonical" href="https://ru.hitmoz.org/"/>')
    )
    p = HitmozProvider(cfg)
    try:
        assert await p.base_url() == BASE
    finally:
        await p.aclose()


@respx.mock
async def test_no_domain_works(provider):
    for d in provider.cfg.domains:
        respx.get(f"https://{d}/").mock(return_value=httpx.Response(403))
    with pytest.raises(ProviderUnavailable):
        await provider.search(TrackQuery(artist="a", title="b"))
    h = await provider.health(force=True)
    assert h["ok"] is False and "403" in h["error"]


@respx.mock
async def test_retry_on_429(provider):
    respx.get(f"{BASE}/").mock(return_value=httpx.Response(200, text=""))
    route = respx.get(f"{BASE}/song/47829428").mock(
        side_effect=[httpx.Response(429), httpx.Response(200, text=fx("track_explicit.html"))]
    )
    c = Candidate(provider="hitmoz", provider_id="47829428", artist="Eminem", title="Lose Yourself", page_url="x")
    e = await provider.enrich(c)
    assert route.call_count == 2
    assert e.enriched and e.explicit is True and e.bitrate_kbps == 320 and e.duration_s == 326
    assert e.extra["full_title"].endswith("(Soundtrack Version (Explicit))")
    assert e.download_url == f"{BASE}/get/music/20170830/Eminem_-_Lose_Yourself_47829428.mp3"
    assert e.upload_date == date(2017, 8, 30)
    assert c.enriched is False  # copy


@respx.mock
async def test_fetch_follows_redirect_to_cdn(provider, tmp_path):
    respx.get(f"{BASE}/").mock(return_value=httpx.Response(200, text=""))
    respx.get(f"{BASE}/search").mock(return_value=httpx.Response(200, text=fx("search_kino.html")))
    respx.get(f"{BASE}/song/79436285").mock(return_value=httpx.Response(200, text=fx("track_kino.html")))
    cdn = "https://cdn12.deliciousoranges.com/s3/get/music/20250814/KINO_-_Gruppa_krovi_79436285.mp3"
    get = respx.get(f"{BASE}/get/music/20250814/KINO_-_Gruppa_krovi_79436285.mp3").mock(
        return_value=httpx.Response(302, headers={"Location": cdn})
    )
    file = respx.get(cdn).mock(return_value=httpx.Response(200, content=MP3, headers={"Content-Type": "audio/mpeg"}))
    res = await provider.search(TrackQuery(artist="Кино", title="Группа крови"), limit=1)
    c = await provider.enrich(res[0])
    progress: list[float] = []
    path = await provider.fetch(c, tmp_path, on_progress=progress.append)
    assert path.parent == tmp_path and path.name.startswith("audio_") and path.suffix == ".mp3"
    assert path.read_bytes()[:3] == b"ID3" and path.stat().st_size == len(MP3)
    assert not list(tmp_path.glob("*.part"))
    assert file.called and progress[-1] == 1.0
    assert get.calls.last.request.headers["accept-language"].startswith("ru")


@respx.mock
async def test_fetch_rejects_html_and_tiny_files(provider, tmp_path):
    url = f"{BASE}/get/music/20250814/x_1.mp3"
    c = Candidate(provider="hitmoz", provider_id="1", artist="a", title="b", page_url="x", download_url=url)
    route = respx.get(url).mock(return_value=httpx.Response(200, text="<html>blocked</html>"))
    with pytest.raises(ProviderError):
        await provider.fetch(c, tmp_path)
    route.return_value = httpx.Response(200, content=b"ID3" + b"\x00" * 1000, headers={"Content-Type": "audio/mpeg"})
    with pytest.raises(ProviderError):
        await provider.fetch(c, tmp_path)
    assert list(tmp_path.iterdir()) == []


async def test_fetch_never_uses_cuts(provider, tmp_path):
    c = Candidate(
        provider="hitmoz",
        provider_id="1",
        artist="a",
        title="b",
        page_url="x",
        download_url=f"{BASE}/get/cuts/0b/7f/hash/1/x_b128f0d284.mp3",
    )
    with respx.mock:
        respx.get(f"{BASE}/").mock(return_value=httpx.Response(200, text=""))
        respx.get(f"{BASE}/song/1").mock(return_value=httpx.Response(200, text="<h1 class='p-track-title'>a - b</h1>"))
        with pytest.raises(ProviderError):
            await provider.fetch(c, tmp_path)


@respx.mock
async def test_provider_id_cannot_choose_download_path(provider, tmp_path):
    url = f"{BASE}/get/music/20250814/x_1.mp3"
    respx.get(url).mock(return_value=httpx.Response(200, content=MP3, headers={"Content-Type": "audio/mpeg"}))
    candidate = Candidate(provider="hitmoz", provider_id="../../outside", artist="a", title="b",
                          page_url="x", download_url=url)
    path = await provider.fetch(candidate, tmp_path)
    assert path.parent == tmp_path and path.name.startswith("audio_")
    assert path.read_bytes() == MP3


async def test_enrich_rejects_path_as_provider_id(provider):
    candidate = Candidate(provider="hitmoz", provider_id="../admin", artist="a", title="b", page_url="x")
    with pytest.raises(ProviderError, match="идентификатор"):
        await provider.enrich(candidate)
