"""Account-scoped playable offers. No plays, fingerprints or negative taste signals."""
from datetime import datetime, timedelta
from itertools import batched, groupby

from sqlalchemy import func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import session_scope
from app.favorites import AliasIndex
from app.mixes.taste_snapshot import utc
from app.models import Mix, MixItem, SmartItem, SmartSession, Track, WaveItem, WaveReoffer, WaveSession

WINDOW = timedelta(days=7)
PENALTY = 0.12  # less than the queued bonus (0.3); only nearby candidates trade places


def mark_ready(item: MixItem, at: datetime) -> None:
    if item.state == "active" and item.nd_song_id and item.offered_at is None:
        item.offered_at = at


def _wave_owner(user_id: int):
    return (WaveItem.user_id == user_id, WaveSession.user_id == user_id)


def _dated(user_id: int, since: datetime, until: datetime, *, latest_only: bool,
           exclude_wave: str | None = None, exclude_smart: str | None = None, with_reoffers: bool = True):
    """One source for scoring and reporting. For a historical cutoff never use a later Wave timestamp."""
    ref = func.coalesce(MixItem.ref, func.json_extract(MixItem.features_json, "$.track.ref"))
    mix = select(literal("mix"), MixItem.mix_id, ref, MixItem.nd_song_id, MixItem.offered_at).join(
        Mix, Mix.id == MixItem.mix_id).where(Mix.user_id == user_id, MixItem.offered_at.between(since, until))
    smart = select(literal("smart"), SmartItem.session_id, SmartItem.ref, SmartItem.nd_song_id, SmartItem.handed_at
                   ).join(SmartSession, SmartSession.id == SmartItem.session_id).where(
        SmartItem.user_id == user_id, SmartSession.user_id == user_id, SmartItem.handed_at.between(since, until))
    if exclude_smart is not None:
        smart = smart.where(SmartItem.session_id != exclude_smart)

    def wave(at):
        q = select(literal("wave"), WaveItem.session_id, WaveItem.ref, WaveItem.nd_song_id, at).select_from(
            WaveItem).join(WaveSession, WaveSession.id == WaveItem.session_id).where(*_wave_owner(user_id))
        if exclude_wave is not None:
            q = q.where(WaveItem.session_id != exclude_wave)
        return q.where(at.between(since, until))

    first = wave(WaveItem.first_handed_at).where(or_(WaveItem.handed_count == 1,
                                                      WaveItem.first_handed_at != WaveItem.last_handed_at))
    last = wave(WaveItem.last_handed_at).where(WaveItem.handed_count > 1)
    if latest_only:
        first = first.where(or_(WaveItem.handed_count == 1, WaveItem.last_handed_at > until))
    branches = [smart, first, last]
    if with_reoffers:
        middle = wave(WaveReoffer.handed_at).join(WaveReoffer, WaveReoffer.item_id == WaveItem.id).where(
            WaveItem.last_handed_at >= since, WaveItem.first_handed_at <= until)
        if latest_only:
            # In live ranking the last handout suffices; only an as-of query needs earlier evidence.
            middle = middle.where(WaveItem.last_handed_at > until)
        branches.append(middle)
    return mix.union_all(*branches)


def _identity_index(rows):
    index = AliasIndex()
    keys = set()
    for _, _, ref, nd, _ in rows:
        pair = {key for key in (ref, f"nd:{nd}" if nd else None) if key}
        index.join("track", pair)
        keys.update(pair)
    return index, keys


async def _live_aliases(s: AsyncSession, rows) -> dict[str, str]:
    """Live scoring also knows indexed library links, including tracks acquired since handout."""
    index, keys = _identity_index(rows)
    pending = {k for k in keys if k.startswith(("nd:", "dz:"))}
    while pending:
        found = set()
        for batch in batched(pending, 500):
            links = select(Track.deezer_track_id, Track.nd_song_id).where(
                Track.nd_song_id.is_not(None), Track.deezer_track_id.is_not(None),
                or_(Track.nd_song_id.in_([k[3:] for k in batch if k.startswith("nd:")]),
                    Track.deezer_track_id.in_([k[3:] for k in batch if k.startswith("dz:")])))
            for dz, nd in await s.execute(links):
                pair = {f"dz:{dz}", f"nd:{nd}"}
                index.join("track", pair)
                found.update(pair)
        pending = found - keys
        keys.update(pending)
    return {key: index.root(("track", key))[1] for key in keys}


def _key(aliases, ref, nd):
    return aliases.get(ref or (f"nd:{nd}" if nd else None))


async def load(user_id: int, *, now: datetime, exclude_wave: str | None = None,
               exclude_smart: str | None = None) -> dict[str, float]:
    """Precompute one linearly decaying deduction per recording, never a sum of offers or aliases."""
    now = utc(now)
    async with session_scope() as s:
        rows = (await s.execute(_dated(user_id, now - WINDOW, now, latest_only=True,
                                       exclude_wave=exclude_wave, exclude_smart=exclude_smart))).all()
        aliases = await _live_aliases(s, rows)
    newest = {}
    for _, _, ref, nd, at in rows:
        key = _key(aliases, ref, nd)
        if key and (key not in newest or newest[key] < at):
            newest[key] = at
    scores = {key: PENALTY * (1 - (now - at) / WINDOW) for key, at in newest.items() if now - at < WINDOW}
    return {alias: scores[key] for alias, key in aliases.items() if key in scores}


def penalty(scores: dict[str, float], ref: str, nd_song_id: str | None) -> float:
    return max(scores.get(ref, 0.0), scores.get(f"nd:{nd_song_id}", 0.0)) if nd_song_id else scores.get(ref, 0.0)


def _report_queries(user_id: int, since: datetime, now: datetime, *, with_reoffers: bool):
    lookback = since - WINDOW
    dated = _dated(user_id, lookback, now, latest_only=False, with_reoffers=with_reoffers)
    preserved = (select(func.count(WaveReoffer.item_id)).where(WaveReoffer.item_id == WaveItem.id).scalar_subquery()
                 if with_reoffers else 0)
    undated = select(func.sum(func.max(0, WaveItem.handed_count - 2 - preserved))).select_from(WaveItem).join(
        WaveSession, WaveSession.id == WaveItem.session_id).where(*_wave_owner(user_id),
        WaveItem.handed_count > 2, WaveItem.last_handed_at.between(lookback, now))
    return dated, undated


def _summarize(rows, since: datetime, undated: int | None) -> dict:
    # Measures should be reproducible offline: use the identities actually recorded on the dated
    # handouts, not today's mutable library links. Neither an attempt nor a like is an exposure.
    index, keys = _identity_index(rows)
    aliases = {key: index.root(("track", key))[1] for key in keys}
    dated = {(at, surface, context, key) for surface, context, ref, nd, at in rows
             if (key := _key(aliases, ref, nd))}
    by_surface = {surface: 0 for surface in ("mix", "wave", "smart")}
    repeats = 0
    last = {}
    for at, batch in groupby(sorted(dated), key=lambda row: row[0]):
        # Simultaneous offers from different sessions are not evidence that one preceded the other.
        keys = set()
        for _, surface, _, key in batch:
            if at >= since:
                by_surface[surface] += 1
                if key in last and at - last[key] < WINDOW:
                    repeats += 1
            keys.add(key)
        last.update(dict.fromkeys(keys, at))
    offers = sum(by_surface.values())
    limitations = [
        "Считаются датированные выдачи, не запуски: активные готовые песни микса, треки Волны и вставки умного перемешивания. Собственные песни списка не входят.",
        "Повтор — у этого аккаунта тот же трек уже выдавался менее семи суток назад. Одинаковые треки одной выдачи считаются один раз.",
        "Тождество треков подтверждают идентификаторы в записях выдачи. Если связь каталога с библиотекой тогда не записали, часть повторов не видна.",
    ]
    if undated:
        limitations.append(
            f"Промежуточных выдач Волны без точной даты: {undated}. Это серии, последняя выдача которых попала в период "
            "или семидневную предысторию. Эти выдачи не включены ни в число треков, ни в число повторов.")
    if any(surface == "wave" for _, surface, _, _ in dated):
        limitations.append("В старых Волнах повторная выдача могла означать подтверждение очереди; теперь такие обновления не считаются.")
    if any(surface == "mix" for _, surface, _, _ in dated):
        limitations.append("Для старых миксов момент готовности приблизителен: он был восстановлен по дате создания микса.")
    return dict(offers=offers, repeats=repeats, per_50=50 * repeats / offers if offers else None,
                by_surface=by_surface, lookback_days=WINDOW.days, wave_undated_handouts=undated or 0,
                limitations=limitations)


async def report(s: AsyncSession, user_id: int, *, days: int, now: datetime) -> dict:
    """Repeats per 50 dated handouts. The prior seven days provide lookback, never the denominator."""
    now = utc(now)
    since = now - timedelta(days=days)
    dated, undated = _report_queries(user_id, since, now, with_reoffers=True)
    return _summarize((await s.execute(dated)).all(), since, await s.scalar(undated))


def snapshot_report(connection, tables: set[str], user_id: int, *, days: int, now: datetime) -> dict | None:
    """Same queries and accounting on the CLI's existing read-only sqlite3 connection, no engine or migration."""
    from sqlalchemy.dialects import sqlite

    if not {"mixes", "mix_items", "wave_sessions", "wave_items", "smart_sessions", "smart_items"} <= tables:
        return None
    if "offered_at" not in {r[1] for r in connection.execute("PRAGMA table_info(mix_items)")}:
        return None  # no date for a mix becoming playable: a missing denominator is not zero
    now = utc(now)
    since = now - timedelta(days=days)
    dated, undated = _report_queries(user_id, since, now, with_reoffers="wave_reoffers" in tables)

    def fetch(statement):
        return connection.execute(str(statement.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True})))

    rows = [(surface, ctx, ref, nd, utc(datetime.fromisoformat(at))) for surface, ctx, ref, nd, at in fetch(dated)]
    return _summarize(rows, since, fetch(undated).fetchone()[0])
