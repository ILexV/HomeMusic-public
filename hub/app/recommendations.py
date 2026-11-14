"""Account-scoped recommendation outcomes and immutable decision explanations.

Only playback attempts with explicit recommendation context are counted; attribution
additionally requires an owned, already-handed-out decision matching the track.
Completion/skip/repeat rates divide by terminal finish/skip attempts, never errors,
interruptions, offers or unfinished starts. New-like rate divides by eligible new
plays. Learning uses 90 days, >=20 samples/source and a 40-sample neutral prior.
"""
from __future__ import annotations

from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from time import monotonic
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import engine, session_scope
from app.insights.launches import Aliases, NOT_USER, PLAY_KINDS, test_traffic
from app.models import Mix, MixItem, PlayEvent, SmartItem, SmartSession, WaveItem, WaveSession, utcnow

LEARNING_DAYS = 90
MIN_SAMPLES = 20
ADAPTATION_TTL_S = 60
ADAPTATION_CACHE_MAX = 256
_adaptation_cache: OrderedDict[tuple[Any, int], tuple[float, dict[str, float]]] = OrderedDict()


@dataclass
class Decision:
    user_id: int
    context: str
    ref: str | None
    nd_song_id: str | None
    at: datetime
    generator: str
    familiar: bool
    policy_version: str | None = None


def source_title(name: str) -> str:
    from app.mixes.wave import generator_title
    return generator_title(name)


def explain_candidate(c) -> list[str]:
    """Pure: only recorded inputs, no catalog, profile refresh or default weights."""
    parts = c.parts or {}
    contributions = parts.get("generator_contributions") or {}
    source_reasons = {
        "favorites": "Из любимой и часто слушаемой музыки вашего профиля",
        "stash": "Из вашей коллекции: давно не звучало или ещё не слушали",
        "new_releases": "Свежий релиз исполнителя, использованного при подборе",
        "session_radio": "Радио по песне, которую вы дослушали или отметили лайком в этой волне",
        "artist_songs": "Песня исполнителя, по которому запущена волна",
        "seed": "Выбранная вами отправная точка волны",
    }
    reasons = [source_reasons.get(g, f"{source_title(g)}: вклад в оценку {value:.2f}") for g, value in
               sorted(contributions.items(), key=lambda x: (-x[1], x[0])) if value > 0]
    if (parts.get("sound") or 0) > 0 and c.sim is not None:
        reasons.append("Звуковая модель отметила близость к вашему вкусу")
    if (parts.get("playlist_sound") or 0) > 0 and parts.get("playlist_similarity") is not None:
        reasons.append("Звуковая модель отметила близость к песням этого списка")
    if c.known and c.known.get("familiar"):
        reasons.append("Знакомый трек по профилю на момент выбора")
    elif c.familiar:
        reasons.append("Знакомый трек или исполнитель по профилю на момент выбора")
    else:
        reasons.append("Отнесено к незнакомой части подборки на момент выбора")
    if parts.get("temporary_multiplier", 1) < 1:
        reasons.append("Приоритет исполнителя временно снижен по вашей просьбе")
    return reasons


def stored_reasons(data: dict | None) -> list[str]:
    value = (data or {}).get("explanation")
    return [x for x in value if isinstance(x, str)] if isinstance(value, list) else []


def learned_factor(samples: int, completed: int, early_skips: int) -> float:
    if samples < MIN_SAMPLES:
        return 1.0
    # Symmetric evidence around neutral; late skips don't reward or punish a source.
    return max(.75, min(1.25, 1 + .25 * (completed - early_skips) / (samples + 40)))


def _attempts(events: list[PlayEvent], aliases: Aliases) -> list[list[PlayEvent]]:
    """Deduplicate retries; legacy starts pair only within a bounded client stream."""
    groups: dict[tuple, list[PlayEvent]] = {}
    legacy: list[list[PlayEvent]] = []
    opened: dict[tuple, list[PlayEvent]] = {}
    seen: set[tuple] = set()
    legacy_seen: set[tuple] = set()
    failed: dict[tuple, list[PlayEvent]] = {}
    for e in sorted(events, key=lambda e: (e.ts, e.id or 0)):
        signature = (e.user_id, e.event_id) if e.event_id else (
            e.user_id, e.client, e.ts, e.kind, e.ref, e.nd_song_id, e.position_s, e.listened_ms)
        if signature in seen:
            continue
        seen.add(signature)
        key = aliases.key(e.ref, e.nd_song_id)
        if e.playback_id:
            groups.setdefault((e.user_id, e.device_id or e.client, e.playback_id, key), []).append(e)
            continue
        stream = (e.user_id, e.device_id or e.client, key, e.context)
        semantic = (stream, e.ts, e.kind, e.position_s, e.listened_ms)
        if semantic in legacy_seen:
            continue
        legacy_seen.add(semantic)
        if e.kind == "start":
            failed.pop(stream, None)
        elif e.kind in ("skip", "finish") and stream in failed and \
                e.ts - failed[stream][-1].ts <= timedelta(minutes=5):
            failed[stream].append(e)
            continue
        current = opened.get(stream)
        if e.kind == "start" or current is None or e.ts - current[0].ts > timedelta(hours=6):
            current = [e]
            legacy.append(current)
            opened[stream] = current
        else:
            current.append(e)
        if e.kind in ("skip", "finish", "error", "interrupted"):
            if e.kind == "error":
                failed[stream] = current
            opened.pop(stream, None)
    return sorted([*groups.values(), *legacy], key=lambda rows: (rows[0].ts, rows[0].id or 0))


def summarize(user_id: int, events: list[PlayEvent], decisions: list[Decision], *, days: int,
              now: datetime) -> dict[str, Any]:
    """Pure aggregation; safe even if a caller accidentally supplies another account."""
    since = now - timedelta(days=days)
    events = [e for e in events if e.user_id == user_id and e.ts <= now and
              not e.superseded_scrobble and not test_traffic(e.client) and e.target_type == "track"]
    decisions = [d for d in decisions if d.user_id == user_id and d.at <= now]
    aliases = Aliases([*events, *decisions])
    lookup: dict[tuple[str, str], list[Decision]] = defaultdict(list)
    for d in decisions:
        lookup[d.context, aliases.key(d.ref, d.nd_song_id)].append(d)
    result = dict(days=days, since=since, attempts=0, starts=0, eligible_plays=0, completed=0,
                  early_skips=0, playback_errors=0, interrupted=0, new_track_plays=0,
                  new_track_likes=0, repeats=0)
    sources: dict[str, dict] = {}
    cohorts: dict[str, dict] = {}
    heard: set[str] = set()
    new_plays: dict[str, list[tuple[datetime, str, dict]]] = defaultdict(list)
    legacy = unattributed = 0
    for rows in _attempts([e for e in events if e.kind in PLAY_KINDS], aliases):
        first = rows[0]
        if first.ts < since:
            continue
        contexts = {e.context for e in rows if e.context}
        context = next(iter(contexts)) if len(contexts) == 1 else ""
        if not context or context.partition(":")[0] not in ("mix", "wave", "smart") or ":" not in context:
            continue
        key = aliases.key(first.ref, first.nd_song_id)
        matches = [d for d in lookup.get((context, key), []) if d.at <= first.ts]
        decision = max(matches, key=lambda d: d.at) if matches else None
        source = None
        if decision:
            source = sources.setdefault(decision.generator, dict(id=decision.generator,
                title=source_title(decision.generator), attempts=0, completed=0, early_skips=0,
                playback_errors=0, new_track_likes=0, learning_samples=0, multiplier=1.0))
            source["attempts"] += 1
        else:
            unattributed += 1
        legacy += not bool(first.playback_id)
        result["attempts"] += 1
        policy = decision.policy_version if decision and decision.policy_version else "unknown"
        cohort = cohorts.setdefault(policy, dict(policy_version=policy, attempts=0, eligible_plays=0,
            completed=0, early_skips=0))
        cohort["attempts"] += 1
        kinds = {e.kind for e in rows}
        result["starts"] += "start" in kinds
        error = "error" in kinds
        interrupted = "interrupted" in kinds and not error
        result["playback_errors"] += error
        result["interrupted"] += interrupted
        if source:
            source["playback_errors"] += error
        if error or interrupted or not kinds.intersection(("finish", "skip")):
            continue
        result["eligible_plays"] += 1
        complete = "finish" in kinds
        skips = [e for e in rows if e.kind == "skip"]
        listened = max((e.listened_ms for e in rows if e.listened_ms is not None), default=None)
        early = not complete and bool(skips) and (listened < 30000 if listened is not None else
                skips[-1].position_s is not None and skips[-1].position_s < 30)
        result["completed"] += complete
        result["early_skips"] += early
        cohort["eligible_plays"] += 1
        cohort["completed"] += complete
        cohort["early_skips"] += early
        result["repeats"] += key in heard
        heard.add(key)
        if source:
            source["completed"] += complete
            source["early_skips"] += early
            source["learning_samples"] += 1
        if decision and not decision.familiar:
            result["new_track_plays"] += 1
            new_plays[key].append((first.ts, context, source))
    # A like rewards at most one preceding eligible new play, not every repeat.
    rewarded: set[tuple[str, datetime, str]] = set()
    seen_likes: set[str] = set()
    for e in sorted(events, key=lambda e: (e.ts, e.id or 0)):
        if e.kind != "like" or e.ts < since or e.context in NOT_USER or e.reverts_event_id or \
                (e.target_data or {}).get("provenance") == "import":
            continue
        key = aliases.key(e.ref, e.nd_song_id)
        if key in seen_likes:
            continue
        matches = [p for p in new_plays.get(key, []) if p[0] <= e.ts <= p[0] + timedelta(days=1)
                   and (not e.context or e.context == p[1])]
        if matches:
            at, context, source = max(matches, key=lambda p: p[0])
            if (key, at, context) not in rewarded:
                rewarded.add((key, at, context))
                seen_likes.add(key)
                result["new_track_likes"] += 1
                source["new_track_likes"] += 1
    for source in sources.values():
        source["multiplier"] = learned_factor(source["learning_samples"], source["completed"], source["early_skips"])
    eligible = result["eligible_plays"]
    result.update(completion_rate=result["completed"] / eligible if eligible else None,
                  early_skip_rate=result["early_skips"] / eligible if eligible else None,
                  repeat_rate=result["repeats"] / eligible if eligible else None,
                  new_track_like_rate=result["new_track_likes"] / result["new_track_plays"]
                  if result["new_track_plays"] else None,
                  sources=sorted(sources.values(), key=lambda x: x["id"]), limitations=[
                      "Только попытки с явным контекстом mix/wave/smart; предложения без запуска не считаются.",
                      "Доли дослушиваний, ранних пропусков и повторов: от завершённых/пропущенных попыток; ошибки, прерывания и незавершённые запуски исключены.",
                      "Новые треки: незнакомые по сохранённому решению; доля лайков — от их допустимых прослушиваний (лайк в течение суток).",
                      "Повтор — повторное допустимое прослушивание трека внутри выбранного периода.",
                      "Обучение: 90 дней, минимум 20 исходов источника, нейтральный приоритет при малой выборке.",
                  ])
    for cohort in cohorts.values():
        n = cohort["eligible_plays"]
        cohort.update(completion_rate=cohort["completed"] / n if n else None,
                      early_skip_rate=cohort["early_skips"] / n if n else None)
    result.update(until=now, metric_version="recommendation-outcomes-v2",
                  policy_cohorts=sorted(cohorts.values(), key=lambda c: c["policy_version"]),
                  provenance="owned play_events joined to recorded recommendation decisions")
    if legacy:
        result["limitations"].append(f"Без playback_id: {legacy}; события объединены по треку и клиенту, возможна неточность.")
    if unattributed:
        result["limitations"].append(f"Без подтверждённого источника: {unattributed}; они не обучают веса.")
    return result


async def load_data(s: AsyncSession, user_id: int, now: datetime, days: int):
    rows = list((await s.execute(select(PlayEvent).where(
        PlayEvent.user_id == user_id, PlayEvent.ts >= now - timedelta(days=days, hours=6),
        PlayEvent.ts <= now, PlayEvent.target_type == "track",
        PlayEvent.superseded_scrobble.is_(False)).order_by(PlayEvent.ts, PlayEvent.id))).scalars())
    contexts = {e.context for e in rows if e.context}
    mix_ids = [int(x[4:]) for x in contexts if x.startswith("mix:") and x[4:].isdigit()]
    decisions = []
    if mix_ids:
        for item, created, stats in (await s.execute(select(MixItem, Mix.created_at, Mix.stats_json).join(Mix, Mix.id == MixItem.mix_id)
                .where(Mix.user_id == user_id, Mix.id.in_(mix_ids), MixItem.state != "reserve"))).all():
            decisions.append(Decision(user_id, f"mix:{item.mix_id}", item.ref, item.nd_song_id, created,
                                      item.generator, item.familiar, (stats or {}).get("taste_model")))
    for model, session, prefix, time_field in ((WaveItem, WaveSession, "wave", "first_handed_at"),
                                               (SmartItem, SmartSession, "smart", "handed_at")):
        ids = [x[len(prefix) + 1:] for x in contexts if x.startswith(prefix + ":")]
        if ids:
            items = (await s.execute(select(model, session.taste_model).join(session, session.id == model.session_id).where(
                model.user_id == user_id, session.user_id == user_id, model.session_id.in_(ids)))).all()
            decisions.extend(Decision(user_id, f"{prefix}:{i.session_id}", i.ref, i.nd_song_id,
                                      getattr(i, time_field), i.generator, i.familiar, version) for i, version in items)
    return rows, decisions


async def quality(s: AsyncSession, user_id: int, days: int = 30, *, now: datetime | None = None) -> dict:
    now = now or utcnow()
    events, decisions = await load_data(s, user_id, now, max(LEARNING_DAYS, days * 2))
    result = summarize(user_id, events, decisions, days=days, now=now)
    learned = summarize(user_id, events, decisions, days=LEARNING_DAYS, now=now)
    by_source = {x["id"]: x for x in result["sources"]}
    from app.config import get_settings
    for name in {*get_settings().cfg.mixes.weights, *(x["id"] for x in learned["sources"])}:
        by_source.setdefault(name, dict(id=name, title=source_title(name), attempts=0, completed=0,
            early_skips=0, playback_errors=0, new_track_likes=0, multiplier=1.0, learning_samples=0))
    for source in learned["sources"]:
        by_source[source["id"]].update(multiplier=source["multiplier"], learning_samples=source["learning_samples"])
    result["sources"] = sorted(by_source.values(), key=lambda x: x["id"])
    since = now - timedelta(days=days)
    previous = summarize(user_id, [e for e in events if e.ts < since], decisions, days=days, now=since)
    fields = ("since", "until", "attempts", "eligible_plays", "completed", "early_skips",
              "completion_rate", "early_skip_rate", "policy_cohorts")
    result["comparison"] = {"current": {k: result[k] for k in fields},
                            "previous": {k: previous[k] for k in fields},
                            "limitations": [
                                "Соседние непересекающиеся периоды одного аккаунта; N — допустимые завершённые/пропущенные попытки.",
                                "Наблюдательное сравнение, не A/B-тест и не причинный эффект политики; состав треков и контекст меняются.",
                                "Версия берётся только из сохранённых данных микса/сессии; неизвестная версия выделена отдельно.",
                                "Shadow/replay не воспроизводит неслучившиеся прослушивания: для альтернативной политики исходы неизвестны.",
                            ]}
    from app.matching.quality import recording_quality
    from app.recording_models import RecordingBinding
    bindings = (await s.execute(select(RecordingBinding).where(RecordingBinding.user_id == user_id,
        RecordingBinding.created_at >= since, RecordingBinding.created_at <= now))).scalars()
    result["recordings"] = recording_quality(bindings, user_id=user_id, since=since, until=now)
    return result


def clear_adaptation_cache() -> None:
    """Explicit test/reset hook; engine identity also isolates database lifecycles."""
    _adaptation_cache.clear()


async def adapted_weights(user_id: int, base: dict[str, float]) -> dict[str, float]:
    key = (engine(), user_id)
    cached = _adaptation_cache.get(key)
    if cached is not None and cached[0] > monotonic():
        _adaptation_cache.move_to_end(key)
        factors = cached[1]
    else:
        now = utcnow()
        async with session_scope() as s:
            events, decisions = await load_data(s, user_id, now, LEARNING_DAYS)
        report = summarize(user_id, events, decisions, days=LEARNING_DAYS, now=now)
        factors = {x["id"]: x["multiplier"] for x in report["sources"]}
        _adaptation_cache[key] = (monotonic() + ADAPTATION_TTL_S, factors)
        _adaptation_cache.move_to_end(key)
        while len(_adaptation_cache) > ADAPTATION_CACHE_MAX:
            _adaptation_cache.popitem(last=False)
    return {name: weight * factors.get(name, 1.0) for name, weight in base.items()}
