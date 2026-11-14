"""Streaming multipart limits shared by authenticated upload and assistant routes."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import HTTPException, Request
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import parse_options_header
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.ai.limits import MAX_IMAGES
from app.config import get_settings

MIB = 1024 * 1024
MULTIPART_OVERHEAD = MIB
MAX_IMAGE_MB = 15
MAX_HEADER_BYTES = 16 * 1024


@dataclass(frozen=True)
class MultipartLimits:
    max_files: int
    max_file_bytes: int
    max_total_bytes: int
    max_fields: int = 0

    @property
    def body_bytes(self) -> int:
        return self.max_total_bytes + MULTIPART_OVERHEAD


def upload_limits() -> MultipartLimits:
    cfg = get_settings().cfg.uploads
    return MultipartLimits(cfg.max_files, cfg.max_file_mb * MIB, cfg.max_total_mb * MIB)


ASSISTANT_LIMITS = MultipartLimits(MAX_IMAGES, MAX_IMAGE_MB * MIB, MAX_IMAGES * MAX_IMAGE_MB * MIB, 2)
COVER_LIMITS = MultipartLimits(1, MAX_IMAGE_MB * MIB, MAX_IMAGE_MB * MIB, 1)


class BodyTooLarge(HTTPException):
    def __init__(self, detail: str = "Превышен размер запроса") -> None:
        super().__init__(413, detail)


class UploadBodyLimitMiddleware:
    """Count receive chunks without pre-reading, buffering, or draining rejected bodies.

    The first receive happens *after* route authentication. In particular, even a
    declared oversized unauthenticated upload returns 401 without reading its body.
    Lifespan, websocket and unrelated HTTP traffic pass through unchanged.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "").rstrip("/")
        if scope["type"] != "http" or scope.get("method") != "POST" or path not in {
            "/api/uploads", "/api/uploads/commit", "/api/assistant/tasks", "/api/assistant/chat",
        }:
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])
        content_type = next((v for k, v in headers if k.lower() == b"content-type"), b"")
        if content_type.split(b";", 1)[0].strip().lower() != b"multipart/form-data":
            await self.app(scope, receive, send)
            return

        received = 0
        limit: int | None = None
        started = False

        async def bounded_receive() -> Message:
            nonlocal received, limit
            if limit is None:
                limits = (upload_limits() if path == "/api/uploads" else
                          COVER_LIMITS if path == "/api/uploads/commit" else ASSISTANT_LIMITS)
                limit = limits.body_bytes
                for name, value in headers:
                    if name.lower() == b"content-length":
                        try:
                            declared = int(value)
                        except ValueError:
                            raise HTTPException(400, "Некорректный Content-Length") from None
                        if declared < 0:
                            raise HTTPException(400, "Некорректный Content-Length")
                        if declared > limit:
                            raise BodyTooLarge()
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise BodyTooLarge()
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, bounded_receive, tracked_send)
        except BodyTooLarge as exc:
            if started:
                raise
            await JSONResponse({"detail": exc.detail}, status_code=413)(scope, receive, send)


class _BoundedMultiPartParser(MultiPartParser):
    """Enforce limits in callbacks, before Starlette queues writes to temp files."""

    def __init__(self, request: Request, limits: MultipartLimits) -> None:
        super().__init__(request.headers, request.stream(), max_files=limits.max_files,
                         max_fields=limits.max_fields, max_part_size=MULTIPART_OVERHEAD)
        self.limits = limits
        self.file_bytes = 0
        self.total_bytes = 0
        self.header_bytes = 0
        self.complete = False

    def on_part_begin(self) -> None:
        super().on_part_begin()
        self.file_bytes = 0
        self.header_bytes = 0

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._count_header(end - start)
        super().on_header_field(data, start, end)

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._count_header(end - start)
        super().on_header_value(data, start, end)

    def _count_header(self, size: int) -> None:
        self.header_bytes += size
        if self.header_bytes > MAX_HEADER_BYTES:
            raise BodyTooLarge("Слишком большие заголовки multipart")

    def on_headers_finished(self) -> None:
        _, options = parse_options_header(self._current_part.content_disposition)
        if b"filename" in options:
            if self._current_files >= self.limits.max_files:
                raise BodyTooLarge(f"Не больше {self.limits.max_files} файлов в запросе")
        elif self._current_fields >= self.limits.max_fields:
            raise BodyTooLarge(f"Не больше {self.limits.max_fields} полей в запросе")
        super().on_headers_finished()

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        size = end - start
        if self._current_part.file is not None:
            self.file_bytes += size
            self.total_bytes += size
            if self.file_bytes > self.limits.max_file_bytes:
                raise BodyTooLarge("Превышен размер файла")
            if self.total_bytes > self.limits.max_total_bytes:
                raise BodyTooLarge("Превышен суммарный размер файлов")
        elif len(self._current_part.data) + size > self.max_part_size:
            raise BodyTooLarge("Превышен размер поля формы")
        super().on_part_data(data, start, end)

    def on_end(self) -> None:
        self.complete = True
        super().on_end()


@asynccontextmanager
async def bounded_form(request: Request, limits: MultipartLimits):
    """Call only after current_user; close every spool on success, abort or disconnect."""
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "multipart/form-data":
        raise HTTPException(415, "Ожидается multipart/form-data")
    if len(content_type) > MAX_HEADER_BYTES:
        raise BodyTooLarge("Слишком большой Content-Type")
    parser = _BoundedMultiPartParser(request, limits)
    try:
        try:
            form = await parser.parse()
        except (MultiPartException, MultipartParseError) as exc:
            raise HTTPException(400, "Некорректная multipart-форма") from exc
        if not parser.complete:
            raise HTTPException(400, "Незавершённая multipart-форма")
        yield form
    finally:
        # Close all parser-owned spools, including an unfinished final part.
        for file in parser._files_to_close_on_error:
            file.close()
