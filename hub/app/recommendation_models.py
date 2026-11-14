"""Account-scoped, expiring recommendation restrictions; never favorite decisions."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, utcnow


class RecommendationPause(Base):
    __tablename__ = "recommendation_pauses"
    __table_args__ = (Index("ix_recommendation_pauses_user_expires", "user_id", "expires_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    action: Mapped[str] = mapped_column(String(16))
    label: Mapped[str] = mapped_column(String(1024))
    ref: Mapped[str | None] = mapped_column(String(512))
    nd_song_id: Mapped[str | None] = mapped_column(String(64))
    artist: Mapped[str] = mapped_column(String(512))
    artist_key: Mapped[str] = mapped_column(String(512))
    aliases: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
