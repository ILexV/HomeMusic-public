import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import health
from app.api import system


@pytest.fixture(autouse=True)
def isolate_registry(monkeypatch):
    monkeypatch.setattr(health, "_tasks", ())
    monkeypatch.setattr(health, "_expected", 1)
    monkeypatch.setattr(health, "_disabled", False)
    monkeypatch.setattr(health, "navidrome_cached", None)


@pytest.mark.parametrize("database,navidrome,expected", [(False, True, 503), (True, False, 503), (True, True, 200)])
async def test_readiness_fails_critical_dependencies_not_optional_embedder(monkeypatch, database, navidrome, expected):
    health.register_background([], expected=0)
    monkeypatch.setattr(health, "database_ok", AsyncMock(return_value=database))
    monkeypatch.setattr(health, "embedder_status", AsyncMock(return_value="degraded"))
    monkeypatch.setattr(system, "_navidrome_ok", AsyncMock(return_value=navidrome))
    response = await system.readyz()
    assert response.status_code == expected
    assert b"degraded" in response.body
    assert b"password" not in response.body


async def test_dead_critical_task_fails_readiness(monkeypatch):
    task = asyncio.create_task(asyncio.sleep(0), name="critical")
    await task
    health.register_background([task], expected=1)
    monkeypatch.setattr(health, "database_ok", AsyncMock(return_value=True))
    monkeypatch.setattr(health, "embedder_status", AsyncMock(return_value="disabled"))
    result = await health.readiness(AsyncMock(return_value=True))
    assert not result["ok"]
    assert health.background_status()["tasks"] == {"critical": "stopped"}


async def test_missing_worker_is_not_healthy(monkeypatch):
    health.register_background([], expected=2)
    assert not health.background_status()["ok"]
    health.register_background([], expected=2, disabled=True)
    assert health.background_status()["status"] == "disabled"


async def test_database_failure_is_sanitized(monkeypatch):
    def broken():
        raise RuntimeError("password=private")

    monkeypatch.setattr(health, "session_scope", broken)
    assert await health.database_ok() is False
    assert await health.queue_status() == {"status": "unavailable"}


def test_disk_failure_does_not_leak_paths(monkeypatch):
    monkeypatch.setattr(health, "get_settings", lambda: SimpleNamespace(data_dir="/private/data", music_root="/private/music"))
    monkeypatch.setattr(health.shutil, "disk_usage", lambda p: (_ for _ in ()).throw(OSError(p)))
    assert health.disk_status() == {"data": {"status": "unavailable"}, "music": {"status": "unavailable"}}
