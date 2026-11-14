"""Feedback changes file selection only, never taste, skips or recommendation weights."""
from __future__ import annotations

from datetime import timedelta
from secrets import token_urlsafe

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from app.db import session_scope
from app.models import utcnow
from app.recording_models import RecordingBinding, RecordingFeedback

BINDING_TTL = timedelta(hours=24)


async def bind_choice(user_id: int, choice) -> str:
    candidate = choice.candidate
    binding = RecordingBinding(id=token_urlsafe(24), user_id=user_id, ref=choice.ref,
        provider=candidate.provider if candidate else None,
        provider_id=candidate.provider_id if candidate else None, kind=choice.kind, created_at=utcnow())
    async with session_scope() as s:
        # Browser audio starts in the gesture, concurrently with metadata resolution.
        # Reuse its recent server observation, including on the post-start metadata refresh.
        previous = (await s.execute(select(RecordingBinding).where(
            RecordingBinding.user_id == user_id, RecordingBinding.ref == choice.ref,
            RecordingBinding.kind == choice.kind,
            RecordingBinding.provider == binding.provider,
            RecordingBinding.provider_id == binding.provider_id,
            RecordingBinding.created_at >= utcnow() - timedelta(minutes=5),
        ).order_by(RecordingBinding.created_at.desc()).limit(1))).scalar_one_or_none()
        if previous is not None:
            return previous.id
        s.add(binding)
        await s.commit()
    return binding.id


async def record_stream(user_id: int, choice, *, binding_id: str | None = None,
                        full_verified: bool | None = None) -> None:
    """Only called after a non-prefetch media response opens successfully.

    Full source metadata or an HTTP 206 is NOT proof of a complete recording.
    A caller may assert full_verified only after the existing full-file probe.
    """
    now = utcnow()
    candidate = choice.candidate
    async with session_scope() as s:
        rows = list((await s.execute(select(RecordingBinding).where(
            RecordingBinding.user_id == user_id, RecordingBinding.ref == choice.ref,
            RecordingBinding.created_at >= now - BINDING_TTL,
            RecordingBinding.id == binding_id if binding_id else True,
            RecordingBinding.kind == choice.kind,
            RecordingBinding.provider == (candidate.provider if candidate else None),
            RecordingBinding.provider_id == (candidate.provider_id if candidate else None),
        ).order_by(RecordingBinding.created_at.desc()).limit(1))).scalars())
        row = rows[0] if rows else None
        if row is None:
            row = RecordingBinding(id=token_urlsafe(24), user_id=user_id, ref=choice.ref,
                provider=candidate.provider if candidate else None,
                provider_id=candidate.provider_id if candidate else None, kind=choice.kind, created_at=now)
            s.add(row)
        if row is not None:
            # The latest actually served candidate wins over metadata/prefetch choices.
            row.served_at = now
            if choice.kind == "preview":
                row.full_verified = False
            elif full_verified is not None:
                row.full_verified = full_verified
            if row.full_verified is not None:
                row.verified_at = row.verified_at or now
            await s.commit()


async def rejected_candidates(user_id: int | None, ref: str | None) -> set[tuple[str, str]]:
    if user_id is None or not ref:
        return set()
    async with session_scope() as s:
        from app.models import Track
        refs = {ref}
        if ref.startswith("nd:"):
            matches = (await s.execute(select(Track.deezer_track_id).where(
                Track.nd_song_id == ref[3:], Track.deezer_track_id.is_not(None)))).scalars()
            refs.update(f"dz:{ident}" for ident in matches)
        elif ref.startswith("dz:"):
            matches = (await s.execute(select(Track.nd_song_id).where(
                Track.deezer_track_id == ref[3:], Track.nd_song_id.is_not(None)))).scalars()
            refs.update(f"nd:{ident}" for ident in matches)
        return set((await s.execute(select(RecordingFeedback.provider, RecordingFeedback.provider_id).where(
            RecordingFeedback.user_id == user_id, RecordingFeedback.ref.in_(refs),
            RecordingFeedback.revoked_at.is_(None)))).tuples())


class FeedbackError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


async def report(user_id: int, ref: str, binding_id: str) -> RecordingFeedback:
    now = utcnow()
    async with session_scope() as s:
        binding = await s.get(RecordingBinding, binding_id)
        if binding is None or binding.user_id != user_id or binding.ref != ref:
            raise FeedbackError(404, "Запись прослушивания не найдена")
        if binding.created_at < now - BINDING_TTL or binding.served_at is None:
            raise FeedbackError(409, "Сначала включите эту запись; данные прослушивания устарели или ещё не получены")
        if binding.kind != "full" or not binding.provider or not binding.provider_id:
            raise FeedbackError(409, "Для этой записи источник файла не подтверждён")
        latest = (await s.execute(select(RecordingBinding).where(
            RecordingBinding.user_id == user_id, RecordingBinding.ref == ref,
            RecordingBinding.served_at.is_not(None),
        ).order_by(RecordingBinding.served_at.desc()).limit(1))).scalar_one()
        if (latest.provider, latest.provider_id, latest.kind) != (binding.provider, binding.provider_id, binding.kind):
            raise FeedbackError(409, "Запись уже сменилась. Сообщите о текущем файле.")
        # An idempotent report can be retried, and an undone report can be submitted again.
        await s.execute(insert(RecordingFeedback).values(user_id=user_id, ref=ref,
            provider=binding.provider, provider_id=binding.provider_id, binding_id=binding.id,
            created_at=now, revoked_at=None).on_conflict_do_update(
                index_elements=["user_id", "ref", "provider", "provider_id"],
                set_={"binding_id": binding.id, "created_at": now, "revoked_at": None}))
        row = (await s.execute(select(RecordingFeedback).where(RecordingFeedback.user_id == user_id,
            RecordingFeedback.ref == ref, RecordingFeedback.provider == binding.provider,
            RecordingFeedback.provider_id == binding.provider_id))).scalar_one()
        await s.commit()
        return row


async def undo(user_id: int, report_id: int) -> None:
    async with session_scope() as s:
        row = await s.get(RecordingFeedback, report_id)
        if row is None or row.user_id != user_id:
            raise FeedbackError(404, "Сообщение не найдено")
        row.revoked_at = utcnow()
        await s.commit()


async def current_binding(user_id: int, ref: str) -> str:
    """For direct-stream clients: only their latest actually served candidate."""
    async with session_scope() as s:
        row = (await s.execute(select(RecordingBinding).where(
            RecordingBinding.user_id == user_id, RecordingBinding.ref == ref,
            RecordingBinding.created_at >= utcnow() - BINDING_TTL,
            RecordingBinding.served_at.is_not(None), RecordingBinding.kind == "full",
        ).order_by(RecordingBinding.served_at.desc()).limit(1))).scalar_one_or_none()
        if row is None:
            raise FeedbackError(404, "Сначала включите полную запись из источника")
        return row.id


async def library_candidate(path, nd_song_id: str | None = None) -> tuple[str, str] | None:
    """Recover file provenance only from a completed import of the current library path."""
    from pathlib import Path
    from app.config import get_settings
    from app.models import DownloadItem, Track
    relative = Path(path)
    if relative.is_absolute():
        try:
            relative = relative.relative_to(get_settings().library_dir)
        except ValueError:
            return None
    async with session_scope() as s:
        row = (await s.execute(select(DownloadItem.chosen_candidate_json, Track.source, Track.source_url)
            .join(Track, Track.id == DownloadItem.track_id)
            .where(Track.path == relative.as_posix(), DownloadItem.status == "done",
                   DownloadItem.chosen_candidate_json.is_not(None))
            .order_by(DownloadItem.updated_at.desc()).limit(1))).first()
        if row is None:
            return None
        candidate, source, url = row
        if not candidate or candidate.get("provider") != source or candidate.get("page_url") != url:
            return None
        provider, ident = candidate.get("provider"), candidate.get("provider_id")
        return (provider, ident) if isinstance(provider, str) and isinstance(ident, str) else None
