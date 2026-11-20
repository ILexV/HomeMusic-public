"""SQLite (WAL) engine, async sessions and Alembic migrations on startup."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _configure_connection(dbapi_conn, _record) -> None:
    # SQLite's built-in lower() and LIKE only fold ASCII.
    dbapi_conn.create_function("unicode_casefold", 1, lambda s: s.casefold() if s is not None else None,
                               deterministic=True)
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=10000")
    cur.close()


def init_engine(db_path: Path | None = None) -> AsyncEngine:
    global _engine, _sessionmaker
    path = db_path or get_settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    _engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}", connect_args={"timeout": 30})
    event.listen(_engine.sync_engine, "connect", _configure_connection)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def engine() -> AsyncEngine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        init_engine()
    assert _sessionmaker is not None
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Session that commits on success and rolls back on error."""
    async with sessionmaker()() as s:
        try:
            yield s
            await s.commit()
        except BaseException:
            await s.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Use with `scope="function"`: the commit then happens before the response is sent
    (with the default request scope a client could see a stale state right after a write, e.g. 401 after login),
    and long responses (SSE) don't hold a connection."""
    async with session_scope() as s:
        yield s


def run_migrations(db_path: Path | None = None) -> None:
    """alembic upgrade head (sync, called once at startup before the event loop work starts)."""
    from alembic import command
    from alembic.config import Config

    path = db_path or get_settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    # keep the app's JSON logging (env.py would otherwise apply alembic.ini's logging config)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")


async def dispose() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
