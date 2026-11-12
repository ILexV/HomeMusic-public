"""Listening before downloading: GET /api/listen picks the file a download would pick and streams it with Range."""

from __future__ import annotations

import asyncio
from datetime import date

import httpx
import pytest
import respx

from tests.support import FakeDeezer, FakeProvider, app_client, cand

AUDIO = bytes(range(256)) * 1024  # 256 KiB standing in for an MP3


async def test_reset_waits_for_cancelled_resolution_cleanup(monkeypatch):
    from app import listen

    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def resolve(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    monkeypatch.setattr(listen, "_resolve", resolve)
    caller = asyncio.create_task(listen.choose("dz:cleanup"))
    await asyncio.wait_for(started.wait(), 1)
    reset = asyncio.create_task(listen.reset())
    await asyncio.wait_for(cleaning.wait(), 1)
    assert not reset.done()
    release.set()
    await asyncio.wait_for(reset, 1)
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert not listen._inflight


def ranged(request: httpx.Request, body: bytes = AUDIO) -> httpx.Response:
    """A CDN that honours `Range: bytes=a-b`."""
    rng = request.headers.get("range")
    if not rng:
        return httpx.Response(200, content=body, headers={"content-type": "audio/mpeg"})
    a, _, b = rng.removeprefix("bytes=").partition("-")
    start, end = int(a), min(int(b) if b else len(body) - 1, len(body) - 1)
    return httpx.Response(206, content=body[start : end + 1], headers={
        "content-type": "audio/mpeg", "content-range": f"bytes {start}-{end}/{len(body)}"})


class Partway(httpx.AsyncByteStream):
    """A CDN body that stops after `at` bytes: the connection drops (`drop`), or the rest waits for `gate`."""

    def __init__(self, body: bytes, at: int, *, drop: bool = False, gate: asyncio.Event | None = None):
        self.body, self.at, self.drop, self.gate = body, at, drop, gate

    async def __aiter__(self):
        yield self.body[: self.at]
        if self.drop:
            raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")
        if self.gate is not None:
            await self.gate.wait()
        yield self.body[self.at :]


def partway(request: httpx.Request, at: int, **kw) -> httpx.Response:
    """`ranged` whose body stops partway (`Partway`)."""
    r = ranged(request)
    return httpx.Response(r.status_code, headers=r.headers, stream=Partway(r.content, at, **kw))


@pytest.fixture
async def env(hub_env):
    from app import config
    from app.main import create_app
    from app.services import svc

    music = hub_env

    def nd_handler(request: httpx.Request) -> httpx.Response:
        ok = {"status": "ok", "version": "1.16.1"}
        if request.url.path.endswith("getUser"):
            return httpx.Response(200, json={"subsonic-response": {**ok, "user": {"adminRole": True}}})
        return httpx.Response(200, json={"subsonic-response": ok})

    app = create_app()
    async with app.router.lifespan_context(app), httpx.AsyncClient() as upstream:
        with respx.mock(assert_all_called=False) as mock:
            mock.route(host="navidrome.test").mock(side_effect=nd_handler)
            mock.route(host="cdn.test", path="/good.mp3").mock(side_effect=ranged)
            mock.route(host="cdn.test", path="/dead.mp3").mock(return_value=httpx.Response(404))
            mock.route(host="cdn.test", path="/page.mp3").mock(
                return_value=httpx.Response(200, text="<html>", headers={"content-type": "text/html"}))
            mock.route(host="preview.test").mock(side_effect=ranged)
            hm = FakeProvider("hitmoz", [], client=upstream)
            fm = FakeProvider("hitmos_fm", [], client=upstream)
            svc.providers = {"hitmoz": hm, "hitmos_fm": fm}
            svc.deezer = FakeDeezer()
            config.get_settings().cfg.sources.ru = ["hitmoz", "hitmos_fm"]
            async with app_client(app) as client:
                yield {"client": client, "hm": hm, "fm": fm, "mock": mock, "music": music, "app": app}


async def test_listen_picks_the_ranked_file_and_streams_ranges(env):
    c, hm = env["client"], env["hm"]
    hm.cands = [
        cand("9", "https://cdn.test/dead.mp3", title="Группа крови (караоке)"),  # rejected by ranking
        cand("2", "https://cdn.test/dead.mp3", bitrate=128),
        cand("1", "https://cdn.test/good.mp3"),  # 320 kbps ranks first
    ]
    r = await c.get("/api/listen", params={"ref": "dz:100"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["kind"] == "full" and out["provider"] == "hitmoz" and out["page_url"] == "https://hitmoz.test/song/1"
    assert out["title"] == "Группа крови" and out["duration_s"] == 245
    assert out["stream_url"] == "/api/listen/stream?ref=dz%3A100"

    r = await c.get(out["stream_url"], headers={"Range": "bytes=100-199"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 100-199/{len(AUDIO)}"
    assert r.headers["content-type"].startswith("audio/mpeg") and r.headers["accept-ranges"] == "bytes"
    assert r.content == AUDIO[100:200]

    r = await c.get(out["stream_url"])
    assert r.status_code == 200 and r.content == AUDIO

    # listening has to start fast: a short search and no track pages (a download asks for 15 and 3)
    assert hm.limits == [4]

    # the choice is cached; once heard, its track page is checked, and a download of this ref tries it first
    await c.get("/api/listen", params={"ref": "dz:100"})
    assert hm.searched == 1
    from app.listen import listened_candidate

    heard = await listened_candidate("dz:100")
    assert heard is not None and heard.provider_id == "1" and heard.enriched
    assert hm.enriched == ["1"]
    assert await listened_candidate("dz:101") is None


async def test_slow_listen_releases_auth_connection_before_resolving_and_allows_likes(env, monkeypatch):
    from app.api import listen as api
    from app.db import engine
    from app.listen import Choice

    entered, release = asyncio.Event(), asyncio.Event()

    async def choose(ref):
        entered.set()
        await release.wait()
        return Choice(ref=ref, kind="preview", artist="QA", title="Song", duration_s=214)

    monkeypatch.setattr(api, "_choose", choose)
    pending = asyncio.create_task(env["client"].get("/api/listen", params={"ref": "dz:100"}))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        # Fifteen such requests used to consume the entire pool while their own resolvers
        # needed another connection, blocking star/starred and authentication as well.
        assert engine().pool.checkedout() == 0
        response = await env["client"].post("/api/star", json={"song_ids": ["qa-song"]})
        assert response.status_code == 200, response.text
    finally:
        release.set()
        response = await asyncio.wait_for(pending, 1)
    assert response.status_code == 200, response.text


async def test_file_that_does_not_open_moves_to_the_next(env):
    c, hm, fm = env["client"], env["hm"], env["fm"]
    hm.cands = [cand("1", "https://cdn.test/dead.mp3"), cand("2", "https://cdn.test/page.mp3")]
    fm.cands = [cand("7", "https://cdn.test/good.mp3", provider="hitmos_fm")]
    # a tap doesn't open the file first: the stream does, so the pick names the first source that has the track
    r = await c.get("/api/listen", params={"ref": "dz:100"})
    assert r.status_code == 200, r.text
    assert r.json()["provider"] == "hitmoz"
    # none of its files opens: the Hub picks again, opening each file before taking it
    r = await c.get(r.json()["stream_url"], headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.content == AUDIO[:10]
    r = await c.get("/api/listen", params={"ref": "dz:100"})
    assert (r.json()["provider"], r.json()["page_url"]) == ("hitmos_fm", "https://hitmos_fm.test/song/7")
    assert hm.searched == 2 and fm.searched == 2


async def test_all_sources_are_searched_at_once(env):
    c, hm, fm = env["client"], env["hm"], env["fm"]
    fm.cands = [cand("7", "https://cdn.test/good.mp3", provider="hitmos_fm")]
    r = await c.get("/api/listen", params={"ref": "dz:100"})
    assert r.json()["provider"] == "hitmos_fm"
    assert hm.searched == 1 and fm.searched == 1


async def test_pick_is_remembered_across_restarts(env):
    from app import listen

    c, hm = env["client"], env["hm"]
    hm.cands = [cand("1", "https://cdn.test/good.mp3")]
    url = (await c.get("/api/listen", params={"ref": "dz:100"})).json()["stream_url"]
    assert (await c.get(url, headers={"Range": "bytes=0-9"})).status_code == 206
    assert await listen.listened_candidate("dz:100") is not None  # (waits for the check after listening)
    await listen.reset()  # the Hub restarted: nothing in memory

    r = await c.get("/api/listen", params={"ref": "dz:100"})
    assert r.json()["provider"] == "hitmoz" and r.json()["page_url"] == "https://hitmoz.test/song/1"
    r = await c.get(url, headers={"Range": "bytes=10-19"})
    assert r.status_code == 206 and r.content == AUDIO[10:20]
    assert hm.searched == 1  # no new search
    heard = await listen.listened_candidate("dz:100")
    assert heard is not None and heard.provider_id == "1"


async def test_russian_track_takes_soundcloud_uploaded_before_the_cutoff_over_youtube(env):
    from app.config import SourcesCfg, get_settings
    from app.services import svc

    get_settings().cfg.sources.ru = SourcesCfg().ru
    up = env["hm"].client
    sc = FakeProvider("soundcloud", [
        # the better file, but uploaded since May 2026: it may be edited
        cand("new", "https://cdn.test/good.mp3", provider="soundcloud").model_copy(
            update={"upload_date": date(2026, 5, 1)}),
        cand("old", "https://cdn.test/good.mp3", provider="soundcloud", bitrate=160),
    ], client=up, streams=False)
    yt = FakeProvider("ytdlp", [cand("yt", "https://cdn.test/good.mp3", provider="ytdlp")], client=up)
    svc.providers.update(soundcloud=sc, ytdlp=yt)
    r = await env["client"].get("/api/listen", params={"ref": "dz:100"})
    assert r.status_code == 200, r.text
    assert r.json()["provider"] == "soundcloud" and r.json()["page_url"] == "https://soundcloud.test/song/old"


@pytest.mark.parametrize("picked", ["hitmoz", "ytdlp"])
async def test_pick_outranked_by_a_new_source_order_is_picked_again(env, picked):
    from app import listen
    from app.config import SourcesCfg, get_settings
    from app.services import svc

    c, hm, up = env["client"], env["hm"], env["hm"].client
    sources = get_settings().cfg.sources
    sources.ru = ["hitmoz", "hitmos_fm", "ytdlp"]  # the order before SoundCloud came ahead of YouTube
    svc.providers["ytdlp"] = FakeProvider("ytdlp", [cand("yt", "https://cdn.test/good.mp3", provider="ytdlp")],
                                          client=up)
    if picked == "hitmoz":
        hm.cands = [cand("1", "https://cdn.test/good.mp3")]
    assert (await c.get("/api/listen", params={"ref": "dz:100"})).json()["provider"] == picked
    await listen.reset()

    sources.ru = SourcesCfg().ru
    sc = FakeProvider("soundcloud", [cand("sc", "https://cdn.test/good.mp3", provider="soundcloud")], client=up,
                      streams=False)
    svc.providers["soundcloud"] = sc
    r = await c.get("/api/listen", params={"ref": "dz:100"})
    # a hitmoz pick still comes first; a YouTube one gives way to SoundCloud, which was not asked then
    assert r.json()["provider"] == ("hitmoz" if picked == "hitmoz" else "soundcloud")
    assert sc.searched == (0 if picked == "hitmoz" else 1)


async def test_youtube_pick_gives_way_when_hitmoz_moves_ahead(env):
    # hitmoz was behind YouTube, so it was not asked when YouTube had the track
    from app import listen
    from app.config import get_settings
    from app.services import svc

    c, hm, up = env["client"], env["hm"], env["hm"].client
    sources = get_settings().cfg.sources
    sources.ru = ["ytdlp", "hitmoz"]
    svc.providers["ytdlp"] = FakeProvider("ytdlp", [cand("yt", "https://cdn.test/good.mp3", provider="ytdlp")],
                                          client=up)
    hm.cands = [cand("1", "https://cdn.test/good.mp3")]
    assert (await c.get("/api/listen", params={"ref": "dz:100"})).json()["provider"] == "ytdlp"
    await listen.reset()

    sources.ru = ["hitmoz", "ytdlp"]
    r = await c.get("/api/listen", params={"ref": "dz:100"})
    assert r.json()["provider"] == "hitmoz", r.text


async def test_imported_track_forgets_the_pick(env):
    from app import listen

    c, hm = env["client"], env["hm"]
    hm.cands = [cand("1", "https://cdn.test/good.mp3")]
    await c.get("/api/listen", params={"ref": "dz:100"})
    await listen.forget("dz:100")
    await listen.reset()
    await c.get("/api/listen", params={"ref": "dz:100"})
    assert hm.searched == 2


async def test_prefetch_picks_and_opens_tracks_on_screen(env):
    import asyncio

    from app import listen

    c, hm = env["client"], env["hm"]
    hm.cands = [cand("1", "https://cdn.test/dead.mp3", bitrate=None), cand("2", "https://cdn.test/good.mp3", bitrate=256)]
    r = await c.post("/api/listen/prefetch", json={"refs": ["dz:100", "dz:100", "bad"]})
    assert r.status_code == 200 and r.json()["queued"] == 2
    for _ in range(200):
        hit = listen._choices.get("dz:100")
        if hit and hit.source is not None and "bad" in listen._missed:
            break
        await asyncio.sleep(0.01)
    hit = listen._choices["dz:100"]
    # the background pick has opened its file already: the dead one gave way to the next
    assert hit.candidate.provider_id == "2" and hit.source is not None
    # what is ready (or recently not found) is not queued again
    assert (await c.post("/api/listen/prefetch", json={"refs": ["dz:100", "bad"]})).json()["queued"] == 0

    r = await c.get("/api/listen", params={"ref": "dz:100"})
    assert r.json()["page_url"] == "https://hitmoz.test/song/2"
    assert (await c.get(r.json()["stream_url"], headers={"Range": "bytes=0-9"})).status_code == 206
    assert hm.searched == 1
    assert (await c.post("/api/listen/prefetch", json={"refs": []})).json()["queued"] == 0


async def test_censored_copy_is_not_downloaded_and_is_picked_again(env):
    from app import listen
    from app.services import svc

    c, hm = env["client"], env["hm"]
    svc.deezer.track = svc.deezer.track.model_copy(update={"explicit": True})
    hm.cands = [cand("1", "https://cdn.test/good.mp3"), cand("3", "https://cdn.test/good.mp3", bitrate=256)]
    hm.explicit = {"1": False, "3": True}  # only the track pages tell: "1" is the clean copy
    url = (await c.get("/api/listen", params={"ref": "dz:100"})).json()["stream_url"]
    assert (await c.get(url, headers={"Range": "bytes=0-9"})).status_code == 206
    assert await listen.listened_candidate("dz:100") is None  # a download searches as usual
    await listen.reset()
    r = await c.get("/api/listen", params={"ref": "dz:100"})
    assert r.json()["page_url"] == "https://hitmoz.test/song/3"  # remembered from the download-grade search
    assert hm.searched == 2


async def test_expired_link_is_renewed(env):
    c, hm, mock = env["client"], env["hm"], env["mock"]
    exp = {"n": 1}
    # hitmoz-like: /get/music/ redirects to a signed CDN link; only the newest link works
    mock.route(host="hm.test").mock(side_effect=lambda req: httpx.Response(
        302, headers={"location": f"https://signed.test/a.mp3?exp={exp['n']}"}))
    mock.route(host="signed.test").mock(side_effect=lambda req: ranged(req) if req.url.params["exp"] == str(exp["n"])
                                        else httpx.Response(403))
    hm.cands = [cand("1", "https://hm.test/get/music/1.mp3")]
    url = (await c.get("/api/listen", params={"ref": "dz:100"})).json()["stream_url"]
    assert (await c.get(url, headers={"Range": "bytes=0-9"})).status_code == 206

    from app.listen import _choices, _copies

    assert _choices["dz:100"].source.url == "https://signed.test/a.mp3?exp=1"  # later requests skip the redirect
    exp["n"] = 2
    _copies.clear()  # the server copy was pruned: the file comes from the source again
    r = await c.get(url, headers={"Range": "bytes=10-19"})
    assert r.status_code == 206 and r.content == AUDIO[10:20]


async def test_audio_is_proxied_over_http1_with_the_provider_headers(env):
    from app import listen

    c, hm, mock = env["client"], env["hm"], env["mock"]
    seen: list[httpx.Request] = []
    mock.route(host="h1.test").mock(side_effect=lambda req: seen.append(req) or ranged(req))
    hm.client = httpx.AsyncClient(headers={"Accept-Language": "ru-RU"}, http2=True)
    hm.cands = [cand("1", "https://h1.test/a.mp3")]
    url = (await c.get("/api/listen", params={"ref": "dz:100"})).json()["stream_url"]
    for rng, body in (("bytes=0-9", AUDIO[:10]), ("bytes=100-", AUDIO[100:])):  # the player leaves for a seek
        r = await c.get(url, headers={"Range": rng})
        assert r.status_code == 206 and r.content == body
    # the Hub downloads the whole file once (its server copy); the seek is served from its disk
    assert [r.headers["range"] for r in seen] == ["bytes=0-"]
    assert all(r.headers["accept-language"] == "ru-RU" and r.headers["accept-encoding"] == "identity" for r in seen)
    # not the provider's HTTP/2 client: abandoned streams there starve the next ones on the connection
    assert listen._media is not None and listen._media is not hm.client
    await hm.client.aclose()


async def test_dropped_upstream_connection_is_resumed(env):
    c, hm, mock = env["client"], env["hm"], env["mock"]
    seen: list[str | None] = []

    def cdn(req: httpx.Request) -> httpx.Response:  # googlevideo cuts a long response midway
        seen.append(req.headers.get("range"))
        return partway(req, 1000, drop=True) if len(seen) == 1 else ranged(req)

    mock.route(host="drop.test").mock(side_effect=cdn)
    hm.cands = [cand("1", "https://drop.test/a.mp3")]
    url = (await c.get("/api/listen", params={"ref": "dz:100"})).json()["stream_url"]
    r = await c.get(url)
    # the player gets the whole file: the Hub resumes its download from the byte the source broke at
    assert r.status_code == 200 and r.content == AUDIO
    assert seen == ["bytes=0-", "bytes=1000-"]


async def test_track_loaded_ahead_waits_for_the_played_one(env):
    from app import listen

    c, hm, mock = env["client"], env["hm"], env["mock"]
    gate = asyncio.Event()
    mock.route(host="slow.test").mock(side_effect=lambda req: partway(req, 1000, gate=gate))
    hm.cands = [cand("1", "https://slow.test/a.mp3"), cand("2", "https://cdn.test/good.mp3")]
    await search_files(c)

    played = await listen.open_media("file:hitmoz:1", None)
    assert played.body is not None
    assert await anext(played.body) == AUDIO[:1000]  # the player reads it while the Hub downloads the rest
    # the app loads the next track ahead: its server copy waits while the played one downloads
    ahead = await listen.open_media("file:hitmoz:2", "bytes=0-9", ahead=True)
    assert ahead.status == 206 and ahead.body is not None
    await asyncio.sleep(0.3)
    assert listen._copies["file:hitmoz:2"].have == 0

    gate.set()
    assert AUDIO[:1000] + b"".join([chunk async for chunk in played.body]) == AUDIO
    assert b"".join([chunk async for chunk in ahead.body]) == AUDIO[:10]


async def test_seek_far_past_the_server_copy_goes_to_the_source(env, monkeypatch):
    from app import listen

    c, hm, mock = env["client"], env["hm"], env["mock"]
    monkeypatch.setattr(listen, "SEEK_WAIT_BYTES", 1000)
    gate = asyncio.Event()
    seen: list[str] = []

    def cdn(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers["range"])
        return partway(req, 1000, gate=gate) if req.headers["range"] == "bytes=0-" else ranged(req)

    mock.route(host="slow.test").mock(side_effect=cdn)
    hm.cands = [cand("1", "https://slow.test/a.mp3")]
    url = (await c.get("/api/listen", params={"ref": "dz:100"})).json()["stream_url"]
    r = await c.get(url, headers={"Range": "bytes=100000-100009"})
    assert r.status_code == 206 and r.content == AUDIO[100000:100010]
    assert r.headers["content-range"] == f"bytes 100000-100009/{len(AUDIO)}"
    assert seen == ["bytes=0-", "bytes=100000-100009"]  # not waiting for the copy to get there
    gate.set()


async def test_deezer_preview_when_no_source_has_it(env):
    c = env["client"]
    r = await c.get("/api/listen", params={"ref": "dz:100"})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "preview" and r.json()["duration_s"] == 30 and r.json()["provider"] is None
    r = await c.get(r.json()["stream_url"], headers={"Range": "bytes=0-"})
    assert r.status_code == 206 and r.content == AUDIO
    from app.services import svc

    assert svc.deezer.previews >= 1  # a fresh signed link, not the cached catalog one


async def test_full_song_request_never_plays_a_preview(env):
    c = env["client"]
    assert (await c.get("/api/listen", params={"ref": "dz:100"})).json()["kind"] == "preview"
    response = await c.get("/api/listen/stream", params={"ref": "dz:100", "full": "1"})
    assert response.status_code == 503, response.text
    assert "Полная версия" in response.json()["detail"]
    metadata = await c.get("/api/listen", params={"ref": "dz:100", "full": "1"})
    assert metadata.status_code == 503, metadata.text
    assert not any(call.request.url.host == "preview.test" for call in env["mock"].calls)


async def test_full_song_replaces_a_cached_preview_with_the_download_fallback(env):
    from app.services import svc

    c = env["client"]
    assert (await c.get("/api/listen", params={"ref": "dz:100"})).json()["kind"] == "preview"
    svc.providers["soundcloud"] = FakeProvider("soundcloud", [
        cand("full", "https://cdn.test/good.mp3", provider="soundcloud"),
    ], client=env["hm"].client)
    response = await c.get("/api/listen/stream", params={"ref": "dz:100", "full": "1"},
                           headers={"Range": "bytes=0-"})
    assert response.status_code == 206, response.text
    assert response.headers["X-HomeMusic-Listen-Kind"] == "full"
    assert response.content == AUDIO
    metadata = await c.get("/api/listen", params={"ref": "dz:100", "full": "1"})
    assert metadata.json()["kind"] == "full"
    assert not any(call.request.url.host == "preview.test" for call in env["mock"].calls)


async def test_slow_regular_sources_have_a_deadline_before_full_song_fallback(env, monkeypatch):
    from app import listen
    from app.services import svc

    # Expire the regular-source budget immediately; no real timer is waited out.
    monkeypatch.setattr(listen, "SEARCH_TIMEOUT_S", 0)
    svc.providers["soundcloud"] = FakeProvider("soundcloud", [
        cand("full", "https://cdn.test/good.mp3", provider="soundcloud"),
    ], client=env["hm"].client)
    response = await env["client"].get("/api/listen", params={"ref": "dz:100"})
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "full" and response.json()["provider"] == "soundcloud"


async def test_optional_name_lookup_cannot_delay_playback(env, monkeypatch):
    from app import listen

    monkeypatch.setattr(listen, "NAME_TIMEOUT_S", 0)
    env["hm"].cands = [cand("full", "https://cdn.test/good.mp3")]
    response = await env["client"].get("/api/listen", params={"ref": "dz:100"})
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "full" and response.json()["provider"] == "hitmoz"


async def test_a_full_request_joining_browser_resolution_does_not_play_the_preview(env, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def search(q, limit=10):
        entered.set()
        await release.wait()
        return []

    monkeypatch.setattr(env["hm"], "search", search)
    browser = asyncio.create_task(env["client"].get("/api/listen", params={"ref": "dz:100"}))
    native = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        native = asyncio.create_task(env["client"].get("/api/listen/stream", params={"ref": "dz:100", "full": "1"}))
        await asyncio.sleep(0)
        release.set()
        assert (await asyncio.wait_for(browser, 1)).json()["kind"] == "preview"
        response = await asyncio.wait_for(native, 1)
        assert response.status_code == 503, response.text
        assert not any(call.request.url.host == "preview.test" for call in env["mock"].calls)
    finally:
        release.set()
        await asyncio.gather(*(t for t in [browser, native] if t is not None), return_exceptions=True)


async def test_import_finishing_during_resolution_replaces_the_preview(env, monkeypatch):
    from app.db import session_scope
    from app.matching.normalize import fingerprint
    from app.models import Track

    entered, release = asyncio.Event(), asyncio.Event()

    async def search(q, limit=10):
        entered.set()
        await release.wait()
        return []

    monkeypatch.setattr(env["hm"], "search", search)
    pending = asyncio.create_task(env["client"].get("/api/listen/stream", params={"ref": "dz:100", "full": "1"}))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        rel = "QA/song.mp3"
        path = env["music"] / "library" / rel
        path.parent.mkdir(parents=True)
        path.write_bytes(AUDIO)
        async with session_scope() as s:
            s.add(Track(uid="imported-during-search", path=rel, artist="Кино", title="Группа крови",
                        duration_s=245, isrc="RUA1D8800001", nd_song_id="qa-song",
                        fingerprint=fingerprint("Кино", "Группа крови")))
    finally:
        release.set()
        response = await asyncio.wait_for(pending, 1)
    assert response.status_code == 200, response.text
    assert response.headers["X-HomeMusic-Listen-Kind"] == "library"
    assert response.content == AUDIO


async def test_preview_picked_while_a_source_is_down_is_not_remembered(env):
    import time

    from app import listen

    c, hm, fm = env["client"], env["hm"], env["fm"]
    hm.down = fm.down = True  # e.g. the Hub's network dropped for a minute
    r = await c.get("/api/listen", params={"ref": "dz:100"})
    assert r.json()["kind"] == "preview"
    hit = listen._choices["dz:100"]
    assert hit.transient
    # back online: the preview isn't kept for long, and nothing was remembered
    hm.down = fm.down = False
    hm.cands = [cand("1", "https://cdn.test/good.mp3")]
    hit.at = time.monotonic() - listen.TRANSIENT_TTL_S - 1
    assert (await c.get("/api/listen", params={"ref": "dz:100"})).json()["kind"] == "full"
    await listen.reset()
    assert (await c.get("/api/listen", params={"ref": "dz:100"})).json()["kind"] == "full"

    # no preview either: an error, not "not found"
    from app.services import svc

    svc.deezer = FakeDeezer(preview=None)
    hm.down = fm.down = True
    await listen.forget("dz:100")
    await listen.reset()
    r = await c.get("/api/listen", params={"ref": "dz:100"})
    assert r.status_code == 502 and "недоступны" in r.json()["detail"]


async def test_nothing_to_play(env):
    from app.services import svc

    svc.deezer = FakeDeezer(preview=None)
    c = env["client"]
    r = await c.get("/api/listen", params={"ref": "dz:100"})
    assert r.status_code == 404 and "ни в одном источнике" in r.json()["detail"]
    assert (await c.get("/api/listen", params={"ref": "dz:404"})).status_code == 404
    assert (await c.get("/api/listen", params={"ref": "nonsense"})).status_code == 400


async def test_owned_track_plays_the_library_file(env, monkeypatch):
    from unittest.mock import AsyncMock
    from app.services import svc
    from app.db import session_scope
    from app.matching.normalize import fingerprint
    from app.models import Track

    rel = "Кино/1988 - Группа крови/1-01 - Группа крови.mp3"
    f = env["music"] / "library" / rel
    f.parent.mkdir(parents=True)
    f.write_bytes(AUDIO)
    async with session_scope() as s:
        s.add(Track(uid="u1", path=rel, artist="Кино", title="Группа крови", duration_s=245, isrc="RUA1D8800001",
                    fingerprint=fingerprint("Кино", "Группа крови")))
    c = env["client"]
    album_lookup = AsyncMock(return_value=svc.deezer.album)
    monkeypatch.setattr(svc.deezer, "get_album", album_lookup)
    r = await c.get("/api/listen", params={"ref": "dz:100"})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "library" and env["hm"].searched == 0
    album_lookup.assert_not_awaited()
    assert r.json()["nd_song_id"] is None  # Navidrome hasn't scanned it yet
    r = await c.get(r.json()["stream_url"], headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.content == AUDIO[:10]

    # scanned meanwhile: the cached choice is looked up again and carries the song id (♥ in the player)
    async with session_scope() as s:
        from sqlalchemy import update

        await s.execute(update(Track).values(nd_song_id="nd7"))
    r = await c.get("/api/listen", params={"ref": "dz:100"})
    assert r.json()["kind"] == "library" and r.json()["nd_song_id"] == "nd7"


async def test_library_song_of_a_mix_plays_its_file(env):
    """A song only the library has (`nd:<song>`, a mix's track) plays its file in the page's player."""
    from app.db import session_scope
    from app.matching.normalize import fingerprint
    from app.models import Track

    rel = "Кино/1988 - Группа крови/1-02 - Закрой за мной дверь.mp3"
    f = env["music"] / "library" / rel
    f.parent.mkdir(parents=True)
    f.write_bytes(AUDIO)
    async with session_scope() as s:
        s.add(Track(uid="u2", path=rel, artist="Кино", title="Закрой за мной дверь", duration_s=260,
                    nd_song_id="nd9", fingerprint=fingerprint("Кино", "Закрой за мной дверь")))
    c = env["client"]
    r = await c.get("/api/listen", params={"ref": "nd:nd9"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["kind"] == "library" and out["nd_song_id"] == "nd9" and out["title"] == "Закрой за мной дверь"
    assert env["hm"].searched == 0
    r = await c.get(r.json()["stream_url"], headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.content == AUDIO[:10]
    assert (await c.get("/api/listen", params={"ref": "nd:gone"})).status_code == 404


async def search_files(c, group: str | None = "sources") -> list[dict]:
    """The Files tab of the search: the files it shows can be listened to by their ref."""
    params = {"q": "Кино - Группа крови", **({"group": group} if group else {})}
    r = await c.get("/api/search/providers", params=params)
    assert r.status_code == 200, r.text
    return r.json()["candidates"]


async def settled(cond) -> None:
    """Waits for the background prefetch (it goes through the DB thread, so a few real ticks)."""
    import asyncio

    for _ in range(200):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the prefetch did not finish")


async def test_file_from_the_search_is_listened_to_by_its_ref(env):
    from app import listen

    c, hm = env["client"], env["hm"]
    hm.cands = [cand("1", "https://cdn.test/good.mp3", bitrate=128), cand("2", "https://cdn.test/dead.mp3")]
    assert {f["provider_id"] for f in await search_files(c)} == {"1", "2"}

    r = await c.get("/api/listen", params={"ref": "file:hitmoz:1"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["kind"], out["provider"], out["page_url"], out["bitrate_kbps"]) == (
        "full", "hitmoz", "https://hitmoz.test/song/1", 128)
    r = await c.get(out["stream_url"], headers={"Range": "bytes=100-199"})
    assert r.status_code == 206 and r.content == AUDIO[100:200]
    # that very file: no search or pick of its own, no track page check
    assert hm.searched == 1 and hm.enriched == []

    await listen.reset()  # the Hub restarted: the files of the search are remembered
    r = await c.get(out["stream_url"], headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.content == AUDIO[:10]
    assert hm.searched == 1

    # a file that doesn't open is an error, not some other file
    r = await c.get("/api/listen/stream", params={"ref": "file:hitmoz:2"})
    assert r.status_code == 502, r.text
    r = await c.get("/api/listen", params={"ref": "file:hitmoz:404"})
    assert r.status_code == 404 and "повторите поиск" in r.json()["detail"]


async def test_prefetch_opens_files_on_screen(env):
    from app import listen

    c, hm = env["client"], env["hm"]
    hm.cands = [cand("1", "https://cdn.test/good.mp3")]
    await search_files(c)
    r = await c.post("/api/listen/prefetch", json={"refs": ["file:hitmoz:1"]})
    assert r.status_code == 200 and r.json()["queued"] == 1
    await settled(lambda: listen._choices["file:hitmoz:1"].source is not None)
    assert (await c.post("/api/listen/prefetch", json={"refs": ["file:hitmoz:1"]})).json()["queued"] == 0


async def test_file_without_http_audio_plays_from_a_local_copy(env, tmp_path):
    from app import listen
    from app.services import svc

    c = env["client"]
    src = tmp_path / "upload.mp3"
    src.write_bytes(AUDIO)
    sc = FakeProvider("soundcloud", [cand("5", "", provider="soundcloud")], src=src, streams=False)
    svc.providers["soundcloud"] = sc
    assert [f["provider"] for f in await search_files(c, group=None)] == ["soundcloud"]

    # on screen: the Hub downloads the copy in the background, so a tap plays at once
    assert (await c.post("/api/listen/prefetch", json={"refs": ["file:soundcloud:5"]})).json()["queued"] == 1
    await settled(lambda: listen._choices["file:soundcloud:5"].local is not None)
    r = await c.get("/api/listen/stream", params={"ref": "file:soundcloud:5"}, headers={"Range": "bytes=10-19"})
    assert r.status_code == 206 and r.content == AUDIO[10:20]
    assert r.headers["content-type"].startswith("audio/mpeg")
    r = await c.get("/api/listen/stream", params={"ref": "file:soundcloud:5"})
    assert r.status_code == 200 and r.content == AUDIO
    assert sc.fetched == 1
    assert len(list((env["music"] / "staging" / listen.LOCAL_DIR).glob("*/*.mp3"))) == 1


def test_local_copies_are_pruned(tmp_path, monkeypatch):
    import os
    import time

    from app import listen

    now = time.time()

    def copy(name: str, age_s: float, size: int = 10) -> None:
        d = tmp_path / name
        d.mkdir()
        f = d / "a.m4a"
        f.write_bytes(b"x" * size)
        os.utime(f, (now - age_s, now - age_s))

    copy("old", listen.LOCAL_KEEP_S + 1)
    copy("busy", listen.LOCAL_KEEP_S + 1)  # being downloaded again
    copy("b", 3)
    copy("c", 2)
    copy("d", 1)
    monkeypatch.setattr(listen, "LOCAL_MAX_BYTES", 25)  # room for two copies
    listen.prune_local(tmp_path, {"busy"}, now)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["busy", "c", "d"]


async def test_file_listened_to_is_downloaded_by_its_ref(env):
    c, hm = env["client"], env["hm"]
    hm.cands = [cand("1", "https://cdn.test/good.mp3")]
    await search_files(c)
    # «Скачать» in the player: the file that played, as if picked in the search
    r = await c.post("/api/downloads", json={"track_refs": ["file:hitmoz:1"]})
    assert r.status_code == 200, r.text
    assert r.json()["queued"] == 1
    items = (await c.get("/api/downloads")).json()
    assert [(i["track_ref"], i["chosen"]["page_url"]) for i in items] == [("file:hitmoz:1", "https://hitmoz.test/song/1")]
    # ♥ on it then joins that download
    r = await c.post("/api/downloads", json={"track_refs": ["file:hitmoz:1"], "star": True})
    assert r.status_code == 200, r.text
    assert len((await c.get("/api/downloads")).json()) == 1
    r = await c.post("/api/downloads", json={"track_refs": ["file:hitmoz:404"]})
    assert r.status_code == 404


async def test_file_liked_in_the_search_is_downloaded_to_be_starred(env):
    c, hm = env["client"], env["hm"]
    hm.cands = [cand("1", "https://cdn.test/good.mp3")]
    await search_files(c)
    # ♥ on a row of the Files tab: that very file is downloaded quietly and starred after the import
    r = await c.post("/api/downloads", json={"track_refs": ["file:hitmoz:1"], "star": True})
    assert r.status_code == 200, r.text
    assert r.json()["queued"] == 1
    items = (await c.get("/api/downloads")).json()
    assert [(i["track_ref"], i["star"], i["chosen"]["page_url"]) for i in items] == [
        ("file:hitmoz:1", True, "https://hitmoz.test/song/1")]


async def test_listen_needs_login(env):
    async with app_client(env["app"], login=None) as anon:
        assert (await anon.get("/api/listen", params={"ref": "dz:100"})).status_code == 401
        assert (await anon.get("/api/listen/stream", params={"ref": "dz:100"})).status_code == 401
