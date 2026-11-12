"""Account-scoped readiness → stop → start protocol, serialized across SQLite workers.

The independent BEGIN IMMEDIATE transaction locks before reading the account aggregate;
no process-local mutex, authentication session, or network work participates in this lock.
Completed commands are bounded tombstones, so delivery is retryable and acknowledgements
cannot replay next/seek or complete an obsolete handoff.
"""

from __future__ import annotations

import copy
import time
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import sessionmaker
from app.playback_models import PlaybackSession
from app.playback_schema import Ack, Claim, Control, Handoff, Heartbeat

ONLINE_SECONDS = 15
MAX_DEVICES = 32
MAX_HISTORY = 128
COMMAND_SECONDS = {"prepare": 25, "stop": 10, "start": 25}
ACTIVE_PHASES = {"preparing", "stopping", "starting"}
PHASES = {"prepare": "preparing", "stop": "stopping", "start": "starting"}
Body = Heartbeat | Claim | Handoff | Control | Ack


def _initial() -> dict[str, Any]:
    return {"owner": None, "revision": 0, "state": None, "devices": {},
            "handoff": None, "commands": [], "error": None, "fenced_owner": None}


def _online(data: dict, device_id: str | None, now: float) -> bool:
    device = data["devices"].get(device_id)
    return device is not None and now - device["last_seen"] <= ONLINE_SECONDS


def _active(data: dict) -> bool:
    return bool(data["handoff"] and data["handoff"]["phase"] in ACTIVE_PHASES)


def _pending(data: dict) -> bool:
    return any(command["status"] == "pending" for command in data["commands"])


def _finish(command: dict, error: str | None = None) -> None:
    command["status"] = "failed" if error else "done"
    command["error"] = error
    command.pop("state", None)


def _abort(data: dict, error: str) -> None:
    transfer = data["handoff"]
    transfer["phase"] = "failed"
    transfer["error"] = error
    data["error"] = error
    for command in data["commands"]:
        if command.get("handoff_id") == transfer["id"] and command["status"] == "pending":
            _finish(command, error)
    # Never issue a compensating play: the target might have started before losing its lease.


def _expire(data: dict, now: float) -> None:
    for command in data["commands"]:
        if command["status"] != "pending" or command["expires_at"] > now:
            continue
        error = "Время выполнения команды истекло"
        if command.get("handoff_id") and _active(data):
            _abort(data, error)
        else:
            _finish(command, error)
            data["error"] = error
    if _active(data):
        transfer = data["handoff"]
        required = ("target",) if transfer.get("recovery") else ("source", "target")
        if not all(_online(data, transfer[key], now) for key in required):
            _abort(data, "Устройство отключилось во время передачи")


def _enqueue(data: dict, target: str, kind: str, now: float, *, state: dict | None = None,
             value: float | None = None, handoff_id: str | None = None) -> None:
    command = {"id": uuid.uuid4().hex, "target": target, "kind": kind,
               "expires_at": now + COMMAND_SECONDS.get(kind, 10),
               "revision": data["revision"], "status": "pending"}
    if state is not None:
        command["state"] = copy.deepcopy(state)
    if value is not None:
        command["value"] = value
    if handoff_id:
        command["handoff_id"] = handoff_id
    data["commands"].append(command)
    completed = [item for item in data["commands"] if item["status"] != "pending"]
    keep = {item["id"] for item in completed[-MAX_HISTORY:]}
    data["commands"] = [item for item in data["commands"] if item["status"] == "pending" or item["id"] in keep]


def _view(data: dict, device_id: str, now: float, error: str | None = None) -> dict:
    transfer = data["handoff"]
    return {
        "device_id": device_id,
        "devices": [{**device, "online": _online(data, device["id"], now)}
                    for device in data["devices"].values()],
        "owner": data["owner"], "revision": data["revision"], "state": data["state"],
        "handoff": ({key: transfer[key] for key in ("id", "source", "target", "phase", "error", "recovery") if key in transfer}
                    if transfer else None),
        "commands": [{key: command[key] for key in ("id", "kind", "expires_at", "state", "value", "handoff_id")
                      if key in command}
                     for command in data["commands"]
                     if command["status"] == "pending" and command["target"] == device_id
                     and command["expires_at"] > now][:32],
        "error": error or data["error"],
    }


def _publish(data: dict, state: dict, *, telemetry: bool = False) -> None:
    previous = data["state"]
    if state != previous:
        # Position is live telemetry, not an optimistic-concurrency boundary.
        # Otherwise a playing owner's 2s heartbeat invalidates every remote
        # control and handoff rendered from the preceding 2s poll.
        changed = previous is None or any(
            value != previous.get(key) for key, value in state.items() if key != "position"
        )
        data["state"] = state
        if not telemetry or changed:
            data["revision"] += 1


def _heartbeat(data: dict, body: Heartbeat, now: float) -> str | None:
    devices = data["devices"]
    was_online = _online(data, body.device_id, now)
    if body.device_id not in devices and len(devices) >= MAX_DEVICES:
        protected = {data["owner"]}
        if _active(data):
            protected.update((data["handoff"]["source"], data["handoff"]["target"]))
        evictable = [device for device in devices.values()
                     if device["id"] not in protected and not _online(data, device["id"], now)]
        if not evictable:
            return "Достигнут предел устройств: 32"
        oldest = min(evictable, key=lambda device: device["last_seen"])
        del devices[oldest["id"]]
    devices[body.device_id] = {"id": body.device_id, "name": body.name, "kind": body.kind, "last_seen": now}
    if body.state is None:
        return None
    if data["owner"] != body.device_id:
        return "Воспроизведением управляет другое устройство" if data["owner"] else "Сначала выберите устройство воспроизведения"
    if data.get("fenced_owner") == body.device_id:
        return "Передача после отключения: возобновление возможно только явным выбором устройства"
    if body.revision != data["revision"]:
        return "Состояние изменилось; обновите данные"
    if _pending(data) or _active(data):
        return "Дождитесь выполнения команды"
    _publish(data, body.state.model_dump(), telemetry=was_online)
    return None


def _ack(data: dict, body: Ack, now: float) -> str | None:
    command = next((item for item in data["commands"] if item["id"] == body.command_id), None)
    if command is None or command["target"] != body.device_id:
        return "Команда не найдена для этого устройства"
    if command["status"] != "pending":
        return command.get("error")
    transfer = data["handoff"]
    kind = command["kind"]
    handoff_id = command.get("handoff_id")
    if handoff_id:
        if not _active(data) or transfer["id"] != handoff_id or transfer["phase"] != PHASES[kind]:
            _finish(command, "Передача уже завершена")
            return command["error"]
    elif data["owner"] != body.device_id or command["revision"] != data["revision"]:
        _finish(command, "Команда относится к устаревшему состоянию")
        return command["error"]
    if body.ok and ((kind != "prepare" and body.state is None)
                    or (kind in ("prepare", "stop") and body.state is not None and body.state.playing)):
        return "Подтверждение должно содержать фактическое состояние остановленного плеера" if kind in ("prepare", "stop") else "Подтверждение должно содержать фактическое состояние плеера"
    if not body.ok:
        # Client exceptions can contain signed URLs, cookies and filesystem paths. Never persist them.
        error = "Устройство не смогло выполнить команду"
        if handoff_id:
            _abort(data, error)
        else:
            _finish(command, error)
            data["error"] = error
        return error
    state = body.state.model_dump() if body.state is not None else None
    _finish(command)
    data["error"] = None
    if not handoff_id:
        data["state"] = state
        data["revision"] += 1
    elif kind == "prepare":
        if transfer.get("recovery") and not _online(data, transfer["source"], now):
            transfer["phase"] = "starting"
            final = {**data["state"], "playing": transfer["playing"]}
            _enqueue(data, transfer["target"], "start", now, state=final, handoff_id=handoff_id)
        else:
            transfer["phase"] = "stopping"
            _enqueue(data, transfer["source"], "stop", now, handoff_id=handoff_id)
    elif kind == "stop":
        data["state"] = state
        final = {**state, "playing": transfer["playing"]}
        transfer["phase"] = "starting"
        _enqueue(data, transfer["target"], "start", now, state=final, handoff_id=handoff_id)
    else:
        data["owner"] = transfer["target"]
        data["fenced_owner"] = None
        data["state"] = state
        data["revision"] += 1
        transfer["phase"] = "done"
    return None


def _apply(data: dict, action: str, body: Body, now: float) -> str | None:
    if action == "heartbeat":
        return _heartbeat(data, body, now)
    if body.device_id not in data["devices"]:
        return "Сначала зарегистрируйте устройство"
    if action == "ack":
        return _ack(data, body, now)
    if not _online(data, body.device_id, now):
        return "Устройство не в сети; обновите подключение"
    if body.revision != data["revision"]:
        return "Состояние изменилось; обновите данные"
    if _active(data) or _pending(data):
        return "Дождитесь выполнения команды"
    if action == "claim":
        if data["owner"] not in (None, body.device_id):
            return "Воспроизведением управляет другое устройство; требуется передача"
        if data.get("fenced_owner") == body.device_id:
            data["fenced_owner"] = None
            data["handoff"] = None
        if data["owner"] is None:
            data["owner"] = body.device_id
            data["state"] = body.state.model_dump()
            data["revision"] += 1
        else:
            _publish(data, body.state.model_dump())
        data["error"] = None
        return None
    if not _online(data, body.target, now):
        return "Устройство воспроизведения не в сети"
    if action == "handoff":
        if data["owner"] is None:
            return "Сначала выберите устройство воспроизведения"
        if body.target == data["owner"]:
            return "Это устройство уже воспроизводит музыку"
        if data["state"] is None or not data["state"]["queue"]:
            return "Нет очереди для передачи"
        transfer = {"id": uuid.uuid4().hex, "source": data["owner"], "target": body.target,
                    "phase": "preparing", "playing": data["state"]["playing"],
                    "recovery": not _online(data, data["owner"], now)}
        if transfer["recovery"]:
            # Explicit takeover after lease loss: fence stale heartbeats even if recovery fails.
            data["fenced_owner"] = data["owner"]
            data["state"] = {**data["state"], "playing": False}
            data["revision"] += 1
        data["handoff"] = transfer
        data["error"] = None
        _enqueue(data, body.target, "prepare", now, state=data["state"], handoff_id=transfer["id"])
        return None
    if data.get("fenced_owner") == data["owner"]:
        return "После отключения явно выберите устройство воспроизведения"
    if body.target != data["owner"]:
        return "Команды доступны только текущему устройству воспроизведения"
    data["error"] = None
    _enqueue(data, body.target, body.kind, now, value=body.value)
    return None


async def dispatch(user_id: int, action: str, body: Body, *,
                   sessions: async_sessionmaker[AsyncSession] | None = None) -> dict:
    """Commit expiry and conflict responses too; callers always receive the current account view."""
    async with (sessions or sessionmaker())() as session:
        try:
            await session.execute(text("BEGIN IMMEDIATE"))
            row = await session.get(PlaybackSession, user_id)
            data = copy.deepcopy(row.payload) if row is not None else _initial()
            now = time.time()
            _expire(data, now)
            error = _apply(data, action, body, now)
            if row is None:
                session.add(PlaybackSession(user_id=user_id, payload=data))
            else:
                row.payload = data
            view = _view(data, body.device_id, now, error)
            await session.commit()
            return view
        except BaseException:
            await session.rollback()
            raise
