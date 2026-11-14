"""Public Internet transports: pin DNS answers before connecting, never after sending HTTP."""
from __future__ import annotations

import asyncio
import ipaddress
import socket

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend
from httpcore._backends.sync import SyncBackend


class UnsafeExternalURL(httpx.RequestError):
    """An external resource resolved to a non-public destination."""


def public_ip(value: str) -> str:
    if "%" in value:
        raise UnsafeExternalURL("Scoped external addresses are not supported")
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        raise UnsafeExternalURL("Invalid external IP address") from None
    mapped = getattr(ip, "ipv4_mapped", None)
    if not ip.is_global or ip.is_multicast or (mapped is not None and not mapped.is_global):
        raise UnsafeExternalURL("External resources must use public Internet addresses")
    # IPv6 transition mechanisms can tunnel a non-public IPv4 destination.
    if ip.version == 6 and (ip in ipaddress.ip_network("64:ff9b::/96") or
                            ip in ipaddress.ip_network("64:ff9b:1::/48")):
        raise UnsafeExternalURL("IPv6 translation addresses are not supported")
    if getattr(ip, "sixtofour", None) is not None or getattr(ip, "teredo", None) is not None:
        raise UnsafeExternalURL("IPv6 transition addresses are not supported")
    return str(ip)


def validate_url(url: httpx.URL) -> None:
    if url.scheme not in ("http", "https") or not url.host or url.userinfo:
        raise UnsafeExternalURL("External resources require an HTTP(S) URL without credentials")
    try:
        ipaddress.ip_address(url.host)
    except ValueError:
        if "%" in url.host:
            raise UnsafeExternalURL("Scoped external addresses are not supported") from None
    else:
        public_ip(url.host)


def resolve_public(host: str, port: int) -> list[str]:
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise httpcore.ConnectError("External hostname could not be resolved") from exc
    # Reject mixed public/private answer sets, rather than depending on address ordering.
    addresses = list(dict.fromkeys(public_ip(answer[4][0]) for answer in answers))
    if not addresses:
        raise httpcore.ConnectError("External hostname has no addresses")
    return addresses


def check_peer(stream, expected: str) -> None:
    peer = stream.get_extra_info("server_addr")
    if not peer or public_ip(peer[0]) != expected:
        raise UnsafeExternalURL("External connection peer did not match its pinned address")


class PublicAsyncBackend(AutoBackend):
    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            addresses = await asyncio.wait_for(asyncio.to_thread(resolve_public, host, port), timeout)
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout("External hostname resolution timed out") from exc
        error = None
        for address in addresses:
            try:
                stream = await super().connect_tcp(address, port, timeout, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                error = exc
                continue
            try:
                check_peer(stream, address)
            except BaseException:
                await stream.aclose()
                raise
            return stream
        raise error or httpcore.ConnectError("External connection failed")


class PublicSyncBackend(SyncBackend):
    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        error = None
        for address in resolve_public(host, port):
            try:
                stream = super().connect_tcp(address, port, timeout, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                error = exc
                continue
            try:
                check_peer(stream, address)
            except BaseException:
                stream.close()
                raise
            return stream
        raise error or httpcore.ConnectError("External connection failed")


class PublicAsyncTransport(httpx.AsyncHTTPTransport):
    def __init__(self, *, http2=True, proxy=None):
        super().__init__(http2=http2, proxy=proxy, trust_env=False,
                         limits=httpx.Limits(max_keepalive_connections=0) if proxy else httpx.Limits())
        self._proxy = proxy
        if proxy is None:
            # httpx 0.28 has no public backend injection; keep the version-specific adapter here.
            self._pool._network_backend = PublicAsyncBackend()

    async def handle_async_request(self, request):
        validate_url(request.url)
        if self._proxy is not None:
            # CONNECT must name a pinned IP: otherwise the proxy can resolve the hostname differently.
            host = request.url.host
            timeout = request.extensions.get("timeout", {}).get("connect")
            port = request.url.port or (443 if request.url.scheme == "https" else 80)
            try:
                addresses = await asyncio.wait_for(asyncio.to_thread(resolve_public, host, port), timeout)
            except (TimeoutError, httpcore.ConnectTimeout) as exc:
                raise httpx.ConnectTimeout("External hostname resolution timed out", request=request) from exc
            except httpcore.ConnectError as exc:
                raise httpx.ConnectError(str(exc), request=request) from exc
            request = httpx.Request(request.method, request.url.copy_with(host=addresses[0]),
                                    headers=request.headers, stream=request.stream,
                                    extensions={**request.extensions, "sni_hostname": host})
        return await super().handle_async_request(request)


class PublicSyncTransport(httpx.HTTPTransport):
    def __init__(self):
        super().__init__(trust_env=False)
        self._pool._network_backend = PublicSyncBackend()

    def handle_request(self, request):
        validate_url(request.url)
        return super().handle_request(request)
