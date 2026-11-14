"""Server-bound recording observations and reversible, account-owned file reports."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, utcnow


class RecordingBinding(Base):
    __tablename__ = "recording_bindings"
    __table_args__ = (Index("ix_recording_bindings_user_ref", "user_id", "ref", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    ref: Mapped[str] = mapped_column(String(512))
    provider: Mapped[str | None] = mapped_column(String(64))
    provider_id: Mapped[str | None] = mapped_column(String(512))
    kind: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    served_at: Mapped[datetime | None] = mapped_column(DateTime)
    full_verified: Mapped[bool | None] = mapped_column()
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    provenance: Mapped[str] = mapped_column(String(64), default="stream-observation-v1")


class RecordingFeedback(Base):
    __tablename__ = "recording_feedback"
    __table_args__ = (UniqueConstraint("user_id", "ref", "provider", "provider_id",
                                      name="uq_recording_feedback_candidate"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    ref: Mapped[str] = mapped_column(String(512))
    provider: Mapped[str] = mapped_column(String(64))
    provider_id: Mapped[str] = mapped_column(String(512))
    binding_id: Mapped[str] = mapped_column(ForeignKey("recording_bindings.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
