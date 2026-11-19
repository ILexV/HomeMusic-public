"""The light variant of the audio for a metered network: GET /api/listen/stream?q=mobile (app/light.py)."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import light
from tests.support import cand
from tests.test_listen import AUDIO, env, search_files, wave_copy  # noqa: F401 (fixtures)

VARIANT = "x-homemusic-variant"
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is needed")


@pytest.fixture
def fake_encoder(monkeypatch):
    """No ffmpeg: a bitrate by the file name ("heavy" is 320 kbps, the rest 141) and an "encode" that keeps half
    of the bytes. `gate` holds the encodes, `running` counts those in progress."""
    st = SimpleNamespace(gate=None, running=0, peak=0, calls=[])

    async def fake_probe(path, timeout=60):
        return SimpleNamespace(bitrate_kbps=320 if "heavy" in str(path) else 141)

    async def fake_ffmpeg(src, out):
        st.calls.append(src)
        st.running += 1
        st.peak = max(st.peak, st.running)
        try:
            if st.gate is not None:
                await st.gate.wait()
            out.write_bytes(b"OggS" + src.read_bytes()[: src.stat().st_size // 2])
        finally:
            st.running -= 1

    monkeypatch.setattr(light, "probe", fake_probe)
    monkeypatch.setattr(light, "_ffmpeg", fake_ffmpeg)
    return st


async def song(env, name: str, nd_id: str, body: bytes = AUDIO) -> Path:
    """A library song `nd:<nd_id>` whose file is called `name`."""
    from app.db import session_scope
    from app.matching.normalize import fingerprint
    from app.models import Track

    rel = f"Artist/Album/{name}"
    f = env["music"] / "library" / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(body)
    async with session_scope() as s:
        s.add(Track(uid=nd_id, path=rel, artist="Artist", title=name, duration_s=200, nd_song_id=nd_id,
                    fingerprint=fingerprint("Artist", name)))
    return f


def test_variant_is_chosen_by_the_bitrate():
    assert light.is_heavy(320) and light.is_heavy(900) and light.is_heavy(193)
    assert not light.is_heavy(192) and not light.is_heavy(141) and not light.is_heavy(128)
    assert not light.is_heavy(None)  # unreadable: nothing to re-encode


def test_ffmpeg_makes_opus_160_without_tags():
    args = light.ffmpeg_args(Path("in.mp3"), Path("out.opus"))
    joined = " ".join(args)
    assert "-c:a libopus -b:a 160k -vbr on" in joined and "-map_metadata -1" in joined and "-vn" in args
    assert "+bitexact" in joined and args[-1] == "out.opus"


async def test_heavy_song_is_served_as_an_opus_copy_with_size_and_range(env, fake_encoder):
    c = env["client"]
    await song(env, "heavy.mp3", "nd1")
    r = await c.get("/api/listen/stream", params={"ref": "nd:nd1", "q": "mobile"}, headers={"Range": "bytes=0-"})
    assert r.status_code == 206 and r.headers[VARIANT] == light.LIGHT
    assert r.headers["content-type"].startswith("audio/ogg")
    copy = b"OggS" + AUDIO[: len(AUDIO) // 2]
    assert r.content == copy and r.headers["content-range"] == f"bytes 0-{len(copy) - 1}/{len(copy)}"

    r = await c.get("/api/listen/stream", params={"ref": "nd:nd1", "q": "mobile"})
    assert r.status_code == 200 and r.headers["content-length"] == str(len(copy)) and r.content == copy
    r = await c.get("/api/listen/stream", params={"ref": "nd:nd1", "q": "mobile"}, headers={"Range": "bytes=10-19"})
    assert r.status_code == 206 and r.content == copy[10:20]
    assert r.headers["content-range"] == f"bytes 10-19/{len(copy)}" and r.headers["accept-ranges"] == "bytes"
    r = await c.get("/api/listen/stream", params={"ref": "nd:nd1", "q": "mobile"},
                    headers={"Range": f"bytes={len(copy) + 5}-"})
    assert r.status_code == 416
    assert len(fake_encoder.calls) == 1  # one encode for all of them


async def test_light_song_is_served_as_it_is(env, fake_encoder):
    c = env["client"]
    await song(env, "plain.opus", "nd2")
    r = await c.get("/api/listen/stream", params={"ref": "nd:nd2", "q": "mobile"})
    assert r.status_code == 200 and r.content == AUDIO and r.headers[VARIANT] == light.ORIGINAL
    assert fake_encoder.calls == []  # never re-encoded: lossy audio again only loses quality


async def test_without_the_quality_the_stream_is_as_before(env, fake_encoder):
    c = env["client"]
    await song(env, "heavy.mp3", "nd3")
    r = await c.get("/api/listen/stream", params={"ref": "nd:nd3"})
    assert r.status_code == 200 and r.content == AUDIO and VARIANT not in r.headers
    r = await c.get("/api/listen/stream", params={"ref": "nd:nd3", "q": "something-new"})
    assert r.content == AUDIO and VARIANT not in r.headers  # an unknown quality is ignored
    assert fake_encoder.calls == []


async def test_copy_not_ready_in_time_gives_the_original_and_is_finished_in_the_background(env, fake_encoder,
                                                                                         monkeypatch):
    c = env["client"]
    monkeypatch.setattr(light, "WAIT_S", 0.05)
    fake_encoder.gate = asyncio.Event()
    await song(env, "heavy.mp3", "nd4")

    r = await c.get("/api/listen/stream", params={"ref": "nd:nd4", "q": "mobile"}, headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.content == AUDIO[:10] and r.headers[VARIANT] == light.PENDING
    assert r.headers["content-range"] == f"bytes 0-9/{len(AUDIO)}"

    fake_encoder.gate.set()
    await settled(lambda: not light._jobs)
    # the same source keeps getting the original for a while: one client cache entry never holds both variants
    r = await c.get("/api/listen/stream", params={"ref": "nd:nd4", "q": "mobile"}, headers={"Range": "bytes=0-9"})
    assert r.content == AUDIO[:10] and r.headers[VARIANT] == light.PENDING
    light._served_original.clear()
    r = await c.get("/api/listen/stream", params={"ref": "nd:nd4", "q": "mobile"})
    assert r.headers[VARIANT] == light.LIGHT and r.content == b"OggS" + AUDIO[: len(AUDIO) // 2]
    assert len(fake_encoder.calls) == 1


async def test_failed_encode_gives_the_original_and_is_not_retried(env, fake_encoder, monkeypatch):
    c = env["client"]

    async def broken(src, out):
        fake_encoder.calls.append(src)
        raise RuntimeError("Unknown encoder 'libopus'")

    monkeypatch.setattr(light, "_ffmpeg", broken)
    await song(env, "heavy.mp3", "nd5")
    for _ in range(2):
        r = await c.get("/api/listen/stream", params={"ref": "nd:nd5", "q": "mobile"})
        assert r.status_code == 200 and r.content == AUDIO and r.headers[VARIANT] == light.PENDING
        light._served_original.clear()
    assert len(fake_encoder.calls) == 1
    assert not any(root_files(env))  # no half-written copy is left


async def test_ffmpeg_runs_a_few_at_a_time(env, fake_encoder):
    fake_encoder.gate = asyncio.Event()
    files = [await song(env, f"heavy{i}.mp3", f"nd1{i}") for i in range(5)]
    tasks = [light.ensure(f, f"lib|{f}") for f in files]
    assert all(tasks)
    # background work leaves one slot to a user's request
    await settled(lambda: fake_encoder.running == light.ENCODERS - 1)
    await asyncio.sleep(0.1)
    assert fake_encoder.running == light.ENCODERS - 1
    fake_encoder.gate.set()
    await asyncio.gather(*tasks)
    assert fake_encoder.peak == light.ENCODERS - 1 and len(fake_encoder.calls) == 5


async def test_background_encode_yields_to_the_one_a_user_waits_for(env, fake_encoder):
    fake_encoder.gate = asyncio.Event()
    # background encodes take the slots they may (one is kept free for a user), the rest wait
    files = [await song(env, f"heavy{i}.mp3", f"nd2{i}") for i in range(4)]
    tasks = [light.ensure(f, f"lib|{f}") for f in files]
    await settled(lambda: fake_encoder.running == light.ENCODERS - 1)
    await asyncio.sleep(0.1)
    assert fake_encoder.running == light.ENCODERS - 1
    # a user's request takes the free slot at once and lifts nothing else
    user = asyncio.create_task(light.get(files[3], f"lib|{files[3]}", wait_s=5))
    await settled(lambda: fake_encoder.running == light.ENCODERS)
    fake_encoder.gate.set()
    path, variant = await user
    assert variant == light.LIGHT and path is not None
    await asyncio.gather(*tasks)


async def test_background_job_joined_by_a_request_goes_first(env, fake_encoder):
    fake_encoder.gate = asyncio.Event()
    f = await song(env, "heavy.mp3", "nd6")
    ident = f"lib|{f}"
    task = light.ensure(f, ident)
    await settled(lambda: light.key_of(ident) in light._jobs)
    assert light._jobs[light.key_of(ident)][1].background
    waiting = asyncio.create_task(light.get(f, ident, wait_s=5))
    await asyncio.sleep(0.05)
    assert not light._jobs[light.key_of(ident)][1].background
    fake_encoder.gate.set()
    assert (await waiting)[1] == light.LIGHT
    await task


def test_least_recently_used_copies_go_first(tmp_path):
    now = time.time()
    for i, name in enumerate(["old", "mid", "new"]):
        f = tmp_path / f"{name}.opus"
        f.write_bytes(b"x" * 100)
        os.utime(f, (now - 300 + i * 100, now - 300 + i * 100))
    leftover = tmp_path / "dead.opus.part"
    leftover.write_bytes(b"x")
    os.utime(leftover, (now - 7200, now - 7200))
    writing = tmp_path / "busy.opus.part"
    writing.write_bytes(b"x")
    os.utime(writing, (now - 7200, now - 7200))

    light.prune(tmp_path, 250, {"busy.opus.part"})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["busy.opus.part", "mid.opus", "new.opus"]
    light.prune(tmp_path, 100, {"busy.opus.part"})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["busy.opus.part", "new.opus"]


async def test_copies_are_kept_within_the_limit_least_recently_used_out(env, fake_encoder, monkeypatch):
    c = env["client"]
    monkeypatch.setattr(light, "MAX_BYTES", 2 * (len(AUDIO) // 2 + 4) + 50)  # room for two copies
    for i in range(3):
        await song(env, f"heavy{i}.mp3", f"nd3{i}")
        r = await c.get("/api/listen/stream", params={"ref": f"nd:nd3{i}", "q": "mobile"})
        assert r.headers[VARIANT] == light.LIGHT
        await asyncio.sleep(0.05)  # distinct times of use
    assert len([f for f in light.root().iterdir() if f.suffix == light.SUFFIX]) == 2


async def test_catalog_track_is_lightened_from_its_server_copy(env, fake_encoder, monkeypatch):
    c, hm = env["client"], env["hm"]
    hm.cands = [cand("1", "https://cdn.test/good.mp3")]
    await search_files(c)
    # the source says heavy: the probe of the server copy is by its path, which has no "heavy" in it
    async def probe(path, timeout=60):
        return SimpleNamespace(bitrate_kbps=320)

    monkeypatch.setattr(light, "probe", probe)
    r = await c.get("/api/listen/stream", params={"ref": "file:hitmoz:1", "q": "mobile"})
    assert r.status_code == 200 and r.headers[VARIANT] == light.LIGHT
    assert r.content == b"OggS" + AUDIO[: len(AUDIO) // 2]
    # the next ask is answered from the copy alone
    r = await c.get("/api/listen/stream", params={"ref": "file:hitmoz:1", "q": "mobile"}, headers={"Range": "bytes=0-3"})
    assert r.status_code == 206 and r.content == b"OggS" and r.headers[VARIANT] == light.LIGHT
    # the plain stream is the original, as ever
    r = await c.get("/api/listen/stream", params={"ref": "file:hitmoz:1"})
    assert r.content == AUDIO and VARIANT not in r.headers


@pytest.mark.parametrize("evict", ["source", "rendition"])
async def test_verified_wave_mobile_pins_the_served_opus(wave_copy, fake_encoder, monkeypatch, evict):
    from app import listen
    from app.db import session_scope
    from app.recording_models import RecordingBinding

    async def heavy(path, timeout=60):
        return SimpleNamespace(bitrate_kbps=320)

    monkeypatch.setattr(light, "probe", heavy)
    client, user, ref, resolved = wave_copy
    r = await client.get(resolved["stream_url"] + "&q=mobile")
    assert r.status_code == 200 and r.headers[VARIANT] == light.LIGHT
    async with session_scope() as s:
        binding = await s.get(RecordingBinding, resolved["recording_binding"])
        assert binding.full_verified is True
    f, expected_duration = listen.open_heard_wave_recording(user, ref)
    with f:
        rendition = Path(f.name)
        assert rendition.parent == light.root() and expected_duration == 245.25
        assert f.read() == r.content == b"OggS" + AUDIO[:len(AUDIO) // 2]
        assert listen.open_heard_wave_recording(user + 1, ref) is None
        if evict == "source":
            listen._full_results[ref].path.unlink()
        else:
            rendition.unlink()
            rendition.write_bytes(r.content)  # even identically named Opus of the same size is not the served inode
        assert listen.open_heard_wave_recording(user, ref) is None


async def test_catalog_track_that_is_light_already_is_served_as_it_is(env, fake_encoder, monkeypatch):
    c, hm = env["client"], env["hm"]
    hm.cands = [cand("1", "https://cdn.test/good.mp3")]
    await search_files(c)

    async def probe(path, timeout=60):
        return SimpleNamespace(bitrate_kbps=128)  # a YouTube m4a

    monkeypatch.setattr(light, "probe", probe)
    r = await c.get("/api/listen/stream", params={"ref": "file:hitmoz:1", "q": "mobile"}, headers={"Range": "bytes=5-9"})
    assert r.status_code == 206 and r.content == AUDIO[5:10] and r.headers[VARIANT] == light.ORIGINAL
    assert fake_encoder.calls == []


async def test_catalog_track_whose_copy_is_slow_plays_the_original_and_the_copy_follows(env, fake_encoder,
                                                                                         monkeypatch):
    from app import listen

    c, hm, mock = env["client"], env["hm"], env["mock"]
    from tests.test_listen import partway

    gate = asyncio.Event()
    mock.route(host="slow.test").mock(side_effect=lambda req: partway(req, 1000, gate=gate))
    hm.cands = [cand("1", "https://slow.test/a.mp3")]
    await search_files(c)

    async def probe(path, timeout=60):
        return SimpleNamespace(bitrate_kbps=320)

    monkeypatch.setattr(light, "probe", probe)
    monkeypatch.setattr(light, "WAIT_S", 0.05)
    m = await listen.open_media("file:hitmoz:1", None, quality="mobile")
    assert m.variant == light.PENDING and m.body is not None
    assert await anext(m.body) == AUDIO[:1000]
    gate.set()
    assert AUDIO[:1000] + b"".join([chunk async for chunk in m.body]) == AUDIO
    await settled(lambda: any(f.suffix == light.SUFFIX for f in root_files(env)))


async def test_warm_makes_the_copies_ahead(env, fake_encoder):
    from app import listen

    await song(env, "heavy.mp3", "nd7")
    await song(env, "plain.opus", "nd8")
    listen.warm_light(["nd:nd7", "nd:nd8", "nd:gone"])
    await settled(lambda: len(fake_encoder.calls) == 1 and not light._jobs)
    assert len(list(root_files(env))) == 1
    t0 = time.monotonic()
    r = await env["client"].get("/api/listen/stream", params={"ref": "nd:nd7", "q": "mobile"})
    assert r.headers[VARIANT] == light.LIGHT and time.monotonic() - t0 < 1 and len(fake_encoder.calls) == 1


def root_files(env):
    return [f for f in light.root().iterdir()] if light.root().is_dir() else []


async def settled(cond) -> None:
    for _ in range(200):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


# --- the real ffmpeg (the Hub's image has it) ------------------------------------------------------------------


def make_audio(path: Path, codec: str, kbps: int, seconds: int = 12) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                    "-c:a", codec, "-b:a", f"{kbps}k", str(path)], check=True)


@needs_ffmpeg
async def test_real_ffmpeg_halves_a_320_mp3_and_the_copy_is_reproducible(env):
    c = env["client"]
    f = env["music"] / "library" / "Artist" / "Album" / "heavy.mp3"
    f.parent.mkdir(parents=True)
    make_audio(f, "libmp3lame", 320)
    plain = env["music"] / "library" / "Artist" / "Album" / "plain.opus"
    make_audio(plain, "libopus", 128)
    from app.db import session_scope
    from app.matching.normalize import fingerprint
    from app.models import Track

    async with session_scope() as s:
        for uid, p in (("a", f), ("b", plain)):
            rel = p.relative_to(env["music"] / "library").as_posix()
            s.add(Track(uid=uid, path=rel, artist="Artist", title=p.name, duration_s=12, nd_song_id=uid,
                        fingerprint=fingerprint("Artist", p.name)))

    r = await c.get("/api/listen/stream", params={"ref": "nd:a", "q": "mobile"}, headers={"Range": "bytes=0-"})
    if r.headers[VARIANT] == light.PENDING:  # a slow machine: the copy is finished in the background
        await settled(lambda: not light._jobs)
        light._served_original.clear()
        r = await c.get("/api/listen/stream", params={"ref": "nd:a", "q": "mobile"}, headers={"Range": "bytes=0-"})
    assert r.headers[VARIANT] == light.LIGHT and r.headers["content-type"].startswith("audio/ogg")
    assert r.content[:4] == b"OggS" and len(r.content) < f.stat().st_size * 0.6
    total = int(r.headers["content-range"].rpartition("/")[2])
    assert total == len(r.content)

    # the same copy made again after it was pruned has the same bytes (a client's half-cached file stays valid)
    first = r.content
    for copy in light.root().iterdir():
        copy.unlink()
    r = await c.get("/api/listen/stream", params={"ref": "nd:a", "q": "mobile"})
    await settled(lambda: not light._jobs)
    light._served_original.clear()
    r = await c.get("/api/listen/stream", params={"ref": "nd:a", "q": "mobile"})
    assert r.content == first

    r = await c.get("/api/listen/stream", params={"ref": "nd:b", "q": "mobile"})
    assert r.headers[VARIANT] == light.ORIGINAL and r.content == plain.read_bytes()
