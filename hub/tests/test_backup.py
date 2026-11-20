from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import backup


def database(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE test (value TEXT)")
        db.execute("INSERT INTO test VALUES ('recover me')")


@pytest.fixture
def settings(tmp_path, monkeypatch):
    data, music, config, nd = (tmp_path / name for name in ("data", "music", "config", "nd"))
    for path in (data, music, config, nd):
        path.mkdir()
    (music / "library").mkdir()
    (config / "config.yaml").write_text("{}")
    database(data / "hub.sqlite3")
    database(nd / "navidrome.db")
    s = SimpleNamespace(data_dir=data, db_path=data / "hub.sqlite3", music_root=music,
                        library_dir=music / "library", config_file=config / "config.yaml")
    monkeypatch.setattr(backup, "get_settings", lambda: s)
    monkeypatch.setenv("BACKUP_NAVIDROME_DB", str(nd / "navidrome.db"))
    monkeypatch.setenv("BACKUP_CONFIG_FILES", "[]")
    monkeypatch.setenv("BACKUP_INCLUDE_MEDIA", "false")
    monkeypatch.setenv("BACKUP_EXTERNAL_ENABLED", "true")
    monkeypatch.setenv("HUB_SECRET_KEY", "test-hub-key")
    monkeypatch.setenv("ND_PASSWORDENCRYPTIONKEY", "test-nd-key")
    monkeypatch.delenv("HUB_SECRET_KEY_FILE", raising=False)
    monkeypatch.delenv("ND_PASSWORDENCRYPTIONKEY_FILE", raising=False)
    return s


def recovery_tree(tmp_path):
    root = tmp_path / "restored"
    root.mkdir()
    database(root / "hub.sqlite3")
    database(root / "navidrome.sqlite3")
    (root / "config.yaml").write_text("{}")
    (root / "recovery-keys.json").write_text(json.dumps({"HUB_SECRET_KEY": "hub", "ND_PASSWORDENCRYPTIONKEY": "nd"}))
    manifest = {"version": 1, "databases": ["hub.sqlite3", "navidrome.sqlite3"], "media_included": False,
                "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()}}
    (root / backup.MANIFEST).write_text(json.dumps(manifest))
    return root, manifest


def test_recovery_validates_checksums_and_sqlite(tmp_path):
    root, manifest = recovery_tree(tmp_path)
    assert backup.verify_recovery(root)["ok"]
    (root / "hub.sqlite3").write_bytes(b"broken database")
    with pytest.raises(backup.BackupError, match="checksum"):
        backup.verify_recovery(root)
    manifest["files"]["hub.sqlite3"] = hashlib.sha256(b"broken database").hexdigest()
    (root / backup.MANIFEST).write_text(json.dumps(manifest))
    with pytest.raises(sqlite3.DatabaseError):
        backup.verify_recovery(root)


@pytest.mark.parametrize("name", ["../outside", "/absolute", "nested/file", "..", "nested\\file"])
def test_manifest_cannot_escape(tmp_path, name):
    root, manifest = recovery_tree(tmp_path)
    manifest["files"][name] = "0" * 64
    (root / backup.MANIFEST).write_text(json.dumps(manifest))
    with pytest.raises(backup.BackupError, match="unsafe"):
        backup.verify_recovery(root)


def test_missing_keys_and_symlinks_are_rejected(tmp_path):
    root, manifest = recovery_tree(tmp_path)
    keys = root / "recovery-keys.json"
    keys.write_text("{}")
    manifest["files"][keys.name] = hashlib.sha256(keys.read_bytes()).hexdigest()
    (root / backup.MANIFEST).write_text(json.dumps(manifest))
    with pytest.raises(backup.BackupError, match="keys"):
        backup.verify_recovery(root)
    keys.unlink()
    keys.symlink_to(root / "config.yaml")
    with pytest.raises(backup.BackupError, match="checksum"):
        backup.verify_recovery(root)


def test_restore_refuses_live_existing_and_symlink_destinations(settings, tmp_path, monkeypatch):
    def forbidden(*args):
        raise AssertionError("must reject before contacting repository")

    monkeypatch.setattr(backup, "_restic", forbidden)
    existing = tmp_path / "existing"
    existing.mkdir()
    link = tmp_path / "link"
    link.symlink_to(settings.data_dir, target_is_directory=True)
    for target in (settings.data_dir / "restore", settings.music_root, settings.config_file.parent,
                   tmp_path, existing, link):
        with pytest.raises(backup.BackupError):
            backup.restore(target)


def test_backup_failure_status_redacts_repository_credentials(settings, monkeypatch):
    monkeypatch.setenv("RESTIC_REPOSITORY", "sftp:user:secret@example/backup")
    monkeypatch.setenv("RESTIC_PASSWORD", "secret-password")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1, stdout="", stderr="secret-password"))
    with pytest.raises(backup.BackupError):
        backup.external_backup()
    status = backup.read_status()
    assert status["external"]["status"] == "error"
    assert "secret" not in json.dumps(status)
    assert not list(settings.data_dir.glob("homemusic-recovery-*"))


def test_local_rotation_keeps_seven_daily_snapshots_without_touching_manual_copies(settings, monkeypatch):
    from app import housekeeping

    settings.backups_dir = settings.music_root / ".backups"
    monkeypatch.setattr(housekeeping, "get_settings", lambda: settings)
    folder = settings.backups_dir / "hub"
    folder.mkdir(parents=True)
    snapshots = [folder / f"hub-200001{day:02}.sqlite3" for day in range(1, 8)]
    for path in snapshots:
        shutil.copyfile(settings.db_path, path)
    manual = [folder / name for name in ("hub-000-before-upgrade.sqlite3", "hub-manual.sqlite3",
                                          "hub-20000231.sqlite3")]
    for path in manual:
        path.write_bytes(b"keep this copy")
    link = folder / "hub-19990101.sqlite3"
    link.symlink_to(manual[0])

    created = housekeeping.backup_hub_db()
    assert created is not None
    with sqlite3.connect(created) as db:
        assert db.execute("SELECT value FROM test").fetchone() == ("recover me",)
    assert all(path.read_bytes() == b"keep this copy" for path in manual)
    assert link.is_symlink()
    assert all(path.is_file() for path in snapshots[1:])
    assert not snapshots[0].exists()


def test_uninitialized_database_does_not_replace_last_good_snapshot(settings, monkeypatch):
    from app import housekeeping

    settings.backups_dir = settings.music_root / ".backups"
    monkeypatch.setattr(housekeeping, "get_settings", lambda: settings)
    previous = housekeeping.backup_hub_db()
    last_success = backup.read_status()["local"]["last_success"]
    settings.db_path.write_bytes(b"")
    with pytest.raises(backup.BackupError, match="no application tables"):
        housekeeping.backup_hub_db()
    status = backup.read_status()["local"]
    assert status["status"] == "error" and status["last_success"] == last_success
    with sqlite3.connect(previous) as db:
        assert db.execute("SELECT value FROM test").fetchone() == ("recover me",)


@pytest.mark.parametrize("initialized", [True, False])
def test_restore_checks_for_tables_not_rows_even_with_matching_checksum(tmp_path, initialized):
    root, manifest = recovery_tree(tmp_path)
    name = "navidrome.sqlite3"
    path = root / name
    with sqlite3.connect(path) as db:
        if initialized:
            db.execute("DELETE FROM test")
        else:
            # sqlite_sequence survives DROP TABLE; it is not application data.
            db.execute("CREATE TABLE seq (id INTEGER PRIMARY KEY AUTOINCREMENT)")
            db.execute("DROP TABLE seq")
            db.execute("DROP TABLE test")
    manifest["files"][name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (root / backup.MANIFEST).write_text(json.dumps(manifest))
    if initialized:
        assert backup.verify_recovery(root)["ok"]
    else:
        with pytest.raises(backup.BackupError, match="no application tables"):
            backup.verify_recovery(root)


def test_snapshot_includes_committed_wal(settings, tmp_path):
    source = settings.db_path
    with sqlite3.connect(source) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("INSERT INTO test VALUES ('in wal')")
        db.commit()
        target = tmp_path / "copy.sqlite3"
        backup.snapshot_sqlite(source, target)
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT value FROM test").fetchall() == [("recover me",), ("in wal",)]


@pytest.mark.skipif(shutil.which("restic") is None, reason="real restic binary required")
def test_restore_latest_uses_absolute_time_across_hosts_and_only_recovery_tags(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("RESTIC_REPOSITORY", str(tmp_path / "repo"))
    monkeypatch.setenv("RESTIC_PASSWORD", "test-password")
    monkeypatch.delenv("RESTIC_REPOSITORY_FILE", raising=False)
    monkeypatch.delenv("RESTIC_PASSWORD_FILE", raising=False)
    root, manifest = recovery_tree(tmp_path)
    backup._restic("init")
    for host, zone, clock, tag in [
        ("older", "Asia/Tokyo", "2025-01-01 18:00:00", backup.TAG),  # 09:00 UTC
        ("newer", "UTC", "2025-01-01 12:00:00", backup.TAG),
        ("unrelated", "UTC", "2025-01-01 23:00:00", "other"),
    ]:
        monkeypatch.setenv("TZ", zone)
        config = root / "config.yaml"
        config.write_text(f"host: {host}\n")
        manifest["files"][config.name] = hashlib.sha256(config.read_bytes()).hexdigest()
        (root / backup.MANIFEST).write_text(json.dumps(manifest))
        backup._restic("backup", "--host", host, "--time", clock, "--tag", tag, "--", str(root))

    restored = backup.restore(tmp_path / "recovery")
    assert (Path(restored["recovery_root"]) / "config.yaml").read_text() == "host: newer\n"


@pytest.mark.skipif(shutil.which("restic") is None, reason="real restic binary required")
@pytest.mark.parametrize("media", [False, True])
def test_real_restic_roundtrip(settings, tmp_path, monkeypatch, media):
    monkeypatch.setenv("RESTIC_REPOSITORY", str(tmp_path / "repository"))
    monkeypatch.setenv("RESTIC_PASSWORD", "temporary-test-password")
    monkeypatch.delenv("RESTIC_REPOSITORY_FILE", raising=False)
    monkeypatch.delenv("RESTIC_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("BACKUP_INCLUDE_MEDIA", str(media).lower())
    song = settings.library_dir / "song.mp3"
    song.write_bytes(b"media payload")
    backup._restic("init")
    backup.external_backup()
    destination = tmp_path / "recovery"
    result = backup.restore(destination)
    assert result["ok"] and result["media_included"] is media
    assert backup.read_status()["external"]["last_success"]
    if media:
        assert (destination / song.relative_to(song.anchor)).read_bytes() == b"media payload"
    assert (Path(result["recovery_root"]) / "recovery-keys.json").is_file()


def test_snapshot_refuses_existing_symlink(settings, tmp_path):
    protected = tmp_path / "protected"
    protected.write_bytes(b"do not overwrite")
    destination = tmp_path / "snapshot"
    destination.symlink_to(protected)
    with pytest.raises(FileExistsError):
        backup.snapshot_sqlite(settings.db_path, destination)
    assert protected.read_bytes() == b"do not overwrite"
