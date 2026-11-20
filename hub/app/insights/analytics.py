"""Statistics of a period (INSIGHTS.md 5, 7.6 `/listening`, `/overview`). Counted listens follow
`listening_summary` exactly; days and hours are local (`Settings.timezone`)."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.insights import launches, names
from app.insights.launches import SELF_SELECTED, Attempt, History, Launch
from app.insights.waves_view import NOT_RECORDED, seed_kind
from app.library.owned import quiet
from app.mixes.build import GENERATOR_TITLES
from app.mixes.taste import artist_key
from app.mixes.taste_snapshot import PLAYBACK_KINDS
from app.mixes.wave import generator_title
from app.models import DownloadItem, User
from app.plays import attempt_trigger

MAX_DAYS = 3 * 366
DEFAULT_DAYS = 30
TOP = 20
DISCOVERIES = 50


def period(start: date | None, end: date | None, timezone: str, now: datetime) -> tuple[date, date]:
    """Local dates, both included; by default the last 30 days. Raises ValueError for a bad period."""
    today = now.replace(tzinfo=UTC).astimezone(ZoneInfo(timezone)).date()
    end = end or today
    start = start or end - timedelta(days=DEFAULT_DAYS - 1)
    if end < start or (end - start).days >= MAX_DAYS or end == date.max:
        raise ValueError("bad period")
    return start, end


def bounds(start: date | None, end: date | None, timezone: str) -> tuple[datetime | None, datetime | None]:
    zone = ZoneInfo(timezone)

    def at(d: date) -> datetime:
        return datetime.combine(d, time.min, zone).astimezone(UTC).replace(tzinfo=None)

    return at(start) if start else None, at(end + timedelta(days=1)) if end else None


def local(ts: datetime, timezone: str) -> datetime:
    return ts.replace(tzinfo=UTC).astimezone(ZoneInfo(timezone))


def source(a: Attempt) -> str:
    if a.launch is not None:
        return a.launch.source
    return "scrobble" if a.scrobble else launches.source_of(a.context)


def _item_of(info, a: Attempt):
    return next((i for i in info.items if i.state == "active" and
                 a.aliases & {x for x in (i.ref, f"nd:{i.nd_song_id}" if i.nd_song_id else None) if x}), None)


def _gen_block(offered: Counter, matched: list[tuple[str, Attempt]], liked: Counter, titles) -> list[dict]:
    out = {}
    for g, n in offered.items():
        out[g] = {"generator": g, "title": titles(g), "offered": n, "started": 0, "heard": 0, "finished": 0,
                  "early_skips": 0, "liked": liked.get(g, 0)}
    for g, a in matched:
        x = out.setdefault(g, {"generator": g, "title": titles(g), "offered": 0, "started": 0, "heard": 0,
                               "finished": 0, "early_skips": 0, "liked": liked.get(g, 0)})
        x["started"] += a.started
        x["heard"] += a.heard_at is not None
        x["finished"] += a.finished
        x["early_skips"] += a.early_skip_at is not None
    return sorted(out.values(), key=lambda x: (-x["offered"], -x["started"], x["generator"]))


def _adopted(h: History, kind: str, inside) -> int:
    """Tracks first heard from the mix/wave, then liked or deliberately chosen at least twice."""
    first: dict[str, Attempt] = {}
    for a in h.attempts:
        if a.heard_at is not None and a.key not in first:
            first[a.key] = a
    cohort = {key: a for key, a in first.items() if source(a) == kind and inside(a.heard_at)}
    manual = Counter()
    for x in h.attempts:
        discovery = cohort.get(x.key)
        if (discovery and x.started_at > discovery.heard_at and x.heard_at is not None and
                x.heard_outcome_at is not None and manual[x.key] < 2 and source(x) in SELF_SELECTED and
                attempt_trigger(x.events) == "manual"):
            manual[x.key] += 1
    return sum(manual[key] >= 2 or any(
        m.kind == "like" and m.ts > a.heard_at and launches.mark_aliases(h, m) & a.aliases for m in h.marks
    ) for key, a in cohort.items())


async def listening(s: AsyncSession, user: User, start: date, end: date, timezone: str, now: datetime,
                    h: History | None = None) -> dict[str, Any]:
    h = h or await launches.load(s, user.id, now)
    begin, stop = bounds(start, end, timezone)
    assert begin is not None and stop is not None

    def inside(ts: datetime | None) -> bool:
        return ts is not None and begin <= ts < stop

    players = [a for a in h.attempts if a.client != "navidrome"]
    in_period = [a for a in players if inside(a.started_at)]
    heard = [a for a in h.attempts if inside(a.heard_at)]
    first_heard: dict[str, datetime] = {}
    first_attempt: dict[str, Attempt] = {}
    for a in h.attempts:
        if a.heard_at and (a.key not in first_heard or a.heard_at < first_heard[a.key]):
            first_heard[a.key], first_attempt[a.key] = a.heard_at, a
    heard_keys = {a.key: a for a in h.attempts if a.heard_at}
    name_of = await names.resolve(s, [(a.ref, a.nd_song_id) for a in heard_keys.values()])

    def name(a: Attempt) -> names.Name:
        return name_of.get(f"nd:{a.nd_song_id}" if a.nd_song_id else a.ref or "") or \
            names.Name(a.ref, a.nd_song_id, "", names.UNKNOWN, False, "unknown")

    artist_first: dict[str, datetime] = {}
    for key, ts in first_heard.items():
        if (n := name(heard_keys[key])).artist:
            a = artist_key(n.artist)
            artist_first[a] = min(artist_first.get(a, ts), ts)

    def marks(kind: str, target: str | None = None) -> int:
        return sum(m.kind == kind and inside(m.ts) and (target is None or m.target_type == target) for m in h.marks)

    downloads = sum(not quiet(i.query_json) for i in (await s.execute(select(DownloadItem).where(
        DownloadItem.user_id == user.id, DownloadItem.status == "done", DownloadItem.created_at >= begin,
        DownloadItem.created_at < stop))).scalars())
    plays = [r for r in h.rows if r.kind in PLAYBACK_KINDS and r.target_type == "track" and inside(r.ts)]
    totals = {"counted_listens": sum(inside(a.counted_at) for a in h.attempts),
              "heard_events": sum(inside(a.heard_outcome_at) for a in h.attempts),
              "starts": sum(a.started for a in in_period), "finished": sum(a.finished for a in in_period),
              "early_skips": sum(a.early_skip_at is not None for a in in_period),
              "late_skips": sum(a.late_skip for a in in_period),
              "listened_ms": sum(a.listened_between(begin, stop) for a in h.attempts),
              "unique_tracks": len({a.key for a in heard}),
              "unique_artists": len({artist_key(name(a).artist) for a in heard if name(a).artist}),
              "new_tracks": sum(inside(ts) for ts in first_heard.values()),
              "new_artists": sum(inside(ts) for ts in artist_first.values()),
              "active_days": len({local(a.heard_at, timezone).date() for a in heard if a.heard_at}),
              "likes": marks("like"), "unlikes": marks("unlike"), "dislikes_track": marks("dislike", "track"),
              "dislikes_artist": marks("dislike", "artist"), "downloads": downloads}

    days: dict[date, dict[str, Any]] = {}
    d = start
    while d <= end:
        days[d] = {"date": d.isoformat(), "heard": 0, "listened_ms": 0, "by_source": Counter()}
        d += timedelta(days=1)
    for a in heard:
        x = days.get(local(a.heard_at, timezone).date())  # type: ignore[arg-type]
        if x is not None:
            x["heard"] += 1
            x["by_source"][source(a)] += 1
    for a in h.attempts:
        for ts, ms in a.deltas:
            if inside(ts) and (x := days.get(local(ts, timezone).date())) is not None:
                x["listened_ms"] += ms

    period_launches = [x for x in h.launches if inside(x.started_at) and not x.restart]
    by_source: dict[str, dict[str, Any]] = {}

    def src(name_: str) -> dict[str, Any]:
        return by_source.setdefault(name_, {"source": name_, "launches": 0, "starts": 0, "heard": 0, "finished": 0,
                                            "early_skips": 0, "listened_ms": 0})

    for x in period_launches:
        src(x.source)["launches"] += 1
    for a in in_period:
        b = src(source(a))
        b["starts"] += a.started
        b["finished"] += a.finished
        b["early_skips"] += a.early_skip_at is not None
    for a in heard:
        src(source(a))["heard"] += 1
    for a in h.attempts:
        if ms := a.listened_between(begin, stop):
            src(source(a))["listened_ms"] += ms

    heatmap = [[0] * 24 for _ in range(7)]
    for a in h.attempts:
        if inside(a.counted_at):
            t = local(a.counted_at, timezone)  # type: ignore[arg-type]
            heatmap[t.weekday()][t.hour] += 1

    clients: dict[str, dict[str, Any]] = {}
    for a in h.attempts:
        if not (inside(a.started_at) or inside(a.heard_at)):
            continue
        c = clients.setdefault(a.client, {"client": a.client, "devices": set(), "starts": 0, "heard": 0,
                                          "last_at": a.ended_at})
        if a.device:
            c["devices"].add(a.device)
        c["starts"] += a.started and inside(a.started_at)
        c["heard"] += inside(a.heard_at)
        c["last_at"] = max(c["last_at"], a.ended_at)

    track_heard: Counter = Counter(a.key for a in heard)
    track_ms: Counter = Counter()
    for a in h.attempts:
        track_ms[a.key] += a.listened_between(begin, stop)
    top_tracks = sorted(track_heard, key=lambda k: (-track_heard[k], -track_ms[k], k))[:TOP]
    artist_heard: Counter = Counter()
    artist_tracks: dict[str, set[str]] = defaultdict(set)
    artist_label: dict[str, str] = {}
    for a in heard:
        if n := name(a).artist:
            ak = artist_key(n)
            artist_heard[ak] += 1
            artist_tracks[ak].add(a.key)
            artist_label.setdefault(ak, n)
    discoveries = sorted((k for k, ts in first_heard.items() if inside(ts)), key=lambda k: first_heard[k],
                         reverse=True)

    return {"user_id": user.id,
            "period": {"from": start.isoformat(), "to": end.isoformat(), "timezone": timezone},
            "totals": totals,
            "by_day": [{**x, "by_source": dict(x["by_source"])} for x in days.values()],
            "by_source": sorted(by_source.values(), key=lambda x: (-x["heard"], -x["starts"], x["source"])),
            "heatmap": heatmap,
            "clients": sorted(({**c, "devices": len(c["devices"])} for c in clients.values()),
                              key=lambda c: (-c["starts"], c["client"])),
            "top_tracks": [{**name(heard_keys[k]).out(), "key": k, "heard": track_heard[k],
                            "listened_ms": track_ms[k]} for k in top_tracks],
            "top_artists": [{"artist": artist_label[a], "heard": artist_heard[a], "tracks": len(artist_tracks[a])}
                            for a in sorted(artist_heard, key=lambda a: (-artist_heard[a], a))[:TOP]],
            "discoveries": [{**name(heard_keys[k]).out(), "key": k, "first_heard_at": first_heard[k],
                             "source": source(first_attempt[k])} for k in discoveries[:DISCOVERIES]],
            "recommendations": {"mix": await _mix_block(s, h, user, period_launches, inside),
                                "wave": _wave_block(h, inside)},
            "quality": _quality(h, plays, period_launches, inside)}


async def _mix_block(s: AsyncSession, h: History, user: User, period_launches: list[Launch], inside) -> dict:
    built = [m for m in h.mixes.values() if inside(m.mix.created_at)]
    ml = [x for x in period_launches if x.mix_id is not None]
    attempts = [a for x in ml for a in x.attempts]
    familiar = {"started": 0, "heard": 0}
    new = {"started": 0, "heard": 0}
    matched: list[tuple[str, Attempt]] = []
    liked: Counter = Counter()
    for x in ml:
        info = h.mixes.get(x.mix_id)  # type: ignore[arg-type]
        if info is None:
            continue
        for a in x.attempts:
            if (item := _item_of(info, a)) is None:
                continue
            block = familiar if item.familiar else new
            block["started"] += a.started
            block["heard"] += a.heard_at is not None
            matched.append((item.generator, a))
        for m in x.marks:
            if m.kind == "like":
                al = launches.mark_aliases(h, m)
                if item := next((i for i in info.items if i.state == "active" and
                                 h.aliases.of(i.ref, i.nd_song_id) & al), None):
                    liked[item.generator] += 1
    offered = Counter(i.generator for m in built for i in m.items if i.state == "active")
    return {"built": len(built), "launched_mixes": len({x.mix_id for x in ml}), "launches": len(ml),
            **{k: v for k, v in launches.attempt_stats(attempts).items() if k in ("started", "heard", "finished",
                                                                                    "early_skips")},
            **launches.mark_stats([m for x in ml for m in x.marks]),
            "familiar": familiar, "new": new,
            "by_generator": _gen_block(offered, matched, liked, lambda g: GENERATOR_TITLES.get(g, g)),
            "adopted": _adopted(h, "mix", inside)}


def _wave_block(h: History, inside) -> dict:
    found = [x for x in h.launches if x.wave_id and inside(x.started_at)]
    users = [x for x in found if not x.restart]
    attempts = [a for x in users for a in x.attempts]
    offered: Counter = Counter()
    matched: list[tuple[str, Attempt]] = []
    liked: Counter = Counter()
    for x in users:
        items = h.wave_items.get(x.wave_id or "", [])
        offered.update(i.generator for i in items)
        for a in x.attempts:
            if item := next((i for i in items if a.aliases & (h.aliases.of(i.ref, i.nd_song_id) | {i.key})), None):
                matched.append((item.generator, a))
        for m in x.marks:
            if m.kind == "like" and (item := next((i for i in items if launches.mark_aliases(h, m) &
                                                   (h.aliases.of(i.ref, i.nd_song_id) | {i.key})), None)):
                liked[item.generator] += 1
    return {"launches": len(users), "restarts": len(found) - len(users),
            "by_origin": dict(Counter((h.sessions[x.wave_id].origin if x.wave_id in h.sessions else None)
                                      or NOT_RECORDED for x in users)),
            "by_seed_kind": dict(Counter(seed_kind(h.sessions.get(x.wave_id or "")) for x in users)),
            **{k: v for k, v in launches.attempt_stats(attempts).items() if k in ("started", "heard", "finished",
                                                                                    "early_skips")},
            **launches.mark_stats([m for x in users for m in x.marks]),
            "by_generator": _gen_block(offered, matched, liked, generator_title),
            "adopted": _adopted(h, "wave", inside)}


def _quality(h: History, plays, period_launches: list[Launch], inside) -> dict:
    player_rows = [r for r in plays if r.client != "navidrome"]
    return {"events_without_context": sum(r.context is None for r in player_rows),
            "events_without_playback": sum(r.playback_id is None for r in player_rows),
            "scrobbles": sum(a.scrobble and inside(a.counted_at) for a in h.attempts),
            "legacy_finishes": sum(r.kind == "finish" and not (r.playback_id and r.device_id) for r in player_rows),
            "inferred_launches": sum(x.attribution == "inferred" for x in period_launches),
            "inferred_playlist_mixes": sum(x.mix_attribution == "inferred_playlist" for x in period_launches),
            "reconstructed_waves": sum(x.attribution == "reconstructed" for x in period_launches),
            "first_event_at": h.rows[0].ts if h.rows else None}


async def overview(s: AsyncSession, users: list[tuple[User, bool]], start: date, end: date, timezone: str,
                   now: datetime) -> dict[str, Any]:
    """`users` — (user, excluded): excluded ones get a row but stay out of the totals."""
    rows = []
    for u, excluded in users:
        h = await launches.load(s, u.id, now)
        x = await listening(s, u, start, end, timezone, now, h)
        t, mix, wave = x["totals"], x["recommendations"]["mix"], x["recommendations"]["wave"]
        versions = [r for r in h.rows if r.client.startswith("app/")]
        rows.append({"user": {"id": u.id, "username": u.nd_username, "is_admin": u.is_admin, "excluded": excluded},
                     "counted_listens": t["counted_listens"], "heard_events": t["heard_events"],
                     "listened_ms": t["listened_ms"], "active_days": t["active_days"],
                     "mixes_built": mix["built"], "mix_launches": mix["launches"], "wave_launches": wave["launches"],
                     "wave_restarts": wave["restarts"], "likes": t["likes"],
                     "dislikes": t["dislikes_track"] + t["dislikes_artist"], "new_tracks": t["new_tracks"],
                     "new_artists": t["new_artists"], "last_activity_at": h.rows[-1].ts if h.rows else None,
                     "app_version": versions[-1].client[4:] if versions else None,
                     "last_seen_at": u.last_seen_at})
    keys = ("counted_listens", "heard_events", "listened_ms", "mixes_built", "mix_launches", "wave_launches",
            "wave_restarts", "likes", "dislikes", "new_tracks")
    counted = [r for r in rows if not r["user"]["excluded"]]
    return {"period": {"from": start.isoformat(), "to": end.isoformat(), "timezone": timezone},
            "users": rows, "totals": {k: sum(r[k] for r in counted) for k in keys} | {"users": len(counted)}}


__all__ = ["bounds", "listening", "overview", "period"]
