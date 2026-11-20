"""Observed, account-scoped discoveries and the conservative Discover subtype policy."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.favorites import AliasIndex, effective_decisions
from app.insights.launches import NOT_USER, SELF_SELECTED, test_traffic
from app.mixes.taste_math import _heard
from app.mixes.taste_snapshot import PLAYBACK_KINDS, Subject, TasteEvent, playback_attempts
from app.models import FavoriteSubject, Mix, PlayEvent
from app.plays import attempt_trigger
from app.recommendations import LEARNING_DAYS, MIN_SAMPLES, Decision, source_shares, source_title

WINDOW = timedelta(days=14)
RECOMMENDED = {"mix", "wave", "smart"}
AUTOMATIC = RECOMMENDED | {"continuous"}
SUBTYPE_TITLES = {"new_track_known_artist": "Новая песня знакомого исполнителя",
                  "new_artist": "Новый исполнитель", "unknown": "Подтип неизвестен"}


def _subtype_row(subtype):
    return dict(id=subtype, title=SUBTYPE_TITLES[subtype], mature=0, adopted=0, rate=None)


def _balance(mature, adopted):
    known, new = "new_track_known_artist", "new_artist"
    support = min(mature[known], mature[new])
    share = None
    if support < MIN_SAMPLES:
        reason = (f"Баланс режима «Незнакомое» не меняется: за {LEARNING_DAYS} дней нужны хотя бы "
                  f"{MIN_SAMPLES} открытий каждого вида с полным сроком наблюдения и определённым исходом "
                  f"(сейчас {mature[known]} и {mature[new]}).")
    else:
        difference = adopted[known] / mature[known] - adopted[new] / mature[new]
        # Shrink the difference (not each rate toward different priors when sample sizes differ).
        shift = .15 * difference * support / (support + 40)
        if abs(shift) < .01:
            reason = "Баланс режима «Незнакомое» не меняется: доли прижившихся песен двух видов близки."
        else:
            share = .5 + shift
            label = "песни знакомых исполнителей" if shift > 0 else "песни новых исполнителей"
            reason = (f"За {LEARNING_DAYS} дней у вас чаще приживались новые {label}. «Незнакомое» "
                      "может предложить немного больше таких песен, оставляя место обоим видам.")
    return dict(days=LEARNING_DAYS, min_per_subtype=MIN_SAMPLES, known_artist_share=share, reason=reason)


def _keys(ref, nd):
    keys = []
    if ref:
        keys.append(f"ref:{ref}")
        if ref.startswith("nd:"):
            keys.append(ref)
    if nd:
        keys.append(f"nd:{nd}")
    return keys


def _organic(attempt, rows, mix_playlists) -> bool | None:
    """True: selected this track. None: eligible context, but no conclusive attempt provenance."""
    head, _, ident = (attempt.context or "").partition(":")
    if head not in SELF_SELECTED and head != "track":
        return False
    if head == "playlist" and (ident in mix_playlists or (not ident and mix_playlists)):
        return False
    for e in attempt.events:
        row = rows[e.event_id]
        if (row.launch_id or "").partition(":")[0] in AUTOMATIC or \
                (row.launch_origin or "").split("_", 1)[0] in AUTOMATIC:
            return False
    trigger = attempt_trigger(rows[e.event_id] for e in attempt.events)
    return trigger == "manual" if trigger is not None else None


def _liked(marks, aliases, heard, deadline):
    # Fold at *this track's* deadline, not today: equal observation time for
    # every cohort member. Keep prior and service decisions in the causal graph
    # (an imported star cannot be renewed by an idempotent user retry).
    decisions = effective_decisions(marks, [Subject("track", tuple(aliases))], deadline)
    if not decisions:
        return False
    e = decisions[-1]
    return (e.kind == "like" and e.ts > heard and e.context not in NOT_USER and
            not test_traffic(e.client) and (e.target_data or {}).get("provenance") != "import")


def summarize(user_id: int, events: Iterable[PlayEvent], decisions: Iterable[Decision], *,
              now: datetime, days: int, subjects=(), mix_playlist_ids=(), balance: dict | None = None) -> dict:
    """Full available journal, not just the selected date range (a track is new only once)."""
    until = now - WINDOW
    since = until - timedelta(days=days)
    learning_since = until - timedelta(days=LEARNING_DAYS) if balance is not None else since
    oldest = min(since, learning_since)
    subtypes = {k: _subtype_row(k) for k in SUBTYPE_TITLES if k != "unknown"}
    mature = dict.fromkeys(subtypes, 0)
    accepted = mature.copy()
    result = dict(window_days=WINDOW.days, cohort_since=since.isoformat(), cohort_until=until.isoformat(),
                  mature=0, adopted=0, pending=0, unknown_return_tracks=0, rate=None, sources=[], limitations=[
        "Первое услышанное в сохранённой истории аккаунта: только подтверждённое решение микса, волны или умного перемешивания. Пропуски истории не восстанавливаются.",
        "Старый явный признак «незнакомое» учитывается без подтипа; старое «знакомое» не доказывает новизну трека.",
        "У каждого открытия 14 суток: действующий на конец окна пользовательский лайк или два последующих услышанных запуска, когда пользователь явно выбрал этот трек в своём разделе. Недавние открытия не входят в долю.",
        "Автопереход, повтор, запуск всего списка и события без подтверждённого ручного выбора не считаются самостоятельным возвращением. Неизвестный способ запуска не восстанавливается из истории.",
    ])
    result["subtypes"] = list(subtypes.values())
    decisions = [d for d in decisions if d.user_id == user_id and d.at <= now and (d.ref or d.nd_song_id) and
                 d.context.partition(":")[0] in RECOMMENDED and d.context.partition(":")[2]]
    if not any(d.is_new_track for d in decisions):
        if balance is not None:
            balance.update(_balance(mature, accepted))
        return result
    rows = {e.event_id: e for e in events if e.user_id == user_id and (e.target_type or "track") == "track" and
            (e.ref or e.nd_song_id) and not e.superseded_scrobble and e.ts <= now and
            e.received_at is not None and e.received_at <= now and
            (not test_traffic(e.client) or (e.client == "import" and e.kind in ("like", "unlike")))}
    index = AliasIndex(subjects=subjects)
    for ref, nd in {(x.ref, x.nd_song_id) for seq in (decisions, rows.values()) for x in seq}:
        index.join("track", set(_keys(ref, nd)))

    def key(x):
        return index.root(("track", _keys(x.ref, x.nd_song_id)[0]))[1]

    aliases = defaultdict(list)
    for typ, alias in index.parent:
        if typ == "track":
            aliases[index.root((typ, alias))[1]].append(alias)
    choices = defaultdict(list)
    for d in decisions:
        choices[key(d), d.context].append(d)
    marks = defaultdict(list)
    plays = []
    for e in rows.values():
        if e.kind in ("like", "unlike"):
            marks[key(e)].append(e)
        elif e.kind in PLAYBACK_KINDS and e.context not in NOT_USER and (e.target_data or {}).get("provenance") != "import":
            plays.append(TasteEvent.from_row(e))

    heard = defaultdict(list)
    for a in playback_attempts(plays, key):
        outcome = a.outcome
        if outcome and _heard(outcome, outcome.duration_s) and all(key(e) == key(outcome) for e in a.events):
            heard[key(outcome)].append(a)

    sources = {}
    mix_playlists = set(mix_playlist_ids)
    for track, attempts in heard.items():
        first = min(attempts, key=lambda a: (a.outcome.ts, a.outcome.id or 0, a.outcome.event_id))
        at = first.outcome.ts
        if at <= oldest:
            continue
        # Context is the exact served list, not a later offer of the same song.
        d = max((d for d in choices.get((track, first.context), ()) if d.at <= first.first.ts),
                key=lambda d: d.at, default=None)
        if d is None or not d.is_new_track or d.generator == "seed":
            continue
        if at > until:
            result["pending"] += 1
            continue
        deadline = at + WINDOW
        manual_returns = unknown_returns = 0
        for a in attempts:
            if a.first.ts > at and a.outcome.ts <= deadline:
                organic = _organic(a, rows, mix_playlists)
                manual_returns += organic is True
                unknown_returns += organic is None
        adopted = manual_returns >= 2 or _liked(marks[track], aliases[track], at, deadline)
        subtype = d.decision_json.get("novelty") if isinstance(d.decision_json, dict) else None
        if (balance is not None and at > learning_since and subtype in mature
                and (adopted or manual_returns + unknown_returns < 2)):
            mature[subtype] += 1
            accepted[subtype] += int(adopted)
        if at <= since:
            continue
        if subtype not in subtypes:
            subtype = "unknown"
            if subtype not in subtypes:
                subtypes[subtype] = _subtype_row(subtype)
                result["subtypes"].append(subtypes[subtype])
        row = subtypes[subtype]
        row["mature"] += 1
        row["adopted"] += int(adopted)
        result["mature"] += 1
        result["adopted"] += int(adopted)
        result["unknown_return_tracks"] += int(unknown_returns > 0)
        for name, share in source_shares(d).items():
            if name not in sources:
                sources[name] = dict(id=name, title=source_title(name), mature=0., adopted=0.)
            sources[name]["mature"] += share
            if adopted:
                sources[name]["adopted"] += share
    for name in sorted(sources):
        row = sources[name]
        row["rate"] = row["adopted"] / row["mature"] if row["mature"] else None
        result["sources"].append(row)
    for row in result["subtypes"]:
        if row["mature"]:
            row["rate"] = row["adopted"] / row["mature"]
    if balance is not None:
        balance.update(_balance(mature, accepted))
    if result["mature"]:
        result["rate"] = result["adopted"] / result["mature"]
    return result


def _possible_hearing(recent, decisions, user_id, now, since):
    """A cheap upper bound, not attribution: unknown track aliases may still join.

    For modern events all retries, missing contexts and errors for the same
    playback id stay together. Older terminals without that id can depend on
    alias-linked checkpoints, so defer their exact fold to the full journal.
    """
    contexts = {d.context for d in decisions if d.user_id == user_id and d.at <= now and
                d.is_new_track and d.generator != "seed" and d.context.partition(":")[0] in RECOMMENDED}
    if not contexts:
        return False
    plays = [e for e in recent if e.user_id == user_id and e.kind in PLAYBACK_KINDS and
             (e.target_type or "track") == "track" and (e.ref or e.nd_song_id) and not e.superseded_scrobble and
             e.ts <= now and e.received_at is not None and e.received_at <= now and
             e.context not in NOT_USER and not test_traffic(e.client) and (e.target_data or {}).get("provenance") != "import"]
    if not any(e.kind in ("finish", "skip") and e.ts > since for e in plays):
        return False
    if any(not e.playback_id and e.context in contexts and e.kind in ("finish", "skip") and e.ts > since for e in plays):
        return True
    # A context may only be present on the start or on a checkpoint.
    ids = {(e.device_id or e.client, e.playback_id) for e in plays if e.playback_id and e.context in contexts}
    if not ids:
        return False
    groups = playback_attempts((TasteEvent.from_row(e) for e in plays if (e.device_id or e.client, e.playback_id) in ids),
                               lambda e: e.ref or e.nd_song_id)
    return any(a.context in contexts and (o := a.outcome) and o.ts > since and _heard(o, o.duration_s) for a in groups)


async def report(s: AsyncSession, user_id: int, recent, decisions, *, now: datetime, days: int,
                 balance: dict | None = None) -> dict:
    # quality already has the whole cohort and its decisions. Earlier listening
    # prevents rediscovery across report windows; earlier marks resolve causal
    # rollbacks and duplicate likes. Ranking reuses the fixed-window aggregation behind a cache.
    span = max(days, LEARNING_DAYS) if balance is not None else days
    if not _possible_hearing(recent, decisions, user_id, now, now - WINDOW - timedelta(days=span)):
        return summarize(user_id, (), (), now=now, days=days, balance=balance)
    older = (await s.scalars(select(PlayEvent).where(PlayEvent.user_id == user_id,
        PlayEvent.target_type == "track", PlayEvent.ts < min(e.ts for e in recent), PlayEvent.received_at <= now,
        PlayEvent.superseded_scrobble.is_(False), PlayEvent.kind.in_((*PLAYBACK_KINDS, "like", "unlike"))))).all()
    aliases = await s.scalars(select(FavoriteSubject.aliases).where(FavoriteSubject.target_type == "track",
        FavoriteSubject.enriched_at <= now))
    subjects = (Subject("track", tuple(keys)) for keys in aliases if keys)
    playlists = (await s.scalars(select(Mix.nd_playlist_id).where(Mix.user_id == user_id,
        Mix.created_at <= now, Mix.nd_playlist_id.is_not(None)))).all()
    return summarize(user_id, [*older, *recent], decisions, now=now, days=days, subjects=subjects,
                     mix_playlist_ids=playlists, balance=balance)
