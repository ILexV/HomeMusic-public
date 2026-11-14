"""A resumed response must prove it contains the remaining bytes of the same file."""

import httpx
import pytest

from app import listen
from app.net import Priority
from app.providers.base import StreamSource
from tests.support import cand

AUDIO = b"abcdefghij"


class Bytes(httpx.AsyncByteStream):
    def __init__(self, body):
        self.body = body

    async def __aiter__(self):
        yield self.body


class Interrupted(httpx.AsyncByteStream):
    def __init__(self, *, complete=False, eof=False, changed=None):
        self.complete, self.eof, self.changed = complete, eof, changed

    async def __aiter__(self):
        yield AUDIO if self.complete else AUDIO[:3]
        if self.changed:
            self.changed()
        if not self.eof:
            raise httpx.RemoteProtocolError("connection lost")


def response(request, first=0, *, interrupted=False, etag='"original"', **kwargs):
    headers = {"content-type": "audio/mpeg", "content-range": f"bytes {first}-9/10",
               "content-length": str(10 - first)}
    if etag is not None:
        headers["etag"] = etag
    if interrupted:
        return httpx.Response(206, headers=headers, stream=Interrupted(**kwargs))
    return httpx.Response(206, headers=headers, stream=Bytes(AUDIO[first:]))


@pytest.mark.parametrize("mode", ["ignored", "wrong_start", "changed_etag", "changed_total", "changed_candidate",
                                  "changed_url", "missing_etag", "weak_etag", "missing_range", "416", "alternate"])
async def test_relay_never_concatenates_unproven_bytes(monkeypatch, mode):
    seen = []
    choice = listen.Choice("dz:1", "full", "Artist", "Song", candidate=cand("1", "https://cdn.test/a.mp3"),
                           alternates=[cand("2", "https://cdn.test/b.mp3")])

    def respond(request):
        seen.append(request)
        if len(seen) == 1:
            etag = None if mode == "missing_etag" else 'W/"original"' if mode == "weak_etag" else '"original"'
            changed = (lambda: setattr(choice, "candidate", choice.alternates[0])) if mode == "changed_candidate" else None
            return response(request, interrupted=True, etag=etag, changed=changed)
        assert request.headers["if-range"] == '"original"'
        assert request.headers["range"] == "bytes=3-9"
        if mode == "ignored":
            return httpx.Response(200, content=AUDIO, headers={"etag": '"original"'})
        if mode == "416":
            return httpx.Response(416, headers={"content-range": "bytes */10"})
        if mode == "alternate":
            return httpx.Response(404)
        if mode == "changed_url" and request.url.path == "/a.mp3":
            return httpx.Response(302, headers={"location": "https://cdn.test/b.mp3"})
        res = response(request, first=0 if mode == "wrong_start" else 3,
                       etag='"different"' if mode == "changed_etag" else '"original"')
        if mode == "changed_total":
            res.headers["content-range"] = "bytes 3-9/11"
        if mode == "missing_range":
            del res.headers["content-range"]
        return res

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = StreamSource("https://cdn.test/a.mp3", client)
        choice.source = source
        monkeypatch.setattr(listen, "_media_client", lambda: client)

        async def renew(_):
            return source

        monkeypatch.setattr(listen, "_upstream", renew)
        res, _ = await listen.open_stream(choice, "bytes=0-")
        received = []
        with pytest.raises(listen.ListenError):
            async for chunk in listen._relay(choice, res):
                received.append(chunk)
        assert b"".join(received) == AUDIO[:3]
        if mode != "changed_candidate":
            assert choice.candidate.provider_id == "1"
        if mode in ("missing_etag", "weak_etag", "changed_candidate"):
            assert len(seen) == 1


@pytest.mark.parametrize("eof", [False, True])
async def test_validated_resume_handles_disconnect_and_short_eof(monkeypatch, eof):
    seen = []

    def respond(request):
        seen.append(request)
        return response(request, interrupted=True, eof=eof) if len(seen) == 1 else response(request, first=3)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        choice = listen.Choice("dz:1", "preview", "Artist", "Song", source=StreamSource("https://cdn.test/a", client))
        monkeypatch.setattr(listen, "_media_client", lambda: client)
        res, src = await listen.open_stream(choice, "bytes=0-")
        media = listen._upstream_media(choice, res, src)
        assert media.headers["content-length"] == "10"
        assert b"".join([part async for part in media.body]) == AUDIO
        assert [r.headers["range"] for r in seen] == ["bytes=0-", "bytes=3-9"]


async def test_complete_body_does_not_resume_past_eof(monkeypatch):
    seen = []

    def respond(request):
        seen.append(request)
        return response(request, interrupted=True, complete=True)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        choice = listen.Choice("dz:1", "preview", "Artist", "Song", source=StreamSource("https://cdn.test/a", client))
        monkeypatch.setattr(listen, "_media_client", lambda: client)
        res, _ = await listen.open_stream(choice, "bytes=0-")
        assert b"".join([part async for part in listen._relay(choice, res)]) == AUDIO
        assert len(seen) == 1


async def test_copy_failure_never_truncates_or_appends_ignored_range(tmp_path, monkeypatch):
    seen = []

    def respond(request):
        seen.append(request)
        if len(seen) == 1:
            return response(request, interrupted=True)
        return httpx.Response(200, content=AUDIO, headers={"etag": '"original"'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        candidate = cand("1", "https://cdn.test/a")
        choice = listen.Choice("dz:1", "full", "Artist", "Song", candidate=candidate,
                               source=StreamSource("https://cdn.test/a", client))
        monkeypatch.setattr(listen, "_media_client", lambda: client)
        cp = listen._Copy(candidate, tmp_path / "copy" / "stream", Priority(False))
        await listen._download_copy(choice, cp)
        assert cp.failed and not cp.done and cp.have == 3 and cp.size == 10
        assert cp.path.read_bytes() == AUDIO[:3]
        media = await listen._from_copy(choice, cp, None, foreground=True)
        assert media.headers["content-length"] == "10"
        received = []
        with pytest.raises(listen.ListenError):
            async for part in media.body:
                received.append(part)
        assert b"".join(received) == AUDIO[:3]
        assert cp.path.read_bytes() == AUDIO[:3]


async def test_oversized_body_is_rejected_before_wrong_bytes_are_sent(monkeypatch):
    def respond(request):
        return httpx.Response(200, headers={"content-length": "3"}, stream=Interrupted(complete=True))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        choice = listen.Choice("dz:1", "preview", "Artist", "Song", source=StreamSource("https://cdn.test/a", client))
        monkeypatch.setattr(listen, "_media_client", lambda: client)
        res, _ = await listen.open_stream(choice, None)
        with pytest.raises(listen.ListenError, match="больше байтов"):
            await anext(listen._relay(choice, res))
