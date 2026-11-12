"""Authenticated remote controls; every operation is scoped to the caller's Hub account."""

from fastapi import APIRouter, Depends

from app.auth import current_user
from app.models import User
from app.playback import dispatch
from app.playback_schema import Ack, Claim, Control, Handoff, Heartbeat

router = APIRouter(prefix="/api/playback", tags=["playback"])


@router.post("/heartbeat")
async def heartbeat(body: Heartbeat, user: User = Depends(current_user)):
    return await dispatch(user.id, "heartbeat", body)


@router.post("/claim")
async def claim(body: Claim, user: User = Depends(current_user)):
    return await dispatch(user.id, "claim", body)


@router.post("/handoff")
async def handoff(body: Handoff, user: User = Depends(current_user)):
    return await dispatch(user.id, "handoff", body)


@router.post("/command")
async def command(body: Control, user: User = Depends(current_user)):
    return await dispatch(user.id, "command", body)


@router.post("/ack")
async def ack(body: Ack, user: User = Depends(current_user)):
    return await dispatch(user.id, "ack", body)
