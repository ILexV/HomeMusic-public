"""Encrypted restic snapshots and isolated recovery drills (python -m app.backup)."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from app.config import get_settings

TAG = "homemusic-recovery-v1"
MANIFEST = "recovery-manifest.json"


class BackupError(RuntimeError):
    """A deliberately credential-free operational error."""


def enabled() -> bool:
    return os.environ.get("BACKUP_EXTERNAL_ENABLED", "false").lower() in {"1", "true", "yes"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def read_status() -> dict:
    try:
        data = json.loads((get_settings().data_dir / "backup-status.json").read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record_status(kind: str, *, success: bool | None, error: str | None = None) -> None:
    data = read_status()
    entry = data.setdefault(kind, {})
    entry.update(last_attempt=_now(), status="running" if success is None else "ok" if success else "error", error=error)
    if success:
        entry["last_success"] = _now()
    path = get_settings().data_dir / "backup-status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".backup-status-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


def sqlite_integrity(path: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise BackupError("missing or unsafe database")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise BackupError("SQLite integrity check failed")


def snapshot_sqlite(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise BackupError("required database unavailable")
    # Exclusive creation prevents following an existing symlink and keeps snapshots private from birth.
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    deadline = time.monotonic() + 120

    def progress(_status, _remaining, _total):
        if time.monotonic() > deadline:
            raise BackupError("SQLite snapshot timed out")

    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as src:
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst, pages=256, progress=progress)
    destination.chmod(0o600)
    sqlite_integrity(destination)


def _digest(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def _restic(*args: str) -> str:
    if not (os.environ.get("RESTIC_REPOSITORY") or os.environ.get("RESTIC_REPOSITORY_FILE")):
        raise BackupError("restic repository is not configured")
    if not (os.environ.get("RESTIC_PASSWORD") or os.environ.get("RESTIC_PASSWORD_FILE")):
        raise BackupError("restic password is not configured")
    try:
        result = subprocess.run(
            ["restic", "--no-cache", *args], capture_output=True, text=True,
            timeout=int(os.environ.get("BACKUP_TIMEOUT_SECONDS", "21600")), check=False,
        )
    except subprocess.TimeoutExpired:
        raise BackupError("restic command timed out") from None
    except OSError:
        raise BackupError("restic executable unavailable") from None
    if result.returncode:
        # stderr, commands and exception repr may contain repository credentials.
        raise BackupError(f"restic {args[0]} failed (exit {result.returncode})")
    return result.stdout


def _secret(name: str) -> str:
    file = os.environ.get(name + "_FILE")
    value = Path(file).read_text().strip() if file else os.environ.get(name, "").strip()
    if not value:
        raise BackupError("required recovery key is missing")
    return value


def external_backup() -> None:
    """Each SQLite snapshot is transactionally consistent; not a distributed transaction across services."""
    if not enabled():
        return
    record_status("external", success=None)
    try:
        s = get_settings()
        # Private transient files, never a plaintext archive or secrets in a process argument.
        with tempfile.TemporaryDirectory(prefix="homemusic-recovery-", dir=s.data_dir) as tmp:
            root = Path(tmp)
            snapshot_sqlite(s.db_path, root / "hub.sqlite3")
            snapshot_sqlite(Path(os.environ.get("BACKUP_NAVIDROME_DB", "/navidrome-data/navidrome.db")), root / "navidrome.sqlite3")
            if not s.config_file.is_file():
                raise BackupError("required Hub configuration is missing")
            shutil.copyfile(s.config_file, root / "config.yaml")
            # The key encrypting this repository must be retained separately, not only inside the repository.
            keys = {"HUB_SECRET_KEY": _secret("HUB_SECRET_KEY"),
                    "ND_PASSWORDENCRYPTIONKEY": _secret("ND_PASSWORDENCRYPTIONKEY")}
            (root / "recovery-keys.json").write_text(json.dumps(keys))
            config_files = json.loads(os.environ.get("BACKUP_CONFIG_FILES", "[]"))
            if not isinstance(config_files, list) or not all(isinstance(p, str) for p in config_files):
                raise BackupError("BACKUP_CONFIG_FILES must be a JSON list of paths")
            origins = {}
            for i, name in enumerate(config_files):
                source = Path(name)
                if not source.is_file():
                    raise BackupError("required extra configuration is missing")
                target = f"extra-config-{i}"
                shutil.copyfile(source, root / target)
                origins[target] = source.name
            media = os.environ.get("BACKUP_INCLUDE_MEDIA", "false").lower() in {"1", "true", "yes"}
            if media and not s.library_dir.is_dir():
                raise BackupError("media directory unavailable")
            files = {}
            for path in root.iterdir():
                path.chmod(0o600)
                files[path.name] = _digest(path)
            manifest = {"version": 1, "created_at": _now(), "files": files, "extra_config": origins,
                        "databases": ["hub.sqlite3", "navidrome.sqlite3"],
                        "media_included": media,
                        "media_path": str(s.library_dir.resolve()) if media else None,
                        "consistency": "independent SQLite online snapshots; services are not jointly quiesced"}
            (root / MANIFEST).write_text(json.dumps(manifest, indent=2))
            (root / MANIFEST).chmod(0o600)
            _restic("backup", "--tag", TAG, "--", str(root), *([str(s.library_dir.resolve())] if media else []))
        record_status("external", success=True)
    except Exception as exc:
        record_status("external", success=False, error=str(exc) if isinstance(exc, BackupError) else type(exc).__name__)
        raise BackupError("external backup failed; see admin backup status") from None


def verify_recovery(destination: Path) -> dict:
    """Validate the restored manifest, every recovery file and both SQLite databases."""
    manifests = list(destination.rglob(MANIFEST))
    if len(manifests) != 1 or manifests[0].is_symlink():
        raise BackupError("exactly one recovery manifest is required")
    root = manifests[0].parent
    data = json.loads(manifests[0].read_text())
    required = {"hub.sqlite3", "navidrome.sqlite3", "config.yaml", "recovery-keys.json"}
    if (not isinstance(data, dict) or data.get("version") != 1
            or not isinstance(data.get("files"), dict) or not required <= data["files"].keys()
            or data.get("databases") != ["hub.sqlite3", "navidrome.sqlite3"]
            or not isinstance(data.get("media_included"), bool)):
        raise BackupError("invalid recovery manifest")
    for name, digest in data["files"].items():
        path = PurePosixPath(name)
        if path.is_absolute() or len(path.parts) != 1 or name in {".", ".."} or "\\" in name:
            raise BackupError("unsafe recovery manifest path")
        target = root / name
        if not target.is_file() or target.is_symlink() or _digest(target) != digest:
            raise BackupError("recovery file checksum mismatch")
    keys = json.loads((root / "recovery-keys.json").read_text())
    if not isinstance(keys, dict) or not all(isinstance(keys.get(k), str) and keys[k].strip()
                                           for k in ("HUB_SECRET_KEY", "ND_PASSWORDENCRYPTIONKEY")):
        raise BackupError("recovery keys are missing")
    for name in data["databases"]:
        sqlite_integrity(root / name)
    if data["media_included"]:
        media = PurePosixPath(data.get("media_path") or "")
        if not media.is_absolute() or ".." in media.parts or media == PurePosixPath("/"):
            raise BackupError("invalid media recovery path")
        restored_media = destination.joinpath(*media.parts[1:])
        if not restored_media.is_dir() or restored_media.is_symlink():
            raise BackupError("media snapshot is missing")
    return {"ok": True, "media_included": data["media_included"], "recovery_root": str(root)}


def restore(destination: Path, snapshot: str = "latest") -> dict:
    """Never overwrite anything: restore into an exclusively created, private directory."""
    s = get_settings()
    dest = destination.resolve()
    protected = [s.data_dir, s.music_root, s.config_file.parent,
                 Path(os.environ.get("BACKUP_NAVIDROME_DB", "/navidrome-data/navidrome.db")).parent]
    if any(dest == p.resolve() or dest.is_relative_to(p.resolve()) or p.resolve().is_relative_to(dest) for p in protected):
        raise BackupError("restore destination overlaps live data")
    if destination.is_symlink() or dest.exists():
        raise BackupError("restore destination must not exist")
    if not snapshot or snapshot.startswith("-"):
        raise BackupError("invalid snapshot selector")
    # Resolve a tagged snapshot to its immutable ID before checking/restoring (no moving latest target).
    snapshots = json.loads(_restic("snapshots", "--json", "--tag", TAG, *([] if snapshot == "latest" else [snapshot])))
    if not isinstance(snapshots, list) or not snapshots:
        raise BackupError("no tagged recovery snapshot found")
    if snapshot == "latest":
        snapshots = [max(snapshots, key=lambda item: item["time"])]
    if len(snapshots) != 1:
        raise BackupError("exactly one tagged recovery snapshot is required")
    snapshot_id = snapshots[0].get("id", "")
    if TAG not in snapshots[0].get("tags", []):
        raise BackupError("snapshot is not a HomeMusic recovery snapshot")
    if len(snapshot_id) != 64 or any(c not in "0123456789abcdef" for c in snapshot_id):
        raise BackupError("invalid snapshot ID")
    _restic("check", "--read-data")
    dest.mkdir(mode=0o700, parents=False, exist_ok=False)
    _restic("restore", snapshot_id, "--target", str(dest), "--verify")
    return verify_recovery(dest)


def main() -> None:
    parser = argparse.ArgumentParser(description="Encrypted backups and isolated recovery checks; never installs live data")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("backup")
    sub.add_parser("init", help="explicitly initialize the configured encrypted restic repository")
    for name in ("restore", "verify-restore"):
        p = sub.add_parser(name, help="restore, restic read-data check, manifest/hash and SQLite integrity check")
        p.add_argument("--destination", type=Path, required=True, help="new directory outside all live data")
        p.add_argument("--snapshot", default="latest")
    args = parser.parse_args()
    try:
        if args.command == "init":
            _restic("init")
        elif args.command == "backup":
            if not enabled():
                raise BackupError("external backups are disabled")
            external_backup()
        else:
            print(json.dumps(restore(args.destination, args.snapshot)))
    except Exception as exc:
        parser.exit(1, f"{str(exc) if isinstance(exc, BackupError) else type(exc).__name__}\n")


if __name__ == "__main__":
    main()
