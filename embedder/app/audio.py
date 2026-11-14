"""Getting audio: download a URL, resolve a library path, decode a fragment to mono float32 with ffmpeg."""

import asyncio
import json
import tempfile
from pathlib import Path

import httpx
import numpy as np

MIN_SECONDS = 1.0
PROCESS_TIMEOUT_SECONDS = 60
PROCESS_TERMINATE_SECONDS = 3


class AudioError(Exception):
    """Can't get audio for an item. `retry` — the error is transient (network, server) and worth repeating."""

    def __init__(self, message: str, *, retry: bool = False):
        super().__init__(message)
        self.retry = retry


def library_path(music_root: Path, path: str) -> Path:
    """A path relative to the library (or absolute inside it). Anything that leaves the library is refused."""
    root = music_root.resolve()
    p = Path(path)
    full = (p if p.is_absolute() else root / p).resolve()
    if not full.is_relative_to(root):
        raise AudioError(f"path outside the library: {path}")
    return full


async def download(client: httpx.AsyncClient, url: str, dest: Path, *, max_bytes: int) -> None:
    try:
        async with client.stream("GET", url) as r:
            if r.status_code >= 500 or r.status_code == 429:
                raise AudioError(f"HTTP {r.status_code}", retry=True)
            if r.status_code >= 400:
                raise AudioError(f"HTTP {r.status_code}")
            size = 0
            with dest.open("wb") as f:
                async for chunk in r.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise AudioError(f"larger than {max_bytes // 2**20} MB")
                    f.write(chunk)
    except httpx.HTTPError as e:
        raise AudioError(f"download failed: {type(e).__name__}: {e}", retry=True) from e


async def _run(*args: str) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    communicate = asyncio.create_task(proc.communicate())
    try:
        out, err = await asyncio.wait_for(asyncio.shield(communicate), PROCESS_TIMEOUT_SECONDS)
        return proc.returncode or 0, out, err
    except (TimeoutError, asyncio.CancelledError) as exc:
        # Keep draining pipes while stopping the child: wait() alone can deadlock on a full stdout pipe.
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(asyncio.shield(communicate), PROCESS_TERMINATE_SECONDS)
        except TimeoutError:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await communicate
        finally:
            await proc.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise AudioError("audio subprocess timed out", retry=True) from None


async def duration(path: Path) -> float | None:
    code, out, _ = await _run(
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)
    )
    if code != 0:
        return None
    try:
        return float(json.loads(out)["format"]["duration"])
    except (KeyError, ValueError, TypeError):
        return None


async def decode(path: Path, *, sr: int, clip_s: float) -> np.ndarray:
    """`clip_s` seconds from the middle of the file (all of it if shorter), mono, `sr` Hz, float32."""
    total = await duration(path)
    start = max(0.0, (total - clip_s) / 2) if total else 0.0
    code, out, err = await _run(
        "ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{clip_s:.3f}", "-i", str(path),
        "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-",
    )
    if code != 0:
        raise AudioError(f"decode failed: {err.decode(errors='replace').strip()[-300:]}")
    wav = np.frombuffer(out, dtype=np.float32)
    if wav.size < sr * MIN_SECONDS:
        raise AudioError(f"too short: {wav.size / sr:.1f} s")
    return wav


async def load(
    client: httpx.AsyncClient, kind: str, source: str, *, music_root: Path, sr: int, clip_s: float, max_bytes: int
) -> np.ndarray:
    if kind == "path":
        p = library_path(music_root, source)
        if not p.is_file():
            raise AudioError(f"no such file: {source}")
        return await decode(p, sr=sr, clip_s=clip_s)
    with tempfile.TemporaryDirectory(prefix="emb-") as tmp:
        f = Path(tmp) / "audio"
        await download(client, source, f, max_bytes=max_bytes)
        return await decode(f, sr=sr, clip_s=clip_s)
