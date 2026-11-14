from types import SimpleNamespace

from app.worker import Worker


def test_worker_distinguishes_idle_loading_and_stuck_batch(monkeypatch):
    monkeypatch.setattr("app.worker.time.monotonic", lambda: 2000)
    engine = SimpleNamespace(state="idle")
    worker = Worker(SimpleNamespace(downloads=1), None, engine, None, 8000)
    worker.last_progress = 0
    assert worker.health()["phase"] == "idle"
    assert not worker.health()["stalled"]
    engine.state = "loading"
    worker.activity["preparing"] = 100
    assert worker.health()["phase"] == "loading"
    assert not worker.health()["stalled"]
    engine.state = "ready"
    worker.activity["embedding"] = 100
    assert worker.health()["phase"] == "embedding"
    assert worker.health()["stalled"]
    worker.last_progress = 1990
    assert not worker.health()["stalled"]
