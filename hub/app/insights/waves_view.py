"""Sessions of «Моя волна» for the admin (INSIGHTS.md 7.6): where and from what they were started, what they handed
out and what became of it. Kept rows, cleared summaries and sessions known only by their events."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.insights import launches, names
from app.insights.launches import History, Launch
from app.mixes.genre_wave import GENRE_TITLES
from app.mixes.wave import generator_title
from app.models import WaveSession

NOT_RECORDED = "not_recorded"


def seed_kind(w: WaveSession | None) -> str:
    """`own` / `genre` / `track` / `track_artist` / `artist` / `library_artist` / `file`; `unknown` for a
    session known only by its events."""
    if w is None:
        return "unknown"
    req = (w.request_json or {}).get("seed") if w.request_json is not None else None
    if w.request_json is not None:
        if not req:
            return "own"
        if req.get("by_artist"):
            return "track_artist"
        if (req.get("ref") or "").startswith("file:"):
            return "file"
        if req.get("ref") or req.get("nd_song_id"):
            return "track"
        if req.get("nd_artist_id"):
            return "library_artist"
        if req.get("artist_ref"):
            return "artist"
    seed = w.seed_json or {}
    if not seed:
        return "own"
    if seed.get("genre"):
        return "genre"
    if (seed.get("ref") or "").startswith("file:"):
        return "file"
    if seed.get("ref"):
        return "track"
    if seed.get("nd_artist_id"):
        return "library_artist"
    if seed.get("artist_ref"):
        return "artist"
    return "own"


def _seed(w: WaveSession | None) -> dict[str, Any] | None:
    if w is None or not w.seed_json:
        return None
    seed, req = w.seed_json, (w.request_json or {}).get("seed") or {}
    if genre := seed.get("genre"):
        return {"genre": genre, "title": GENRE_TITLES.get(genre, genre)}
    ref = seed.get("ref")
    return {"title": seed.get("title"), "artist": seed.get("artist"), "ref": ref,
            "nd_song_id": req.get("nd_song_id") or (ref[3:] if ref and ref.startswith("nd:") else None),
            "artist_ref": seed.get("artist_ref"), "nd_artist_id": seed.get("nd_artist_id")}


def row(h: History, launch: Launch) -> dict[str, Any]:
    w = h.sessions.get(launch.wave_id or "")
    state = (w.state_json or {}) if w else {}
    return {"session_id": launch.wave_id, "created_at": launch.started_at,
            "last_activity_at": max(filter(None, [w.updated_at if w else None, launch.ended_at])),
            "origin": w.origin if w else None, "client": (w.client if w else None) or launch.client,
            "restart_of": w.restart_of if w else None, "restart": launch.restart, "seed_kind": seed_kind(w),
            "seed": _seed(w), "mode": w.mode if w else None, "taste_model": (w.taste_model if w else None) or
            state.get("taste_model"), "next_calls": w.next_calls if w else None,
            "handed": len(h.wave_items.get(launch.wave_id or "", ())) if w else None,
            **launch.stats(), "downloads": len(state.get("downloads") or ()) if w else None,
            "cleared": bool(w and w.cleared_at), "reconstructed": w is None}


def _local_day(ts: datetime, begin: datetime | None, stop: datetime | None) -> bool:
    return (begin is None or ts >= begin) and (stop is None or ts < stop)


def sessions(h: History, *, begin: datetime | None, stop: datetime | None, origin: str | None,
             kind: str | None) -> list[Launch]:
    out = [x for x in h.launches if x.wave_id and _local_day(x.started_at, begin, stop)]
    if origin:
        out = [x for x in out if ((h.sessions[x.wave_id].origin if x.wave_id in h.sessions else None)
                                  or NOT_RECORDED) == origin]
    if kind:
        out = [x for x in out if seed_kind(h.sessions.get(x.wave_id or "")) == kind]
    return sorted(out, key=lambda x: (x.started_at, x.id), reverse=True)


def listing(h: History, *, begin: datetime | None, stop: datetime | None, origin: str | None, kind: str | None,
            limit: int, offset: int) -> dict[str, Any]:
    found = sessions(h, begin=begin, stop=stop, origin=origin, kind=kind)
    users = [x for x in found if not x.restart]
    return {"total": len(found), "offset": offset, "rows": [row(h, x) for x in found[offset:offset + limit]],
            "launches": len(users), "restarts": len(found) - len(users),
            "by_origin": dict(Counter((h.sessions[x.wave_id].origin if x.wave_id in h.sessions else None)
                                      or NOT_RECORDED for x in users)),
            "by_seed_kind": dict(Counter(seed_kind(h.sessions.get(x.wave_id or "")) for x in users))}


async def detail(s: AsyncSession, h: History, sid: str) -> dict[str, Any] | None:
    launch = next((x for x in h.launches if x.wave_id == sid), None)
    if launch is None:
        return None
    w = h.sessions.get(sid)
    state = (w.state_json or {}) if w else {}
    items = h.wave_items.get(sid, [])
    events = sorted([e for a in launch.attempts for e in a.events] + launch.marks, key=lambda e: (e.ts, e.id))
    pairs = [(i.ref, i.nd_song_id) for i in items] + [(e.ref, e.nd_song_id) for e in events]
    known = {(i.ref, i.nd_song_id): (i.artist, i.title) for i in items}
    name_of = await names.resolve(s, pairs, known)

    def name(ref, nd):
        n = name_of.get(f"nd:{nd}" if nd else ref or "")
        return n.out() if n else {"key": ref, "ref": ref, "nd_song_id": nd, "artist": "", "title": names.UNKNOWN,
                                  "owned": False}

    return {**row(h, launch),
            "gens": {g: {**(v if isinstance(v, dict) else {"tracks": v}), "title": generator_title(g)}
                     for g, v in (state.get("gens") or {}).items()},
            "seeds_used": state.get("seeds_used"),
            "items": [{**name(i.ref, i.nd_song_id), "position": i.position, "first_handed_at": i.first_handed_at,
                       "last_handed_at": i.last_handed_at, "handed_count": i.handed_count, "generator": i.generator,
                       "generator_title": generator_title(i.generator), "gens": i.gens, "familiar": i.familiar,
                       "score": i.score, "sim": i.sim, "value": i.value,
                       **launches.outcome(launch.attempts, launch.marks, h.aliases.of(i.ref, i.nd_song_id) | {i.key})}
                      for i in items],
            "events": [{"ts": e.ts, "kind": e.kind, "track": name(e.ref, e.nd_song_id), "position_s": e.position_s,
                        "client": e.client} for e in events]}


__all__ = ["NOT_RECORDED", "detail", "listing", "row", "seed_kind", "sessions"]
