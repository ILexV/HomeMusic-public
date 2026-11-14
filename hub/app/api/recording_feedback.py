"""Authenticated reports about a server-bound, actually served provider candidate."""
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app import recording_feedback
from app.auth import current_user
from app.models import User

router = APIRouter(prefix="/api/recording-feedback", tags=["recording-feedback"])


class ReportIn(BaseModel):
    ref: str = Field(min_length=1, max_length=512)
    binding: str = Field(min_length=1, max_length=64)


@router.get("/current")
async def current(ref: str = Query(min_length=1, max_length=512), user: User = Depends(current_user)):
    try:
        return {"ref": ref, "binding": await recording_feedback.current_binding(user.id, ref)}
    except recording_feedback.FeedbackError as e:
        raise HTTPException(e.status, str(e)) from e


@router.post("")
async def report(body: ReportIn, user: User = Depends(current_user)):
    try:
        row = await recording_feedback.report(user.id, body.ref, body.binding)
    except recording_feedback.FeedbackError as e:
        raise HTTPException(e.status, str(e)) from e
    return {"id": row.id, "ref": row.ref, "message": "Эта версия больше не будет выбрана для вас. Вкус не изменён."}


@router.delete("/{report_id}", status_code=204)
async def undo(report_id: int, user: User = Depends(current_user)):
    try:
        await recording_feedback.undo(user.id, report_id)
    except recording_feedback.FeedbackError as e:
        raise HTTPException(e.status, str(e)) from e
    return Response(status_code=204)
