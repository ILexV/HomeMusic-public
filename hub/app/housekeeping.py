"""Periodic maintenance: DB backup, trash/staging cleanup, cache purge, provider smoke test (NFR-5, NFR-6)."""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from app.config import get_settings

log = logging.getLogger(__name__)

KEEP_BACKUPS = 7


def backup_hub_db() -> Path | None:
    from app.backup import record_status, snapshot_sqlite

    s = get_settings()
    record_status("local", success=None)
    if not s.db_path.is_file():
        record_status("local", success=False, error="Hub database unavailable")
        return None
    dest_dir = s.backups_dir / "hub"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"hub-{datetime.now():%Y%m%d}.sqlite3"
    tmp = dest_dir / ".hub-backup.part"
    local_tmp = s.data_dir / "backup.tmp.sqlite3"
    try:
        # SQLite locking on Windows mounts is unreliable: snapshot locally before copying.
        local_tmp.unlink(missing_ok=True)
        snapshot_sqlite(s.db_path, local_tmp)
        shutil.copyfile(local_tmp, tmp)
        tmp.chmod(0o600)
        tmp.replace(dest)
        for old in sorted(dest_dir.glob("hub-*.sqlite3"))[:-KEEP_BACKUPS]:
            old.unlink(missing_ok=True)
        record_status("local", success=True)
        return dest
    except Exception as exc:
        record_status("local", success=False, error=type(exc).__name__)
        raise
    finally:
        local_tmp.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)


async def smoke_test_providers() -> dict[str, str]:
    """Search a well-known track on every provider; a parser that returns nothing means the layout changed."""
    from app.providers.base import TrackQuery
    from app.services import svc

    q = TrackQuery(artist="Кино", title="Группа крови")
    out: dict[str, str] = {}
    for name, p in svc.providers.items():
        try:
            res = await p.search(q, limit=5)
            out[name] = f"ok ({len(res)})" if res else "EMPTY — возможно, сменилась вёрстка"
        except Exception as e:
            out[name] = f"error: {e!r}"[:200]
    bad = {k: v for k, v in out.items() if not v.startswith("ok")}
    (log.warning if bad else log.info)("provider smoke test: %s", out)
    return out


async def run_daily() -> None:
    from app.accounts import purge_email_codes
    from app.api.uploads import purge_stale_uploads
    from app.catalog.cache import cache_purge_expired
    from app.library.editor import purge_trash

    cfg = get_settings().cfg
    try:
        path = await asyncio.to_thread(backup_hub_db)
        log.info("hub db backup: %s", path)
        if path:
            from app.backup import external_backup

            try:
                await asyncio.to_thread(external_backup)
            except Exception:
                log.error("external backup failed; see admin backup status")
    except Exception:
        log.exception("hub db backup failed")
    try:
        n = await asyncio.to_thread(purge_trash, cfg.library.trash_days)
        m = await asyncio.to_thread(purge_stale_uploads)
        c = await cache_purge_expired()
        e = await purge_email_codes()
        log.info("housekeeping: trash dirs %s, stale uploads %s, cache rows %s, email codes %s removed", n, m, c, e)
    except Exception:
        log.exception("housekeeping failed")
    await smoke_test_providers()


async def scheduler_loop() -> None:
    """Runs `run_daily` at cfg.library.backup_hour local time every day."""
    hour = get_settings().cfg.library.backup_hour
    while True:
        now = datetime.now()
        nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        await run_daily()
