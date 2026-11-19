"""Send the middle of a verified recording the user actually heard to the audio model."""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import BinaryIO

from app import light, listen
from app.config import get_settings
from app.library.probe import ffmpeg_bin, probe
from app.mixes import embedder

# A scheduled job pins one cache file, including if normal cache cleanup unlinks it.
_MAX_PINNED = 16
_slots = asyncio.Semaphore(2)
_pending: dict[str, asyncio.Task] = {}


async def _extract(source: BinaryIO, duration: float) -> bytes:
    if Path(source.name).parent == light.root():
        # The reserve probed the original. For a mobile rendition, verify the audio
        # actually served, not a potentially replaced file at its old pathname.
        info = await probe(f"/proc/{os.getpid()}/fd/{source.fileno()}", timeout=10)
        if info.codec != "opus" or abs(info.duration_s - duration) > get_settings().cfg.matching.duration_tolerance_s:
            raise ValueError("mobile recording does not match the verified duration")
        duration = info.duration_s
    # Linux containers: pass the verified open descriptor, never reopen an evictable path.
    # A seekable output lets ffmpeg finalize both WAV length fields before upload.
    with tempfile.TemporaryFile() as wav:
        fd = source.fileno()
        proc = await asyncio.create_subprocess_exec(
            ffmpeg_bin(), "-nostdin", "-v", "error", "-protocol_whitelist", "file",
            "-format_whitelist", "mp3,mov,matroska,webm,ogg,flac,wav,aac,asf,ape,wv,aiff,mpegts",
            "-ss", str(max(0., (duration - 30) / 2)), "-i", f"/proc/self/fd/{fd}",
            "-map", "0:a:0", "-t", "30", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le",
            "-map_metadata", "-1", "-bitexact", "-f", "wav", "-y", "/proc/self/fd/1",
            pass_fds=(fd,), stdin=asyncio.subprocess.DEVNULL, stdout=wav, stderr=asyncio.subprocess.DEVNULL)
        try:
            async with asyncio.timeout(30):
                if await proc.wait():
                    raise ValueError("cannot decode cached recording")
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        wav.seek(0)
        return wav.read()


async def _send(ref: str, source: BinaryIO, duration: float) -> None:
    async with _slots, embedder.EmbedderClient() as client:
        if not await client.has_vector_or_clip(ref):
            await client.submit_clip(ref, await _extract(source, duration))


def schedule(user_id: int, ref: str) -> bool:
    """Never resolve or fetch media. A wave refill can retry if a source or slot wasn't ready."""
    if ref in _pending:
        return True
    if len(_pending) >= _MAX_PINNED or not embedder.configured():
        return False
    heard = listen.open_heard_wave_recording(user_id, ref)
    if heard is None:
        return False
    source, duration = heard
    task = listen._spawn(_send(ref, source, duration), background=True)
    _pending[ref] = task

    def release(_task: asyncio.Task) -> None:
        # Also runs if shutdown cancels the task before its coroutine even starts.
        source.close()
        _pending.pop(ref, None)

    task.add_done_callback(release)
    return True
