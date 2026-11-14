"""SQLite-backed job queue with an in-process asyncio worker pool.

- Jobs survive restarts: on startup every `running` job is put back to `queued`.
- Claiming is atomic (single UPDATE ... RETURNING), so several workers never take the same job.
- Failures retry with exponential backoff up to `max_attempts`; `PermanentError` fails at once.
- Higher `priority` goes first. Background work (imports: `PRIORITY_IMPORT`) is below 0: it never takes the last
  worker (`background_slots`), so a tap or a manual download starts at once, and it runs with the background network
  priority (`app.net.Priority`): its catalog and source requests yield to the user's.
"""

from __future__ import annotations

import asyncio
import logging
import os
import traceback
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import session_scope
from app.models import DownloadItem, Job, utcnow
from app.net import Priority, _priority

log = logging.getLogger(__name__)

Handler = Callable[[Job], Awaitable[None]]
_handlers: dict[str, Handler] = {}
_wakeup = asyncio.Event()

PRIORITY_IMPORT = -10


class PermanentError(Exception):
    """Do not retry this job."""


class RetryLater(Exception):
    """Retry after `delay_s` without counting it as a failure-worthy error message."""

    def __init__(self, delay_s: float, reason: str = ""):
        super().__init__(reason or f"retry in {delay_s}s")
        self.delay_s = delay_s


def handler(kind: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _handlers[kind] = fn
        return fn

    return deco


async def enqueue(
    s: AsyncSession,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    delay_s: float = 0,
    max_attempts: int | None = None,
    priority: int = 0,
) -> Job:
    """Add a job inside the caller's transaction. Workers are woken after the commit by `notify()`."""
    job = Job(
        kind=kind,
        payload_json=payload or {},
        priority=priority,
        run_after=utcnow() + timedelta(seconds=delay_s),
        max_attempts=max_attempts or get_settings().cfg.jobs.max_attempts,
    )
    s.add(job)
    await s.flush()
    return job


def notify() -> None:
    _wakeup.set()


async def recover_stale() -> int:
    """Reclaim interrupted jobs and imports whose old handler finished before scheduling their scan."""
    async with session_scope() as s:
        res = await s.execute(
            update(Job).where(Job.status == "running").values(status="queued", locked_by=None, locked_at=None)
        )
        recovered = res.rowcount or 0
        active = select(func.json_extract(Job.payload_json, "$.item_id")).where(
            Job.kind == "download_item", Job.status.in_(("queued", "running")))
        orphaned = (await s.execute(select(DownloadItem.id).where(
            DownloadItem.status == "importing",
            ~DownloadItem.id.in_(active.where(func.json_extract(Job.payload_json, "$.item_id").is_not(None))),
        ))).scalars().all()
        for item_id in orphaned:
            await enqueue(s, "download_item", {"item_id": item_id})
        return recovered + len(orphaned)


def background_slots() -> int:
    """Workers background jobs may hold at once: all but one."""
    return max(1, get_settings().cfg.jobs.workers - 1)


# a queued job a worker may take now: urgent, or background while a background slot is free
_CLAIMABLE = (
    "status='queued' AND (priority >= 0 OR "
    "(SELECT COUNT(*) FROM jobs WHERE status='running' AND priority < 0) < :bg)"
)


async def _claim(worker_id: str) -> Job | None:
    now = utcnow()
    async with session_scope() as s:
        row = (
            await s.execute(
                text(
                    "UPDATE jobs SET status='running', locked_by=:w, locked_at=:now, attempts=attempts+1, updated_at=:now "
                    f"WHERE id = (SELECT id FROM jobs WHERE {_CLAIMABLE} AND run_after <= :now "
                    "ORDER BY priority DESC, run_after, id LIMIT 1) RETURNING id"
                ),
                {"w": worker_id, "now": now, "bg": background_slots()},
            )
        ).first()
        if row is None:
            return None
        return await s.get(Job, row[0])


async def _finish(job_id: int, *, ok: bool, error: str | None = None, retry_in: float | None = None) -> None:
    async with session_scope() as s:
        job = await s.get(Job, job_id)
        if job is None:
            return
        job.locked_by = None
        job.locked_at = None
        if ok:
            job.status = "done"
            job.last_error = None
        elif retry_in is not None:
            job.status = "queued"
            job.run_after = utcnow() + timedelta(seconds=retry_in)
            job.last_error = error
        else:
            job.status = "failed"
            job.last_error = error


async def _next_wakeup_in() -> float:
    async with session_scope() as s:
        row = (
            await s.execute(text(f"SELECT MIN(run_after) FROM jobs WHERE {_CLAIMABLE}"), {"bg": background_slots()})
        ).first()
    if not row or row[0] is None:
        return 30.0
    nxt = row[0] if isinstance(row[0], datetime) else datetime.fromisoformat(str(row[0]))
    return max(0.05, min(30.0, (nxt - utcnow()).total_seconds()))


async def run_job(job: Job) -> None:
    fn = _handlers.get(job.kind)
    if fn is None:
        await _finish(job.id, ok=False, error=f"no handler for {job.kind}")
        return
    token = _priority.set(Priority(background=True)) if (job.priority or 0) < 0 else None
    try:
        await fn(job)
    except RetryLater as e:
        await _finish(job.id, ok=False, error=str(e), retry_in=e.delay_s)
    except PermanentError as e:
        log.warning("job %s (%s) failed permanently: %s", job.id, job.kind, e, extra={"job_id": job.id})
        await _finish(job.id, ok=False, error=str(e))
    except asyncio.CancelledError:
        await asyncio.shield(_finish(job.id, ok=False, error="cancelled (shutdown)", retry_in=0))
        raise
    except Exception as e:
        tb = traceback.format_exc(limit=8)
        if job.attempts < job.max_attempts:
            delay = min(600.0, 5 * 2 ** (job.attempts - 1))
            log.warning("job %s (%s) attempt %s failed: %r, retry in %ss", job.id, job.kind, job.attempts, e, delay,
                        extra={"job_id": job.id})
            await _finish(job.id, ok=False, error=f"{e!r}\n{tb}", retry_in=delay)
        else:
            log.error("job %s (%s) failed after %s attempts: %r", job.id, job.kind, job.attempts, e,
                      extra={"job_id": job.id})
            await _finish(job.id, ok=False, error=f"{e!r}\n{tb}")
            on_fail = _failure_hooks.get(job.kind)
            if on_fail:
                try:
                    await on_fail(job, e)
                except Exception:
                    log.exception("failure hook for job %s crashed", job.id)
    else:
        await _finish(job.id, ok=True)
    finally:
        if token is not None:
            _priority.reset(token)
        _wakeup.set()  # a background slot may be free now


_failure_hooks: dict[str, Callable[[Job, Exception], Awaitable[None]]] = {}


def on_final_failure(kind: str):
    """Register a hook called when a job of this kind exhausted its attempts (e.g. mark the item failed)."""

    def deco(fn):
        _failure_hooks[kind] = fn
        return fn

    return deco


class WorkerPool:
    def __init__(self, n: int):
        self.n = n
        self._tasks: list[asyncio.Task] = []
        self._stopping = False

    @property
    def tasks(self) -> tuple[asyncio.Task, ...]:
        return tuple(self._tasks)

    async def _worker(self, idx: int) -> None:
        wid = f"{os.getpid()}-{idx}"
        while not self._stopping:
            try:
                job = await _claim(wid)
            except Exception:
                log.exception("claim failed")
                await asyncio.sleep(2)
                continue
            if job is None:
                _wakeup.clear()
                try:
                    await asyncio.wait_for(_wakeup.wait(), timeout=await _next_wakeup_in())
                except TimeoutError:
                    pass
                continue
            # let other idle workers look for more work too
            _wakeup.set()
            await run_job(job)

    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._worker(i), name=f"job-worker-{i}") for i in range(self.n)]

    async def stop(self) -> None:
        self._stopping = True
        _wakeup.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
