"""SQLite store: the embedding queue and the vectors in one table (survives restarts)."""

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import clip

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,            -- url | path | clip
    source TEXT NOT NULL,
    status TEXT NOT NULL,          -- queued | running | done | error
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    not_before REAL NOT NULL DEFAULT 0,
    model TEXT,
    dim INTEGER,
    vector BLOB,
    seconds REAL,                  -- length of the embedded fragment
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS items_queue ON items (status, not_before, created_at);
"""

QUEUE_ITEM = (
    "INSERT INTO items (key, kind, source, status, created_at, updated_at) VALUES (?,?,?,'queued',?,?)"
    " ON CONFLICT(key) DO UPDATE SET kind=excluded.kind, source=excluded.source, status='queued',"
    " error=NULL, attempts=0, not_before=0, updated_at=excluded.updated_at"
)


@dataclass
class Item:
    key: str
    kind: str
    source: str
    status: str
    error: str | None
    attempts: int
    model: str | None
    seconds: float | None
    updated_at: float


def to_blob(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float32).tobytes()


def from_blob(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float32)


class Store:
    def __init__(self, path: Path, now=time.time):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()
        self.clip_dir = path.parent / "pending-clips"
        self.clip_dir.mkdir(mode=0o700, exist_ok=True)
        self._clip_keys: set[str] = set()
        self.now = now
        # bumps on every new vector: the in-memory index for /text rebuilds when it changes
        self.version = 0

    def close(self) -> None:
        self._db.close()

    def recover(self) -> int:
        """Reconcile the private spool after a restart, then resume interrupted work."""
        with self._lock:
            n = self._db.execute("UPDATE items SET status='queued' WHERE status='running'").rowcount
            self._clip_keys.clear()
            files = set(self.clip_dir.glob("*.wav"))
            (self.clip_dir / ".incoming").unlink(missing_ok=True)
            if files:
                # A clip attached to an interrupted preview can now run instead. Completed/never
                # enqueued keys are orphans (a crash between the SQLite and filesystem operations).
                promote = []
                for key, kind in self._db.execute("SELECT key, kind FROM items WHERE status='queued'"):
                    path = clip.clip_path(self.clip_dir, key)
                    if path in files:
                        files.remove(path)
                        self._clip_keys.add(key)
                        if kind != "clip":
                            promote.append((key,))
                        if not files:
                            break
                self._db.executemany(
                    "UPDATE items SET kind='clip', source='', attempts=0, error=NULL, not_before=0 WHERE key=?", promote
                )
                for path in files:
                    path.unlink()
            return n

    def has_clip(self, key: str) -> bool:
        with self._lock:
            return key in self._clip_keys and clip.clip_path(self.clip_dir, key).is_file()

    def enqueue_clip(self, key: str, frames: memoryview, model: str) -> tuple[int, int]:
        """Keep current vectors. Replace queued previews; keep a fallback for already-running work."""
        with self._lock:
            item = self.get(key)
            path = clip.clip_path(self.clip_dir, key)
            if (item and item.status == "done" and item.model == model) or (key in self._clip_keys and path.is_file()):
                return 0, 1
            if key not in self._clip_keys and len(self._clip_keys) >= clip.MAX_PENDING:
                raise clip.ClipQueueFull
            try:
                clip.publish(path, frames)
                if not item or item.status != "running":
                    t = self.now()
                    self._db.execute(QUEUE_ITEM, (key, "clip", "", t, t))
                    self.version += 1
                self._clip_keys.add(key)
            except Exception:
                path.unlink(missing_ok=True)
                raise
            return 1, 0

    def _discard_clip(self, key: str) -> None:
        if key in self._clip_keys:
            clip.clip_path(self.clip_dir, key).unlink(missing_ok=True)
            self._clip_keys.remove(key)

    def enqueue(self, items: list[tuple[str, str, str]], *, force: bool, model: str) -> tuple[int, int]:
        """(key, kind, source) → queued. An item already embedded by this model from the same source is kept
        unless `force`; one that failed is retried. Returns (queued, known)."""
        queued = known = 0
        t = self.now()
        with self._lock:
            self._db.execute("BEGIN")
            for key, kind, source in items:
                if key in self._clip_keys:
                    known += 1
                    continue
                row = self._db.execute("SELECT status, kind, source, model FROM items WHERE key=?", (key,)).fetchone()
                if row and not force:
                    status, old_kind, old_source, old_model = row
                    if status in ("queued", "running") or (
                        status == "done" and (old_kind == "clip" or old_source == source) and old_model == model
                    ):
                        known += 1
                        continue
                self._db.execute(QUEUE_ITEM, (key, kind, source, t, t))
                queued += 1
            self._db.execute("COMMIT")
            if queued:
                # Requeueing a done item removes it from the current geometry too.
                self.version += 1
        return queued, known

    def claim(self, n: int) -> list[Item]:
        """Up to n items that are due, oldest first, marked running."""
        with self._lock:
            rows = self._db.execute(
                "SELECT key FROM items WHERE status='queued' AND not_before<=? ORDER BY created_at LIMIT ?",
                (self.now(), n),
            ).fetchall()
            keys = [r[0] for r in rows]
            if keys:
                self._db.executemany("UPDATE items SET status='running' WHERE key=?", [(k,) for k in keys])
        return [it for k in keys if (it := self.get(k))]

    def release(self, keys: list[str]) -> None:
        """Back to the queue without spending an attempt (the model could not be loaded)."""
        with self._lock:
            self._db.executemany("UPDATE items SET status='queued' WHERE key=? AND status='running'", [(k,) for k in keys])

    def finish(self, key: str, vector: np.ndarray, model: str, seconds: float) -> None:
        with self._lock:
            updated = self._db.execute(
                "UPDATE items SET status='done', error=NULL, model=?, dim=?, vector=?, seconds=?, updated_at=?"
                " WHERE key=? AND status='running'",
                (model, int(vector.shape[0]), to_blob(vector), seconds, self.now(), key),
            ).rowcount
            if updated:
                self._discard_clip(key)
            self.version += 1

    def fail(self, key: str, error: str, *, retry: bool, max_attempts: int, retry_s: float) -> None:
        """A transient error goes back to the queue after a pause while attempts last; anything else is final."""
        with self._lock:
            row = self._db.execute("SELECT attempts, kind FROM items WHERE key=? AND status='running'", (key,)).fetchone()
            if not row:
                return
            if row[1] != "clip" and key in self._clip_keys:
                # The in-flight source failed; the uploaded recording gets its own attempt budget.
                t = self.now()
                self._db.execute(QUEUE_ITEM, (key, "clip", "", t, t))
                return
            attempts = row[0] + 1
            if retry and attempts < max_attempts:
                self._db.execute(
                    "UPDATE items SET status='queued', error=?, attempts=?, not_before=?, updated_at=? WHERE key=?",
                    (error, attempts, self.now() + retry_s * attempts, self.now(), key),
                )
            else:
                self._db.execute(
                    "UPDATE items SET status='error', error=?, attempts=?, updated_at=? WHERE key=?",
                    (error, attempts, self.now(), key),
                )
                self._discard_clip(key)

    def get(self, key: str) -> Item | None:
        row = self._db.execute(
            "SELECT key, kind, source, status, error, attempts, model, seconds, updated_at FROM items WHERE key=?",
            (key,),
        ).fetchone()
        return Item(*row) if row else None

    def vectors(self, keys: list[str], model: str) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        for i in range(0, len(keys), 500):
            chunk = keys[i : i + 500]
            q = ",".join("?" * len(chunk))
            for key, blob in self._db.execute(
                f"SELECT key, vector FROM items WHERE status='done' AND model=? AND key IN ({q})", (model, *chunk)
            ):
                out[key] = from_blob(blob)
        return out

    def all_vectors(self, model: str) -> tuple[list[str], np.ndarray | None]:
        keys, vecs = [], []
        for key, blob in self._db.execute("SELECT key, vector FROM items WHERE status='done' AND model=?", (model,)):
            keys.append(key)
            vecs.append(from_blob(blob))
        return keys, (np.stack(vecs) if vecs else None)

    def geometry_snapshot(self, model: str) -> tuple[int, list[tuple[str, np.ndarray]]]:
        """Read one revision atomically with worker writes; malformed vectors are handled by v2."""
        with self._lock:
            rows = self._db.execute(
                "SELECT key, vector FROM items WHERE status='done' AND model=? ORDER BY key", (model,)
            ).fetchall()
            return self.version, [(key, from_blob(blob).copy()) for key, blob in rows]

    def counts(self) -> dict[str, int]:
        counts = {"queued": 0, "running": 0, "done": 0, "error": 0}
        for status, n in self._db.execute("SELECT status, count(*) FROM items GROUP BY status"):
            counts[status] = n
        return counts

    def pending(self) -> int:
        """Queued items due now (the worker's 'is there work' check)."""
        return self._db.execute(
            "SELECT count(*) FROM items WHERE status='queued' AND not_before<=?", (self.now(),)
        ).fetchone()[0]
