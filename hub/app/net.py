"""Shared HTTP plumbing: httpx clients, polite rate limiting, exponential backoff, optional proxy per host."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import random
import time
from collections import deque

import httpx

from app.config import get_settings
from app.external import PublicAsyncTransport

log = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Mobile Safari/537.36"
)
RETRY_STATUSES = {403, 408, 425, 429, 500, 502, 503, 504}


class Priority:
    """Whether the current job runs in the background (prefetching what the user may tap). Mutable: a background
    job that a user request joins (`foreground()`) goes first from then on."""

    __slots__ = ("background",)

    def __init__(self, background: bool = False):
        self.background = background

    def foreground(self) -> None:
        self.background = False


_priority: contextvars.ContextVar[Priority | None] = contextvars.ContextVar("hm_priority", default=None)


def in_background() -> bool:
    p = _priority.get()
    return p is not None and p.background


def priority_context(p: Priority) -> contextvars.Context:
    """A copy of the current context running with priority `p` (for `asyncio.create_task(..., context=...)`)."""
    ctx = contextvars.copy_context()
    ctx.run(_priority.set, p)
    return ctx


class RateLimiter:
    """At most `rate` requests per second (minimum interval between request starts). Background callers
    (`in_background()`) take a slot only while no foreground caller waits: taps and downloads go first."""

    def __init__(self, rate_per_sec: float):
        self.interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0
        self._lock = asyncio.Lock()
        self._next = 0.0
        self._urgent = 0  # foreground callers waiting
        self._calm = asyncio.Event()  # set while no foreground caller waits
        self._calm.set()

    async def wait(self) -> None:
        if not self.interval:
            return
        while in_background():
            if not self._calm.is_set():
                try:  # re-checked now and then: the job may have become foreground meanwhile
                    await asyncio.wait_for(self._calm.wait(), self.interval)
                except TimeoutError:
                    pass
                continue
            async with self._lock:
                now = time.monotonic()
                if self._urgent:
                    continue
                if self._next <= now:
                    self._next = now + self.interval
                    return
                delay = self._next - now
            await asyncio.sleep(delay)  # outside the lock: a foreground caller may take this slot
        self._urgent += 1
        self._calm.clear()
        try:
            async with self._lock:
                now = time.monotonic()
                if self._next > now:
                    await asyncio.sleep(self._next - now)
                    now = time.monotonic()
                self._next = now + self.interval
        finally:
            self._urgent -= 1
            if not self._urgent:
                self._calm.set()


class PrioritySemaphore:
    """asyncio.Semaphore whose background holders (`in_background()`) leave `reserve` slots free and wait while
    a foreground caller waits."""

    def __init__(self, value: int, reserve: int = 1):
        self.value = value
        self.reserve = min(reserve, value - 1)
        self._held = 0
        self._urgent = 0
        self._cond = asyncio.Condition()

    async def __aenter__(self) -> None:
        async with self._cond:
            if in_background():
                await self._cond.wait_for(
                    lambda: not in_background() and self._held < self.value
                    or not self._urgent and self._held < self.value - self.reserve
                )
            else:
                self._urgent += 1
                try:
                    await self._cond.wait_for(lambda: self._held < self.value)
                finally:
                    self._urgent -= 1
            self._held += 1

    async def __aexit__(self, *exc: object) -> None:
        async with self._cond:
            self._held -= 1
            self._cond.notify_all()

    async def kick(self) -> None:
        """Waiters look again: a job waiting for a slot was lifted to the foreground (`Priority.foreground()`)."""
        async with self._cond:
            self._cond.notify_all()


class WindowLimiter:
    """At most `requests` per sliding window of `per_seconds` (Deezer: ~50 per 5 s)."""

    def __init__(self, requests: int, per_seconds: float):
        self.requests = requests
        self.per = per_seconds
        self._times: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._times and now - self._times[0] >= self.per:
                    self._times.popleft()
                if len(self._times) < self.requests:
                    self._times.append(now)
                    return
                await asyncio.sleep(self.per - (now - self._times[0]) + 0.01)


def make_client(
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
    proxy_hosts: set[str] | None = None,
    http2: bool = True,
) -> httpx.AsyncClient:
    """Public-Internet client; redirects and DNS/peer validation apply on every connection."""
    s = get_settings()
    mounts: dict[str, httpx.AsyncHTTPTransport] = {}
    hosts = proxy_hosts if proxy_hosts is not None else s.proxy_hosts
    if s.outbound_proxy and hosts:
        for h in hosts:
            mounts[f"all://{h}"] = PublicAsyncTransport(proxy=s.outbound_proxy, http2=http2)
    h = {"User-Agent": DEFAULT_UA}
    h.update(headers or {})
    return httpx.AsyncClient(
        headers=h,
        timeout=httpx.Timeout(timeout, connect=15),
        follow_redirects=True,
        http2=http2,
        transport=PublicAsyncTransport(http2=http2),
        trust_env=False,
        mounts=mounts or None,
    )


async def request_with_backoff(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    limiter: RateLimiter | WindowLimiter | None = None,
    retries: int = 4,
    base_delay: float = 1.5,
    retry_statuses: set[int] = RETRY_STATUSES,
    **kwargs,
) -> httpx.Response:
    """Send a request; on 403/429/5xx or network errors retry with exponential backoff + jitter.

    Returns the last response (caller checks status) or raises the last network error.
    """
    last_exc: Exception | None = None
    resp: httpx.Response | None = None
    for attempt in range(retries + 1):
        if limiter:
            await limiter.wait()
        try:
            resp = await client.request(method, url, **kwargs)
            if resp.status_code not in retry_statuses:
                return resp
            retry_after = resp.headers.get("retry-after")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else base_delay * 2**attempt
            log.warning("http %s %s -> %s, retry in %.1fs", method, url, resp.status_code, delay)
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last_exc = e
            delay = base_delay * 2**attempt
            log.warning("http %s %s failed: %r, retry in %.1fs", method, url, e, delay)
        if attempt < retries:
            await asyncio.sleep(delay + random.uniform(0, 0.5))
    if resp is not None:
        return resp
    assert last_exc is not None
    raise last_exc
