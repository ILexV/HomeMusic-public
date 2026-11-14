"""yt-dlp's native HTTP/HLS downloader uses the same public-address boundary as httpx media."""
from __future__ import annotations

import io

import httpx
from yt_dlp.networking.common import Features, RequestHandler, Response
from yt_dlp.networking.exceptions import HTTPError, TransportError

from app.external import PublicSyncTransport


class _Body(io.RawIOBase):
    def __init__(self, response: httpx.Response):
        self.response = response
        self.chunks = response.iter_bytes()
        self.pending = memoryview(b"")

    def readable(self):
        return True

    def readinto(self, buffer):
        while not self.pending:
            try:
                self.pending = memoryview(next(self.chunks))
            except StopIteration:
                return 0
            except httpx.HTTPError as exc:
                raise TransportError(cause=exc) from exc
        count = min(len(buffer), len(self.pending))
        buffer[:count] = self.pending[:count]
        self.pending = self.pending[count:]
        return count

    def close(self):
        self.response.close()
        super().close()


class PublicRH(RequestHandler):
    _SUPPORTED_URL_SCHEMES = ("http", "https")
    _SUPPORTED_FEATURES = (Features.ALL_PROXY, Features.NO_PROXY)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.clients: dict[int, httpx.Client] = {}
        self.token_service: str | None = None
        self.token_client: httpx.Client | None = None

    def _check_extensions(self, extensions):
        for key in ("cookiejar", "timeout", "legacy_ssl", "keep_header_casing"):
            extensions.pop(key, None)

    def _send(self, request):
        jar = self._get_cookiejar(request)
        client = self.clients.get(id(jar))
        # The operator's token service is infrastructure, not an external media source.
        # Only these exact operations are trusted, and they never follow redirects.
        token_operation = self.token_service is not None and (
            (request.method == "GET" and request.url == self.token_service + "/ping") or
            (request.method == "POST" and request.url == self.token_service + "/get_pot"))
        if token_operation:
            if self.token_client is None:
                self.token_client = httpx.Client(trust_env=False, follow_redirects=False)
            client = self.token_client
        elif client is None:
            client = httpx.Client(transport=PublicSyncTransport(), cookies=jar, follow_redirects=True, trust_env=False)
            self.clients[id(jar)] = client
        try:
            req = client.build_request(request.method, request.url, headers=self._get_headers(request),
                                       content=request.data, timeout=self._calculate_timeout(request))
            result = client.send(req, stream=True)
        except httpx.HTTPError as exc:
            raise TransportError(cause=exc) from exc
        response = Response(io.BufferedReader(_Body(result)), str(result.url), result.headers,
                            status=result.status_code, reason=result.reason_phrase)
        if result.status_code >= 400:
            raise HTTPError(response)
        return response

    def close(self):
        for client in self.clients.values():
            client.close()
        self.clients.clear()
        if self.token_client is not None:
            self.token_client.close()
