"""Pure v2 taste calculation. All clocks, identity and state inputs belong to one explicit snapshot."""
from __future__ import annotations

import bisect
import math
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from app.favorites import AliasIndex, State, fold
from app.mixes.embedder import GeometryProfile
from app.mixes.taste import Profile, TasteTrack, artist_key, skip_weight
from app.mixes.taste_snapshot import Subject, TasteEvent, TasteSnapshot, calendar_cutoff, playback_attempts, playback_evidence, utc

MIN_WEIGHT = .01
EXPERIMENTS = ("P1", "P2", "P3", "P4", "P5")


def saturation(value: float) -> float:
    return value if value <= 2 else 2 + 2 * math.log1p((value - 2) / 2)


def recommended(context: str | None) -> bool:
    return bool(context and (context.startswith(("mix:", "wave:", "smart:")) or context == "continuous" or
                             context.startswith("continuous:")))


def self_selected(context: str | None) -> bool:
    return bool(context and context.split(":", 1)[0] in {"album", "playlist", "search", "artist", "track", "library"})


def visible_events(snapshot: TasteSnapshot, now: datetime, mode: str) -> tuple[TasteEvent, ...]:
    if mode not in ("occurred-at", "available-at"):
        raise ValueError("mode must be occurred-at or available-at")
    if snapshot.user_id in snapshot.excluded_user_ids:
        return ()
    result = {}
    for event in snapshot.events:
        e = replace(event, ts=utc(event.ts), received_at=utc(event.received_at or event.ts))
        if e.user_id != snapshot.user_id or e.ts > now or e.client.lower().endswith("-debug"):
            continue
        if mode == "available-at" and e.received_at > now:
            continue
        # Explicit provenance only: never guess test traffic from an app version/date.
        if (e.target_data or {}).get("provenance") == "import" or e.client in ("import", "qa"):
            continue
        result.setdefault(e.event_id, e)
    return tuple(sorted(result.values(), key=lambda e: (e.ts, e.received_at, e.id, e.event_id)))


@dataclass(frozen=True)
class Contribution:
    """One input of a track's weight (INSIGHTS.md 7.3): `value` is what it added to `positive_raw` (side P) or to
    `negative` (side N, as a positive amount); side "0" — counted for nothing, `note` says why."""

    kind: str  # finish | skip | start | like | dislike | dislike_legacy | download | album_like
    ts: datetime | None
    event_id: str | None
    side: Literal["P", "N", "0"]
    base: float  # the M4 weight before decay: +2, −1, skip_weight(...), +1, −5, 0
    age: float | None  # A — active units after the event's day
    w: float  # the decay multiplier (0 below MIN_WEIGHT or older than three years)
    value: float
    context: str | None = None
    client: str | None = None
    position_s: float | None = None
    duration_s: float | None = None
    note: str | None = None
    effective_seconds: float | None = None  # input to the skip curve, distinct from the seek cursor


@dataclass
class Trace:
    """Collects `Contribution`s by the final `TasteTrack.key`; never changes the profile it observes."""

    tracks: dict[str, list[Contribution]] = field(default_factory=dict)

    def add(self, key: str, c: Contribution) -> None:
        self.tracks.setdefault(key, []).append(c)


@dataclass(frozen=True)
class ActiveClock:
    days: tuple[date, ...]
    units: tuple[float, ...]
    prefix: tuple[float, ...]
    timezone: str

    @classmethod
    def from_counts(cls, counts: dict[date, int], timezone: str) -> ActiveClock:
        days = tuple(sorted(counts))
        units = tuple(min(1.0, counts[d] / 5) for d in days)
        total = 0.0
        prefix = [0.0]
        for unit in units:
            total += unit
            prefix.append(total)
        return cls(days, units, tuple(prefix), timezone)

    def age(self, at: datetime) -> float:
        day = utc(at).replace(tzinfo=UTC).astimezone(ZoneInfo(self.timezone)).date()
        # Units are exact fifths; avoid a 30.00000000001 boundary excluding a valid seed.
        return max(0.0, round(self.prefix[-1] - self.prefix[bisect.bisect_right(self.days, day)], 10))

    def weight(self, at: datetime) -> float:
        return .5 ** (self.age(at) / 30)


@dataclass
class V2Profile(Profile):
    model: str = "v2"
    timezone: str = "UTC"
    clock: ActiveClock | None = None
    evidence: float = 0.0
    maturity: float = 0.0
    explicit_negative_keys: set[str] = field(default_factory=set)
    geometry: GeometryProfile | None = None
    limitations: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (any(t.score > 0 or t.liked or t.downloaded or t.album_liked for t in self.tracks.values()) or
                    any(st.liked for st in self.favorite_states.values()) or self.picked_artists)

    def seeds(self, n: int, max_per_artist: int = 2) -> list[TasteTrack]:
        from app.mixes.taste_select import select_profile_seeds
        return select_profile_seeds(self, n, max_per_artist)

    def top_artists(self, n: int) -> list[str]:
        from app.mixes.taste_select import top_artists
        return top_artists(self, n)

    def disliked(self, *, ref, nd_song_id, artist, artist_ref) -> bool:
        t = self.find(ref=ref, nd_song_id=nd_song_id)
        return bool(t and t.key in self.explicit_negative_keys) or super().disliked(
            ref=ref, nd_song_id=nd_song_id, artist=artist, artist_ref=artist_ref)

    def positive_weights(self) -> dict[str, float]:
        return {k: t.score for k, t in sorted(self.tracks.items()) if t.score >= 1}

    def negative_keys(self) -> set[str]:
        return self.explicit_negative_keys | {k for k, t in self.tracks.items() if t.negative >= 2}

    def active_seed_pool(self) -> list[TasteTrack]:
        return sorted((t for t in self.tracks.values() if t.score >= 1 and t.positive_age is not None and
                       t.positive_age <= 30 and not self.disliked(ref=t.ref, nd_song_id=t.nd_song_id,
                           artist=t.artist, artist_ref=None)), key=lambda t: (-t.score, t.key))

    @property
    def has_recent_positive(self) -> bool:
        """Independent of score threshold and metadata: only this controls stash fallback eligibility."""
        return any(t.last_positive is not None and t.positive_age is not None and t.positive_age <= 30
                   for t in self.tracks.values())


def _event_subject(e: TasteEvent) -> bool:
    return bool(e.ref or getattr(e, {"track": "nd_song_id", "album": "nd_album_id",
                                   "artist": "nd_artist_id"}[e.target_type]))


def _heard(e: TasteEvent, duration: float | None) -> bool:
    seconds = e.seconds
    if e.kind == "finish":
        return seconds is None or seconds >= 30 or bool(duration and seconds >= duration)
    return e.kind == "skip" and seconds is not None and seconds >= 30


def _real_actions(events, subjects, index):
    # Reuse B's causal resolution; activity must never implement a second rollback engine.
    from app.favorites import effective_decisions

    decisions = list(effective_decisions(events, subjects))
    decisions.extend(e for e in events if e.kind in ("dislike", "undislike") and not e.reverts_event_id)
    states = {}
    for e in sorted(decisions, key=lambda x: (x.ts, x.received_at, x.id, x.event_id)):
        group = "favorite" if e.kind in ("like", "unlike") else "dislike"
        key = index.key(e), group
        value = e.kind in ("like", "dislike")
        previous = states.get(key, False)
        states[key] = value
        if previous != value and e.context not in ("favorite:bootstrap", "favorite:reconcile"):
            yield e


def calculate_profile(snapshot: TasteSnapshot, *, now: datetime, timezone: str,
                      mode: Literal["occurred-at", "available-at"] = "available-at",
                      trace: Trace | None = None) -> V2Profile:
    """Online M4 only. Offline hypotheses are deliberately exposed by a different function. `trace` (the admin's
    breakdown) only observes: the profile is the same with and without it."""
    return _calculate(snapshot, now=now, timezone=timezone, mode=mode, experiment=None, trace=trace)


def calculate_offline(snapshot: TasteSnapshot, *, now: datetime, timezone: str, experiment: str,
                      mode: Literal["occurred-at", "available-at"] = "available-at") -> V2Profile:
    if experiment not in EXPERIMENTS:
        raise ValueError("unknown offline experiment")
    return _calculate(snapshot, now=now, timezone=timezone, mode=mode, experiment=experiment)


def calculate_baseline(snapshot: TasteSnapshot, *, now: datetime, timezone: str,
                       mode: Literal["occurred-at", "available-at"] = "available-at",
                       trace: Trace | None = None) -> Profile:
    """Offline local v1 baseline, using B's corrected state and no phantom network snapshots."""
    p = _calculate(snapshot, now=now, timezone=timezone, mode=mode, experiment=None, version="v1", trace=trace)
    return Profile(**{key: getattr(p, key) for key in Profile.__dataclass_fields__})


def _calculate(snapshot, *, now, timezone, mode, experiment, version="v2", trace=None):
    now = utc(now)
    zone = ZoneInfo(timezone)  # No fallback to the host timezone.
    cutoff = calendar_cutoff(now) if version == "v2" else now - timedelta(days=365)
    events = tuple(e for e in visible_events(snapshot, now, mode) if _event_subject(e))
    subjects = list(snapshot.subjects)
    for tr in snapshot.metadata:
        keys = tuple(k for k in (f"ref:{tr.ref}" if tr.ref else None,
                                 f"nd:{tr.nd_song_id}" if tr.nd_song_id else None) if k)
        if keys:
            subjects.append(Subject("track", keys, tr.ref, tr.nd_song_id,
                                    {"artist": tr.artist, "title": tr.title, "duration_s": tr.duration_s}))
    # A verified pair on a playback event must also unify ref-only/ND-only favorite
    # decisions: the ledger helper intentionally consumes only like/unlike rows.
    pairs = {(e.ref, e.nd_song_id) for e in events if e.target_type == "track" and e.ref and e.nd_song_id}
    subjects.extend(Subject("track", (f"ref:{ref}", f"nd:{nd}"), ref, nd) for ref, nd in sorted(pairs))
    index = AliasIndex(events, subjects)
    state = fold(events, subjects, now)
    p = V2Profile(snapshot.user_id, now, timezone=timezone, favorite_states=state, limitations=snapshot.limitations)
    if snapshot.user_id in snapshot.excluded_user_ids:
        p.clock = ActiveClock.from_counts({}, timezone)
        return p
    aliases_by_key = defaultdict(set)
    for typ, alias in list(index.parent):
        aliases_by_key[index.root((typ, alias))].add(alias)
    metadata, preferred = {}, {}
    for subject in subjects:
        if subject.target_type == "track" and subject.aliases:
            k = index.root(("track", min(subject.aliases)))
            preferred.setdefault(k, (subject.ref, subject.nd_id))
            data = metadata.setdefault(k, {})
            for key, value in subject.data.items():
                if value is not None:
                    data.setdefault(key, value)
    for e in events:
        if e.target_type == "track":
            data = metadata.setdefault(index.key(e), {})
            for key, value in (e.target_data or {}).items():
                if value is not None:
                    data.setdefault(key, value)

    def get_track(ref=None, nd=None):
        aliases = {k for k in (f"ref:{ref}" if ref else None, f"nd:{nd}" if nd else None) if k}
        if not aliases:
            return None
        root = index.root(("track", min(aliases)))
        all_aliases = aliases_by_key.get(root, aliases)
        nds = sorted(a[3:] for a in all_aliases if a.startswith("nd:"))
        refs = sorted(a[4:] for a in all_aliases if a.startswith("ref:"))
        pref_ref, pref_nd = preferred.get(root, (None, None))
        canonical_nd = pref_nd or (nds[0] if nds else nd)
        canonical_ref = pref_ref or (refs[0] if refs else ref)
        key = f"nd:{canonical_nd}" if canonical_nd else canonical_ref
        t = p.tracks.get(key)
        if t is None:
            data = metadata.get(root, {})
            t = TasteTrack(key, canonical_ref, canonical_nd,
                           data.get("artist") or "", data.get("title") or "",
                           data.get("duration_s") or data.get("duration"))
            p.tracks[key] = t
        for alias in all_aliases:
            p.by_id[alias[4:] if alias.startswith("ref:") else alias] = key
        if t.fp:
            p.by_id[t.fp] = key
        return t

    heard_by_day, actions_by_day = defaultdict(set), defaultdict(set)
    attempts = playback_attempts(events, index.key)
    playback = playback_evidence(a for a in attempts if version == "v2" or a.outcome)
    prediction_at = {a.outcome.event_id: a.first.ts - timedelta(microseconds=1)
                     for a in attempts if a.outcome} if experiment == "P3" else {}
    for e in playback:
        if e.ts < cutoff:
            continue
        t = get_track(e.ref, e.nd_song_id)
        if t and _heard(e, e.duration_s or t.duration_s):
            heard_by_day[e.ts.replace(tzinfo=UTC).astimezone(zone).date()].add(t.key)
    for e in _real_actions(events, subjects, index):
        if e.ts >= cutoff:
            # Keep the object of an explicit action even after an unlike/undislike removes its weight.
            if e.target_type == "track":
                get_track(e.ref, e.nd_song_id)
            actions_by_day[e.ts.replace(tzinfo=UTC).astimezone(zone).date()].add(index.key(e))
    counts = {d: len(heard_by_day[d]) + len(actions_by_day[d]) for d in heard_by_day.keys() | actions_by_day.keys()}
    clock = p.clock = ActiveClock.from_counts(counts, timezone)

    def weight(at):
        if at is None or at > now or (version == "v2" and at < cutoff):
            return 0.0
        w = clock.weight(at) if version == "v2" else .5 ** ((now - at).total_seconds() / 86400 / 30)
        return w if version == "v1" or w >= MIN_WEIGHT else 0.0

    def positive(t, amount, at):
        t.positive_raw += amount
        if amount > 0 and (t.last_positive is None or t.last_positive < at):
            t.last_positive, t.positive_age = at, clock.age(at)

    previous_skip = None
    for e in playback:
        if e.ts < cutoff:
            continue
        t = get_track(e.ref, e.nd_song_id)
        if not t:
            continue
        t.last_event = max(t.last_event or e.ts, e.ts)
        t.last_played = max(t.last_played or e.ts, e.ts)
        if _heard(e, e.duration_s or t.duration_s):
            t.heard30 = True
            t.last_heard = max(t.last_heard or e.ts, e.ts)
            if t.artist:
                a = artist_key(t.artist)
                p.artist_listens[a] = p.artist_listens.get(a, 0) + 1
        seconds = e.seconds
        base = 2.0 if e.kind == "finish" else skip_weight(seconds or 0, e.duration_s or t.duration_s)
        if e.kind == "start":
            base = 0.0
        early = e.early_skip
        if early:
            base = -1.0
            if experiment == "P1" and seconds < 3 and self_selected(e.context) and previous_skip and \
                    self_selected(previous_skip.context) and e.context == previous_skip.context and \
                    previous_skip.seconds < 3 and (e.ts - previous_skip.ts).total_seconds() <= 20:
                base = 0.0
            elif experiment == "P2" and self_selected(e.context):
                base = -.5
            elif experiment == "P3":
                # P3's threshold is the pre-decision M4 score, never the final cut's
                # decayed score or future likes. This extra work is offline only.
                at = prediction_at[e.event_id]
                prior = replace(snapshot, subjects=(), metadata=(), downloads=(), dislikes=(), picked_artists=())
                before = calculate_profile(prior, now=at, timezone=timezone, mode=mode)
                familiar = before.find(ref=e.ref, nd_song_id=e.nd_song_id)
                if familiar and familiar.score >= 4:
                    base = -.5
            elif experiment == "P4":
                base = -max(0.0, min(1.0, (30 - seconds) / 20))
        if e.kind == "finish" and experiment == "P5" and recommended(e.context):
            base *= .75
        if e.kind in ("skip", "finish"):
            previous_skip = e if early else None
        w = weight(e.ts)
        if base > 0:
            positive(t, base * w, e.ts)
        elif base < 0:
            t.negative += -base * w
        if trace is not None:
            if not base:
                note = "start — вес 0" if e.kind == "start" else \
                    "длина неизвестна — вес 0" if not (e.duration_s or t.duration_s) else "пропуск в самом начале — вес 0"
            else:
                note = None if w else "w < 0.01"
            trace.add(t.key, Contribution(e.kind, e.ts, e.event_id, "P" if base > 0 else "N" if base < 0 else "0",
                                          base, clock.age(e.ts), w, abs(base) * w, e.context, e.client,
                                          e.position_s, e.duration_s or t.duration_s, note, seconds))

    for st in state.values():
        if not st.liked or st.target_type != "track":
            continue
        t = get_track(st.ref, st.nd_id)
        if t:
            t.liked = True
            t.last_event = max(filter(None, (t.last_event, st.since)), default=None)
            counted = bool(st.since and st.since >= cutoff)
            if counted:
                positive(t, weight(st.since), st.since)
            if trace is not None:
                w = weight(st.since) if counted else 0.0
                trace.add(t.key, Contribution(
                    "like", st.since, st.event.event_id, "P" if counted else "0", 1.0,
                    clock.age(st.since) if st.since else None, w, w, st.event.context, st.event.client,
                    note=None if counted and w else "w < 0.01" if counted else "старше 3 лет"))
    for saved in snapshot.downloads:
        if utc(saved.at) <= now and (t := get_track(saved.ref, saved.nd_song_id)):
            t.downloaded = True
            t.last_event = max(t.last_event or utc(saved.at), utc(saved.at))
            if trace is not None:
                trace.add(t.key, Contribution("download", utc(saved.at), None, "0", 0.0, clock.age(utc(saved.at)),
                                              0.0, 0.0, note="загрузка — вес 0"))

    # Album membership is explicit catalog/verified metadata, never inferred by title equality.
    for st in state.values():
        if version == "v1" or not st.liked or st.target_type != "album":
            continue
        refs = set((st.data or {}).get("track_refs") or ())
        album_aliases = set(st.aliases)
        for tr in snapshot.metadata:
            if (tr.album_ref and f"ref:{tr.album_ref}" in album_aliases) or \
                    (tr.album_nd_id and f"nd:{tr.album_nd_id}" in album_aliases):
                t = get_track(tr.ref, tr.nd_song_id)
                if t:
                    t.album_liked = True
                    if trace is not None:
                        _album_like(trace, t, st)
        for ref in refs:
            if t := get_track(ref):
                t.album_liked = True
                if trace is not None:
                    _album_like(trace, t, st)

    # Current explicit state is folded from events; legacy rows fill only unjournalled objects.
    explicit = {}
    for e in events:
        if e.kind in ("dislike", "undislike") and not e.reverts_event_id:
            explicit[index.key(e)] = e
    # A filtered debug/QA/future decision must not come back through its mutable table row.
    journalled = {index.key(e) for e in snapshot.events if e.user_id == snapshot.user_id and
                   e.kind in ("dislike", "undislike") and _event_subject(e)}
    for e in explicit.values():
        if e.kind == "undislike":
            continue
        data = e.target_data or {}
        if e.target_type == "artist":
            p.disliked_artist_refs.add(e.ref or f"nd:{e.nd_artist_id}")
            if name := data.get("name"):
                p.disliked_artists.add(artist_key(name))
        elif t := get_track(e.ref, e.nd_song_id):
            p.explicit_negative_keys.add(t.key)
            p.disliked_ids.update(k for k in (t.ref, f"nd:{t.nd_song_id}" if t.nd_song_id else None) if k)
            t.negative += 5 * weight(e.ts)
            if trace is not None:
                w = weight(e.ts)
                trace.add(t.key, Contribution("dislike", e.ts, e.event_id, "N", -5.0, clock.age(e.ts), w, 5 * w,
                                              e.context, e.client, note=None if w else
                                              "старше 3 лет" if e.ts < cutoff else "w < 0.01"))
    for d in snapshot.dislikes:
        if d.artist_ref:
            key = index.root(("artist", f"ref:{d.artist_ref}"))
            if key not in journalled:
                p.disliked_artist_refs.add(d.artist_ref)
                if d.artist:
                    p.disliked_artists.add(artist_key(d.artist))
        else:
            aliases = [f"ref:{d.ref}"] if d.ref else [f"nd:{d.nd_song_id}"]
            if index.root(("track", aliases[0])) in journalled:
                continue
            t = get_track(d.ref, d.nd_song_id)
            if not t:
                continue
            p.explicit_negative_keys.add(t.key)
            p.disliked_ids.update(k for k in (t.ref, f"nd:{t.nd_song_id}" if t.nd_song_id else None) if k)
            t.negative += 5 * weight(utc(d.at) if d.at else now)
            if trace is not None:
                at = utc(d.at) if d.at else now
                w = weight(at)
                trace.add(t.key, Contribution("dislike_legacy", at, None, "N", -5.0, clock.age(at), w, 5 * w,
                                              note=None if w else "старше 3 лет" if at < cutoff else "w < 0.01"))
            if not t.artist:
                t.artist, t.title = d.artist, d.title

    artist_pos, artist_neg = defaultdict(float), defaultdict(float)
    for t in p.tracks.values():
        t.score = (saturation(t.positive_raw) if version == "v2" else t.positive_raw) - t.negative
        if t.artist:
            a = artist_key(t.artist)
            p.artist_names[a] = t.artist
            artist_pos[a] += max(t.score, 0)
            artist_neg[a] += max(-t.score, 0)
        if t.fp:
            p.by_id[t.fp] = t.key
    p.artists = {a: saturation(artist_pos[a]) - .5 * saturation(artist_neg[a]) if version == "v2"
                 else artist_pos[a] - artist_neg[a] for a in artist_pos.keys() | artist_neg.keys()}
    for st in state.values():
        if version == "v1" or not st.liked or st.target_type not in ("artist", "album"):
            continue
        data = st.data or {}
        name = data.get("name") if st.target_type == "artist" else data.get("artist")
        if name:
            a = artist_key(name)
            p.artist_names[a] = name
            p.artists[a] = p.artists.get(a, 0) + (1 if st.target_type == "artist" else .5) * weight(st.since)
    if trace is not None:
        _ignored(trace, snapshot, p, now)
    p.evidence = sum(max(t.score, 0) for t in p.tracks.values())
    p.maturity = min(1.0, p.evidence / 20)
    p.picked_artists = list(snapshot.picked_artists)
    return p


def _album_like(trace: Trace, t: TasteTrack, st: State) -> None:
    if not any(c.kind == "album_like" and c.event_id == st.event.event_id for c in trace.tracks.get(t.key, ())):
        trace.add(t.key, Contribution("album_like", st.since, st.event.event_id, "0", 0.0, None, 0.0, 0.0,
                                      st.event.context, st.event.client, note="♥ альбома — вклад в исполнителя"))


def _ignored(trace: Trace, snapshot: TasteSnapshot, p: V2Profile, now: datetime) -> None:
    """What the profile saw and counted for nothing: imported or test ♥, and own ♥ taken back since."""
    for e in snapshot.events:
        if e.user_id != snapshot.user_id or e.kind != "like" or e.target_type != "track":
            continue
        t = p.find(ref=e.ref, nd_song_id=e.nd_song_id)
        if t is None:
            continue
        if (e.target_data or {}).get("provenance") == "import" or e.client == "import":
            note = "импорт — не свой"
        elif e.client == "qa" or e.client.lower().endswith("-debug"):
            note = "тестовый клиент — не учтён"
        elif not t.liked and utc(e.ts) <= now and not e.reverts_event_id:
            note = "♥ снят — вклад 0"
        else:
            continue
        trace.add(t.key, Contribution("like", utc(e.ts), e.event_id, "0", 1.0, None, 0.0, 0.0, e.context, e.client,
                                      note=note))
