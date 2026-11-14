"""No real network: exercise redirects, pinned DNS/peers and yt-dlp's HTTP boundary."""
import socket

import httpcore
import httpx
import pytest
from httpcore._backends.auto import AutoBackend
from httpcore._backends.sync import SyncBackend
from yt_dlp.networking.common import Request
from yt_dlp.networking.exceptions import TransportError

from app.external import PublicAsyncBackend, PublicAsyncTransport, PublicSyncBackend, UnsafeExternalURL, public_ip
from app.providers.external_ytdlp import PublicRH


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1", "169.254.169.254",
                                    "0.0.0.0", "100.64.0.1", "224.0.0.1", "::1", "fe80::1", "fc00::1",
                                    "::ffff:127.0.0.1", "64:ff9b::7f00:1", "2002:7f00:1::"])
def test_non_public_addresses_are_rejected(address):
    with pytest.raises(UnsafeExternalURL):
        public_ip(address)


class Stream(httpcore.AsyncNetworkStream):
    def __init__(self, peer="93.184.216.34", reply=b""):
        self.peer, self.reply, self.closed = peer, reply, False
        self.writes = []

    async def read(self, max_bytes, timeout=None):
        data, self.reply = self.reply, b""
        return data

    async def write(self, buffer, timeout=None):
        self.writes.append(buffer)

    async def aclose(self):
        self.closed = True

    def get_extra_info(self, info):
        return (self.peer, 80) if info == "server_addr" else False if info == "is_readable" else None


def answer(address):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 80))]


async def test_dns_is_pinned_and_peer_checked_before_http(monkeypatch):
    queries, connections = [], []
    stream = Stream()

    def resolve(host, port, **kwargs):
        queries.append(host)
        return answer("93.184.216.34" if len(queries) == 1 else "127.0.0.1")

    async def connect(self, host, port, *args):
        connections.append(host)
        return stream

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(AutoBackend, "connect_tcp", connect)
    assert await PublicAsyncBackend().connect_tcp("cdn.test", 80) is stream
    assert queries == ["cdn.test"] and connections == ["93.184.216.34"]
    assert stream.writes == []


async def test_unexpected_peer_is_closed_before_sending(monkeypatch):
    stream = Stream(peer="127.0.0.1")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: answer("93.184.216.34"))

    async def connect(*args):
        return stream

    monkeypatch.setattr(AutoBackend, "connect_tcp", connect)
    with pytest.raises(UnsafeExternalURL):
        await PublicAsyncBackend().connect_tcp("cdn.test", 80)
    assert stream.closed and not stream.writes


@pytest.mark.parametrize("location", ["http://127.0.0.1/private", "http://internal.test/private"])
async def test_redirect_revalidates_before_connecting(monkeypatch, location):
    connections = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k:
                        answer("127.0.0.1" if host == "internal.test" else "93.184.216.34"))

    async def connect(self, host, port, *args):
        connections.append(host)
        return Stream(reply=f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\n\r\n".encode())

    monkeypatch.setattr(AutoBackend, "connect_tcp", connect)
    async with httpx.AsyncClient(transport=PublicAsyncTransport(), follow_redirects=True) as client:
        with pytest.raises(UnsafeExternalURL):
            await client.get("http://public.test/audio")
    assert connections == ["93.184.216.34"]


async def test_mixed_dns_answer_set_rejected_without_connection(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: answer("93.184.216.34") + answer("10.0.0.1"))

    async def connect(*args):
        pytest.fail("No connection may use a mixed public/private DNS answer set")

    monkeypatch.setattr(AutoBackend, "connect_tcp", connect)
    with pytest.raises(UnsafeExternalURL):
        await PublicAsyncBackend().connect_tcp("mixed.test", 80)


def test_sync_downloader_pins_dns_and_checks_peer(monkeypatch):
    connections = []

    class SyncStream:
        def get_extra_info(self, info):
            return ("93.184.216.34", 443)

    def connect(self, host, port, *args):
        connections.append(host)
        return SyncStream()

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: answer("93.184.216.34"))
    monkeypatch.setattr(SyncBackend, "connect_tcp", connect)
    PublicSyncBackend().connect_tcp("cdn.test", 443)
    assert connections == ["93.184.216.34"]


def test_ytdlp_cannot_fetch_private_media():
    with PublicRH(logger=None) as handler:
        with pytest.raises(TransportError):
            handler.send(Request("http://169.254.169.254/latest/meta-data"))


async def test_proxy_receives_pinned_destination_and_original_tls_name(monkeypatch):
    seen = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: answer("93.184.216.34"))

    async def send(self, request):
        seen.append(request)
        return httpx.Response(200, content=b"audio")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    async with httpx.AsyncClient(transport=PublicAsyncTransport(proxy="http://proxy.test:8080")) as client:
        await client.get("https://cdn.test/audio")
    assert seen[0].url.host == "93.184.216.34"
    assert seen[0].headers["host"] == "cdn.test"
    assert seen[0].extensions["sni_hostname"] == "cdn.test"


def test_public_media_redirect_cannot_enter_configured_token_service():
    import respx

    with respx.mock(assert_all_called=False) as mock, PublicRH(logger=None) as handler:
        handler.token_service = "http://127.0.0.1:4416"
        mock.get("https://cdn.test/audio").respond(302, headers={"Location": handler.token_service + "/ping"})
        private = mock.get(handler.token_service + "/ping").respond(200, json={"version": "test"})
        with pytest.raises(TransportError):
            handler.send(Request("https://cdn.test/audio"))
        assert not private.called


def test_configured_token_service_never_follows_redirects():
    import respx

    with respx.mock(assert_all_called=False) as mock, PublicRH(logger=None) as handler:
        handler.token_service = "http://127.0.0.1:4416"
        mock.get(handler.token_service + "/ping").respond(302, headers={"Location": "http://127.0.0.1/private"})
        private = mock.get("http://127.0.0.1/private").respond(200)
        response = handler.send(Request(handler.token_service + "/ping"))
        assert response.status == 302 and not private.called
        response.close()


async def test_proxy_dns_obeys_connect_timeout_before_sending(monkeypatch):
    import threading

    from app import external

    release = threading.Event()

    def resolve(host, port):
        release.wait(1)
        return ["93.184.216.34"]

    async def send(self, request):
        pytest.fail("A timed-out lookup must not send a proxy request")

    monkeypatch.setattr(external, "resolve_public", resolve)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    try:
        async with httpx.AsyncClient(transport=PublicAsyncTransport(proxy="http://proxy.test:8080"),
                                     timeout=httpx.Timeout(1, connect=0.01)) as client:
            with pytest.raises(httpx.ConnectTimeout) as error:
                await client.get("https://cdn.test/audio")
            assert str(error.value.request.url) == "https://cdn.test/audio"
    finally:
        release.set()
