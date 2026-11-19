import asyncio
from dataclasses import replace
from threading import Event

import pytest
import respx

from app import clip

from .support import drain, running, tone

LIB = "Artist/Album"
PREVIEW = "https://cdn.example/preview/450.mp3"


@pytest.fixture
def previews(tones):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(PREVIEW).respond(200, content=tones["a450"])
        mock.get("https://cdn.example/gone.mp3").respond(404)
        mock.get("https://cdn.example/busy.mp3").respond(503)
        mock.get("https://cdn.example/huge.mp3").respond(200, content=b"\0" * (2 * 2**20))
        yield mock


async def embed(c, *items, force=False):
    r = await c.post("/embed", json={"items": list(items), "force": force})
    assert r.status_code == 200, r.text
    return r.json()


async def status(c, key):
    r = await c.get(f"/embed/{key}")
    assert r.status_code == 200, r.text
    return r.json()


async def upload(c, key, data):
    return await c.post("/embed/clip", params={"key": key}, content=data, headers={"Content-Type": "audio/wav"})


async def test_uploaded_clip_survives_restart_and_is_not_replaced_by_preview(settings, previews, library):
    key = "provider:album/track"
    wav = tone(440, sr=24_000)
    preview = {"key": key, "url": "https://cdn.example/gone.mp3"}
    async with running(settings) as c:
        await embed(c, preview)
        assert (await upload(c, key, wav)).json() == {"queued": 1, "known": 0, "rejected": []}
        assert (await embed(c, preview, force=True))["known"] == 1
        assert (await upload(c, key, wav)).json()["known"] == 1
        assert (await status(c, key))["has_clip"]
        assert len(list(c.app.state.store.clip_dir.iterdir())) == 1
        c.app.state.store.claim(1)
    async with running(settings) as c:
        assert (await status(c, key))["has_clip"]
        await embed(c, {"key": "same_pitch", "path": f"{LIB}/a440_quiet.wav"},
                    {"key": "other_pitch", "path": f"{LIB}/c2000.wav"})
        await drain(c)
        st = await status(c, key)
        assert st["has_vector"] and not st["has_clip"] and st["seconds"] == pytest.approx(settings.clip_s)
        assert (await c.post("/vectors", json={"keys": [key]})).json()["vectors"][key]
        ranking = await c.post("/similarity", json={
            "candidates": ["same_pitch", "other_pitch"], "profile": [{"key": key, "weight": -1}],
        })
        assert ranking.status_code == 200, ranking.text
        scores = {r["key"]: r["score"] for r in ranking.json()["results"]}
        assert scores["same_pitch"] < scores["other_pitch"]
        assert (await upload(c, key, tone(900, sr=24_000))).json()["known"] == 1
        assert (await embed(c, preview))["known"] == 1
        assert not list(c.app.state.store.clip_dir.iterdir())
        assert previews.calls.call_count == 0


async def test_clip_waits_for_inflight_preview_and_takes_over_on_failure(client, previews):
    await embed(client, {"key": "bad", "url": "https://cdn.example/gone.mp3"}, {"key": "ok", "url": PREVIEW})
    st = client.app.state
    pending = st.store.claim(2)
    wav = tone(440, sr=24_000)
    for key in ("bad", "ok"):
        assert not (await status(client, key))["has_clip"]
        assert (await upload(client, key, wav)).json()["queued"] == 1
        item = await status(client, key)
        assert item["status"] == "running" and item["has_clip"]
    await st.worker.embed(await st.worker.prepare(pending))
    assert (await status(client, "bad"))["status"] == "queued" and (await status(client, "bad"))["has_clip"]
    assert (await status(client, "ok"))["has_vector"] and not (await status(client, "ok"))["has_clip"]
    assert (await upload(client, "ok", wav)).json()["known"] == 1
    await drain(client)
    assert (await status(client, "bad"))["has_vector"] and not list(st.store.clip_dir.iterdir())
    assert previews.calls.call_count == 2


async def test_clip_capacity_is_reclaimed_after_terminal_failure(client, monkeypatch):
    monkeypatch.setattr(clip, "MAX_PENDING", 1)
    wav = tone(440, sr=24_000)
    assert (await upload(client, "bad", wav)).json()["queued"] == 1
    assert (await upload(client, "other", wav)).status_code == 503

    def broken(_wavs):
        raise ValueError("model failed")

    monkeypatch.setattr(client.model, "embed_audio", broken)
    await client.app.state.worker.step()
    assert (await status(client, "bad"))["has_clip"]  # retry, not terminal
    await drain(client)
    item = await status(client, "bad")
    assert item["status"] == "error" and not item["has_clip"]
    assert not list(client.app.state.store.clip_dir.iterdir())
    assert (await upload(client, "other", wav)).json()["queued"] == 1


async def test_clip_rejects_wrong_format_and_oversized_stream(client, tones):
    async def oversized():
        yield bytes(clip.MAX_BYTES)
        yield b"x"

    assert (await client.post("/embed/clip?key=x", content=b"not a wav")).status_code == 415
    for malformed in (tones["a440"], tone(440, sr=24_000)[:-1], tone(440, seconds=30.5, sr=24_000)):
        assert (await upload(client, "x", malformed)).status_code == 422
    assert (await upload(client, "x", oversized())).status_code == 413
    assert (await client.get("/embed/x")).status_code == 404
    assert not list(client.app.state.store.clip_dir.iterdir())


async def test_library_and_preview_are_embedded_and_compared(client, previews):
    out = await embed(
        client,
        {"key": "nd:a440", "path": f"{LIB}/a440.wav"},
        {"key": "nd:a440q", "path": f"{client.app.state.worker.s.music_root}/{LIB}/a440_quiet.wav"},
        {"key": "nd:c2000", "path": f"{LIB}/c2000.wav"},
        {"key": "dz:450", "url": PREVIEW},
    )
    assert out == {"queued": 4, "known": 0, "rejected": []}
    assert (await status(client, "dz:450"))["status"] == "queued"

    await drain(client)

    for key in ("nd:a440", "nd:a440q", "nd:c2000", "dz:450"):
        st = await status(client, key)
        assert st["status"] == "done" and st["has_vector"], st
    assert (await status(client, "nd:a440"))["seconds"] == pytest.approx(1.0, abs=0.01)  # clip_s from the middle

    r = await client.post("/similarity", json={
        "candidates": ["dz:450", "nd:c2000", "nd:a440", "dz:nope"],
        "profile": [{"key": "nd:a440", "weight": 3}, {"key": "nd:a440q", "weight": 2}],
        "top": 1,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"] == "fake" and body["missing"] == ["dz:nope"]
    res = {x["key"]: x for x in body["results"]}
    assert res["dz:450"]["pos"] > res["nd:c2000"]["pos"]
    assert res["dz:450"]["neg"] is None and res["dz:450"]["score"] == res["dz:450"]["pos"]
    assert res["dz:450"]["nearest"][0]["key"] in ("nd:a440", "nd:a440q")
    # a profile key is not its own nearest neighbour
    assert res["nd:a440"]["nearest"] == [{"key": "nd:a440q", "sim": pytest.approx(res["nd:a440"]["nearest"][0]["sim"])}]


async def test_negative_weights_lower_the_score(client, previews):
    await embed(
        client,
        {"key": "a", "path": f"{LIB}/a440.wav"},
        {"key": "c", "path": f"{LIB}/c2000.wav"},
        {"key": "dz:450", "url": PREVIEW},
    )
    await drain(client)
    body = (await client.post("/similarity", json={
        "candidates": ["dz:450"], "profile": [{"key": "c", "weight": 2}, {"key": "a", "weight": -5}],
    })).json()
    r = body["results"][0]
    assert r["neg"] > r["pos"]
    assert r["score"] == pytest.approx(r["pos"] - 0.5 * r["neg"])
    assert r["nearest_neg"]["key"] == "a"


async def test_bad_items_fail_alone_and_transient_ones_are_retried(client, previews):
    await embed(
        client,
        {"key": "ok", "path": f"{LIB}/a440.wav"},
        {"key": "missing", "path": f"{LIB}/nope.flac"},
        {"key": "broken", "path": f"{LIB}/broken.mp3"},
        {"key": "gone", "url": "https://cdn.example/gone.mp3"},
        {"key": "busy", "url": "https://cdn.example/busy.mp3"},
        {"key": "huge", "url": "https://cdn.example/huge.mp3"},
    )
    await client.app.state.worker.step()

    assert (await status(client, "ok"))["status"] == "done"
    for key, words in [("missing", "no such file"), ("broken", "decode failed"), ("gone", "HTTP 404"), ("huge", "larger than")]:
        st = await status(client, key)
        assert st["status"] == "error" and words in st["error"], st
    busy = await status(client, "busy")
    assert busy["status"] == "queued" and busy["attempts"] == 1 and "503" in busy["error"]

    await drain(client)  # retry_s = 0: retried until max_attempts
    busy = await status(client, "busy")
    assert busy["status"] == "error" and busy["attempts"] == 3
    assert previews.routes[2].call_count == 3


async def test_bad_requests_are_rejected(client):
    out = await embed(
        client,
        {"key": "escape", "path": "../../etc/passwd"},
        {"key": "abs", "path": "/etc/passwd"},
        {"key": "ftp", "url": "ftp://x/y.mp3"},
    )
    assert out["queued"] == 0 and [r["key"] for r in out["rejected"]] == ["escape", "abs", "ftp"]
    r = await client.post("/embed", json={"items": [{"key": "both", "url": PREVIEW, "path": "a.mp3"}]})
    assert r.status_code == 422
    assert (await client.get("/embed/escape")).status_code == 404


async def test_known_items_are_not_requeued_unless_forced_or_changed(client, previews):
    item = {"key": "nd:1", "path": f"{LIB}/a440.wav"}
    await embed(client, item)
    await drain(client)
    assert (await embed(client, item)) == {"queued": 0, "known": 1, "rejected": []}
    assert (await embed(client, item, force=True))["queued"] == 1
    await drain(client)
    # the file moved: a new source is embedded again
    assert (await embed(client, {"key": "nd:1", "path": f"{LIB}/a440_quiet.wav"}))["queued"] == 1
    # a failed item is retried when posted again
    await embed(client, {"key": "x", "url": "https://cdn.example/gone.mp3"})
    await drain(client)
    assert (await embed(client, {"key": "x", "url": "https://cdn.example/gone.mp3"}))["queued"] == 1


async def test_queue_survives_a_restart(settings, library):
    async with running(settings) as c:
        await embed(c, {"key": "a", "path": f"{LIB}/a440.wav"}, {"key": "b", "path": f"{LIB}/c2000.wav"})
        c.app.state.store.claim(1)  # "a" was in progress when the process stopped
        assert (await status(c, "a"))["status"] == "running"
    async with running(settings) as c:
        assert (await status(c, "a"))["status"] == "queued"
        await drain(c)
        assert [(await status(c, k))["status"] for k in "ab"] == ["done", "done"]
    async with running(settings) as c:  # vectors are kept
        assert (await c.post("/vectors", json={"keys": ["a"]})).json()["vectors"]["a"]


async def test_model_load_failure_keeps_items_and_shows_in_healthz(settings, library):
    def broken(_settings):
        raise RuntimeError("CUDA out of memory")

    async with running(settings, factory=broken) as c:
        await embed(c, {"key": "a", "path": f"{LIB}/a440.wav"})
        await c.app.state.worker.step()
        st = await status(c, "a")
        assert st["status"] == "queued" and "CUDA out of memory" in st["error"]
        h = (await c.get("/healthz")).json()
        assert h["state"] == "error" and "CUDA out of memory" in h["error"] and h["queue"]["queued"] == 1


async def test_text_preferences_do_not_wait_for_entire_cpu_audio_batch(client, monkeypatch):
    await embed(client, {"key": "nd:known", "path": f"{LIB}/a440.wav"})
    await drain(client)
    await embed(client, {"key": "nd:first", "path": f"{LIB}/a440.wav"},
                {"key": "nd:second", "path": f"{LIB}/c2000.wav"})
    entered, first_release, second_release = Event(), Event(), Event()
    original = client.model.embed_audio
    calls = 0

    def slow_audio(wavs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert first_release.wait(5)
        else:
            assert second_release.wait(5)
        return original(wavs)

    monkeypatch.setattr(client.model, "embed_audio", slow_audio)
    batch = asyncio.create_task(client.app.state.worker.step())
    text = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        text = asyncio.create_task(client.post("/text", json={"text": "calm piano", "keys": ["nd:known"]}))
        await asyncio.sleep(0)  # Let the request acquire its place in the model lock's FIFO.
        first_release.set()
        response = await asyncio.wait_for(text, 2)
        assert response.status_code == 200
        assert [r["key"] for r in response.json()["results"]] == ["nd:known"]
        assert not batch.done()  # The second song is still blocked, but the interactive result is available.
    finally:
        first_release.set()
        second_release.set()
        await batch
        if text is not None and not text.done():
            text.cancel()
            await asyncio.gather(text, return_exceptions=True)
    assert (await status(client, "nd:first"))["status"] == "done"
    assert (await status(client, "nd:second"))["status"] == "done"


async def test_text_finds_nearest_keys_with_filters(client):
    await embed(client, *({"key": f"{p}:{n}", "path": f"{LIB}/{n}.wav"} for p in ("nd", "dz") for n in ("a440", "c2000")))
    await drain(client)
    r = await client.post("/text", json={"text": "calm piano", "limit": 10, "prefix": "nd:"})
    assert r.status_code == 200, r.text
    res = r.json()["results"]
    assert {x["key"] for x in res} == {"nd:a440", "nd:c2000"}
    assert res[0]["sim"] >= res[1]["sim"]
    r = await client.post("/text", json={"text": "calm piano", "keys": ["dz:c2000", "nope"]})
    assert [x["key"] for x in r.json()["results"]] == ["dz:c2000"]
    # a new vector shows up in the next search
    await embed(client, {"key": "nd:new", "path": f"{LIB}/a450.wav"})
    await drain(client)
    r = await client.post("/text", json={"text": "calm piano", "prefix": "nd:"})
    assert "nd:new" in {x["key"] for x in r.json()["results"]}


async def test_vectors_and_healthz(client):
    await embed(client, {"key": "a", "path": f"{LIB}/a440.wav"})
    await drain(client)
    v = (await client.post("/vectors", json={"keys": ["a", "b"]})).json()
    assert v["dim"] == 32 and len(v["vectors"]["a"]) == 32 and v["missing"] == ["b"]
    h = (await client.get("/healthz")).json()
    assert h["status"] == "ok" and h["model"] == "fake" and h["state"] == "ready" and h["device"] == "cpu"
    assert h["queue"] == {"queued": 0, "running": 0, "done": 1, "error": 0} and h["songs_per_s"] > 0


async def test_token_is_required_when_set(settings, library):
    async with running(replace(settings, token="s3cret")) as c:
        assert (await c.get("/healthz")).status_code == 200
        assert (await c.post("/embed", json={"items": []})).status_code == 401
        assert (await upload(c, "a", tone(440, sr=24_000))).status_code == 401
        r = await c.post("/embed", json={"items": []}, headers={"Authorization": "Bearer s3cret"})
        assert r.status_code == 200, r.text


async def test_background_worker_wakes_on_post_and_unloads_when_idle(settings, library):
    s = replace(settings, disable_worker=False, idle_unload_s=0.01)
    async with running(s) as c:
        await embed(c, {"key": "a", "path": f"{LIB}/a440.wav"})
        async with asyncio.timeout(5):
            # the real worker loop runs on its own: watch it through the API
            while (await status(c, "a"))["status"] != "done" or c.app.state.engine.model:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        assert (await c.get("/healthz")).json()["state"] == "idle"


async def test_redirects_are_followed(client):
    with respx.mock() as mock:
        mock.get("https://cdn.example/r").respond(302, headers={"Location": PREVIEW})
        target = mock.get(PREVIEW).respond(200, content=b"x")
        await embed(client, {"key": "r", "url": "https://cdn.example/r"})
        await drain(client)
        assert target.called
    assert "decode failed" in (await status(client, "r"))["error"]
