"""User-visible download history, separate from current-track/native status lookups."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import case, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import DownloadItemOut
from app.auth import current_user
from app.db import get_session
from app.jobs.downloads import FAILED, item_links, item_out
from app.library.owned import ACTIVE_STATUSES
from app.models import DownloadItem, User

router = APIRouter(prefix="/api/downloads", tags=["downloads"])


class DownloadCounts(BaseModel):
    all: int
    active: int
    problems: int
    finished: int


class DownloadPageOut(BaseModel):
    items: list[DownloadItemOut]
    next_cursor: int | None
    counts: DownloadCounts


def _truthy_flag(key: str):
    """SQLite equivalent of Python truthiness for the JSON flags used by owned.quiet."""
    path = f"$.{key}"
    value = func.json_extract(DownloadItem.query_json, path)
    kind = func.json_type(DownloadItem.query_json, path)
    return case(
        (kind == "text", value != ""),
        (kind == "array", value != "[]"),
        (kind == "object", value != "{}"),
        else_=func.coalesce(value != 0, False),
    )


@router.get("/page", response_model=DownloadPageOut)
async def list_page(
    filter: Literal["all", "active", "problems"] = "all",
    limit: int = Query(50, ge=1, le=200),
    before_id: int | None = Query(None, ge=1),
    user: User = Depends(current_user),
    s: AsyncSession = Depends(get_session, scope="function"),
):
    # item_out.star is owned.quiet(query_json), not simply the favorite flag. Explicitly requested
    # imports/favorites are visible; quiet failures stay quiet too, just as in the existing UI.
    visible = or_(
        _truthy_flag("requested"),
        not_(or_(*(_truthy_flag(key) for key in ("star", "import", "mix", "wave")))),
    )
    scope = (DownloadItem.user_id == user.id, visible)
    totals = dict((await s.execute(
        select(DownloadItem.status, func.count()).where(*scope).group_by(DownloadItem.status)
    )).all())
    counts = DownloadCounts(
        all=sum(totals.values()),
        active=sum(totals.get(status, 0) for status in ACTIVE_STATUSES),
        problems=sum(totals.get(status, 0) for status in FAILED),
        finished=totals.get("done", 0) + totals.get("cancelled", 0),
    )

    stmt = select(DownloadItem).where(*scope)
    if filter == "active":
        stmt = stmt.where(DownloadItem.status.in_(ACTIVE_STATUSES))
    elif filter == "problems":
        stmt = stmt.where(DownloadItem.status.in_(FAILED))
    if before_id is not None:
        stmt = stmt.where(DownloadItem.id < before_id)
    rows = list((await s.execute(stmt.order_by(DownloadItem.id.desc()).limit(limit + 1))).scalars())
    has_more = len(rows) > limit
    if has_more:
        rows.pop()
    links = await item_links(s, rows)
    return DownloadPageOut(
        items=[item_out(row, links=links[row.id]) for row in rows],
        next_cursor=rows[-1].id if has_more else None,
        counts=counts,
    )
