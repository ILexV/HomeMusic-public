"""Account-scoped recommendation outcomes and immutable decision explanations.

Only playback attempts with explicit recommendation context are counted; attribution
additionally requires an owned, already-handed-out decision matching the track.
Completion/skip/repeat rates divide by terminal finish/skip attempts, never errors,
interruptions, offers or unfinished starts. New-like rate divides by eligible new
plays. Learning uses 90 days, >=20 effective samples/source and a 40-sample neutral prior.
"""
from __future__ import annotations

from collections import Counter, OrderedDict, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import fsum
from sys import float_info
from time import monotonic
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import engine, session_scope
from app.insights.launches import Aliases, NOT_USER, test_traffic
from app.mixes.taste_snapshot import PLAYBACK_KINDS, TasteEvent, playback_attempts
from app.models import Mix, MixItem, PlayEvent, SmartItem, SmartSession, WaveItem, WaveSession, utcnow

LEARNING_DAYS = 90
MIN_SAMPLES = 20
ADAPTATION_TTL_S = 60
ADAPTATION_CACHE_MAX = 256
_adaptation_cache: OrderedDict[tuple[Any, int], tuple[float, dict[str, float]]] = OrderedDict()


@dataclass(eq=False, frozen=True)
class Decision:
    user_id: int
    context: str
    ref: str | None
    nd_song_id: str | None
    at: datetime
    generator: str | None
    familiar: bool | None
    policy_version: str | None = None
    decision_json: dict | None = None

    @property
    def is_new_track(self) -> bool:
        snapshot = self.decision_json
        if isinstance(snapshot, dict) and "novelty" in snapshot:
            return snapshot["novelty"] in ("new_track_known_artist", "new_artist")
        # Historical False confirms the track wasn't known; True could refer to just the artist.
        return self.familiar is False


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
    if c.novelty == "familiar_track":
        reasons.append("Знакомый трек по вашему профилю на момент выбора")
    elif c.novelty == "new_track_known_artist":
        reasons.append("Новый трек знакомого исполнителя по вашему профилю на момент выбора")
    elif c.novelty == "new_artist":
        reasons.append("Новый для вашего профиля исполнитель на момент выбора")
    if parts.get("temporary_multiplier", 1) < 1:
        reasons.append("Приоритет исполнителя временно снижен по вашей просьбе")
    return reasons


def stored_reasons(data: dict | None) -> list[str]:
    value = (data or {}).get("explanation")
    return [x for x in value if isinstance(x, str)] if isinstance(value, list) else []


def _source_credit(decision: Decision) -> tuple[dict[str, float], bool]:
    snapshot = decision.decision_json
    parts = snapshot.get("score_parts") if isinstance(snapshot, dict) else None
    raw = parts.get("generator_contributions") if isinstance(parts, dict) else None
    if isinstance(raw, dict):
        positives = {k: v for k, v in raw.items() if isinstance(k, str) and k.strip() and
                     type(v) in (int, float) and 0 < v <= float_info.max}
        if positives:
            # Two large finite contributions may overflow when added; their ratios are still well-defined.
            peak = max(positives.values())
            for name in positives:
                positives[name] /= peak
            total = fsum(positives.values())
            for name in positives:
                positives[name] /= total
            return positives, False
    return ({decision.generator: 1.0}, True) if decision.generator else ({}, False)


def source_shares(decision: Decision) -> dict[str, float]:
    """One unit across positive frozen contributions, else only the recorded leader."""
    return _source_credit(decision)[0]


def _empty_source(name: str) -> dict:
    return dict(id=name, title=source_title(name), attempts=0, completed=0, early_skips=0,
                playback_errors=0, new_track_likes=0, learning_samples=0, fallback_attempts=0,
                effective_attempts=0., effective_eligible_plays=0., effective_completed=0.,
                effective_early_skips=0., effective_playback_errors=0., effective_new_track_likes=0.,
                effective_learning_samples=0., multiplier=1., winner_only_multiplier=1.)


def learned_factor(samples: float, completed: float, early_skips: float) -> float:
    if samples < MIN_SAMPLES:
        return 1.0
    # Symmetric evidence around neutral; late skips don't reward or punish a source.
    return max(.75, min(1.25, 1 + .25 * (completed - early_skips) / (samples + 40)))


def _source_report(counts: dict[Decision, Counter]) -> list[dict]:
    sources = {}
    fractions: dict[str, list[tuple[float, Counter]]] = defaultdict(list)
    for decision, outcomes in counts.items():
        shares, fallback = _source_credit(decision)
        leader = decision.generator
        if leader:
            if leader not in sources:
                sources[leader] = _empty_source(leader)
            legacy = sources[leader]
            for metric, count in outcomes.items():
                legacy["learning_samples" if metric == "eligible_plays" else metric] += count
            if fallback:
                legacy["fallback_attempts"] += outcomes["attempts"]
        for name, share in shares.items():
            if name not in sources:
                sources[name] = _empty_source(name)
            fractions[name].append((share, outcomes))
    for name, source in sources.items():
        for metric in ("attempts", "eligible_plays", "completed", "early_skips", "playback_errors", "new_track_likes"):
            # Sum the weighted per-decision tallies accurately even at the strict learning threshold.
            source["effective_" + metric] = fsum(share * counts[metric] for share, counts in fractions[name])
        source["effective_learning_samples"] = n = source["effective_eligible_plays"]
        source["multiplier"] = learned_factor(n, source["effective_completed"], source["effective_early_skips"])
        source["winner_only_multiplier"] = learned_factor(source["learning_samples"], source["completed"],
                                                         source["early_skips"])
    return sorted(sources.values(), key=lambda x: x["id"])


def summarize(user_id: int, events: list[PlayEvent], decisions: list[Decision], *, days: int,
              now: datetime) -> dict[str, Any]:
    """Pure aggregation; safe even if a caller accidentally supplies another account."""
    since = now - timedelta(days=days)
    events = [e for e in events if e.user_id == user_id and e.ts <= now and (not e.received_at or e.received_at <= now) and
              not e.superseded_scrobble and not test_traffic(e.client) and e.target_type == "track"]
    decisions = [d for d in decisions if d.user_id == user_id and d.at <= now and (d.ref or d.nd_song_id)]
    aliases = Aliases([*events, *decisions])
    lookup: dict[tuple[str, str], list[Decision]] = defaultdict(list)
    for d in decisions:
        lookup[d.context, aliases.key(d.ref, d.nd_song_id)].append(d)
    result = dict(days=days, since=since, attempts=0, starts=0, eligible_plays=0, completed=0,
                  early_skips=0, playback_errors=0, interrupted=0, new_track_plays=0,
                  new_track_likes=0, repeats=0)
    counts: dict[Decision, Counter] = defaultdict(Counter)
    cohorts: dict[str, dict] = {}
    heard: set[str] = set()
    new_plays: dict[str, list[tuple[datetime, str, Decision]]] = defaultdict(list)
    legacy = unattributed = 0
    playback = (TasteEvent.from_row(e) for e in events if e.kind in PLAYBACK_KINDS)
    for attempt in playback_attempts(playback, lambda e: aliases.key(e.ref, e.nd_song_id)):
        first = attempt.first
        if first.ts < since:
            continue
        context = attempt.context
        if not context or context.partition(":")[0] not in ("mix", "wave", "smart") or ":" not in context:
            continue
        key = aliases.key(first.ref, first.nd_song_id)
        decision = None
        if key and all(aliases.key(e.ref, e.nd_song_id) == key for e in attempt.events):
            decision = max((d for d in lookup.get((context, key), ()) if d.at <= first.ts),
                           key=lambda d: d.at, default=None)
        if decision:
            counts[decision]["attempts"] += 1
        else:
            unattributed += 1
        legacy += not bool(first.playback_id)
        result["attempts"] += 1
        policy = decision.policy_version if decision and decision.policy_version else "unknown"
        cohort = cohorts.setdefault(policy, dict(policy_version=policy, attempts=0, eligible_plays=0,
            completed=0, early_skips=0))
        cohort["attempts"] += 1
        result["starts"] += attempt.start is not None
        kind = attempt.terminal.kind if attempt.terminal else None
        error = kind == "error"
        interrupted = kind == "interrupted"
        result["playback_errors"] += error
        result["interrupted"] += interrupted
        if decision and error:
            counts[decision]["playback_errors"] += 1
        if not (outcome := attempt.outcome):
            continue
        result["eligible_plays"] += 1
        complete = outcome.kind == "finish"
        early = outcome.early_skip
        result["completed"] += complete
        result["early_skips"] += early
        cohort["eligible_plays"] += 1
        cohort["completed"] += complete
        cohort["early_skips"] += early
        result["repeats"] += key in heard
        heard.add(key)
        if decision:
            counts[decision]["completed"] += complete
            counts[decision]["early_skips"] += early
            counts[decision]["eligible_plays"] += 1
        if decision and decision.is_new_track:
            result["new_track_plays"] += 1
            new_plays[key].append((first.ts, context, decision))
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
            at, context, decision = max(matches, key=lambda p: p[0])
            if (key, at, context) not in rewarded:
                rewarded.add((key, at, context))
                seen_likes.add(key)
                result["new_track_likes"] += 1
                counts[decision]["new_track_likes"] += 1
    eligible = result["eligible_plays"]
    result.update(completion_rate=result["completed"] / eligible if eligible else None,
                  early_skip_rate=result["early_skips"] / eligible if eligible else None,
                  repeat_rate=result["repeats"] / eligible if eligible else None,
                  new_track_like_rate=result["new_track_likes"] / result["new_track_plays"]
                  if result["new_track_plays"] else None,
                  sources=_source_report(counts), limitations=[
                      "Только попытки с явным контекстом mix/wave/smart; предложения без запуска не считаются.",
                      "Доли дослушиваний, ранних пропусков и повторов: от завершённых/пропущенных попыток; ошибки, прерывания и незавершённые запуски исключены.",
                      "Новые треки: незнакомые по сохранённому решению; доля лайков — от их допустимых прослушиваний (лайк в течение суток).",
                      "Повтор — повторное допустимое прослушивание трека внутри выбранного периода.",
                      "Обучение: 90 дней, минимум 20 эффективных исходов. Один исход делится пропорционально сохранённым положительным вкладам.",
                      "Без положительных вкладов используется только записанный источник-лидер (fallback_attempts); его целые счётчики и winner_only_multiplier доступны для сравнения.",
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


def wave_snapshot_time(first, last, count, snapshot):
    # Older repeats overwrote the snapshot, so it cannot explain earlier plays.
    # New-format snapshots carry the novelty key and remain frozen at first handout.
    if (count or 1) > 1 and not (isinstance(snapshot, dict) and "novelty" in snapshot):
        return last
    return first


async def load_data(s: AsyncSession, user_id: int, now: datetime, days: int):
    rows = list((await s.execute(select(PlayEvent).where(
        PlayEvent.user_id == user_id, PlayEvent.ts >= now - timedelta(days=days, hours=6),
        PlayEvent.ts <= now, PlayEvent.received_at <= now, PlayEvent.target_type == "track",
        PlayEvent.superseded_scrobble.is_(False)).order_by(PlayEvent.ts, PlayEvent.id))).scalars())
    contexts = {e.context for e in rows if e.context}
    mix_ids = [int(x[4:]) for x in contexts if x.startswith("mix:") and x[4:].isdigit()]
    decisions = []
    if mix_ids:
        for item, created, stats in (await s.execute(select(MixItem, Mix.created_at, Mix.stats_json).join(Mix, Mix.id == MixItem.mix_id)
                .where(Mix.user_id == user_id, Mix.id.in_(mix_ids), MixItem.state != "reserve",
                       (MixItem.state == "active") | MixItem.offered_at.is_not(None)))).all():
            decisions.append(Decision(user_id, f"mix:{item.mix_id}", item.ref, item.nd_song_id,
                                      item.offered_at or created, item.generator, item.familiar,
                                      (stats or {}).get("taste_model"), item.features_json))
    for model, session, prefix in ((WaveItem, WaveSession, "wave"), (SmartItem, SmartSession, "smart")):
        ids = [x[len(prefix) + 1:] for x in contexts if x.startswith(prefix + ":")]
        if ids:
            items = (await s.execute(select(model, session.taste_model).join(session, session.id == model.session_id).where(
                model.user_id == user_id, session.user_id == user_id, model.session_id.in_(ids)))).all()
            for item, version in items:
                at = (wave_snapshot_time(item.first_handed_at, item.last_handed_at, item.handed_count, item.decision_json)
                      if prefix == "wave" else item.handed_at)
                decisions.append(Decision(user_id, f"{prefix}:{item.session_id}", item.ref, item.nd_song_id, at,
                                          item.generator, item.familiar, version, item.decision_json))
    return rows, decisions


def with_learning_window(result: dict, events: list[PlayEvent], decisions: list[Decision],
                         *, user_id: int, now: datetime, configured_sources: Iterable[str] = ()) -> None:
    """Keep displayed outcomes in the report window; both weight models always learn over 90 days."""
    learned = result if result["days"] == LEARNING_DAYS else summarize(
        user_id, events, decisions, days=LEARNING_DAYS, now=now)
    by_source = {x["id"]: x for x in result["sources"]}
    for name in configured_sources:
        if name not in by_source:
            by_source[name] = _empty_source(name)
    history = {s["id"]: s for s in learned["sources"]}
    for name in history:
        if name not in by_source:
            by_source[name] = _empty_source(name)
    for name, row in by_source.items():
        learned_row = history.get(name)
        for field, neutral in (("learning_samples", 0), ("effective_learning_samples", 0.),
                               ("multiplier", 1.), ("winner_only_multiplier", 1.)):
            row[field] = learned_row[field] if learned_row else neutral
    result["sources"] = sorted(by_source.values(), key=lambda x: x["id"])


async def quality(s: AsyncSession, user_id: int, days: int = 30, *, now: datetime | None = None) -> dict:
    now = now or utcnow()
    events, decisions = await load_data(s, user_id, now, max(LEARNING_DAYS, days * 2))
    result = summarize(user_id, events, decisions, days=days, now=now)
    from app.discovery_adoption import report as discovery_adoption
    result["discovery_adoption"] = await discovery_adoption(s, user_id, events, decisions, now=now, days=days)
    # The names alone populate empty report rows; never reconstruct historical credit from current weights.
    from app.config import get_settings
    with_learning_window(result, events, decisions, user_id=user_id, now=now,
                         configured_sources=get_settings().cfg.mixes.weights)
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
