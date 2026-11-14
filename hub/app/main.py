"""HomeMusic Hub: FastAPI app (API + PWA static files) with in-process job workers."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import db
from app.config import get_settings
from app.logging_setup import setup_logging

log = logging.getLogger("app")


def _import_handlers() -> None:
    # registering job handlers is a side effect of importing these modules
    import app.imports.core
    import app.jobs.downloads
    import app.mixes.core

    try:
        import app.ai.pipeline  # noqa: F401
    except ImportError:
        log.warning("assistant pipeline not available")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    setup_logging(s.log_level)
    for d in (s.library_dir, s.staging_dir, s.trash_dir, s.backups_dir):
        d.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(db.run_migrations)
    db.init_engine()

    from app import listen
    from app.housekeeping import scheduler_loop
    from app.health import register_background
    from app.jobs.downloads import playlist_adds_loop
    from app.jobs.queue import WorkerPool, recover_stale
    from app.mixes import smart, wave
    from app.mixes.core import schedule_loop
    from app.plays import scrobbles_loop
    from app.services import svc

    svc.start()
    _import_handlers()
    pool = WorkerPool(s.cfg.jobs.workers)
    tasks: list[asyncio.Task] = []
    if not s.disable_background:
        n = await recover_stale()
        if n:
            log.info("re-queued %s interrupted jobs", n)
        pool.start()
        tasks.append(asyncio.create_task(scheduler_loop(), name="scheduler"))
        tasks.append(asyncio.create_task(scrobbles_loop(), name="scrobbles"))
        tasks.append(asyncio.create_task(schedule_loop(), name="mixes"))
        tasks.append(asyncio.create_task(playlist_adds_loop(), name="playlist-adds"))
    register_background([*pool.tasks, *tasks], expected=s.cfg.jobs.workers + 4, disabled=s.disable_background)
    log.info("hub started")
    try:
        yield
    finally:
        register_background([], expected=1)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await pool.stop()
        await listen.reset()
        await wave.reset()
        await smart.reset()
        await svc.stop()
        await db.dispose()


def create_app() -> FastAPI:
    from app.api import (
        admin,
        auth,
        catalog,
        download_pages,
        downloads,
        events,
        imports,
        insights,
        library,
        listen,
        lyrics,
        medialib,
        mixes,
        playlists,
        playback,
        plays,
        recommendations,
        recommendation_pauses,
        recording_feedback,
        releases,
        search,
        smart_shuffle,
        smart_playlists,
        system,
        uploads,
        wave,
    )

    app = FastAPI(title="HomeMusic Hub", version="0.1.0", lifespan=lifespan, docs_url="/api/docs",
                  openapi_url="/api/openapi.json", redoc_url=None)
    from app.upload_ingress import UploadBodyLimitMiddleware

    app.add_middleware(UploadBodyLimitMiddleware)
    app.include_router(download_pages.router)
    app.include_router(recording_feedback.router)
    for r in (auth.router, search.router, catalog.router, downloads.router, listen.router, library.router, medialib.router,
              uploads.router, playlists.router, plays.router, mixes.router, wave.router, smart_shuffle.router, imports.router, events.router, system.router, admin.router,
              insights.router, lyrics.router, smart_playlists.router, playback.router,
              recommendations.router, recommendation_pauses.router, releases.router):
        app.include_router(r)
    try:
        from app.api import assistant

        app.include_router(assistant.router)
    except ImportError:
        pass

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"detail": "Внутренняя ошибка сервера"}, status_code=500)

    static = get_settings().static_dir
    if (static / "index.html").is_file():
        if (static / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            if path.startswith("api/"):
                return JSONResponse({"detail": "Не найдено"}, status_code=404)
            f = (static / path).resolve()
            if path and f.is_file() and static.resolve() in f.parents:
                headers = {"Cache-Control": "no-cache"} if f.name in ("sw.js", "manifest.webmanifest") else None
                return FileResponse(f, headers=headers)
            return FileResponse(static / "index.html", headers={"Cache-Control": "no-cache"})

    return app


app = create_app()
