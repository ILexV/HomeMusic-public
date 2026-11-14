"""AI assistant API (FR-6): screenshots / links / text → confirmation card → downloads into a playlist."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.limits import reserve_message, validate_message_text
from app.ai.pipeline import confirm_task, task_dir, task_out
from app.api.schemas import AssistantConfirmIn, AssistantTaskOut, AssistantTextIn
from app.auth import current_user
from app.db import get_session
from app.events import bus
from app.jobs.queue import enqueue, notify
from app.models import AssistantChat, AssistantTask, User, utcnow
from app.upload_ingress import ASSISTANT_LIMITS, bounded_form

router = APIRouter(prefix="/api/assistant", tags=["assistant"])

IMAGE_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/heic": "heic"}


@router.post("/tasks", response_model=AssistantTaskOut)
async def create_task(request: Request, user: User = Depends(current_user), s: AsyncSession = Depends(get_session, scope="function")):
    ctype = request.headers.get("content-type", "")
    images: list[tuple[bytes, str]] = []
    body: AssistantTextIn | None = None
    if ctype.startswith("multipart/form-data"):
        async with bounded_form(request, ASSISTANT_LIMITS) as form:
            files = [f for f in form.getlist("images") if hasattr(f, "read")]
            body = AssistantTextIn(url=form.get("url") or None, text=form.get("text") or None)
            for value in (body.text, body.url):
                if value:
                    validate_message_text(value)
            for f in files:
                data = await f.read()
                images.append((data, IMAGE_EXT.get(getattr(f, "content_type", "") or "", "png")))
    else:
        body = AssistantTextIn.model_validate(await request.json())
        for value in (body.text, body.url):
            if value:
                validate_message_text(value)

    if images:
        task = AssistantTask(user_id=user.id, input_kind="images", input_ref={"count": len(images)})
    elif body and body.url:
        task = AssistantTask(user_id=user.id, input_kind="link", input_ref={"url": body.url.strip()})
    elif body and body.text and body.text.strip():
        text = body.text.strip()
        # a shared text that is just a link
        if text.startswith("http") and " " not in text:
            task = AssistantTask(user_id=user.id, input_kind="link", input_ref={"url": text})
        else:
            task = AssistantTask(user_id=user.id, input_kind="text", input_ref={"text": text})
    else:
        raise HTTPException(400, "Пришлите скриншот, ссылку или текст")
    await reserve_message(s, user)
    s.add(task)
    await s.flush()
    if images:
        d = task_dir(task.id)
        for i, (data, ext) in enumerate(images):
            (d / f"{i:02d}.{ext}").write_bytes(data)
    await enqueue(s, "assistant_task", {"task_id": task.id}, max_attempts=2)
    await s.commit()
    notify()
    bus.publish(user.id, "assistant", {"id": task.id, "status": task.status})
    return task_out(task)


@router.get("/tasks", response_model=list[AssistantTaskOut])
async def list_tasks(user: User = Depends(current_user), s: AsyncSession = Depends(get_session, scope="function")):
    rows = (
        await s.execute(
            select(AssistantTask).where(AssistantTask.user_id == user.id).order_by(AssistantTask.id.desc()).limit(50)
        )
    ).scalars().all()
    return [task_out(t, with_rows=False) for t in rows]


async def _own(s: AsyncSession, user: User, task_id: int) -> AssistantTask:
    t = await s.get(AssistantTask, task_id)
    if t is None or t.user_id != user.id:
        raise HTTPException(404, "Задача не найдена")
    return t


@router.get("/tasks/{task_id}", response_model=AssistantTaskOut)
async def get_task(task_id: int, user: User = Depends(current_user), s: AsyncSession = Depends(get_session, scope="function")):
    return task_out(await _own(s, user, task_id))


@router.post("/tasks/{task_id}/confirm", response_model=AssistantTaskOut)
async def confirm(task_id: int, body: AssistantConfirmIn, user: User = Depends(current_user),
                  s: AsyncSession = Depends(get_session, scope="function")):
    task = await _own(s, user, task_id)
    task = await confirm_task(s, user, task, body)
    await s.commit()
    notify()
    bus.publish(user.id, "assistant", {"id": task.id, "status": task.status})
    return task_out(task)


# --- free chat with tools (stage 3.8) ------------------------------------------------------------


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatIn(BaseModel):
    messages: list[ChatMessage]
    chat_id: int | None = None  # the saved chat this turn continues; none → a new chat


class ChatConfirmIn(BaseModel):
    confirm_token: str
    chat_id: int | None = None


class ChatDismissIn(BaseModel):
    chat_id: int | None = None


async def _own_chat(s: AsyncSession, user: User, chat_id: int) -> AssistantChat:
    c = await s.get(AssistantChat, chat_id)
    if c is None or c.user_id != user.id:
        raise HTTPException(404, "Чат не найден")
    return c


def _chat_title(text: str, images: int) -> str:
    text = " ".join(text.split())
    if not text:
        return "Скриншот" if images == 1 else "Скриншоты"
    return text if len(text) <= 80 else text[:79].rstrip() + "…"


async def _save_turn(s: AsyncSession, user: User, chat_id: int | None, text: str, images: int,
                     out: dict) -> AssistantChat:
    chat = await _own_chat(s, user, chat_id) if chat_id else None
    if chat is None:
        chat = AssistantChat(user_id=user.id, title=_chat_title(text, images), messages=[])
        s.add(chat)
    mine: dict = {"role": "user", "content": text}
    if images:
        mine["images"] = images
    reply: dict = {"role": "assistant", "content": out.get("reply") or "", "cost": out.get("cost_usd") or 0,
                   "done": out.get("done") or []}
    if out.get("task_id"):
        reply["proposal"] = {"task_id": out["task_id"], "token": out["confirm_token"], "summary": out.get("summary", ""),
                             "actions": out.get("actions") or [], "state": "pending"}
    chat.messages = [*(chat.messages or []), mine, reply]  # a new list: JSON columns do not track in-place changes
    chat.updated_at = utcnow()
    await s.flush()
    return chat


async def _set_proposal(s: AsyncSession, user: User, chat_id: int | None, task_id: int, state: str) -> None:
    if not chat_id:
        return
    chat = await _own_chat(s, user, chat_id)
    msgs = [dict(m) for m in chat.messages or []]
    for m in msgs:
        if (m.get("proposal") or {}).get("task_id") == task_id:
            m["proposal"] = {**m["proposal"], "state": state}
    chat.messages = msgs


def _chat_brief(c: AssistantChat) -> dict:
    return {"id": c.id, "title": c.title, "messages": len(c.messages or []),
            "created_at": c.created_at, "updated_at": c.updated_at}


@router.get("/chats")
async def list_chats(user: User = Depends(current_user), s: AsyncSession = Depends(get_session, scope="function")):
    rows = (
        await s.execute(
            select(AssistantChat).where(AssistantChat.user_id == user.id)
            .order_by(AssistantChat.updated_at.desc(), AssistantChat.id.desc()).limit(100)
        )
    ).scalars().all()
    return [_chat_brief(c) for c in rows]


@router.get("/chats/{chat_id}")
async def get_chat(chat_id: int, user: User = Depends(current_user), s: AsyncSession = Depends(get_session, scope="function")):
    c = await _own_chat(s, user, chat_id)
    return {**_chat_brief(c), "messages": c.messages or []}


@router.delete("/chats/{chat_id}")
async def delete_chat(chat_id: int, user: User = Depends(current_user), s: AsyncSession = Depends(get_session, scope="function")):
    await s.delete(await _own_chat(s, user, chat_id))
    await s.commit()
    return {"ok": True}


@router.post("/chat")
async def chat(request: Request, user: User = Depends(current_user), s: AsyncSession = Depends(get_session, scope="function")):
    from app.ai.agent import run_chat

    images: list[tuple[bytes, str]] = []
    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        async with bounded_form(request, ASSISTANT_LIMITS) as form:
            try:
                raw_messages = form.get("messages")
                if not isinstance(raw_messages, str):
                    raise ValueError("messages field is missing")
                raw_chat = form.get("chat_id")
                body = ChatIn(messages=json.loads(raw_messages), chat_id=int(raw_chat) if isinstance(raw_chat, str) and raw_chat else None)
            except Exception as e:
                raise HTTPException(400, "Некорректная история чата") from e
            if body.messages:
                validate_message_text(body.messages[-1].content)
            files = [f for f in form.getlist("images") if hasattr(f, "read")]
            for f in files:
                data = await f.read()
                mime = getattr(f, "content_type", "") or ""
                if mime not in IMAGE_EXT:
                    raise HTTPException(400, "Поддерживаются PNG, JPEG, WebP и HEIC")
                images.append((data, mime))
    else:
        body = ChatIn.model_validate(await request.json())
    if not body.messages:
        raise HTTPException(400, "Пустой запрос")
    validate_message_text(body.messages[-1].content)
    if body.messages[-1].role != "user" or (not body.messages[-1].content.strip() and not images):
        raise HTTPException(400, "Пришлите сообщение или скриншот")
    if body.chat_id:
        await _own_chat(s, user, body.chat_id)  # a foreign or deleted chat fails before the model runs
    await reserve_message(s, user)
    try:
        out = await run_chat(s, user, [m.model_dump() for m in body.messages], images=images)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"ИИ недоступен: {e}") from e
    last = body.messages[-1]
    saved = await _save_turn(s, user, body.chat_id, last.content if last.role == "user" else "", len(images), out)
    await s.commit()
    notify()  # downloads the assistant queued
    return {**out, "chat_id": saved.id, "title": saved.title}


@router.post("/chat/{task_id}/confirm")
async def chat_confirm(task_id: int, body: ChatConfirmIn, user: User = Depends(current_user),
                       s: AsyncSession = Depends(get_session, scope="function")):
    from app.ai.agent import confirm_chat

    task = await _own(s, user, task_id)
    out = await confirm_chat(s, user, task, body.confirm_token)
    await _set_proposal(s, user, body.chat_id, task_id, "confirmed")
    await s.commit()
    notify()
    return out


@router.post("/chat/{task_id}/dismiss")
async def chat_dismiss(task_id: int, body: ChatDismissIn, user: User = Depends(current_user),
                       s: AsyncSession = Depends(get_session, scope="function")):
    task = await _own(s, user, task_id)
    if task.input_kind == "chat" and task.status == "awaiting_confirmation":
        task.status, task.confirm_token = "cancelled", None
    await _set_proposal(s, user, body.chat_id, task_id, "dismissed")
    await s.commit()
    return {"ok": True}
