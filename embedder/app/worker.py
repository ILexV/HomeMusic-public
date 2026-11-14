"""The model on demand (loaded lazily, unloaded when idle) and the queue worker: download/decode the next batch
while the current one is on the GPU."""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
import numpy as np

from . import audio
from .config import Settings
from .db import Item, Store
from .models import GpuBusy, Model

log = logging.getLogger("embedder.worker")


class Engine:
    """One model at a time. `state`: idle (not loaded) | loading | ready | error."""

    def __init__(self, settings: Settings, factory: Callable[[Settings], Model], *, name: str):
        self.settings = settings
        self.factory = factory
        # vectors are stored and compared under this name; it does not depend on the device
        self.name = name
        self.model: Model | None = None
        self.state = "idle"
        self.error: str | None = None
        self.last_used = time.monotonic()
        self._lock = asyncio.Lock()
        # serializes model calls: the worker's batches and /text share one model
        self.busy = asyncio.Lock()

    async def ensure(self) -> Model:
        self.last_used = time.monotonic()
        if self.model:
            return self.model
        async with self._lock:
            if self.model:
                return self.model
            self.state, self.error = "loading", None
            t = time.monotonic()
            try:
                self.model = await asyncio.to_thread(self.factory, self.settings)
            except GpuBusy as e:  # expected while the owner's job holds the card: no traceback every retry
                self.state, self.error = "error", f"{type(e).__name__}: {e}"
                log.warning("model not loaded: %s", e)
                raise
            except Exception as e:
                self.state, self.error = "error", f"{type(e).__name__}: {e}"
                log.exception("model load failed")
                raise
            self.state = "ready"
            log.info("model %s loaded on %s in %.1f s", self.name, self.model.device, time.monotonic() - t)
            return self.model

    async def unload(self) -> None:
        async with self._lock, self.busy:
            if self.model:
                await asyncio.to_thread(self.model.close)
                self.model = None
                self.state = "idle"
                log.info("model unloaded (idle)")

    async def run(self, fn: Callable, *args):
        """A model call in a thread, one at a time."""
        model = await self.ensure()
        async with self.busy:
            self.last_used = time.monotonic()
            try:
                return await asyncio.to_thread(fn, model, *args)
            finally:
                self.last_used = time.monotonic()


@dataclass
class Prepared:
    items: list[Item]
    wavs: list[np.ndarray]
    seconds_fetch: float


@dataclass
class Stats:
    songs: int = 0
    seconds_gpu: float = 0.0
    last_rate: float | None = None  # songs per second of model time, last batch


class Worker:
    def __init__(self, settings: Settings, store: Store, engine: Engine, client: httpx.AsyncClient, sr: int):
        self.s = settings
        self.store = store
        self.engine = engine
        self.client = client
        self.sr = sr
        self.wake = asyncio.Event()
        self.stats = Stats()
        self._sem = asyncio.Semaphore(settings.downloads)
        self.activity: dict[str, float] = {}
        self.last_progress = time.monotonic()

    def health(self) -> dict:
        now = time.monotonic()
        active = {name: round(now - since, 1) for name, since in self.activity.items()}
        phase = "loading" if self.engine.state == "loading" else "embedding" if "embedding" in active else "preparing" if active else "idle"
        # Model loading has its own state; a quiet unloaded worker is not a stuck batch.
        stalled = phase not in {"idle", "loading"} and now - self.last_progress > 900
        return {"phase": phase, "active_seconds": active, "stalled": stalled,
                "seconds_since_progress": round(now - self.last_progress, 1) if active else None}

    async def _prepare_one(self, it: Item) -> np.ndarray | None:
        async with self._sem:
            try:
                return await audio.load(
                    self.client, it.kind, it.source, music_root=self.s.music_root, sr=self.sr,
                    clip_s=self.s.clip_s, max_bytes=self.s.max_download_mb * 2**20,
                )
            except audio.AudioError as e:
                self._fail(it.key, str(e), retry=e.retry)
            except Exception as e:  # a bad file must not stop the queue
                log.exception("prepare %s", it.key)
                self._fail(it.key, f"{type(e).__name__}: {e}", retry=False)
            return None

    def _fail(self, key: str, error: str, *, retry: bool) -> None:
        log.info("item %s failed: %s", key, error)
        self.store.fail(key, error, retry=retry, max_attempts=self.s.max_attempts, retry_s=self.s.retry_s)

    async def prepare(self, items: list[Item]) -> Prepared:
        t = time.monotonic()
        self.activity["preparing"] = t
        self.last_progress = t
        try:
            wavs = await asyncio.gather(*(self._prepare_one(it) for it in items))
            ok = [(it, w) for it, w in zip(items, wavs, strict=True) if w is not None]
            return Prepared([it for it, _ in ok], [w for _, w in ok], time.monotonic() - t)
        finally:
            self.activity.pop("preparing", None)
            self.last_progress = time.monotonic()

    async def embed(self, p: Prepared) -> None:
        if not p.items:
            return
        t = time.monotonic()
        self.activity["embedding"] = t
        self.last_progress = t
        try:
            model = await self.engine.ensure()
            if model.device == "cpu":
                # A CPU batch can take minutes. Release the model between songs so interactive
                # text preferences do not wait behind the entire background audio batch.
                vecs = np.empty((len(p.wavs), model.dim), dtype=np.float32)
                for i, wav in enumerate(p.wavs):
                    vecs[i] = (await self.engine.run(lambda m, w: m.embed_audio(w), [wav]))[0]
                    self.last_progress = time.monotonic()
            else:
                vecs = await self.engine.run(lambda m, w: m.embed_audio(w), p.wavs)
        except Exception as e:
            # out of GPU memory and the like: the batch is retried later; the queue goes on
            log.exception("embedding a batch of %d failed", len(p.items))
            for it in p.items:
                self._fail(it.key, f"model: {type(e).__name__}: {e}", retry=True)
            return
        finally:
            self.activity.pop("embedding", None)
            self.last_progress = time.monotonic()
        dt = time.monotonic() - t
        for it, w, v in zip(p.items, p.wavs, vecs, strict=True):
            self.store.finish(it.key, v, self.engine.name, len(w) / self.sr)
        self.stats.songs += len(p.items)
        self.stats.seconds_gpu += dt
        self.stats.last_rate = len(p.items) / dt if dt > 0 else None
        log.info(
            "embedded %d in %.2f s on %s (%.1f songs/s; fetch+decode %.2f s)",
            len(p.items), dt, getattr(self.engine.model, "device", "?"), self.stats.last_rate or 0, p.seconds_fetch,
        )

    async def step(self) -> int:
        """One batch, sequentially (tests drive the queue with this). Returns the number of items taken."""
        items = self.store.claim(self.s.batch)
        if items:
            await self.embed(await self.prepare(items))
        return len(items)

    async def _load_or_wait(self) -> bool:
        try:
            await self.engine.ensure()
            return True
        except Exception:
            await asyncio.sleep(self.s.retry_s)
            return False

    async def run(self) -> None:
        """Forever: download batch N+1 while batch N is on the model."""
        pending: asyncio.Task | None = None
        while True:
            try:
                if pending is None:
                    if not self.store.pending():
                        await self._idle()
                        continue
                    if not await self._load_or_wait():
                        continue
                    items = self.store.claim(self.s.batch)
                    if not items:
                        continue
                    pending = asyncio.create_task(self.prepare(items))
                prepared = await pending
                nxt = self.store.claim(self.s.batch)
                pending = asyncio.create_task(self.prepare(nxt)) if nxt else None
                await self.embed(prepared)
            except asyncio.CancelledError:
                if pending:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                raise
            except Exception:
                log.exception("worker loop")
                pending = None
                await asyncio.sleep(self.s.retry_s)

    async def _idle(self) -> None:
        idle = self.s.idle_unload_s
        if idle and self.engine.model and time.monotonic() - self.engine.last_used >= idle and not self.engine.busy.locked():
            await self.engine.unload()
        self.wake.clear()
        if self.store.pending():  # queued between the check and clear()
            return
        try:
            await asyncio.wait_for(self.wake.wait(), self.s.poll_s)
        except TimeoutError:
            pass
