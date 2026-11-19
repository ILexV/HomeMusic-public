"""Account-scoped exposure, not listening history. A recording gets at most one small deduction."""
from datetime import datetime, timedelta

from sqlalchemy import case, func, or_, select

from app.db import session_scope
from app.mixes.taste_snapshot import utc
from app.models import Mix, MixItem, Track, WaveItem

WINDOW = timedelta(days=7)
PENALTY = 0.12  # less than the queued bonus (0.3); only nearby candidates trade places


def mark_ready(item: MixItem, at: datetime) -> None:
    if item.state == "active" and item.nd_song_id and item.offered_at is None:
        item.offered_at = at


async def load(user_id: int, *, now: datetime, exclude_session: str | None = None) -> set[str]:
    """Track refs and nd:<song> aliases of playable offers, once per ranking decision, not per candidate."""
    now = utc(now)
    since = now - WINDOW
    mix = select(func.coalesce(MixItem.ref, func.json_extract(MixItem.features_json, "$.track.ref")).label("ref"),
                 MixItem.nd_song_id.label("nd")).join(Mix, Mix.id == MixItem.mix_id).where(
        Mix.user_id == user_id, MixItem.offered_at.between(since, now))
    wave = select(WaveItem.ref, func.coalesce(WaveItem.nd_song_id, case(
        (WaveItem.ref.like("nd:%"), func.substr(WaveItem.ref, 4))))).where(
        WaveItem.user_id == user_id, WaveItem.first_handed_at.between(since, now))
    if exclude_session is not None:
        wave = wave.where(WaveItem.session_id != exclude_session)
    offered = mix.union_all(wave).cte("offered")

    # An external recording may have joined the library *after* the hand-out. Resolve either direction
    # against indexed library IDs (never fuzzy titles), without reading the account's plays or whole library.
    linked = select("dz:" + Track.deezer_track_id, Track.nd_song_id).where(
        Track.nd_song_id.is_not(None), Track.deezer_track_id.is_not(None),
        or_(Track.nd_song_id.in_(select(offered.c.nd)), Track.deezer_track_id.in_(
            select(func.substr(offered.c.ref, 4)).where(offered.c.ref.like("dz:%")))))
    keys = set()
    async with session_scope() as s:
        for ref, nd in await s.execute(select(offered.c.ref, offered.c.nd).union_all(linked)):
            if ref:
                keys.add(ref)
            if nd:
                keys.add(f"nd:{nd}")
    return keys


def penalty(keys: set[str], ref: str, nd_song_id: str | None) -> float:
    return PENALTY if ref in keys or (nd_song_id and f"nd:{nd_song_id}" in keys) else 0.0
