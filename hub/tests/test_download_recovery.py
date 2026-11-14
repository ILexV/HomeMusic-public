"""Download import recovery across the filesystem/SQLite boundary."""

import json

import pytest
from sqlalchemy import select

from app import db
from app.config import get_settings
from app.jobs import downloads, queue
from app.library import importer
from app.models import DownloadBatch, DownloadItem, Job, Track, User


@pytest.fixture
async def pending_import(hub_env, monkeypatch):
    db.init_engine()
    async with db.session_scope() as s:
        user = User(nd_username="recovery", nd_password_enc="unused")
        s.add(user)
        await s.flush()
        batch = DownloadBatch(user_id=user.id, origin="search")
        s.add(batch)
        await s.flush()
        item = DownloadItem(user_id=user.id, batch_id=batch.id, status="importing", force=True,
                            query_json={"query": {"artist": "Artist", "title": "Song"}})
        s.add(item)
        await s.flush()
        item_id, user_id = item.id, user.id
    stage = importer.staging_dir_for("dl", item_id)
    src = stage / "audio.mp3"
    src.write_bytes(b"audio")
    journal = stage / ".import.json"
    spec = importer.ImportSpec(artist="Artist", title="Song", added_by=user_id, force=True)
    importer.prepare_import(journal, src, spec)
    monkeypatch.setattr(importer.tagger, "write_tags", lambda path, tags: path.write_bytes(b"tagged audio"))
    try:
        yield item_id, src, spec, journal
    finally:
        await db.dispose()


@pytest.mark.parametrize("window", ["before_move", "after_move", "after_track", "after_scan", "after_commit"])
async def test_import_replays_once_after_each_crash_window(pending_import, monkeypatch, window):
    item_id, src, spec, journal = pending_import
    job = Job(payload_json={"item_id": item_id})

    def crash():
        raise RuntimeError("simulated crash")

    with monkeypatch.context() as m:
        if window in ("before_move", "after_move"):
            move = importer._move

            def interrupted_move(source, target):
                if window == "after_move":
                    move(source, target)
                crash()

            m.setattr(importer, "_move", interrupted_move)
        elif window == "after_track":
            async def interrupted_import(*args, **kwargs):
                await importer.import_file(*args, **kwargs)
                crash()

            m.setattr(downloads, "import_file", interrupted_import)
        elif window == "after_scan":
            scan = downloads.enqueue_scan

            async def interrupted_scan(s):
                await scan(s)
                crash()

            m.setattr(downloads, "enqueue_scan", interrupted_scan)
        else:
            m.setattr(downloads.bus, "publish", lambda *args, **kwargs: crash())
        with pytest.raises(RuntimeError, match="simulated crash"):
            await downloads.handle_download_item(job)

    assert journal.is_file()
    async with db.session_scope() as s:
        item = await s.get(DownloadItem, item_id)
        tracks = list((await s.execute(select(Track))).scalars())
        scans = list((await s.execute(select(Job).where(Job.kind == "nd_scan"))).scalars())
        assert bool(item.track_id) == bool(tracks) == bool(scans) == (window == "after_commit")
    await downloads.handle_download_item(job)
    await downloads.handle_download_item(job)
    async with db.session_scope() as s:
        item = await s.get(DownloadItem, item_id)
        tracks = list((await s.execute(select(Track))).scalars())
        scans = list((await s.execute(select(Job).where(Job.kind == "nd_scan"))).scalars())
        assert len(tracks) == len(scans) == 1
        assert item.track_id == tracks[0].id and item.status == "importing"
        assert importer.library_abs(tracks[0].path).read_bytes() == b"tagged audio"
    assert len(list(get_settings().library_dir.rglob("*.mp3"))) == 1
    assert not journal.parent.exists()


async def test_journal_recovers_before_importing_status_was_saved(pending_import):
    item_id, _, _, _ = pending_import
    async with db.session_scope() as s:
        (await s.get(DownloadItem, item_id)).status = "tagging"
    await downloads.handle_download_item(Job(payload_json={"item_id": item_id}))
    async with db.session_scope() as s:
        assert (await s.get(DownloadItem, item_id)).track_id is not None


async def test_committed_track_is_reattached_without_another_version(pending_import):
    item_id, src, spec, journal = pending_import
    async with db.session_scope() as s:
        track = await importer.import_file(s, src, spec, journal=journal)
        track_id = track.id
    await downloads.handle_download_item(Job(payload_json={"item_id": item_id}))
    async with db.session_scope() as s:
        assert (await s.get(DownloadItem, item_id)).track_id == track_id
        assert len(list((await s.execute(select(Track))).scalars())) == 1


async def test_startup_recovers_orphan_import_without_duplicate_jobs(pending_import):
    item_id, _, _, _ = pending_import
    async with db.session_scope() as s:
        s.add(Job(kind="download_item", payload_json={"item_id": item_id}, status="done"))
    assert await queue.recover_stale() == 1
    assert await queue.recover_stale() == 0
    async with db.session_scope() as s:
        job = (await s.execute(select(Job).where(Job.status == "queued"))).scalar_one()
    await downloads.handle_download_item(job)
    async with db.session_scope() as s:
        assert (await s.get(DownloadItem, item_id)).track_id is not None


async def test_recovery_refuses_another_file_at_journal_destination(pending_import):
    item_id, src, spec, journal = pending_import
    with pytest.raises(RuntimeError):
        async with db.session_scope() as s:
            track = await importer.import_file(s, src, spec, journal=journal)
            dest = importer.library_abs(track.path)
            raise RuntimeError("rollback after move")
    dest.write_bytes(b"somebody else's recording")
    with pytest.raises(ValueError, match="destination changed"):
        await downloads.handle_download_item(Job(payload_json={"item_id": item_id}))
    assert dest.read_bytes() == b"somebody else's recording"
    assert json.loads(journal.read_text())["dest"] == dest.relative_to(get_settings().library_dir).as_posix()


async def test_failure_after_commit_does_not_mark_imported_download_failed(pending_import):
    item_id, src, spec, journal = pending_import
    async with db.session_scope() as s:
        track = await importer.import_file(s, src, spec, journal=journal)
        (await s.get(DownloadItem, item_id)).track_id = track.id
    await downloads._download_failed(Job(payload_json={"item_id": item_id}), RuntimeError("event delivery failed"))
    async with db.session_scope() as s:
        item = await s.get(DownloadItem, item_id)
        assert item.status == "importing" and item.track_id
        assert (await s.execute(select(Job).where(Job.kind == "nd_scan"))).scalar_one()


async def test_scan_can_finish_before_recovered_download_cleans_its_journal(pending_import):
    item_id, src, spec, journal = pending_import
    async with db.session_scope() as s:
        track = await importer.import_file(s, src, spec, journal=journal)
        item = await s.get(DownloadItem, item_id)
        item.track_id, item.status = track.id, "done"
    await downloads.handle_download_item(Job(payload_json={"item_id": item_id}))
    assert not journal.parent.exists()
    async with db.session_scope() as s:
        assert (await s.get(DownloadItem, item_id)).status == "done"
        assert not list((await s.execute(select(Job).where(Job.kind == "nd_scan"))).scalars())


async def test_candidate_reported_during_fetch_is_not_imported(pending_import, monkeypatch):
    from types import SimpleNamespace

    from app import recording_feedback
    from app.providers.base import TrackQuery
    from app.services import svc
    from tests.support import cand

    item_id, _, _, journal = pending_import
    candidate = cand("late-report", "https://cdn.test/audio.mp3")
    rejected = set()
    fetched = journal.parent / "reported.mp3"

    async def fetch(*args, **kwargs):
        fetched.write_bytes(b"audio")
        rejected.add((candidate.provider, candidate.provider_id))
        return fetched

    async def rejections(*args):
        return set(rejected)

    async def checked(*args):
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(recording_feedback, "rejected_candidates", rejections)
    monkeypatch.setattr(downloads.probe, "postcheck", checked)
    monkeypatch.setattr(svc, "providers", {candidate.provider: SimpleNamespace(fetch=fetch)})
    async with db.session_scope() as s:
        user_id = (await s.get(DownloadItem, item_id)).user_id
    ok, reason = await downloads.try_candidates(
        item_id, user_id, TrackQuery(artist="Artist", title="Song"), {"track_ref": "dz:1"},
        [candidate], journal.parent, False)
    assert not ok and "неверная запись" in reason
    assert not fetched.exists()
    async with db.session_scope() as s:
        assert not list((await s.execute(select(Track))).scalars())
