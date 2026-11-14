"""Authenticated multipart ingress: streaming limits and temp-file ownership."""

from __future__ import annotations

import asyncio
import io
import wave
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import select
from starlette import formparsers
from starlette.requests import ClientDisconnect

from app import upload_ingress
from app.api import assistant, uploads
from app.config import get_settings
from app.db import dispose, init_engine, session_scope
from app.models import Session, Upload, User, utcnow
from app.security import hash_token
from app.upload_ingress import MultipartLimits, UploadBodyLimitMiddleware, bounded_form


@pytest.fixture
async def env(hub_env, db_file):
    init_engine(db_file)
    async with session_scope() as session:
        user = User(nd_username="uploader", nd_password_enc="unused")
        session.add(user)
        await session.flush()
        session.add(Session(id=hash_token("upload-token"), user_id=user.id,
                            expires_at=utcnow() + timedelta(days=1)))
    app = FastAPI()
    app.include_router(uploads.router)
    app.include_router(assistant.router)
    app.add_middleware(UploadBodyLimitMiddleware)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                cookies={"hm_session": "upload-token"}) as client:
        try:
            yield SimpleNamespace(client=client, app=app, cfg=get_settings().cfg.uploads,
                                  staging=get_settings().staging_dir)
        finally:
            await dispose()


@pytest.fixture
def spools(monkeypatch):
    opened = []
    original = formparsers.SpooledTemporaryFile

    def track(*args, **kwargs):
        file = original(*args, **kwargs)
        opened.append(file)
        return file

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", track)
    monkeypatch.setattr(formparsers.MultiPartParser, "spool_max_size", 16)
    return opened


def audio() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\0\0" * 80)
    return output.getvalue()


def multipart(parts):
    chunks = []
    for name, filename, data in parts:
        header = (
            f'--boundary\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        )
        chunks.append(header.encode())
        chunks.extend([data, b"\r\n"])
    chunks.append(b"--boundary--\r\n")
    return b"".join(chunks)


HEADERS = {"content-type": "multipart/form-data; boundary=boundary"}


@pytest.mark.parametrize("path", [
    "/api/uploads", "/api/uploads/commit", "/api/assistant/tasks", "/api/assistant/chat",
])
@pytest.mark.parametrize("token", ["", "invalid-token"])
async def test_unauthenticated_multipart_never_reads_body(env, path, token):
    env.client.cookies.clear()
    if token:
        env.client.cookies.set("hm_session", token)

    async def unread():
        raise AssertionError("unauthenticated request body was consumed")
        yield b""  # makes this an async iterator, never reached

    response = await env.client.post(path, headers={**HEADERS, "content-length": str(600 * 1024**2)},
                                     content=unread())
    assert response.status_code == 401


async def test_oversized_declared_body_rejected_without_receive(env, spools):
    env.cfg.max_total_mb = 1

    async def unread():
        raise AssertionError("declared oversized request body was consumed")
        yield b""

    response = await env.client.post("/api/uploads", content=unread(),
                                     headers={**HEADERS, "content-length": str(2 * 1024**2 + 1)})
    assert response.status_code == 413
    assert all(file.closed for file in spools)


@pytest.mark.parametrize("declared", [None, "1"])
async def test_raw_body_cap_counts_chunked_and_underdeclared_requests(env, spools, declared):
    env.cfg.max_total_mb = 1
    consumed = []

    async def chunks():
        consumed.append(1)
        yield multipart([("files", "large.wav", b"x" * (2 * 1024**2))])
        consumed.append(2)
        raise AssertionError("rejected body was drained")

    headers = dict(HEADERS)
    if declared is not None:
        headers["content-length"] = declared
    response = await env.client.post("/api/uploads", headers=headers, content=chunks())
    assert response.status_code == 413
    assert consumed == [1]
    assert all(file.closed for file in spools)


async def test_authenticated_multiple_upload_persists_files_and_closes_spools(env, spools):
    data = audio()
    response = await env.client.post("/api/uploads", files=[
        ("files", ("first.wav", data, "audio/wav")),
        ("files", ("second.wav", data, "audio/wav")),
    ])
    assert response.status_code == 200, response.text
    assert [item["orig_name"] for item in response.json()] == ["first.wav", "second.wav"]
    assert [item["size"] for item in response.json()] == [len(data), len(data)]
    async with session_scope() as session:
        rows = (await session.scalars(select(Upload))).all()
        assert len(rows) == 2
        assert all((env.staging / row.staging_path).read_bytes() == data for row in rows)
    assert len(spools) == 2
    assert all(file.closed for file in spools)


@pytest.mark.parametrize(("limits", "parts"), [
    (MultipartLimits(1, 1024, 2048), [("files", "a.wav", b"x"), ("files", "b.wav", b"x")]),
    (MultipartLimits(2, 5, 20), [("files", "a.wav", b"x" * 6)]),
    (MultipartLimits(2, 5, 9), [("files", "a.wav", b"x" * 5), ("files", "b.wav", b"x" * 5)]),
])
async def test_file_count_per_file_and_aggregate_limits_before_staging(env, spools, monkeypatch, limits, parts):
    monkeypatch.setattr(upload_ingress, "upload_limits", lambda: limits)
    monkeypatch.setattr(uploads, "upload_limits", lambda: limits)
    response = await env.client.post("/api/uploads", headers=HEADERS, content=multipart(parts))
    assert response.status_code == 413
    assert all(file.closed for file in spools)
    assert not list(env.staging.glob("uploads/**/*.*"))
    async with session_scope() as session:
        assert (await session.scalars(select(Upload))).all() == []


@pytest.mark.parametrize("path", ["/api/assistant/tasks", "/api/assistant/chat"])
async def test_assistant_limits_apply_to_all_file_fields(env, spools, monkeypatch, path):
    limits = MultipartLimits(1, 20, 20, 2)
    monkeypatch.setattr(upload_ingress, "ASSISTANT_LIMITS", limits)
    monkeypatch.setattr(assistant, "ASSISTANT_LIMITS", limits)
    response = await env.client.post(path, headers=HEADERS, content=multipart([
        ("images", "a.png", b"x"), ("ignored", "b.png", b"x"),
    ]))
    assert response.status_code == 413
    assert all(file.closed for file in spools)


async def test_failed_multiple_upload_removes_staged_files(env, spools):
    response = await env.client.post("/api/uploads", files=[
        ("files", ("first.wav", audio(), "audio/wav")),
        ("files", ("second.exe", b"invalid", "application/octet-stream")),
    ])
    assert response.status_code == 415
    assert not list(env.staging.glob("uploads/**/*.*"))
    assert all(file.closed for file in spools)
    async with session_scope() as session:
        assert (await session.scalars(select(Upload))).all() == []


@pytest.mark.parametrize("failure", ["disconnect", "cancel", "limit", "truncated"])
async def test_partial_spooled_file_closed_on_receive_abort(spools, failure):
    prefix = multipart([("files", "unfinished.wav", b"x" * 32)]).split(b"\r\n--boundary--")[0]
    reads = 0

    async def receive():
        nonlocal reads
        reads += 1
        if reads == 1:
            return {"type": "http.request", "body": prefix, "more_body": True}
        if failure == "disconnect":
            return {"type": "http.disconnect"}
        if failure == "cancel":
            raise asyncio.CancelledError()
        if failure == "limit":
            raise upload_ingress.BodyTooLarge()
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request({"type": "http", "headers": [(b"content-type", HEADERS["content-type"].encode())]}, receive)
    expected = {"disconnect": ClientDisconnect, "cancel": asyncio.CancelledError,
                "limit": upload_ingress.BodyTooLarge, "truncated": upload_ingress.HTTPException}[failure]
    with pytest.raises(expected):
        async with bounded_form(request, MultipartLimits(1, 100, 100)):
            pytest.fail("an incomplete body was accepted")
    assert len(spools) == 1
    assert spools[0]._rolled
    assert spools[0].closed


async def test_middleware_preserves_disconnect_and_non_http_scopes():
    seen = []
    message = {"type": "http.disconnect"}

    async def app(scope, receive, send):
        seen.append(scope["type"])
        assert await receive() is message

    async def receive():
        return message

    async def send(message):
        pytest.fail("middleware unexpectedly sent a response")

    middleware = UploadBodyLimitMiddleware(app)
    for scope in (
        {"type": "lifespan"},
        {"type": "websocket", "path": "/api/uploads"},
        {"type": "http", "method": "POST", "path": "/api/assistant/chat",
         "headers": [(b"content-type", HEADERS["content-type"].encode())]},
        {"type": "http", "method": "POST", "path": "/api/unrelated", "headers": []},
    ):
        await middleware(scope, receive, send)
    assert seen == ["lifespan", "websocket", "http", "http"]
