"""Attempts and launches of one user's history (INSIGHTS.md 5, 7.5), read from the local database only.

An attempt — the events of one try to play a track: one `(device_id, playback_id)`; old events without them — a
`start` and the next `skip`/`finish` of the same track on the same client. Listening is counted as in
`listening_summary`: measured `listened_ms` crossing the threshold, or a Navidrome scrobble. A launch — the user began
playing a list: by `launch_id`, by the wave session, or (old history) by the 30-minute heuristic.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.listening_summary import listened_threshold_ms
from app.mixes.taste_math import _heard
from app.mixes.taste_snapshot import PLAYBACK_KINDS, PlaybackAttempt, TasteEvent, playback_attempts
from app.models import Mix, MixItem, PlayEvent, WaveItem, WaveSession

MARK_KINDS = ("like", "unlike", "dislike", "undislike")
LAUNCH_GAP = timedelta(minutes=30)
MARK_TAIL = timedelta(minutes=5)  # a like right after the list ended still belongs to it
PLAYLIST_SHARE = .6  # an old `playlist` launch is a mix's when this share of its tracks are the mix's songs
SOURCES = {"mix", "wave", "smart", "playlist", "album", "artist", "liked", "search", "queue", "continuous", "library", "other"}
SELF_SELECTED = {"album", "playlist", "search", "artist", "liked", "library"}  # «прижившаяся рекомендация»
NOT_USER = ("favorite:bootstrap", "favorite:reconcile")


def test_traffic(client: str | None) -> bool:
    """Excluded everywhere, as in `taste_math.visible_events`."""
    c = (client or "").lower()
    return c in ("qa", "import") or c.endswith("-debug")


def source_of(context: str | None) -> str:
    if not context:
        return "unknown"
    head = context.split(":", 1)[0]
    return head if head in SOURCES else "other"


def wave_of(value: str | None) -> str | None:
    return value[5:] or None if value and value.startswith("wave:") else None


def mix_of(context: str | None) -> int | None:
    if context and context.startswith("mix:") and context[4:].isdigit():
        return int(context[4:])
    return None


class Aliases:
    """`ref` ↔ `nd:<song>` pairs seen in the user's events: one track's keys, as `listening_summary.canonical`."""

    def __init__(self, rows: list[Any]):
        self.songs: dict[str, set[str]] = defaultdict(set)
        self.refs: dict[str, set[str]] = defaultdict(set)
        for r in rows:
            if r.ref and r.nd_song_id:
                self.songs[r.ref].add(r.nd_song_id)
                self.refs[r.nd_song_id].add(r.ref)

    def key(self, ref: str | None, nd: str | None) -> str:
        if nd:
            return f"nd:{nd}"
        known = self.songs.get(ref or "", set())
        return f"nd:{next(iter(known))}" if len(known) == 1 else ref or ""

    def of(self, ref: str | None, nd: str | None) -> set[str]:
        out = {x for x in (ref, f"nd:{nd}" if nd else None) if x}
        if ref:
            out.update(f"nd:{n}" for n in self.songs.get(ref, ()))
        if nd:
            out.update(self.refs.get(nd, ()))
        return out


@dataclass(eq=False)
class Attempt:
    events: list[PlayEvent]
    key: str
    ref: str | None
    nd_song_id: str | None
    aliases: set[str]
    started_at: datetime
    client: str
    device: str | None
    context: str | None
    launch_id: str | None
    launch_origin: str | None
    deltas: list[tuple[datetime, int]] = field(default_factory=list)  # measured listening, at checkpoint time
    measured: bool = False
    counted_at: datetime | None = None  # a counted listen (measured threshold, or a scrobble)
    heard_at: datetime | None = None  # counted_at; without measurements — by the events (finish, skip ≥ 30 s)
    heard_outcome_at: datetime | None = None  # Eligible terminal, distinct from a measured checkpoint.
    started: bool = False
    finished: bool = False
    early_skip_at: float | None = None
    late_skip: bool = False
    scrobble: bool = False
    launch: Launch | None = None

    @property
    def listened_ms(self) -> int:
        return sum(d for _, d in self.deltas)

    @property
    def ended_at(self) -> datetime:
        return self.events[-1].ts

    def listened_between(self, begin: datetime, stop: datetime) -> int:
        return sum(d for ts, d in self.deltas if begin <= ts < stop)


@dataclass(eq=False)
class Launch:
    id: str
    source: str
    context: str | None
    attribution: str  # launch_id / wave_session / reconstructed / inferred
    started_at: datetime
    origin: str | None = None
    client: str | None = None
    device: str | None = None
    attempts: list[Attempt] = field(default_factory=list)
    marks: list[PlayEvent] = field(default_factory=list)  # like/dislike while it played
    mix_id: int | None = None
    mix_attribution: str | None = None  # context / playlist_id / inferred_playlist
    wave_id: str | None = None
    restart: bool = False  # the app re-made a wave session after a 404: not the user's launch
    session_end: datetime | None = None

    @property
    def ended_at(self) -> datetime:
        ends = [a.ended_at for a in self.attempts] + [m.ts for m in self.marks]
        return max(ends) if ends else self.session_end or self.started_at

    @property
    def last_play(self) -> datetime:
        return max((a.ended_at for a in self.attempts), default=self.started_at)

    def stats(self) -> dict[str, Any]:
        return {**attempt_stats(self.attempts), **mark_stats(self.marks)}


def attempt_stats(attempts: list[Attempt]) -> dict[str, int]:
    return {"tracks_started": len({a.key for a in attempts if a.started}),
            "started": sum(a.started for a in attempts),
            "heard": sum(a.heard_at is not None for a in attempts),
            "finished": sum(a.finished for a in attempts),
            "early_skips": sum(a.early_skip_at is not None for a in attempts),
            "late_skips": sum(a.late_skip for a in attempts),
            "listened_ms": sum(a.listened_ms for a in attempts)}


def mark_stats(marks: list[PlayEvent]) -> dict[str, int]:
    return {"likes": sum(m.kind == "like" for m in marks), "dislikes": sum(m.kind == "dislike" for m in marks)}


def _attempt(group: PlaybackAttempt, events: list[PlayEvent], aliases: Aliases) -> Attempt:
    ref = next((e.ref for e in events if e.ref), None)
    nd = next((e.nd_song_id for e in events if e.nd_song_id), None)
    first = events[0]
    a = Attempt(events, aliases.key(ref, nd), ref, nd, aliases.of(ref, nd), first.ts, first.client, first.device_id,
                group.context,
                next((e.launch_id for e in events if e.launch_id), None),
                next((e.launch_origin for e in events if e.launch_origin), None))
    duration = next((e.duration_s for e in reversed(events) if e.duration_s), None) or next(
        ((e.track_metadata or {}).get("duration_s") for e in events if (e.track_metadata or {}).get("duration_s")),
        None)
    if first.playback_id and first.device_id and any(e.listened_ms is not None for e in events):
        a.measured = True
        threshold, previous = listened_threshold_ms(duration), 0
        for e in events:
            if e.listened_ms is None:
                continue
            current = max(previous, e.listened_ms)
            if current > previous:
                a.deltas.append((e.ts, current - previous))
            if previous < threshold <= current and a.counted_at is None:
                a.counted_at = e.ts
            previous = current
    a.started = group.start is not None
    if outcome := group.outcome:
        a.finished = outcome.kind == "finish"
        if outcome.early_skip:
            a.early_skip_at = outcome.seconds
        elif outcome.kind == "skip" and outcome.seconds is not None:
            a.late_skip = True
        if _heard(outcome, outcome.duration_s or duration):
            a.heard_outcome_at = outcome.ts
            if not a.measured:
                a.heard_at = outcome.ts
    if a.client == "navidrome" and not a.measured and a.finished:
        a.scrobble, a.counted_at, a.heard_at, a.finished = True, first.ts, first.ts, False
    if a.measured:
        a.heard_at = a.counted_at
    return a


def attempts_of(rows: list[PlayEvent], aliases: Aliases) -> list[Attempt]:
    """`rows` — the user's playback events in `(ts, id)` order."""
    by_id = {r.event_id: r for r in rows}
    groups = playback_attempts((TasteEvent.from_row(r) for r in rows), lambda e: aliases.key(e.ref, e.nd_song_id))
    return [_attempt(group, [by_id[e.event_id] for e in group.events], aliases) for group in groups]


@dataclass
class MixInfo:
    mix: Mix
    items: list[MixItem]
    active: set[str]  # aliases of its active songs

    def live_at(self, ts: datetime) -> bool:
        return self.mix.created_at <= ts and (self.mix.closed_at is None or ts < self.mix.closed_at)


@dataclass
class History:
    """Everything one user's insights read: events (no test traffic, none after `now`), attempts and launches."""

    user_id: int
    now: datetime
    rows: list[PlayEvent]
    marks: list[PlayEvent]  # like/unlike/dislike/undislike by the user (not bootstrap/reconcile, not imports)
    aliases: Aliases
    attempts: list[Attempt]
    launches: list[Launch]
    sessions: dict[str, WaveSession]
    wave_items: dict[str, list[WaveItem]]
    mixes: dict[int, MixInfo]

    def mix_at(self, ts: datetime) -> MixInfo | None:
        live = [m for m in self.mixes.values() if m.live_at(ts)]
        return max(live, key=lambda m: m.mix.created_at) if live else None


async def load(s: AsyncSession, user_id: int, now: datetime) -> History:
    rows = [r for r in (await s.execute(select(PlayEvent).where(
        PlayEvent.user_id == user_id, PlayEvent.ts <= now, PlayEvent.superseded_scrobble.is_(False),
        PlayEvent.kind.in_((*PLAYBACK_KINDS, *MARK_KINDS))).order_by(PlayEvent.ts, PlayEvent.id))).scalars()
        if not test_traffic(r.client)]
    plays = [r for r in rows if r.kind in PLAYBACK_KINDS and r.target_type == "track"]
    marks = [r for r in rows if r.kind in MARK_KINDS and r.context not in NOT_USER and not r.reverts_event_id and
             (r.target_data or {}).get("provenance") != "import"]
    aliases = Aliases(plays)
    attempts = attempts_of(plays, aliases)
    sessions = {w.id: w for w in (await s.execute(select(WaveSession).where(
        WaveSession.user_id == user_id, WaveSession.created_at <= now).order_by(WaveSession.created_at))).scalars()}
    wave_items: dict[str, list[WaveItem]] = defaultdict(list)
    for w in (await s.execute(select(WaveItem).where(WaveItem.user_id == user_id)
                              .order_by(WaveItem.session_id, WaveItem.position))).scalars():
        wave_items[w.session_id].append(w)
    mixes: dict[int, MixInfo] = {}
    for m in (await s.execute(select(Mix).where(Mix.user_id == user_id, Mix.created_at <= now)
                              .order_by(Mix.created_at))).scalars():
        mixes[m.id] = MixInfo(m, [], set())
    if mixes:
        for i in (await s.execute(select(MixItem).where(MixItem.mix_id.in_(list(mixes)))
                                  .order_by(MixItem.mix_id, MixItem.position))).scalars():
            info = mixes[i.mix_id]
            info.items.append(i)
            if i.state == "active":
                info.active |= aliases.of(i.ref, i.nd_song_id)
    h = History(user_id, now, rows, marks, aliases, attempts, [], sessions, wave_items, mixes)
    h.launches = _launches(h)
    return h


def _launches(h: History) -> list[Launch]:
    out: list[Launch] = []
    by_id: dict[str, Launch] = {}
    for sid, w in h.sessions.items():
        launch = Launch(f"wave:{sid}", "wave", f"wave:{sid}", "wave_session", w.created_at, origin=w.origin,
                        client=w.client, wave_id=sid, restart=w.origin == "restart", session_end=w.updated_at)
        by_id[launch.id] = launch
        out.append(launch)
    rest: list[Attempt] = []
    for a in h.attempts:
        if a.client == "navidrome":
            continue  # a scrobble is evidence of listening, not a launch
        sid = wave_of(a.launch_id) or wave_of(a.context)
        lid = f"wave:{sid}" if sid else a.launch_id
        if lid is None:
            rest.append(a)
            continue
        launch = by_id.get(lid)
        if launch is None:
            if sid:
                launch = Launch(lid, "wave", lid, "reconstructed", a.started_at, client=a.client, device=a.device,
                                wave_id=sid)
            else:
                launch = Launch(lid, source_of(a.context), a.context, "launch_id", a.started_at,
                                origin=a.launch_origin, client=a.client, device=a.device)
            by_id[lid] = launch
            out.append(launch)
        launch.attempts.append(a)
        a.launch = launch
    current: dict[str, Launch] = {}
    counter = 0
    for a in rest:  # old history: a new launch on another context or after a 30-minute gap on the stream
        stream = a.device or a.client
        launch = current.get(stream)
        if launch is None or a.started_at - launch.last_play > LAUNCH_GAP or (a.started and a.context != launch.context):
            counter += 1
            launch = Launch(f"inferred:{counter}", source_of(a.context), a.context, "inferred", a.started_at,
                            client=a.client, device=a.device)
            current[stream] = launch
            out.append(launch)
        launch.attempts.append(a)
        a.launch = launch
    for launch in out:
        if launch.wave_id and launch.attribution == "reconstructed":
            launch.started_at = min(x.started_at for x in launch.attempts)
        _attach_mix(h, launch)
    out.sort(key=lambda x: (x.started_at, x.id))
    _attach_marks(h, out)
    return out


def _attach_mix(h: History, launch: Launch) -> None:
    if (mid := mix_of(launch.context)) is not None:
        launch.mix_id, launch.mix_attribution = mid, "context"
        return
    if launch.source != "playlist" or not launch.attempts:
        return
    info = h.mix_at(launch.started_at)
    if info is None:
        return
    pid = launch.context.split(":", 1)[1] if launch.context and ":" in launch.context else None
    if pid:
        if pid == info.mix.nd_playlist_id:
            launch.mix_id, launch.mix_attribution = info.mix.id, "playlist_id"
        return
    started = [a for a in launch.attempts if a.started] or launch.attempts
    share = sum(bool(a.aliases & info.active) for a in started) / len(started)
    if share >= PLAYLIST_SHARE:
        launch.mix_id, launch.mix_attribution = info.mix.id, "inferred_playlist"


def _attach_marks(h: History, launches: list[Launch]) -> None:
    by_lid = {x.id: x for x in launches}
    by_context: dict[str, list[Launch]] = defaultdict(list)
    for x in launches:
        if x.context:
            by_context[x.context].append(x)
    for m in h.marks:
        if m.kind not in ("like", "dislike"):
            continue
        target = by_lid.get(f"wave:{sid}") if (sid := wave_of(m.launch_id) or wave_of(m.context)) else None
        target = target or (by_lid.get(m.launch_id) if m.launch_id else None)
        if target is None and m.context:
            target = next((x for x in by_context.get(m.context, ())
                           if x.started_at <= m.ts <= x.last_play + MARK_TAIL), None)
        if target is not None:
            target.marks.append(m)


def mark_aliases(h: History, m: PlayEvent) -> set[str]:
    return h.aliases.of(m.ref, m.nd_song_id)


def outcome(attempts: list[Attempt], marks: list[PlayEvent], aliases: set[str]) -> dict[str, Any]:
    """What became of one offered track: `not_played` / `started` / `heard` / `finished` / `early_skip` / `liked` /
    `disliked` (the strongest), by the attempts and marks of the list that offered it."""
    mine = [a for a in attempts if a.aliases & aliases]
    kinds = {m.kind for m in marks if {x for x in (m.ref, f"nd:{m.nd_song_id}" if m.nd_song_id else None) if x} &
             aliases}
    early = next((a.early_skip_at for a in mine if a.early_skip_at is not None), None)
    if "dislike" in kinds:
        value = "disliked"
    elif "like" in kinds:
        value = "liked"
    elif any(a.finished for a in mine):
        value = "finished"
    elif any(a.heard_at for a in mine):
        value = "heard"
    elif early is not None:
        value = "early_skip"
    elif mine:
        value = "started"
    else:
        value = "not_played"
    return {"outcome": value, "early_skip_at": early if value == "early_skip" else None,
            "listened_ms": sum(a.listened_ms for a in mine)}


__all__ = ["LAUNCH_GAP", "SELF_SELECTED", "Attempt", "History", "Launch", "attempt_stats", "load", "mark_stats",
           "outcome", "source_of", "test_traffic"]
