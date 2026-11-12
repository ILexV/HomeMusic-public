"""Persistent account aggregate for the serialized shared-playback protocol."""

from typing import Any

from sqlalchemy import JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class PlaybackSession(Base):
    __tablename__ = "playback_sessions"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
