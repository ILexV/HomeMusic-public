"""End-to-end flow through the HTTP API with a fake provider, fake Deezer and a mocked Navidrome.

Needs ffmpeg/ffprobe (generates real audio and runs the post-download check) — runs in Docker, skipped on Windows.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from app import listen
from app.providers.base import Candidate
from tests.support import FakeDeezer, FakeProvider, app_client

FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")
pytestmark = pytest.mark.skipif(shutil.which(FFMPEG) is None, reason="ffmpeg not installed")


def make_mp3(path: Path, seconds: int = 4) -> Path:
    subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-b:a", "128k", str(path)],
        check=True,
    )
    return path


def jpeg_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 30, 30)).save(buf, "JPEG")
    return buf.getvalue()


def found(q):
    """What the fake hitmoz finds for any query: the song and its karaoke version."""
    from app.providers.base import Candidate

    return [
        Candidate(provider="hitmoz", provider_id="1", artist=q.artist, title=q.title, duration_s=4,
                  bitrate_kbps=320, page_url="https://hm.test/song/1", download_url="https://hm.test/get/1",
                  upload_date=date(2019, 5, 1)),
        Candidate(provider="hitmoz", provider_id="2", artist=q.artist, title=q.title + " (караоке)",
                  duration_s=4, page_url="https://hm.test/song/2"),
    ]


def reupload_found(q):
    """What the fake YouTube re-uploads find: a small channel's copy titled "(без цензуры)"."""
    from app.providers.base import Candidate

    return [Candidate(provider="yt_reupload", provider_id="r1", artist=q.artist, title=q.title, duration_s=4,
                      explicit=True, upload_date=date(2026, 6, 1), page_url="https://yt.test/watch?v=r1",
                      extra={"reupload": True, "uncensored": True})]


@pytest.fixture(scope="session")
def src_mp3(tmp_path_factory) -> Path:
    """The file every fake download gets (read-only: the provider copies it)."""
    return make_mp3(tmp_path_factory.mktemp("src") / "src.mp3")


@pytest.fixture
async def env(hub_env, src_mp3):
    from app import config
    from app.main import create_app
    from app.services import svc

    music = hub_env
    app = create_app()
    src = src_mp3
    state = {"songs": [], "playlists": {}, "scans": 0, "stars": [], "ids": {}}

    def nd_handler(request: httpx.Request) -> httpx.Response:
        ep = request.url.path.rsplit("/", 1)[-1]
        params = httpx.QueryParams(request.content.decode()) if request.method == "POST" else request.url.params
        ok = {"status": "ok", "version": "1.16.1"}
        if ep == "ping":
            return httpx.Response(200, json={"subsonic-response": ok})
        if ep == "getUser":
            return httpx.Response(200, json={"subsonic-response": {**ok, "user": {"adminRole": params["u"] == "admin"}}})
        if ep == "startScan":
            state["scans"] += 1
            # the scan "discovers" every file in the library folder; a song keeps its id across scans
            lib = music / "library"
            ids = state["ids"]
            for p in sorted(lib.rglob("*.mp3")):
                ids.setdefault(p.relative_to(lib).as_posix(), f"nd{len(ids)}")
            state["songs"] = [
                {"id": ids[p.relative_to(lib).as_posix()], "title": p.stem.split(" - ", 1)[-1],
                 "path": p.relative_to(lib).as_posix(), "album": p.parent.name.split(" - ", 1)[-1], "duration": 4}
                for p in sorted(lib.rglob("*.mp3"))
            ]
            return httpx.Response(200, json={"subsonic-response": {**ok, "scanStatus": {"scanning": False}}})
        if ep == "getScanStatus":
            return httpx.Response(200, json={"subsonic-response": {**ok, "scanStatus": {"scanning": False}}})
        if ep == "search3":
            return httpx.Response(200, json={"subsonic-response": {**ok, "searchResult3": {"song": state["songs"]}}})
        if ep == "createPlaylist":
            pid = params.get("playlistId") or f"pl{len(state['playlists']) + 1}"
            name = params.get("name") or state["playlists"].get(pid, {}).get("name")
            state["playlists"][pid] = {"name": name, "songs": params.get_list("songId")}
            return httpx.Response(200, json={"subsonic-response": {**ok, "playlist": {"id": pid, "name": params.get("name")}}})
        if ep == "getPlaylist":
            pl = state["playlists"][params["id"]]
            return httpx.Response(200, json={"subsonic-response": {**ok, "playlist": {
                "id": params["id"], "name": pl["name"], "entry": [{"id": i} for i in pl["songs"]]}}})
        if ep == "star":
            state["stars"] += [i for i in params.get_list("id") if i not in state["stars"]]
            return httpx.Response(200, json={"subsonic-response": ok})
        if ep == "unstar":
            state["stars"][:] = [i for i in state["stars"] if i not in params.get_list("id")]
            return httpx.Response(200, json={"subsonic-response": ok})
        if ep == "getStarred2":
            songs = [{"id": i} for i in dict.fromkeys(state["stars"])]
            return httpx.Response(200, json={"subsonic-response": {**ok, "starred2": {"song": songs}}})
        if ep == "updatePlaylist":
            state["playlists"][params["playlistId"]]["songs"] += params.get_list("songIdToAdd")
            return httpx.Response(200, json={"subsonic-response": ok})
        return httpx.Response(200, json={"subsonic-response": {**ok}})

    async with app.router.lifespan_context(app):
        # the fake provider enriches every file as explicit, as before
        fake = FakeProvider("hitmoz", found, src=src, explicit={"1": True, "2": True})
        svc.providers = {"hitmoz": fake}
        svc.deezer = FakeDeezer(duration_s=4, preview=None, cover_url="https://img.test/cover.jpg")
        config.get_settings().cfg.sources.ru = ["hitmoz"]
        config.get_settings().cfg.sources.default = ["hitmoz"]
        with respx.mock(assert_all_called=False) as mock:
            mock.route(host="navidrome.test").mock(side_effect=nd_handler)
            mock.get("https://img.test/cover.jpg").mock(
                return_value=httpx.Response(200, content=jpeg_bytes(), headers={"content-type": "image/jpeg"}))
            async with app_client(app) as client:
                yield {"client": client, "state": state, "music": music, "fake": fake, "src": src, "mock": mock}


async def drain_jobs(max_rounds: int = 20) -> None:
    from sqlalchemy import select

    from app.db import session_scope
    from app.jobs.queue import _claim, run_job
    from app.models import Job, utcnow

    for _ in range(max_rounds):
        async with session_scope() as s:
            # ignore delays in tests
            for j in (await s.execute(select(Job).where(Job.status == "queued"))).scalars():
                j.run_after = utcnow()
        job = await _claim("test")
        if job is None:
            return
        await run_job(job)


async def run_one_job() -> None:
    from app.jobs.queue import _claim, run_job

    job = await _claim("test")
    assert job is not None
    await run_job(job)


async def test_download_import_scan_playlist_dedupe(env):
    c = env["client"]
    r = await c.post("/api/downloads", json={
        "items": [{"artist": "Кино", "title": "Группа крови"}],
        "new_playlist_name": "Тест",
    })
    assert r.status_code == 200, r.text
    assert r.json()["playlist_id"] == "pl1"
    await drain_jobs()

    items = (await c.get("/api/downloads")).json()
    assert items[0]["status"] == "done", items
    assert items[0]["provider"] == "hitmoz"
    assert items[0]["nd_song_id"] == "nd0" and items[0]["playlist_id"] == "pl1"
    assert env["fake"].fetched == 1

    files = list((env["music"] / "library").rglob("*.mp3"))
    assert len(files) == 1
    rel = files[0].relative_to(env["music"] / "library").as_posix()
    assert rel == "Кино/1988 - Группа крови/1-01 - Группа крови.mp3"
    assert (files[0].parent / "cover.jpg").is_file()
    assert env["state"]["playlists"]["pl1"]["songs"] == ["nd0"]

    from app.library.tagger import read_tags

    tags = read_tags(files[0])
    assert tags["title"] == "Группа крови" and tags["album"] == "Группа крови" and tags["homemusic_track_id"]
    assert tags["isrc"] == "RUA1D8800001"

    r = await c.get("/api/library/track-source", params={"nd_song_id": "nd0"})
    assert r.status_code == 200 and r.json()["source"] == "hitmoz", r.text
    assert (await c.get("/api/library/track-source", params={"nd_song_id": "nope"})).status_code == 404

    # second request: already owned → no new download, linked at once
    r = await c.post("/api/downloads", json={"items": [{"artist": "КИНО", "title": "группа крови"}],
                                             "new_playlist_name": "Already owned"})
    assert r.json()["already_owned"] == 1
    await drain_jobs()
    assert env["fake"].fetched == 1
    assert len(list((env["music"] / "library").rglob("*.mp3"))) == 1
    assert env["state"]["playlists"]["pl2"]["songs"] == ["nd0"]

    # catalog album page shows ownership
    r = await c.get("/api/catalog/album/dz:10")
    assert r.json()["owned_status"] == "full"


async def test_review_does_not_block_playlist_and_clear_keeps_delayed_insertion_order(env):
    c, state = env["client"], env["state"]
    r = await c.post("/api/downloads", json={
        "items": [{"artist": "Неизвестный", "title": "Демо"},
                  {"artist": "Кино", "title": "Группа крови"}],
        "new_playlist_name": "Review",
    })
    assert r.status_code == 200, r.text
    await drain_jobs()
    items = (await c.get("/api/downloads")).json()
    review = next(i for i in items if i["status"] == "needs_review")
    assert state["playlists"]["pl1"]["songs"] == ["nd0"]
    r = await c.post("/api/downloads/clear")
    assert r.status_code == 200 and r.json()["removed"] == 0, r.text

    # Picking a file for the missing song fills the gap before the already added song.
    r = await c.post(f"/api/downloads/items/{review['id']}/retry", json={"candidate_index": 0})
    assert r.status_code == 200, r.text
    await drain_jobs()
    assert state["playlists"]["pl1"]["songs"] == ["nd1", "nd0"]
    r = await c.post("/api/downloads/clear")
    assert r.status_code == 200 and r.json()["removed"] == 2, r.text


def add_reuploads(env, *, has_track: bool = True) -> FakeProvider:
    """YouTube re-uploads as a source: the copy titled "(без цензуры)", or nothing."""
    from app.services import svc

    reups = FakeProvider("yt_reupload", reupload_found if has_track else (), src=env["src"])
    svc.providers["yt_reupload"] = reups
    return reups


def explicit_catalog_with_reuploads(env, *, hitmoz_uploaded: date, reuploaded: bool = True) -> FakeProvider:
    """The catalog track is explicit; hitmoz has a copy uploaded on `hitmoz_uploaded`; re-uploads are a source
    (`reuploaded` — they have the track)."""
    from app.services import svc

    dz = svc.deezer
    dz.track = dz.track.model_copy(update={"explicit": True})
    dz.album = dz.album.model_copy(update={"tracks": [dz.track]})
    env["fake"].cands = lambda q: [c.model_copy(update={"upload_date": hitmoz_uploaded}) for c in found(q)]
    return add_reuploads(env, has_track=reuploaded)


async def download_one(c, item: dict) -> dict:
    r = await c.post("/api/downloads", json={"items": [item]})
    assert r.status_code == 200, r.text
    await drain_jobs()
    item = (await c.get("/api/downloads")).json()[0]
    return (await c.get(f"/api/downloads/{item['id']}")).json()


async def test_explicit_russian_track_takes_an_uncensored_reupload_over_a_new_copy(env):
    reups = explicit_catalog_with_reuploads(env, hitmoz_uploaded=date(2026, 5, 1))
    item = await download_one(env["client"], {"artist": "Кино", "title": "Группа крови"})
    assert item["status"] == "done", item
    assert item["provider"] == "yt_reupload" and reups.fetched == 1 and env["fake"].fetched == 0
    assert [x["provider"] for x in item["candidates"]] == ["hitmoz", "yt_reupload"]
    assert item["warnings"] == []


async def test_explicit_russian_track_without_reuploads_takes_the_new_copy_with_a_warning(env):
    from app.jobs.downloads import MAYBE_CENSORED

    reups = explicit_catalog_with_reuploads(env, hitmoz_uploaded=date(2026, 5, 1), reuploaded=False)
    item = await download_one(env["client"], {"artist": "Кино", "title": "Группа крови"})
    assert item["status"] == "done", item
    assert item["provider"] == "hitmoz" and reups.searched == 1
    assert MAYBE_CENSORED in item["warnings"]


async def test_reuploads_in_the_russian_source_order_are_not_searched_twice(env):
    from app.config import SourcesCfg, get_settings
    from app.jobs.downloads import MAYBE_CENSORED

    reups = explicit_catalog_with_reuploads(env, hitmoz_uploaded=date(2026, 5, 1), reuploaded=False)
    get_settings().cfg.sources.ru = SourcesCfg().ru  # YouTube re-uploads are a regular source of Russian tracks
    item = await download_one(env["client"], {"artist": "Кино", "title": "Группа крови"})
    assert item["status"] == "done", item
    assert item["provider"] == "hitmoz" and MAYBE_CENSORED in item["warnings"]
    assert reups.searched == 1


async def test_explicit_russian_track_with_an_old_copy_skips_reuploads(env):
    reups = explicit_catalog_with_reuploads(env, hitmoz_uploaded=date(2024, 5, 1))
    item = await download_one(env["client"], {"artist": "Кино", "title": "Группа крови"})
    assert item["status"] == "done" and item["provider"] == "hitmoz"
    assert reups.searched == 0 and item["warnings"] == []


async def test_provider_search_groups(env):
    add_reuploads(env)

    async def providers(**group: str) -> list[str]:
        r = await env["client"].get("/api/search/providers", params={"q": "Кино - Группа крови", **group})
        assert r.status_code == 200, r.text
        return sorted({c["provider"] for c in r.json()["candidates"]})

    assert await providers() == ["hitmoz", "yt_reupload"]
    assert await providers(group="sources") == ["hitmoz"]
    assert await providers(group="reuploads") == ["yt_reupload"]


async def test_picked_reupload_gets_catalog_tags(env):
    add_reuploads(env)
    cand = {"provider": "yt_reupload", "provider_id": "r1", "artist": "кино", "title": "группа крови",
            "duration_s": 4, "page_url": "https://yt.test/watch?v=r1",
            "extra": {"reupload": True, "uncensored": True, "original_title": "группа крови – кино (без цензуры)"}}
    await listen.remember_files([Candidate(**cand)])
    item = await download_one(env["client"], {"artist": "кино", "title": "группа крови",
                                            "file_ref": "file:yt_reupload:r1"})
    assert item["status"] == "done", item
    files = [f.relative_to(env["music"] / "library").as_posix() for f in (env["music"] / "library").rglob("*.mp3")]
    assert files == ["Кино/1988 - Группа крови/1-01 - Группа крови.mp3"]


async def test_liked_uncensored_file_goes_next_to_the_owned_censored_copy_and_takes_its_like(env):
    c, stars = env["client"], env["state"]["stars"]
    reups = add_reuploads(env)
    r = await c.post("/api/downloads", json={"track_refs": ["dz:100"], "star": True})
    assert r.status_code == 200, r.text
    await drain_jobs()
    assert stars == ["nd0"] and env["fake"].fetched == 1  # the copy from hitmoz (censored, say) is liked

    # ♥ on the uncensored re-upload in the search: that very file is downloaded, not "already in the library"
    cand = {"provider": "yt_reupload", "provider_id": "r1", "artist": "кино", "title": "группа крови",
            "duration_s": 4, "page_url": "https://yt.test/watch?v=r1",
            "extra": {"reupload": True, "uncensored": True}}
    await listen.remember_files([Candidate(**cand)])
    r = await c.post("/api/downloads", json={"track_refs": ["file:yt_reupload:r1"], "star": True})
    assert r.status_code == 200, r.text
    assert r.json()["already_owned"] == 0
    await drain_jobs()
    item = (await c.get("/api/downloads")).json()[0]
    assert item["status"] == "done" and item["warnings"] == [], item
    assert reups.fetched == 1 and item["nd_song_id"] == "nd1"
    files = sorted(f.name for f in (env["music"] / "library").rglob("*.mp3"))
    assert files == ["1-01 - Группа крови [v2].mp3", "1-01 - Группа крови.mp3"]  # the old version stays

    # «Понравившиеся» play the picked file: the ♥ moved over to it; the catalog track is that file now
    assert stars == ["nd1"]
    r = await c.get("/api/catalog/album/dz:10")
    assert r.json()["tracks"][0]["nd_song_id"] == "nd1"

    # ♥ on the same file again: already in the library, nothing downloaded
    r = await c.post("/api/downloads", json={"track_refs": ["file:yt_reupload:r1"], "star": True})
    assert r.json()["already_owned"] == 1
    await drain_jobs()
    assert reups.fetched == 1 and stars == ["nd1"]


async def test_no_catalog_record_needs_review_then_pick(env):
    c = env["client"]
    await c.post("/api/downloads", json={"items": [{"artist": "Неизвестный", "title": "Демо"}]})
    await drain_jobs()
    item = (await c.get("/api/downloads")).json()[0]
    assert item["status"] == "needs_review"
    detail = (await c.get(f"/api/downloads/{item['id']}")).json()
    assert detail["candidates"] and "караоке" not in detail["candidates"][0]["title"]
    r = await c.post(f"/api/downloads/items/{item['id']}/retry", json={"candidate_index": 0})
    assert r.status_code == 200, r.text
    await drain_jobs()
    item = (await c.get(f"/api/downloads/{item['id']}")).json()
    assert item["status"] == "done"
    files = list((env["music"] / "library").rglob("*.mp3"))
    assert [f.name for f in files] == ["1-01 - Демо.mp3"]  # single: album = title


async def test_upload_commit_and_album_edit(env, tmp_path):
    c = env["client"]
    files = []
    for i in range(3):
        p = make_mp3(tmp_path / f"song{i}.mp3", seconds=2)
        files.append(("files", (f"song{i}.mp3", p.read_bytes(), "audio/mpeg")))
    r = await c.post("/api/uploads", files=files)
    assert r.status_code == 200, r.text
    ups = r.json()
    assert [u["tags"]["title"] for u in ups] == ["song0", "song1", "song2"]
    payload = {
        "album": {"mode": "new", "title": "Мой альбом", "album_artist": "Я", "year": 2026},
        "tracks": [{"upload_id": u["id"], "title": f"Трек {i + 1}", "artist": "Я", "track_no": i + 1}
                   for i, u in enumerate(ups)],
    }
    r = await c.post("/api/uploads/commit", data={"payload": json.dumps(payload)},
                     files={"cover": ("c.jpg", jpeg_bytes(), "image/jpeg")})
    assert r.status_code == 200, r.text
    album_id = r.json()["album_id"]
    assert r.json()["imported"] == 3
    album_dir = env["music"] / "library" / "Я" / "2026 - Мой альбом"
    assert sorted(p.name for p in album_dir.iterdir()) == [
        "1-01 - Трек 1.mp3", "1-02 - Трек 2.mp3", "1-03 - Трек 3.mp3", "cover.jpg"]

    r = await c.patch(f"/api/library/albums/{album_id}", json={"title": "Переименован"})
    assert r.status_code == 200, r.text
    new_dir = env["music"] / "library" / "Я" / "2026 - Переименован"
    assert len(list(new_dir.glob("*.mp3"))) == 3 and not album_dir.exists()
    from app.library.tagger import read_tags

    assert read_tags(next(new_dir.glob("*.mp3")))["album"] == "Переименован"

    # move one track into a new custom album, then delete another to trash
    r = await c.post("/api/library/albums", json={"title": "Подборка", "album_artist": "Разные"})
    target = r.json()["id"]
    tracks = (await c.get(f"/api/library/albums/{album_id}")).json()["tracks"]
    r = await c.post("/api/library/tracks/move", json={"track_ids": [tracks[0]["id"]], "album_id": target})
    assert r.json()["track_count"] == 1
    r = await c.delete(f"/api/library/tracks/{tracks[1]['id']}")
    assert r.status_code == 200
    assert len(list(new_dir.glob("*.mp3"))) == 1
    assert list((env["music"] / ".trash").rglob("*.mp3"))


async def test_assistant_link_confirm(env, monkeypatch):
    from app.ai import extract, links

    async def fake_link(url):
        return extract.Extraction(
            source_app="yandex_music", page_type="playlist",
            context=extract.ExtractionContext(playlist_name="Рок"),
            items=[extract.ExtractedItem(artist_raw="Кино", title_raw="Группа крови", conf=1.0),
                   extract.ExtractedItem(artist_raw="Нет такого", title_raw="Совсем", conf=1.0)],
        )

    monkeypatch.setattr(links, "extract_from_link", fake_link)
    c = env["client"]
    r = await c.post("/api/assistant/tasks", json={"url": "https://music.yandex.ru/users/x/playlists/1"})
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    await drain_jobs()
    t = (await c.get(f"/api/assistant/tasks/{tid}")).json()
    assert t["status"] == "awaiting_confirmation", t
    assert t["suggested_playlist_name"] == "Рок"
    # not in the catalog, Cyrillic -> hitmoz files are offered, never auto-picked
    assert [row["status"] for row in t["rows"]] == ["auto", "confirm"]
    assert t["rows"][1]["options"] and t["rows"][1]["options"][0]["title"] == "Совсем"
    # side effects need the token
    bad = await c.post(f"/api/assistant/tasks/{tid}/confirm", json={"confirm_token": "x", "rows": []})
    assert bad.status_code == 403
    r = await c.post(f"/api/assistant/tasks/{tid}/confirm", json={
        "confirm_token": t["confirm_token"],
        "rows": [{"index": 0, "selected": True}, {"index": 1, "selected": False}],
        "target": {"kind": "new", "name": "Рок"},
    })
    assert r.status_code == 200, r.text
    await drain_jobs()
    assert env["state"]["playlists"]["pl1"] == {"name": "Рок", "songs": ["nd0"]}


async def test_refs_expand_skips_dead_ref(env):
    c = env["client"]
    r = await c.post("/api/downloads", json={"track_refs": ["dz:999", "dz:100"]})
    assert r.status_code == 200, r.text
    assert r.json()["expanding"] is True and r.json()["queued"] == 0
    await drain_jobs()
    items = (await c.get("/api/downloads")).json()
    assert [(i["title"], i["status"]) for i in items] == [("Группа крови", "done")]


async def test_app_precache_of_a_track_is_no_listen(env, monkeypatch):
    """The app loads the next tracks of its queue ahead (`X-HomeMusic-Prefetch`): an economy import doesn't download
    them for it; a plain stream (and the app's `start` event, test_imports) is a listen."""
    import app.imports.core as imports

    heard: list[str] = []

    async def listened(user_id: int, ref: str) -> None:
        heard.append(ref)

    monkeypatch.setattr(imports, "listened", listened)
    c = env["client"]
    env["mock"].get("https://hm.test/get/1").mock(return_value=httpx.Response(
        200, content=env["src"].read_bytes(), headers={"content-type": "audio/mpeg"}))
    url = (await c.get("/api/listen", params={"ref": "dz:100"})).json()["stream_url"]
    assert (await c.get(url, headers={"X-HomeMusic-Prefetch": "1"})).status_code == 200
    assert heard == []
    assert (await c.get(url)).status_code == 200
    assert heard == ["dz:100"]


async def test_download_after_listening_takes_the_heard_file(env):
    c, fake = env["client"], env["fake"]
    env["mock"].get("https://hm.test/get/1").mock(return_value=httpx.Response(
        200, content=env["src"].read_bytes(), headers={"content-type": "audio/mpeg"}))
    r = await c.get("/api/listen", params={"ref": "dz:100"})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "full" and r.json()["page_url"] == "https://hm.test/song/1"
    assert fake.searched == 1
    # listened to: the stream was played (a pick alone, e.g. a prefetch of tracks on screen, doesn't count)
    assert (await c.get(r.json()["stream_url"], headers={"Range": "bytes=0-9"})).status_code in (200, 206)

    r = await c.post("/api/downloads", json={"track_refs": ["dz:100"]})
    assert r.status_code == 200, r.text
    await drain_jobs()
    items = (await c.get("/api/downloads")).json()
    assert [(i["status"], i["chosen"]["page_url"]) for i in items] == [("done", "https://hm.test/song/1")]
    assert fake.searched == 1 and fake.fetched == 1  # no new search: the listened file went first

    # now owned: listening plays the library file
    r = await c.get("/api/listen", params={"ref": "dz:100"})
    assert r.json()["kind"] == "library"


async def test_like_before_download_stars_the_song_once_imported(env):
    c, stars = env["client"], env["state"]["stars"]
    r = await c.post("/api/downloads", json={"track_refs": ["dz:100"], "star": True})
    assert r.status_code == 200, r.text
    await drain_jobs()
    items = (await c.get("/api/downloads")).json()
    assert [(i["status"], i["nd_song_id"]) for i in items] == [("done", "nd0")]
    assert stars == ["nd0"]
    r = await c.get("/api/starred")
    assert r.status_code == 200, r.text
    body = r.json()
    assert {k: body[k] for k in ("song_ids", "album_ids", "artist_ids", "refs")} == {
        "song_ids": ["nd0"], "album_ids": [], "artist_ids": [], "refs": []}
    assert [(x["target_type"], x["ref"], x["liked"]) for x in body["subjects"]] == [("track", "dz:100", True)]

    # ♥ on a track that is already owned: no new download, starred at once
    stars.clear()
    await c.post("/api/downloads", json={"track_refs": ["dz:100"], "star": True})
    await drain_jobs()
    assert stars == ["nd0"] and env["fake"].fetched == 1

    # a plain download stars nothing
    stars.clear()
    await c.post("/api/downloads", json={"track_refs": ["dz:100"]})
    await drain_jobs()
    assert stars == []


async def test_failed_like_download_shows_up_and_stars_once_retried(env):
    c, stars = env["client"], env["state"]["stars"]
    env["fake"].cands = []  # no source has the track
    r = await c.post("/api/downloads", json={"track_refs": ["dz:100"], "star": True})
    assert r.status_code == 200, r.text
    await drain_jobs()
    # the ♥ download failed: it is the user's download now (Downloads, notifications), not the Hub's quiet one
    [item] = (await c.get("/api/downloads")).json()
    assert (item["status"], item["star"], item["liked"]) == ("needs_review", False, False), item
    assert stars == []

    env["fake"].cands = found
    r = await c.post(f"/api/downloads/items/{item['id']}/retry", json={})
    assert r.status_code == 200, r.text
    await drain_jobs()
    item = (await c.get(f"/api/downloads/{item['id']}")).json()
    assert item["status"] == "done" and item["liked"], item
    assert stars == [item["nd_song_id"]]  # the ♥ still applies once a file is downloaded


@pytest.mark.parametrize("primary", ["available", "blocked", "unavailable"])
async def test_like_download_uses_full_file_fallback_only_when_regular_source_fails(env, monkeypatch, primary):
    from unittest.mock import AsyncMock

    from app.providers.base import ProviderError
    from app.services import svc

    fallback = FakeProvider("soundcloud", lambda q: [
        # A short preview must never be imported as the full song.
        found(q)[0].model_copy(update={"provider": "soundcloud", "provider_id": "preview", "duration_s": 30}),
        found(q)[0].model_copy(update={"provider": "soundcloud", "provider_id": "full"}),
    ], src=env["src"])
    svc.providers["soundcloud"] = fallback
    if primary == "blocked":
        monkeypatch.setattr(env["fake"], "fetch", AsyncMock(side_effect=ProviderError("YouTube sign-in required")))
    elif primary == "unavailable":
        env["fake"].down = True

    c = env["client"]
    response = await c.post("/api/downloads", json={"track_refs": ["dz:100"], "star": True})
    assert response.status_code == 200, response.text
    await drain_jobs()
    [item] = (await c.get("/api/downloads")).json()
    assert item["status"] == "done" and item["liked"], item
    assert env["state"]["stars"] == [item["nd_song_id"]]
    assert fallback.searched == fallback.fetched == (0 if primary == "available" else 1)
    if primary != "available":
        assert item["provider"] == "soundcloud"
        assert item["chosen"]["provider_id"] == "full"


async def test_retry_failed_like_uses_new_fallback_and_restores_the_favorite(env):
    from app.config import get_settings
    from app.services import svc

    sources = get_settings().cfg.sources
    sources.download_fallback = []
    env["fake"].cands = []
    c = env["client"]
    response = await c.post("/api/downloads", json={"track_refs": ["dz:100"], "star": True})
    assert response.status_code == 200, response.text
    await drain_jobs()
    [item] = (await c.get("/api/downloads")).json()
    assert item["status"] == "needs_review" and not item["liked"]

    svc.providers["soundcloud"] = FakeProvider("soundcloud", lambda q: [
        found(q)[0].model_copy(update={"provider": "soundcloud"}),
    ], src=env["src"])
    sources.download_fallback = ["soundcloud"]
    response = await c.post(f"/api/downloads/items/{item['id']}/retry", json={})
    assert response.status_code == 200, response.text
    await drain_jobs()
    item = (await c.get(f"/api/downloads/{item['id']}")).json()
    assert item["status"] == "done" and item["liked"], item
    assert env["state"]["stars"] == [item["nd_song_id"]]


async def test_pending_unlike_before_expansion_prevents_star_but_keeps_explicit_download(env):
    c, stars = env["client"], env["state"]["stars"]
    assert (await c.post("/api/downloads", json={"track_refs": ["dz:100"], "star": True})).status_code == 200
    # Explicit download joins after expansion, so it must survive the subsequent unlike.
    await run_one_job()
    assert (await c.post("/api/downloads", json={"track_refs": ["dz:100"]})).status_code == 200
    r = await c.post("/api/star", json={"refs": ["dz:100"], "unstar": True})
    assert r.status_code == 200, r.text
    await drain_jobs()
    [item] = (await c.get("/api/downloads")).json()
    assert item["status"] == "done" and not item["liked"] and stars == []


async def test_failed_like_unliked_before_retry_never_returns(env):
    c, stars = env["client"], env["state"]["stars"]
    env["fake"].cands = []
    await c.post("/api/downloads", json={"track_refs": ["dz:100"], "star": True})
    await drain_jobs()
    [item] = (await c.get("/api/downloads")).json()
    assert item["status"] == "needs_review"
    assert (await c.post("/api/star", json={"refs": ["dz:100"], "unstar": True})).status_code == 200
    env["fake"].cands = found
    await c.post(f"/api/downloads/items/{item['id']}/retry", json={})
    await drain_jobs()
    item = (await c.get(f"/api/downloads/{item['id']}")).json()
    assert item["status"] == "done" and not item["liked"] and stars == []


async def test_search_tracks_come_in_pages(env):
    c = env["client"]
    r = await c.get("/api/search", params={"q": "Кино"})
    assert r.status_code == 200, r.text
    first = r.json()
    refs = [t["ref"] for t in first["tracks"]]
    assert refs[0] == "dz:100" and len(refs) == 24 and len(set(refs)) == 24  # the repeat is dropped
    assert first["tracks_next"] == 25 and first["albums"]

    r = await c.get("/api/search/tracks", params={"q": "Кино", "offset": first["tracks_next"]})
    assert r.status_code == 200, r.text
    page = r.json()
    assert [t["ref"] for t in page["tracks"]] == [f"dz:{200 + i}" for i in range(23, 29)]
    assert page["next_offset"] is None


async def test_like_download_is_quiet_until_downloaded_explicitly(env):
    c, stars, state = env["client"], env["state"]["stars"], env["state"]
    r = await c.post("/api/downloads", json={"track_refs": ["dz:100"], "star": True, "playlist_id": "pl9"})
    assert r.status_code == 200, r.text
    assert r.json()["playlist_id"] is None  # ♥ downloads stay out of playlists
    await run_one_job()  # expand_batch: the item exists, not downloaded yet
    items = (await c.get("/api/downloads")).json()
    assert [(i["status"], i["star"], i["playlist_id"]) for i in items] == [("queued", True, None)]

    track = (await c.get("/api/search", params={"q": "Кино"})).json()["tracks"][0]
    assert track["ref"] == "dz:100" and not track["downloading"] and track["star_pending"]
    r = await c.get(f"/api/catalog/album/{'dz:10'}")
    assert r.json()["tracks"][0]["star_pending"]

    # «Скачать» on it: the same download, now a visible one (no second item, no second file)
    r = await c.post("/api/downloads", json={"track_refs": ["dz:100"]})
    assert r.json()["queued"] == 1 and not r.json()["expanding"]
    items = (await c.get("/api/downloads")).json()
    assert [(i["id"], i["star"]) for i in items] == [(items[0]["id"], False)]
    track = (await c.get("/api/search", params={"q": "Кино"})).json()["tracks"][0]
    assert track["downloading"] and track["star_pending"]

    await drain_jobs()
    items = (await c.get("/api/downloads")).json()
    assert [(i["status"], i["nd_song_id"]) for i in items] == [("done", "nd0")]
    assert stars == ["nd0"] and env["fake"].fetched == 1  # still starred after the import
    assert state["playlists"] == {}


async def test_like_joins_an_explicit_download(env):
    c, stars = env["client"], env["state"]["stars"]
    await c.post("/api/downloads", json={"track_refs": ["dz:100"]})
    await run_one_job()
    r = await c.post("/api/downloads", json={"track_refs": ["dz:100"], "star": True})
    assert r.json()["queued"] == 1 and not r.json()["expanding"]
    items = (await c.get("/api/downloads")).json()
    assert [i["star"] for i in items] == [False]  # asked for explicitly: stays a visible download
    track = (await c.get("/api/search", params={"q": "Кино"})).json()["tracks"][0]
    assert track["downloading"] and track["star_pending"]

    await drain_jobs()
    items = (await c.get("/api/downloads")).json()
    assert [(i["status"], i["nd_song_id"]) for i in items] == [("done", "nd0")]
    assert stars == ["nd0"] and env["fake"].fetched == 1


async def add_unlinked(path: str, title: str, album: str = "Альбом", artist: str = "Артист") -> int:
    import uuid

    from app.db import session_scope
    from app.matching.normalize import fingerprint
    from app.models import Track

    async with session_scope() as s:
        t = Track(uid=str(uuid.uuid4()), path=path, artist=artist, title=title, album=album, duration_s=4,
                  fingerprint=fingerprint(artist, title))
        s.add(t)
        await s.flush()
        return t.id


async def test_a_decomposed_title_is_linked_by_its_album(env):
    """Navidrome finds nothing by «и» + U+0306 (a «й» some sources send decomposed); the album finds the song."""
    from app.db import session_scope
    from app.jobs.downloads import link_unlinked
    from app.models import Track

    title = "Мой путь".replace("й", "й")
    path = "Артист/2020 - Альбом/1-01 - " + title + ".mp3"
    tid = await add_unlinked(path, title)
    song = {"id": "ndZ", "title": "Мой путь", "album": "Альбом", "path": "/music/" + path, "duration": 4}
    asked: list[str] = []

    class Nd:
        async def search3(self, q: str, song_count: int = 20) -> dict:
            asked.append(q)
            return {"song": [song] if q == "Альбом" else []}

    assert await link_unlinked(Nd()) == 1
    assert asked == ["Мой путь", "Альбом"]  # the title asked in NFC first
    async with session_scope() as s:
        assert (await s.get(Track, tid)).nd_song_id == "ndZ"


async def test_a_track_navidrome_never_finds_stops_the_scans_after_a_few_passes(env):
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.jobs.downloads import SCAN_PASSES, enqueue_scan
    from app.models import Job

    await add_unlinked("Нет/Такого/файла.mp3", "Призрак")
    async with session_scope() as s:
        await enqueue_scan(s)

    await drain_jobs(max_rounds=3 * SCAN_PASSES)

    async with session_scope() as s:
        scans = (await s.execute(select(Job.status, func.count()).where(Job.kind == "nd_scan").group_by(Job.status))).all()
    assert dict(scans) == {"done": 1 + SCAN_PASSES}
    assert env["state"]["scans"] == 1 + SCAN_PASSES
