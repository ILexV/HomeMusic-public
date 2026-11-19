"""The Hub's bounded PCM uploads. Only canonical WAVs, never client paths, reach ffmpeg."""

import hashlib
import os
import struct
import wave
from pathlib import Path

from .audio import MIN_SECONDS, AudioError

SAMPLE_RATE = 24_000
MAX_FRAMES = 30 * SAMPLE_RATE
MAX_BYTES = 1536 * 1024
# Bound disk usage even if inference stays offline indefinitely (<177 MiB of canonical WAVs).
MAX_PENDING = 128


class ClipQueueFull(Exception):
    pass


def pcm_frames(body: bytearray) -> memoryview:
    """Validate a finalized RIFF/WAVE, dropping optional metadata rather than passing it to ffmpeg."""
    if len(body) < 12 or body[:4] != b"RIFF" or body[8:12] != b"WAVE":
        raise AudioError("invalid WAV")
    if struct.unpack_from("<I", body, 4)[0] != len(body) - 8:
        raise AudioError("invalid RIFF length")
    pos = 12
    has_format = False
    frames = None
    while pos < len(body):
        if pos + 8 > len(body):
            raise AudioError("truncated WAV chunk")
        tag, size = struct.unpack_from("<4sI", body, pos)
        start = pos + 8
        pos = start + size + (size & 1)
        if pos > len(body):
            raise AudioError("truncated WAV chunk")
        if tag == b"fmt ":
            if has_format or size < 16 or struct.unpack_from("<HHIIHH", body, start) != (
                1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16,
            ):
                raise AudioError("expected mono 24 kHz 16-bit PCM WAV")
            has_format = True
        elif tag == b"data":
            if not has_format or frames is not None or size % 2 or not SAMPLE_RATE * MIN_SECONDS <= size // 2 <= MAX_FRAMES:
                raise AudioError("expected 1 to 30 seconds of PCM")
            frames = memoryview(body)[start:start + size]
    if frames is None:
        raise AudioError("missing WAV data")
    return frames


def clip_path(directory: Path, key: str) -> Path:
    return directory / (hashlib.sha256(key.encode()).hexdigest() + ".wav")


def publish(path: Path, frames: memoryview) -> None:
    # Single writer under the queue lock. A crash leaves at most one staging file, never a partial job.
    staging = path.parent / ".incoming"
    try:
        with staging.open("wb") as f:
            with wave.open(f, "wb") as wav:
                wav.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
                wav.writeframes(frames)
            f.flush()
            os.fsync(f.fileno())
        staging.replace(path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        staging.unlink(missing_ok=True)
