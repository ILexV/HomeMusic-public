"""Recommendation readiness verifies bytes without changing the ordinary listening contract."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest
import respx

from app import listen
from app.library.probe import ProbeError, ProbeInfo
from tests.support import FakeDeezer, FakeProvider, cand

AUDIO = bytes(range(256)) * 4


@pytest.fixture
async def readiness(tmp_path, monkeypatch):
    await listen.reset()
    state = SimpleNamespace(
        actual=180.0, choices={}, resolved=[], probed=[], resolve_error=None, probe_error=None,
        copy_gate=None, probe_gate=None, copy_started=asyncio.Event(), probes=asyncio.Queue(),
        clock=time.monotonic(), original_resolve=listen._resolve,
    )
    monkeypatch.setattr(listen, "time", SimpleNamespace(monotonic=lambda: state.clock, time=time.time))
    monkeypatch.setattr(listen, "get_settings", lambda: SimpleNamespace(
        staging_dir=tmp_path / "staging", cfg=SimpleNamespace(matching=SimpleNamespace(duration_tolerance_s=3))))

    def choice(ref="dz:1", *, expected=180):
        picked = cand(ref, "https://cdn.test/full.mp3").model_copy(update={"duration_s": expected})
        result = listen.Choice(ref, "full", "Artist", "Track", expected, candidate=picked, verified=True)
        state.choices[ref] = result
        return result

    state.choice = choice

    async def resolve(ref, **kwargs):
        state.resolved.append(ref)
        if state.resolve_error is not None:
            raise state.resolve_error
        result = state.choices.get(ref) or choice(ref)
        listen._choices[ref] = result
        return result

    async def measured(path, *, timeout):
        assert timeout == listen.FULL_PROBE_TIMEOUT_S
        assert path.read_bytes() == AUDIO  # verification is never run against the growing partial copy
        state.probed.append(path)
        state.probes.put_nowait(path)
        if state.probe_gate is not None:
            await state.probe_gate.wait()
        if state.probe_error is not None:
            raise state.probe_error
        return ProbeInfo(duration_s=state.actual, codec="mp3")

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield AUDIO[:100]
            state.copy_started.set()
            if state.copy_gate is not None:
                await state.copy_gate.wait()
            yield AUDIO[100:]

    def response(request):
        return httpx.Response(200, headers={"content-type": "audio/mpeg", "content-length": str(len(AUDIO))},
                              stream=Body())

    monkeypatch.setattr(listen, "_resolve", resolve)
    monkeypatch.setattr(listen, "probe", measured)
    async with httpx.AsyncClient() as client:
        monkeypatch.setattr(listen, "_media_client", lambda: client)
        monkeypatch.setattr(listen.svc, "providers", {"hitmoz": FakeProvider(client=client)})
        with respx.mock(assert_all_called=False) as mock:
            state.route = mock.get("https://cdn.test/full.mp3").mock(side_effect=response)
            try:
                yield state
            finally:
                await listen.reset()


@pytest.mark.parametrize(("actual", "expected_status"), [
    (180, "ready"), (177, "ready"), (183, "ready"),
    (30, "unavailable"), (120, "unavailable"), (184, "unavailable"),
    (0, "unavailable"), (float("nan"), "unavailable"),
])
async def test_full_metadata_does_not_replace_file_duration(readiness, actual, expected_status):
    readiness.actual = actual
    choice = readiness.choice()
    assert choice.kind == "full" and choice.verified
    result = await listen.prepare_full(1, ["dz:1"], timeout_s=1)
    assert result == {"dz:1": expected_status}
    assert listen._copies["dz:1"].done
    assert len(readiness.probed) == 1
    assert not choice.heard and choice.checking is None
    assert (await listen.choose("dz:1")) is choice  # rejection is recommendation-only


@pytest.mark.parametrize("duration", [12, 24, 30])
async def test_real_short_recordings_remain_eligible(readiness, duration):
    readiness.actual = duration
    readiness.choice(expected=duration)
    assert await listen.prepare_full(1, ["dz:1"], timeout_s=1) == {"dz:1": "ready"}


async def test_catalog_duration_wins_over_misleading_provider_metadata(readiness):
    readiness.actual = 30
    choice = readiness.choice(expected=30)
    choice.query = {"duration_s": 180}
    assert await listen.prepare_full(1, ["dz:1"], timeout_s=1) == {"dz:1": "unavailable"}


async def test_unknown_catalog_duration_cannot_prove_full_recording(readiness):
    readiness.actual = 30
    readiness.choice(expected=None)
    assert await listen.prepare_full(1, ["dz:1"], timeout_s=1) == {"dz:1": "unavailable"}


@pytest.mark.parametrize(("transient", "status"), [(False, "unavailable"), (True, "pending")])
async def test_preview_absence_and_source_failure_are_distinct(readiness, monkeypatch, transient, status):
    preview = listen.Choice("dz:1", "preview", "Artist", "Track", 30, transient=transient)
    listen._choices["dz:1"] = preview

    async def remembered(*args, **kwargs):
        return None

    async def resolve_deezer(*args, **kwargs):
        return preview

    monkeypatch.setattr(listen, "_resolve", readiness.original_resolve)
    monkeypatch.setattr(listen, "_remembered", remembered)
    monkeypatch.setattr(listen, "_resolve_deezer", resolve_deezer)
    monkeypatch.setattr(listen.svc, "deezer", FakeDeezer())
    assert await listen.prepare_full(1, ["dz:1"], timeout_s=1) == {"dz:1": status}
    assert not readiness.probed and readiness.route.call_count == 0
    assert await listen.choose("dz:1") is preview  # ordinary preview playback remains allowed


async def test_concurrent_callers_share_complete_copy_and_probe_after_timeout(readiness):
    readiness.copy_gate = asyncio.Event()
    first = asyncio.create_task(listen.prepare_full(1, ["dz:1", "dz:1"], timeout_s=0.01))
    await asyncio.wait_for(readiness.copy_started.wait(), 1)
    second = asyncio.create_task(listen.prepare_full(2, ["dz:1"], timeout_s=1))
    assert await first == {"dz:1": "pending"}
    copy = listen._copies["dz:1"]
    assert not copy.done and not copy.task.cancelled() and not readiness.probed
    readiness.copy_gate.set()
    assert await second == {"dz:1": "ready"}
    assert readiness.resolved == ["dz:1"]
    assert readiness.route.call_count == 1 and len(readiness.probed) == 1
    assert await listen.prepare_full(3, ["dz:1"]) == {"dz:1": "ready"}
    assert len(readiness.probed) == 1


async def test_cancelling_caller_does_not_cancel_shared_preparation(readiness):
    readiness.copy_gate = asyncio.Event()
    caller = asyncio.create_task(listen.prepare_full(1, ["dz:1"], timeout_s=10))
    await asyncio.wait_for(readiness.copy_started.wait(), 1)
    future = listen._full_jobs["dz:1"]
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert not future.cancelled() and not listen._copies["dz:1"].task.cancelled()
    readiness.copy_gate.set()
    assert await listen.prepare_full(2, ["dz:1"], timeout_s=1) == {"dz:1": "ready"}
    assert readiness.route.call_count == 1


@pytest.mark.parametrize(("error", "status", "ttl"), [
    (listen.ListenError(502, "temporary failure"), "pending", listen.FULL_RETRY_TTL_S),
    (listen.ListenError(404, "missing recording"), "unavailable", listen.FULL_NEGATIVE_TTL_S),
])
async def test_failed_resolution_retries_only_after_its_short_ttl(readiness, error, status, ttl):
    readiness.resolve_error = error
    assert await listen.prepare_full(1, ["dz:1"], timeout_s=1) == {"dz:1": status}
    readiness.resolve_error = None
    assert await listen.prepare_full(1, ["dz:1"], timeout_s=1) == {"dz:1": status}
    assert readiness.resolved == ["dz:1"]
    readiness.clock += ttl + 0.1
    assert await listen.prepare_full(1, ["dz:1"], timeout_s=1) == {"dz:1": "ready"}
    assert readiness.resolved == ["dz:1", "dz:1"]


async def test_probe_timeout_retries_without_redownloading(readiness):
    readiness.probe_error = TimeoutError()
    assert await listen.prepare_full(1, ["dz:1"], timeout_s=1) == {"dz:1": "pending"}
    readiness.clock += listen.FULL_RETRY_TTL_S + 0.1
    readiness.probe_error = None
    assert await listen.prepare_full(1, ["dz:1"], timeout_s=1) == {"dz:1": "ready"}
    assert len(readiness.probed) == 2 and readiness.route.call_count == 1


@pytest.mark.parametrize(("missing_binary", "status"), [(False, "unavailable"), (True, "pending")])
async def test_unplayable_file_is_distinct_from_missing_probe_service(readiness, missing_binary, status):
    error = ProbeError("cannot inspect audio")
    if missing_binary:
        error.__cause__ = FileNotFoundError("ffprobe")
    readiness.probe_error = error
    assert await listen.prepare_full(1, ["dz:1"], timeout_s=1) == {"dz:1": status}


@pytest.mark.parametrize(("expected", "actual", "status"), [(24, 24, "ready"), (None, 24, "ready"),
                                                           (180, 30, "unavailable")])
async def test_library_files_are_probed_without_network_and_preserved_on_reset(readiness, tmp_path,
                                                                            expected, actual, status):
    path = tmp_path / "owned.mp3"
    path.write_bytes(AUDIO)
    readiness.actual = actual
    readiness.choices["nd:owned"] = listen.Choice("nd:owned", "library", "Artist", "Track", expected,
                                                path=path, nd_song_id="owned")
    assert await listen.prepare_full(1, ["nd:owned"], timeout_s=1) == {"nd:owned": status}
    assert readiness.route.call_count == 0 and readiness.probed == [path]
    await listen.reset()
    assert path.read_bytes() == AUDIO
    assert not listen._full_results and not listen._full_jobs


async def test_ready_cache_revalidates_file_removal_and_replacement(readiness):
    assert await listen.prepare_full(1, ["dz:1"], timeout_s=1) == {"dz:1": "ready"}
    copy = listen._copies["dz:1"]
    copy.path.unlink()
    assert await listen.prepare_full(1, ["dz:1"], timeout_s=1) == {"dz:1": "ready"}
    assert readiness.route.call_count == 2 and len(readiness.probed) == 2
    # The same ref/candidate now points to different bytes: cached verification cannot survive the change.
    copy = listen._copies["dz:1"]
    copy.path.write_bytes(AUDIO + b"changed")
    assert listen._full_status("dz:1") is None


async def test_download_only_sources_reuse_the_shared_local_copy(readiness, monkeypatch, tmp_path):
    source = tmp_path / "provider.mp3"
    source.write_bytes(AUDIO)
    provider = FakeProvider(src=source, streams=False)
    monkeypatch.setattr(listen.svc, "providers", {"hitmoz": provider})
    choice = readiness.choice()
    original = await listen._predownload(choice)
    assert await listen.prepare_full(1, ["dz:1"], timeout_s=1) == {"dz:1": "ready"}
    assert readiness.probed == [original]
    assert readiness.route.call_count == 0 and not listen._copies


async def test_readiness_uses_bounded_existing_workers_and_queue(readiness, monkeypatch):
    monkeypatch.setattr(listen, "PREFETCH_WORKERS", 2)
    monkeypatch.setattr(listen, "PREFETCH_MAX", 3)
    monkeypatch.setattr(listen, "FULL_MAX_JOBS", 5)
    readiness.probe_gate = asyncio.Event()
    refs = [f"dz:{i}" for i in range(10)]
    assert set((await listen.prepare_full(1, refs, timeout_s=0)).values()) == {"pending"}
    assert len(listen._full_jobs) == 3
    await listen.prepare_full(2, [f"dz:{i}" for i in range(10, 20)], timeout_s=0)
    assert len(listen._full_jobs) == 5
    await asyncio.wait_for(readiness.probes.get(), 1)
    await asyncio.wait_for(readiness.probes.get(), 1)
    assert len(listen._workers) == 2 and len(listen._full_active) == 2
    assert len(readiness.resolved) == 2
    queued = set(listen._full_jobs)
    readiness.probe_gate.set()
    done, pending = await asyncio.wait(list(listen._full_jobs.values()), timeout=1)
    assert not pending and len(done) == 5
    assert set(readiness.resolved) == queued


async def test_reset_finishes_copy_and_worker_cancellation(readiness):
    readiness.copy_gate = asyncio.Event()
    assert await listen.prepare_full(1, ["dz:1"], timeout_s=0) == {"dz:1": "pending"}
    await asyncio.wait_for(readiness.copy_started.wait(), 1)
    workers = list(listen._workers)
    copy = listen._copies["dz:1"]
    future = listen._full_jobs["dz:1"]
    await listen.reset()
    assert all(task.done() for task in workers) and copy.task.done() and future.done()
    assert not listen._full_jobs and not listen._full_active and not listen._full_results
    assert not listen._full_wanted and not listen._copies and not listen._tasks


async def test_readiness_result_cache_is_bounded_and_expires(readiness, monkeypatch):
    monkeypatch.setattr(listen, "MEM_MAX", 2)
    for index in range(5):
        listen._full_results[str(index)] = listen._FullResult("unavailable", readiness.clock + 60)
    listen._full_results["expired"] = listen._FullResult("pending", readiness.clock - 1)
    listen._prune_full()
    assert len(listen._full_results) == 2 and "expired" not in listen._full_results


async def test_refreshing_reserve_keeps_shared_work_and_drops_only_obsolete_queue(readiness, monkeypatch):
    monkeypatch.setattr(listen, "PREFETCH_WORKERS", 1)
    readiness.probe_gate = asyncio.Event()
    refs = ["dz:1", "dz:2", "dz:3"]
    await listen.prepare_full(1, refs, timeout_s=0)
    futures = dict(listen._full_jobs)
    await listen.prepare_full(1, refs, timeout_s=0)
    assert listen._full_jobs == futures
    await asyncio.wait_for(readiness.probes.get(), 1)
    await listen.prepare_full(2, ["dz:2"], timeout_s=0)
    await listen.prepare_full(1, ["dz:1", "dz:4"], timeout_s=0)
    assert listen._full_jobs["dz:2"] is futures["dz:2"]
    assert futures["dz:3"].done() and "dz:3" not in listen._full_jobs
    assert set(listen._full_jobs) == {"dz:1", "dz:2", "dz:4"}
    readiness.probe_gate.set()
    _, pending = await asyncio.wait(list(listen._full_jobs.values()), timeout=1)
    assert not pending
    assert set(readiness.resolved) == {"dz:1", "dz:2", "dz:4"}


async def test_ready_bytes_are_reused_by_normal_full_playback(readiness, monkeypatch):
    async def after_heard(choice):
        # Track-page enrichment is unrelated to the full-file playback boundary under test.
        assert choice.heard

    monkeypatch.setattr(listen, "_after_heard", after_heard)
    assert await listen.prepare_full(1, ["dz:1"], timeout_s=1) == {"dz:1": "ready"}
    copy = listen._copies["dz:1"]
    choice = listen._choices["dz:1"]
    media = await listen.open_media("dz:1", None, full_only=True)
    assert media.choice is choice and listen._copies["dz:1"] is copy
    body = media.path.read_bytes() if media.path else b"".join([part async for part in media.body])
    assert body == AUDIO and readiness.route.call_count == 1 and len(readiness.probed) == 1


async def test_empty_reserve_drops_queued_work_without_stopping_active_copy(readiness, monkeypatch):
    monkeypatch.setattr(listen, "PREFETCH_WORKERS", 1)
    readiness.copy_gate = asyncio.Event()
    await listen.prepare_full(1, ["dz:1", "dz:2"], timeout_s=0)
    await asyncio.wait_for(readiness.copy_started.wait(), 1)
    obsolete = listen._full_jobs["dz:2"]
    assert await listen.prepare_full(1, [], timeout_s=0) == {}
    assert obsolete.done() and set(listen._full_jobs) == {"dz:1"}
    assert not listen._copies["dz:1"].task.cancelled()


async def test_import_invalidates_readiness_negative_cache(readiness, monkeypatch):
    readiness.resolve_error = listen.ListenError(404, "not imported yet")
    assert await listen.prepare_full(1, ["dz:1"], timeout_s=1) == {"dz:1": "unavailable"}
    deleted = []

    async def cache_delete(key):
        deleted.append(key)

    monkeypatch.setattr(listen, "cache_delete", cache_delete)
    await listen.forget("dz:1")
    assert deleted == ["listen:dz:1"] and "dz:1" not in listen._full_results


async def test_missing_upstream_file_has_short_negative_cache_and_can_recover(readiness):
    readiness.route.mock(return_value=httpx.Response(404))
    assert await listen.prepare_full(1, ["dz:1"], timeout_s=1) == {"dz:1": "unavailable"}
    # Public/manual playback errors retain their existing HTTP status.
    assert listen._copies["dz:1"].failed.status == 502
    failed_calls = readiness.route.call_count
    readiness.route.mock(return_value=httpx.Response(200, content=AUDIO, headers={"content-type": "audio/mpeg"}))
    assert await listen.prepare_full(1, ["dz:1"], timeout_s=1) == {"dz:1": "unavailable"}
    assert readiness.route.call_count == failed_calls
    readiness.clock += listen.FULL_NEGATIVE_TTL_S + 0.1
    assert await listen.prepare_full(1, ["dz:1"], timeout_s=1) == {"dz:1": "ready"}
    assert readiness.route.call_count == failed_calls + 1
