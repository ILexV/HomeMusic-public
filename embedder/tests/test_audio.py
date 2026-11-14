from pathlib import Path

import numpy as np
import pytest

from app import audio

from .support import tone


@pytest.fixture(scope="module")
def long_file(tmp_path_factory) -> Path:
    """4 s: 1 s of 200 Hz, 2 s of 1000 Hz, 1 s of 200 Hz — the middle is the 1000 Hz part."""
    import wave

    sr = 8000
    t1, t2 = np.arange(sr) / sr, np.arange(2 * sr) / sr
    sig = np.concatenate([np.sin(2 * np.pi * 200 * t1), np.sin(2 * np.pi * 1000 * t2), np.sin(2 * np.pi * 200 * t1)])
    f = tmp_path_factory.mktemp("a") / "long.wav"
    with wave.open(str(f), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((0.5 * sig * 32767).astype("<i2").tobytes())
    return f


def peak_hz(wav, sr):
    return np.argmax(np.abs(np.fft.rfft(wav))) * sr / len(wav)


async def test_fragment_is_taken_from_the_middle(long_file):
    wav = await audio.decode(long_file, sr=8000, clip_s=1.0)
    assert len(wav) == 8000 and wav.dtype == np.float32
    assert peak_hz(wav, 8000) == pytest.approx(1000, abs=5)


async def test_short_file_is_taken_whole_and_resampled(tmp_path):
    f = tmp_path / "p.wav"
    f.write_bytes(tone(440, seconds=2))
    wav = await audio.decode(f, sr=24000, clip_s=30)
    assert len(wav) == pytest.approx(48000, abs=50)
    assert peak_hz(wav, 24000) == pytest.approx(440, abs=2)


@pytest.mark.parametrize(
    "path,ok",
    [("a/b.mp3", True), ("a/../b.mp3", True), ("../x.mp3", False), ("/etc/passwd", False)],
    ids=["relative", "dotdot-inside", "escape", "absolute-outside"],
)
def test_library_path_stays_inside(tmp_path, path, ok):
    if ok:
        assert audio.library_path(tmp_path, path).is_relative_to(tmp_path.resolve())
    else:
        with pytest.raises(audio.AudioError):
            audio.library_path(tmp_path, path)


@pytest.mark.parametrize("cancel", [False, True])
async def test_hung_subprocess_is_terminated_killed_and_reaped(monkeypatch, cancel):
    import asyncio

    class Child:
        returncode = None

        def __init__(self):
            self.started = asyncio.Event()
            self.stopped = asyncio.Event()
            self.terminated = self.killed = self.waited = False

        async def communicate(self):
            self.started.set()
            await self.stopped.wait()
            return b"", b""

        def terminate(self):
            self.terminated = True  # emulate a process ignoring SIGTERM

        def kill(self):
            self.killed = True
            self.returncode = -9
            self.stopped.set()

        async def wait(self):
            self.waited = True
            await self.stopped.wait()

    child = Child()

    async def spawn(*args, **kwargs):
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(audio, "PROCESS_TIMEOUT_SECONDS", 60 if cancel else 0.01)
    monkeypatch.setattr(audio, "PROCESS_TERMINATE_SECONDS", 0.01)
    task = asyncio.create_task(audio._run("ffmpeg"))
    await child.started.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else audio.AudioError):
        await task
    assert child.terminated and child.killed and child.waited
