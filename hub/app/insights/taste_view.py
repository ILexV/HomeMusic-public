"""The taste profile as the admin sees it (INSIGHTS.md 7.2, 7.3, 7.6): the same code the mixes and the wave use
(`load_snapshot` + `calculate_profile`), with the breakdown of every weight; no network but the embedder's facets."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.insights import launches, names
from app.mixes import embedder
from app.mixes.build import FAVORITE_REST_DAYS, GENERATOR_TITLES, RECENT_DAYS, STASH_REST_DAYS
from app.mixes.taste import Profile, TasteTrack, artist_key
from app.mixes.taste_math import Contribution, Trace, V2Profile, calculate_baseline, calculate_profile, saturation
from app.mixes.taste_snapshot import load_snapshot
from app.models import User

Model = Literal["v1", "v2"]
FACETS_TIMEOUT_S = 8
TRACK_FILTERS = ("all", "positive", "seed", "negative", "liked", "disliked", "downloaded")
TRACK_SORTS = ("s", "p", "n", "last_heard", "plays", "title")
MATRIX_FILTERS = ("all", "shared_liked", "conflict", "single", "disliked")
SEEDS_NOTE = "без холодного старта и сетевого обогащения; что реально взял микс — в карточке микса"
V1_NOTE = "локальный v1: для сравнения, без сети (рабочий v1 дозаполнял треки из каталога)"


def model_of(model: str | None) -> Model:
    return (model or get_settings().cfg.mixes.taste_model)  # type: ignore[return-value]


async def admin_profile(s: AsyncSession, user_id: int, *, model: str | None, now: datetime,
                        trace: Trace | None = None) -> Profile:
    """The profile for the admin, without side effects: never `taste.build_profile(model="v1")` (the network,
    a reconcile), `taste_select.seeds()` / `build.cold_seeds` (the network) or anything of the wave's memory."""
    snapshot = await load_snapshot(s, user_id, now=now)
    tz = get_settings().timezone
    if model_of(model) == "v2":
        return calculate_profile(snapshot, now=now, timezone=tz, trace=trace)
    return calculate_baseline(snapshot, now=now, timezone=tz, mode="occurred-at", trace=trace)


async def facets(p: Profile) -> tuple[list[dict] | None, Any]:
    """The facets of a v2 profile from the embedder (`/v2/profile` only: no model load, no embedding). Returns
    (facets or None, the embedder state: "off", "down", "timeout" or {space_id, model})."""
    if not isinstance(p, V2Profile):
        return None, "v1"
    if not embedder.configured():
        return None, "off"
    from app.mixes.taste_select import facet_names, prepare

    try:
        await asyncio.wait_for(prepare(p), FACETS_TIMEOUT_S)
    except TimeoutError:
        p.geometry = None
        return None, "timeout"
    if p.geometry is None:
        return None, "down"
    return facet_names(p), {"space_id": p.geometry.space_id, "model": p.geometry.model}


def heard_event(c: Contribution) -> bool:
    """The profile's `heard30` rule (finish, skip ≥ 30 s) over a recorded contribution."""
    if c.kind == "finish":
        return c.effective_seconds is None or c.effective_seconds >= 30 or bool(
            c.duration_s and c.effective_seconds >= c.duration_s)
    return c.kind == "skip" and c.effective_seconds is not None and c.effective_seconds >= 30


def counters(contribs: list[Contribution]) -> dict[str, int]:
    return {"starts": sum(c.kind == "start" for c in contribs),
            "finishes": sum(c.kind == "finish" for c in contribs),
            "early_skips": sum(c.kind == "skip" and c.effective_seconds is not None and c.effective_seconds < 30 for c in contribs),
            "late_skips": sum(c.kind == "skip" and c.effective_seconds is not None and c.effective_seconds >= 30 for c in contribs),
            "plays": sum(heard_event(c) for c in contribs)}


def disliked_track(p: Profile, t: TasteTrack) -> bool:
    return t.key in getattr(p, "explicit_negative_keys", set()) or bool(
        (t.ref and t.ref in p.disliked_ids) or (t.nd_song_id and f"nd:{t.nd_song_id}" in p.disliked_ids))


def liked_artists(p: Profile) -> set[str]:
    return {artist_key((st.data or {}).get("name")) for st in getattr(p, "favorite_states", {}).values()
            if st.target_type == "artist" and st.liked and (st.data or {}).get("name")}


def sat_p(p: Profile, t: TasteTrack) -> float:
    return saturation(t.positive_raw) if isinstance(p, V2Profile) else t.positive_raw


def facet_index(found: list[dict] | None) -> dict[str, str]:
    return {key: f["id"] for f in found or () for key in f.get("members") or ()}


def seed_pool(p: Profile) -> set[str]:
    return {t.key for t in p.active_seed_pool()} if isinstance(p, V2Profile) else set()


async def _names(s: AsyncSession, tracks: list[TasteTrack]) -> dict[str, names.Name]:
    known = {(t.ref, t.nd_song_id): (t.artist, t.title) for t in tracks}
    found = await names.resolve(s, [(t.ref, t.nd_song_id) for t in tracks], known)
    return {t.key: found.get(f"nd:{t.nd_song_id}" if t.nd_song_id else t.ref or "") or
            names.Name(t.ref, t.nd_song_id, t.artist, t.title or names.UNKNOWN, False, "profile") for t in tracks}


def track_row(p: Profile, t: TasteTrack, trace: Trace, name: names.Name, facet_of: dict[str, str],
              seeds: set[str]) -> dict[str, Any]:
    c = counters(trace.tracks.get(t.key, []))
    return {"key": t.key, "ref": t.ref, "nd_song_id": t.nd_song_id, "artist": name.artist or t.artist,
            "title": name.title or t.title, "owned": name.owned, "s": t.score, "p": t.positive_raw,
            "sat_p": sat_p(p, t), "n": t.negative, "liked": t.liked, "disliked": disliked_track(p, t),
            "downloaded": t.downloaded, "album_liked": t.album_liked, "heard30": t.heard30, "familiar": t.familiar,
            "seed_eligible": t.key in seeds, "last_heard": t.last_heard, "last_positive": t.last_positive,
            "positive_age": t.positive_age, "plays": c["plays"], "early_skips": c["early_skips"],
            "facet_id": facet_of.get(t.key)}


async def summary(s: AsyncSession, user: User, model: str | None, now: datetime) -> dict[str, Any]:
    cfg = get_settings().cfg.mixes
    m = model_of(model)
    p = await admin_profile(s, user.id, model=m, now=now)
    found, state = await facets(p)
    tracks = list(p.tracks.values())
    v2 = isinstance(p, V2Profile)
    name_of = await _names(s, tracks)
    facet_list = None
    if found is not None:
        facet_list = []
        for f in found:
            members = sorted((p.tracks[k] for k in f["members"] if k in p.tracks), key=lambda t: (-t.score, t.key))
            facet_list.append({"id": f["id"], "weight": f["weight"], "artists": f["artists"][:2],
                               "size": len(f["members"]),
                               "top_members": [{**name_of[t.key].out(), "key": t.key, "s": t.score}
                                               for t in members[:5]]})
    n = min(8, max(0, cfg.seeds))
    selected = p.seeds(n) if v2 else p.seeds(cfg.seeds)
    facet_of = facet_index(found)
    onboarding = list(p.picked_artists)
    if v2 and not p.has_recent_positive:
        onboarding += [{"ref": st.ref, "name": (st.data or {}).get("name", "")}
                       for st in sorted(p.favorite_states.values(), key=lambda x: (x.target_type, x.key))
                       if st.target_type == "artist" and st.liked and st.ref and st.ref.startswith("dz:")]
    onboarding = list({a["ref"]: a for a in onboarding if a.get("ref")}.values())[:n]
    liked_a = liked_artists(p)
    artists = sorted(p.artists.items(), key=lambda x: (-x[1], x[0]))[:50]
    return {
        "user_id": user.id, "model": m, "model_note": None if v2 else V1_NOTE, "now": now,
        "timezone": get_settings().timezone, "evidence": getattr(p, "evidence", sum(max(t.score, 0) for t in tracks)),
        "maturity": getattr(p, "maturity", None),
        "active_units": p.clock.prefix[-1] if v2 and p.clock else None,
        "active_days": len(p.clock.days) if v2 and p.clock else None,
        "counts": {"total": len(tracks), "positive": sum(t.score > 0 for t in tracks),
                   "strong": sum(t.score >= 1 for t in tracks), "negative": sum(t.score < 0 for t in tracks),
                   "liked": sum(t.liked for t in tracks), "downloaded": sum(t.downloaded for t in tracks),
                   "album_liked": sum(t.album_liked for t in tracks),
                   "explicit_dislikes": sum(disliked_track(p, t) for t in tracks),
                   "disliked_artists": len(p.disliked_artists | p.disliked_artist_refs)},
        "facets": facet_list, "embedder": state,
        "seeds_today": [{**name_of[t.key].out(), "key": t.key, "s": t.score, "facet_id": facet_of.get(t.key)}
                        for t in selected],
        "onboarding_reserve": round((1 - p.maturity) * n) if v2 else 0,
        "onboarding_artists": onboarding, "seeds_note": SEEDS_NOTE,
        "top_artists": [{"key": a, "name": p.artist_names.get(a, a), "score": sc,
                         "listens": p.artist_listens.get(a, 0), "liked": a in liked_a,
                         "disliked": a in p.disliked_artists} for a, sc in artists],
        "limitations": list(getattr(p, "limitations", ())) + ([] if found is not None or not v2 else
                                                               ["facets: embedder " + str(state)]),
    }


def _filtered(p: Profile, rows: list[dict], flt: str, q: str | None) -> list[dict]:
    keep = {"all": lambda r: True, "positive": lambda r: r["s"] > 0, "seed": lambda r: r["seed_eligible"],
            "negative": lambda r: r["s"] < 0, "liked": lambda r: r["liked"], "disliked": lambda r: r["disliked"],
            "downloaded": lambda r: r["downloaded"]}[flt]
    needle = (q or "").strip().lower()
    return [r for r in rows if keep(r) and (not needle or needle in f"{r['artist']} {r['title']}".lower())]


def _sorted(rows: list[dict], sort: str) -> list[dict]:
    if sort == "title":
        return sorted(rows, key=lambda r: (r["title"].lower(), r["artist"].lower(), r["key"]))
    field = {"p": "p", "n": "n", "plays": "plays", "s": "s", "last_heard": "last_heard"}[sort]
    return sorted(rows, key=lambda r: (r[field] is not None, r[field] or 0, r["key"]), reverse=True)


async def tracks(s: AsyncSession, user: User, *, model: str | None, flt: str, q: str | None, sort: str,
                 limit: int, offset: int, now: datetime) -> dict[str, Any]:
    trace = Trace()
    p = await admin_profile(s, user.id, model=model, now=now, trace=trace)
    found, state = await facets(p)
    all_tracks = list(p.tracks.values())
    name_of, facet_of, seeds = await _names(s, all_tracks), facet_index(found), seed_pool(p)
    rows = _sorted(_filtered(p, [track_row(p, t, trace, name_of[t.key], facet_of, seeds) for t in all_tracks],
                             flt, q), sort)
    return {"model": model_of(model), "now": now, "embedder": state, "total": len(rows), "offset": offset,
            "rows": rows[offset:offset + limit]}


def formula(p: Profile, t: TasteTrack) -> str:
    if isinstance(p, V2Profile):
        return (f"s = sat(P) − N = sat({t.positive_raw:.3f}) − {t.negative:.3f} = "
                f"{saturation(t.positive_raw):.3f} − {t.negative:.3f} = {t.score:.3f}")
    return f"s = P − N = {t.positive_raw:.3f} − {t.negative:.3f} = {t.score:.3f} (локальный v1)"


def artist_block(p: Profile, t: TasteTrack) -> dict[str, Any] | None:
    if not t.artist:
        return None
    a = artist_key(t.artist)
    mine = [x for x in p.tracks.values() if x.artist and artist_key(x.artist) == a]
    pos, neg = sum(max(x.score, 0) for x in mine), sum(max(-x.score, 0) for x in mine)
    base = saturation(pos) - .5 * saturation(neg) if isinstance(p, V2Profile) else pos - neg
    score = p.artists.get(a, 0.0)
    return {"key": a, "name": p.artist_names.get(a, t.artist), "score": score, "positive_sum": pos,
            "negative_sum": neg, "tracks_part": base, "likes_part": score - base, "tracks": len(mine),
            "this_track": t.score, "listens": p.artist_listens.get(a, 0), "liked": a in liked_artists(p),
            "disliked": a in p.disliked_artists,
            "formula": "A = sat(Σ max(s,0)) − 0.5·sat(Σ max(−s,0)) + ♥исполнителя·w + 0.5·Σ ♥альбома·w"
            if isinstance(p, V2Profile) else "A = Σ s"}


def _track_aliases(p: Profile, t: TasteTrack) -> set[str]:
    out = {k for k, v in p.by_id.items() if v == t.key and k != t.fp}
    out |= {x for x in (t.ref, f"nd:{t.nd_song_id}" if t.nd_song_id else None, t.key) if x}
    return out


async def track(s: AsyncSession, user: User, *, key: str, model: str | None, now: datetime) -> dict[str, Any] | None:
    trace = Trace()
    p = await admin_profile(s, user.id, model=model, now=now, trace=trace)
    t = p.tracks.get(key) or (p.tracks.get(p.by_id[key]) if key in p.by_id else None)
    if t is None:
        return None
    name = (await _names(s, [t]))[t.key]
    contribs = sorted(trace.tracks.get(t.key, []), key=lambda c: (c.ts is not None, c.ts or now), reverse=True)
    aliases = _track_aliases(p, t)
    h = await launches.load(s, user.id, now)
    by_mix: dict[int, list[launches.Launch]] = {}
    for x in h.launches:
        if x.mix_id is not None:
            by_mix.setdefault(x.mix_id, []).append(x)
    mixes = []
    for info in h.mixes.values():
        for i in info.items:
            if h.aliases.of(i.ref, i.nd_song_id) & aliases:
                ls = by_mix.get(info.mix.id, [])
                mixes.append({"mix_id": info.mix.id, "created_at": info.mix.created_at, "state": i.state,
                              "position": i.position, "generator": i.generator,
                              "generator_title": GENERATOR_TITLES.get(i.generator, i.generator), "score": i.score,
                              **launches.outcome([a for x in ls for a in x.attempts], [m for x in ls for m in x.marks],
                                                 aliases)})
    waves = []
    for sid, items in h.wave_items.items():
        for w in items:
            if h.aliases.of(w.ref, w.nd_song_id) & aliases or w.key in aliases:
                lw = next((x for x in h.launches if x.wave_id == sid), None)
                waves.append({"session_id": sid, "first_handed_at": w.first_handed_at, "position": w.position,
                              "generator": w.generator, "score": w.score, "value": w.value,
                              **launches.outcome(lw.attempts if lw else [], lw.marks if lw else [], aliases)})
    seeds = seed_pool(p)
    c = counters(contribs)
    found, _ = await facets(p)
    from app.insights import maps  # the map needs this module's profile helpers

    cos = await maps.track_facet_cos(p, t.key)
    return {**track_row(p, t, trace, name, facet_index(found), seeds), **c, "model": model_of(model), "now": now,
            "contributions": [asdict(x) for x in contribs], "formula": formula(p, t), "artist": artist_block(p, t),
            "rest": {"recent_until": t.last_heard + timedelta(days=RECENT_DAYS) if t.last_heard else None,
                     "favorites_after": t.last_heard + timedelta(days=FAVORITE_REST_DAYS) if t.last_heard else None,
                     "stash_after": t.last_heard + timedelta(days=STASH_REST_DAYS) if t.last_heard else None},
            "facet_cos": cos,
            "mixes": sorted(mixes, key=lambda x: x["created_at"], reverse=True),
            "waves": sorted(waves, key=lambda x: x["first_handed_at"], reverse=True)}


class _Union:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def join(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


async def matrix(s: AsyncSession, users: list[User], *, by: str, model: str | None, flt: str, q: str | None,
                 sort: str, limit: int, offset: int, now: datetime) -> dict[str, Any]:
    """Songs (or artists) × users. Rows join only verified aliases (ref ↔ nd:<song> the profile itself unified),
    never an «artist|title» fingerprint: different versions stay apart."""
    profiles: dict[int, tuple[Profile, Trace]] = {}
    for u in users:  # one `now` for everyone
        trace = Trace()
        profiles[u.id] = (await admin_profile(s, u.id, model=model, now=now, trace=trace), trace)
    rows: dict[str, dict[str, Any]] = {}
    if by == "artist":
        for uid, (p, _) in profiles.items():
            liked_a = liked_artists(p)
            for a, score in p.artists.items():
                row = rows.setdefault(a, {"key": a, "artist": p.artist_names.get(a, a), "title": None,
                                          "owned": None, "cells": {}})
                row["cells"][str(uid)] = {"s": score, "p": None, "n": None, "liked": a in liked_a,
                                          "disliked": a in p.disliked_artists, "heard": p.artist_listens.get(a, 0)}
            for a in p.disliked_artists - set(p.artists):
                row = rows.setdefault(a, {"key": a, "artist": p.artist_names.get(a, a), "title": None,
                                          "owned": None, "cells": {}})
                row["cells"][str(uid)] = {"s": 0.0, "p": None, "n": None, "liked": False, "disliked": True,
                                          "heard": p.artist_listens.get(a, 0)}
    else:
        union = _Union()
        for p, _ in profiles.values():
            for t in p.tracks.values():
                for alias in _track_aliases(p, t):
                    union.join(t.key, alias)
        members: dict[str, list[tuple[int, Profile, Trace, TasteTrack]]] = {}
        for uid, (p, trace) in profiles.items():
            for t in p.tracks.values():
                members.setdefault(union.find(t.key), []).append((uid, p, trace, t))
        all_tracks = [t for group in members.values() for (_, _, _, t) in group]
        pairs = {}
        for group in members.values():
            keys = {a for (_, p, _, t) in group for a in _track_aliases(p, t)}
            nds = sorted(k for k in keys if k.startswith("nd:"))
            refs = sorted(k for k in keys if not k.startswith("nd:"))
            key = nds[0] if nds else refs[0] if refs else group[0][3].key
            pairs[union.find(group[0][3].key)] = (key, refs[0] if refs else None, nds[0][3:] if nds else None)
        known = {(t.ref, t.nd_song_id): (t.artist, t.title) for t in all_tracks}
        found = await names.resolve(s, [(ref, nd) for _, ref, nd in pairs.values()], known)
        for root, group in members.items():
            key, ref, nd = pairs[root]
            name = found.get(key) or names.Name(ref, nd, "", names.UNKNOWN, False, "unknown")
            row = rows.setdefault(key, {"key": key, "artist": name.artist, "title": name.title,
                                        "owned": name.owned, "cells": {}})
            for uid, p, trace, t in group:
                cell = row["cells"].get(str(uid))
                c = counters(trace.tracks.get(t.key, []))
                if cell is None or abs(t.score) > abs(cell["s"]):
                    row["cells"][str(uid)] = {"s": t.score, "p": t.positive_raw, "n": t.negative, "liked": t.liked,
                                              "disliked": disliked_track(p, t), "heard": c["plays"]}
    out = []
    for row in rows.values():
        values = [c["s"] for c in row["cells"].values()]
        row.update(sum_pos=sum(max(v, 0) for v in values), min=min(values), max=max(values),
                   users_positive=sum(v > 0 for v in values), users_negative=sum(v < 0 for v in values))
        out.append(row)
    keep = {"all": lambda r: True, "shared_liked": lambda r: r["users_positive"] >= 2,
            "conflict": lambda r: r["max"] >= 1 and r["min"] <= -1 and len(r["cells"]) >= 2,
            "single": lambda r: sum(c["s"] != 0 for c in r["cells"].values()) == 1,
            "disliked": lambda r: any(c["disliked"] for c in r["cells"].values())}[flt]
    needle = (q or "").strip().lower()
    out = [r for r in out if keep(r) and (not needle or needle in f"{r['artist']} {r['title'] or ''}".lower())]
    if sort == "title":
        out.sort(key=lambda r: ((r["title"] or r["artist"] or "").lower(), r["key"]))
    elif sort == "spread":
        out.sort(key=lambda r: (-(r["max"] - r["min"]), r["key"]))
    elif sort.startswith("user:"):
        uid = sort[5:]
        out.sort(key=lambda r: (uid not in r["cells"], -(r["cells"].get(uid) or {"s": 0})["s"], r["key"]))
    else:
        out.sort(key=lambda r: (-r["sum_pos"], r["key"]))
    return {"model": model_of(model), "by": by, "now": now,
            "users": [{"id": u.id, "username": u.nd_username} for u in users],
            "rows": out[offset:offset + limit], "total": len(out), "offset": offset}


__all__ = ["MATRIX_FILTERS", "TRACK_FILTERS", "TRACK_SORTS", "admin_profile", "matrix", "summary", "track", "tracks"]
