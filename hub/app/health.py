"""Bounded operational probes; public responses contain only component status, never errors/paths."""
from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import Field
from sqlalchemy import func, select

from app.api.schemas import HealthOut
from app.config import get_settings
from app.db import session_scope
from app.models import Job

_tasks: tuple[asyncio.Task, ...] = ()
_expected = 1
_disabled = False
navidrome_cached: bool | None = None


class AdminHealthOut(HealthOut):
    status: str
    components: dict[str, Any] = Field(default_factory=dict)
    queue: dict[str, Any] = Field(default_factory=dict)
    disk: dict[str, Any] = Field(default_factory=dict)
    backups: dict[str, Any] = Field(default_factory=dict)
    background: dict[str, Any] = Field(default_factory=dict)


def register_background(tasks, *, expected: int, disabled: bool = False) -> None:
    global _tasks, _expected, _disabled
    _tasks, _expected, _disabled = tuple(tasks), expected, disabled


def background_status() -> dict:
    if _disabled:
        return {"ok": True, "status": "disabled", "tasks": {}}
    states = {t.get_name(): "cancelled" if t.cancelled() else "stopped" if t.done() else "running" for t in _tasks}
    ok = len(_tasks) == _expected and all(not t.done() for t in _tasks)
    return {"ok": ok, "status": "ok" if ok else "unavailable", "expected": _expected, "tasks": states}


async def database_ok() -> bool:
    try:
        async with asyncio.timeout(3):
            async with session_scope() as session:
                # Touch a real required table, not merely a newly opened empty SQLite file.
                await session.execute(select(Job.id).limit(1))
        return True
    except Exception:
        return False


async def embedder_status() -> str:
    settings = get_settings()
    if not settings.embedder_url:
        return "disabled"
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            response = await client.get(settings.embedder_url.rstrip("/") + "/healthz")
            response.raise_for_status()
            data = response.json()
        worker = data.get("worker", {})
        if data.get("state") == "error" or data.get("status") != "ok" or worker.get("stalled"):
            return "degraded"
        return "loading" if data.get("state") == "loading" else "ok"
    except Exception:
        return "degraded"


async def readiness(navidrome_probe) -> dict:
    global navidrome_cached
    database, navidrome, embedder = await asyncio.gather(database_ok(), navidrome_probe(), embedder_status())
    navidrome_cached = navidrome
    workers = background_status()
    ok = database and navidrome and workers["ok"]
    return {"ok": ok, "status": "unavailable" if not ok else "degraded" if embedder == "degraded" else "ok",
            "components": {"database": "ok" if database else "unavailable",
                           "navidrome": "ok" if navidrome else "unavailable",
                           "workers": workers["status"], "embedder": embedder}}


async def queue_status() -> dict:
    try:
        async with asyncio.timeout(3):
            async with session_scope() as session:
                rows = (await session.execute(select(Job.status, func.count(), func.min(Job.created_at),
                                                     func.min(Job.updated_at)).group_by(Job.status))).all()
        now = datetime.now(UTC)

        def age(at):
            return max(0, int((now - at.replace(tzinfo=UTC)).total_seconds())) if at else None

        return {"status": "ok", "counts": {state: count for state, count, _, _ in rows},
                "oldest_age_seconds": {state: age(created) for state, _, created, _ in rows if state in {"queued", "running"}},
                "oldest_update_age_seconds": {state: age(updated) for state, _, _, updated in rows if state == "running"}}
    except Exception:
        return {"status": "unavailable"}


def disk_status() -> dict:
    s = get_settings()
    out = {}
    for label, path in (("data", s.data_dir), ("music", s.music_root)):
        try:
            usage = shutil.disk_usage(path)
            out[label] = {"free_bytes": usage.free, "total_bytes": usage.total,
                          "status": "low" if usage.free < max(512 * 2**20, usage.total // 100) else "ok"}
        except OSError:
            out[label] = {"status": "unavailable"}
    return out


async def admin_details() -> dict:
    from app.backup import enabled, read_status

    queue, disk, backups = await asyncio.gather(queue_status(), asyncio.to_thread(disk_status), asyncio.to_thread(read_status))
    return {"queue": queue, "disk": disk, "backups": {"external_enabled": enabled(), **backups},
            "background": background_status()}
