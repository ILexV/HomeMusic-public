"""«Умное перемешивание» (MIXES.md 8): a playlist or «Понравившиеся» played shuffled, with recommendations "in the
mood of the playlist" put between its own songs — Spotify's Smart Shuffle.

- The Hub keeps all the logic, the clients only show. `start` gets the context (`playlist:<Navidrome id>` or
  `liked`) and the ids of its songs; the answer is the order of the own songs (`smart_order.fewer_repeats`) and the
  first recommendations; `next` brings the events since the last call and the place of the player, and hands out
  more recommendations for the slots ahead (one after every 3 own songs of a playlist of more than 15, after every 2
  of a shorter one). The playlist itself is never changed.
- Candidates come from the mix's generators (`app.mixes.build`) asked by a sample of the playlist's songs (the newest
  ones, then random, different artists), and are scored by the mix's score, by sound against the center of the
  playlist's songs (the embedder) and by what the session learned (`app.mixes.wave`: a skipped artist and a track that
  sounds like a skipped one go down, a track played to its end, a ♥ or a «+» raise what sounds like it).
- Left out: the songs of the context, «Не нравится», what the user hid with «−» in this context (`smart_hidden`),
  what was heard lately (the mix's rule) — and what the previous runs in this context offered lately is worth less,
  so every run brings a fresh set. An artist at most once in 8 recommendations, never next to its own artist's song.
- The session reuses the wave's pool, filters and learning (`wave.Runtime` with its own row, `smart_sessions`); the
  recommendations handed out go to `smart_items`; the events of a recommendation get the context `smart:<id>`, the
  events of the own songs keep the list they came from (`playlist`, `liked`).
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.catalog.models import CatalogTrack, split_ref
from app.config import get_settings
from app.db import session_scope
from app.matching.normalize import fingerprint
from app.mixes import build, embedder, smart_order, wave
from app.mixes.resolve import library_track
from app.mixes.taste import TasteTrack, artist_key, build_profile
from app.mixes.taste_math import V2Profile
from app.models import CatalogCache, PlayEvent, SmartHidden, SmartItem, SmartSession, Track, User, utcnow
from app.services import svc

log = logging.getLogger(__name__)

AHEAD = 3  # recommendations kept handed out ahead of the player
PLAYLIST_SOUND = 3.0  # × closeness by sound to the center of the context's songs
OFFERED_PENALTY = 3.0  # a track a recent run of the same context offered: the others come first (a fresh set)
OFFERED_DAYS = 3
HEARD_PLAY_S = 30  # a skip after this counts as a listen for the freshness of an own song
IDLE = wave.IDLE
KEEP = wave.KEEP
CENTER_MAX = 200  # songs of the context whose vectors make its center
SEED_REFILL = 3


class SmartError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def valid_context(context: str) -> bool:
    return context == "liked" or (context.startswith("playlist:") and 0 < len(context) - 9 <= 64
                                  and not any(c.isspace() for c in context))


def is_ref(song_id: str) -> bool:
    """An id with a colon is a catalog ref (a ♥ not downloaded yet); without — a Navidrome song id."""
    return ":" in song_id


@dataclass
class OwnInfo:
    id: str
    artist: str = ""
    title: str = ""
    nd_song_id: str | None = None
    ref: str | None = None
    duration_s: int | None = None
    track: CatalogTrack | None = None

    @property
    def fp(self) -> str:
        return fingerprint(self.artist, self.title) if self.artist and self.title else ""


@dataclass
class SmartRuntime(wave.Runtime):
    sid: str = ""
    context: str = ""
    own: list[str] = field(default_factory=list)  # the order of the own songs (ids as the client sent them)
    own_artist: dict[str, str] = field(default_factory=dict)  # id -> artist key ("": unknown)
    filled: dict[str, str] = field(default_factory=dict)  # recommendation slot -> its ref
    rec_artists: list[str] = field(default_factory=list)  # artist keys of the recommendations in hand-out order
    hidden: set[str] = field(default_factory=set)  # fingerprints hidden in this context
    offered: set[str] = field(default_factory=set)  # fingerprints recent runs of this context offered
    own_refs: set[str] = field(default_factory=set)
    own_nd: set[str] = field(default_factory=set)
    center: dict[str, float] = field(default_factory=dict)  # embedder keys of the context's songs -> weight
    psim: dict[str, float] = field(default_factory=dict)  # candidate key -> closeness to the center
    accepted: list[str] = field(default_factory=list)  # keys of recommendations the user added with «+»
    seed_pool: list[dict[str, Any]] = field(default_factory=list)  # catalog tracks of the playlist's songs (seeds)
    seed_next: int = 0


# --- state --------------------------------------------------------------------------------------------------------


def dump(rt: SmartRuntime) -> dict[str, Any]:
    return {"wave": rt.state.dump(), "own": rt.own, "own_artist": rt.own_artist, "filled": rt.filled,
            "rec_artists": rt.rec_artists, "hidden": sorted(rt.hidden), "offered": sorted(rt.offered),
            "own_refs": sorted(rt.own_refs), "own_nd": sorted(rt.own_nd), "center": rt.center, "psim": rt.psim,
            "accepted": rt.accepted, "seed_pool": rt.seed_pool, "seed_next": rt.seed_next}


async def _save(sid: str, rt: wave.Runtime) -> None:
    assert isinstance(rt, SmartRuntime)
    async with session_scope() as s:
        row = await s.get(SmartSession, sid)
        if row is not None:
            row.state_json, row.updated_at = dump(rt), utcnow()


def _blocked(rt: SmartRuntime):
    def blocked(c: build.Cand) -> bool:
        return (c.track.ref in rt.own_refs or (c.nd_song_id is not None and c.nd_song_id in rt.own_nd)
                or wave.fp_of(c) in rt.hidden)

    return blocked


def _wire(rt: SmartRuntime) -> SmartRuntime:
    rt.save, rt.download_finished, rt.blocked = _save, False, _blocked(rt)
    return rt


_rt: dict[str, SmartRuntime] = {}


async def _runtime(sid: str, user: User) -> SmartRuntime:
    rt = _rt.get(sid)
    if rt is None:
        async with session_scope() as s:
            row = await s.get(SmartSession, sid)
            if row is None or row.user_id != user.id or row.updated_at < utcnow() - IDLE or \
                    (row.state_json or {}).get("cleared"):
                raise SmartError(404, "Умное перемешивание закончилось: запустите его заново")
            d = row.state_json or {}
            rt = _wire(SmartRuntime(
                user_id=user.id, mode="normal", state=wave.State.load(d.get("wave") or {}), sid=sid,
                context=row.context, own=list(d.get("own") or []), own_artist=dict(d.get("own_artist") or {}),
                filled=dict(d.get("filled") or {}), rec_artists=list(d.get("rec_artists") or []),
                hidden=set(d.get("hidden") or []), offered=set(d.get("offered") or []),
                own_refs=set(d.get("own_refs") or []), own_nd=set(d.get("own_nd") or []),
                center=dict(d.get("center") or {}), psim=dict(d.get("psim") or {}),
                accepted=list(d.get("accepted") or []), seed_pool=list(d.get("seed_pool") or []),
                seed_next=int(d.get("seed_next") or 0)))
        rt = _rt.setdefault(sid, rt)
    elif rt.user_id != user.id:
        raise SmartError(404, "Умное перемешивание закончилось: запустите его заново")
    return rt


# --- the context's songs -------------------------------------------------------------------------------------------


async def own_infos(ids: list[str]) -> dict[str, OwnInfo]:
    """What the Hub knows of the context's songs (the library record; for a ♥ not downloaded yet — the catalog
    cache). A song it knows nothing of is still played: it has no artist and no title here."""
    out = {i: OwnInfo(i, nd_song_id=None if is_ref(i) else i, ref=i if is_ref(i) else None) for i in ids}
    nd_ids = [i for i in ids if not is_ref(i)]
    dz = {split_ref(i)[1]: i for i in ids if is_ref(i) and i.startswith("dz:")}
    async with session_scope() as s:
        rows: list[Track] = []
        for k in range(0, len(nd_ids), 500):
            rows += (await s.execute(select(Track).where(Track.nd_song_id.in_(nd_ids[k:k + 500])))).scalars()
        for k in range(0, len(dz), 500):
            rows += (await s.execute(select(Track).where(Track.deezer_track_id.in_(list(dz)[k:k + 500])))).scalars()
        for t in rows:
            i = t.nd_song_id if t.nd_song_id in out else dz.get(str(t.deezer_track_id))
            if i is None or out[i].title:
                continue
            ct = library_track(t)
            out[i].artist, out[i].title, out[i].duration_s = t.artist, t.title, t.duration_s
            out[i].nd_song_id, out[i].ref, out[i].track = t.nd_song_id, ct.ref, ct
        # a ♥ without a file: the names the catalog cache kept (the same sources `/api/nd/liked` reads)
        todo = [i for i, x in out.items() if is_ref(i) and not x.title]
        keys = {f"{p}{i}": i for i in todo for p in ("listen:", "mix:track:")}
        for k in range(0, len(keys), 500):
            for row in (await s.execute(select(CatalogCache).where(CatalogCache.key.in_(list(keys)[k:k + 500])))).scalars():
                meta = row.payload_json.get("v") or {}
                x = out[keys[row.key]]
                if not x.title and meta.get("title"):
                    x.artist, x.title, x.duration_s = meta.get("artist") or "", meta["title"], meta.get("duration_s")
    return out


async def last_heard(user_id: int, ids: list[str], now: datetime) -> dict[str, datetime]:
    """When the user last heard each song (a play to its end, or past `HEARD_PLAY_S`), in any context; the
    scrobbles of Navidrome are `finish` events too."""
    heard = or_(PlayEvent.kind == "finish", and_(PlayEvent.kind == "skip", PlayEvent.position_s >= HEARD_PLAY_S))
    out: dict[str, datetime] = {}
    async with session_scope() as s:
        for col, wanted in ((PlayEvent.nd_song_id, [i for i in ids if not is_ref(i)]),
                            (PlayEvent.ref, [i for i in ids if is_ref(i)])):
            for k in range(0, len(wanted), 500):
                rows = await s.execute(select(col, func.max(PlayEvent.ts)).where(
                    PlayEvent.user_id == user_id, col.in_(wanted[k:k + 500]), PlayEvent.ts <= now, heard,
                    PlayEvent.target_type == "track").group_by(col))
                for key, ts in rows:
                    out[key] = max(ts, out[key]) if key in out else ts
    return out


async def hidden_in(user_id: int, context: str) -> set[str]:
    async with session_scope() as s:
        return set((await s.execute(select(SmartHidden.fingerprint).where(
            SmartHidden.user_id == user_id, SmartHidden.context == context))).scalars())


async def offered_in(user_id: int, context: str, now: datetime) -> set[str]:
    async with session_scope() as s:
        return {x for x in (await s.execute(select(SmartItem.fingerprint).join(
            SmartSession, SmartSession.id == SmartItem.session_id).where(
            SmartItem.user_id == user_id, SmartSession.context == context,
            SmartItem.handed_at > now - timedelta(days=OFFERED_DAYS)))).scalars() if x}


# --- candidates -----------------------------------------------------------------------------------------------------


def generators(p, seeds: list[TasteTrack]) -> dict[str, build.Gen]:
    """The sources of the pool: the radios and similar tracks of the playlist's songs, the Deezer radio of its two
    main artists, and the user's own forgotten ♥ (the sound term then keeps those that fit)."""
    all_gens = build.generators(p, seeds)
    out = {k: all_gens[k] for k in ("ytm_radio", "lastfm_similar", "yandex_similar", "stash")}
    names: dict[str, int] = {}
    for t in seeds:
        names[t.artist] = names.get(t.artist, 0) + 1
    top = [a for a, _ in sorted(names.items(), key=lambda x: -x[1]) if a][:2]

    async def radio() -> list[CatalogTrack]:
        if svc.deezer is None:
            raise build.Off("Deezer")
        refs = [r for r in [await build.artist_by_name(a) for a in top] if r]
        lists = await asyncio.gather(*(wave.artist_radio(r) for r in refs))
        return build._interleave(list(lists))

    if top:
        out["deezer_radio"] = radio
    return out


def _seed_tracks(rt: SmartRuntime, ids: list[str], infos: dict[str, OwnInfo]) -> list[TasteTrack]:
    out = []
    for i in ids:
        x = infos[i]
        if x.artist and x.title:
            out.append(wave.taste_of(x.track or CatalogTrack(
                ref=x.ref or f"nd:{x.nd_song_id}", source="dz" if (x.ref or "").startswith("dz:") else "nd",
                id=(x.ref or f"nd:{x.nd_song_id}").split(":", 1)[1], title=x.title, artist=x.artist,
                artists=[x.artist], duration_s=x.duration_s)))
    return out


# --- scoring and picking --------------------------------------------------------------------------------------------


async def _sound(rt: SmartRuntime) -> None:
    """`psim` of the pool and of what is handed out: the closeness by sound to the center of the context's songs.
    Previews of the best candidates without a vector are queued for the embedder (they count next time)."""
    st = rt.state
    if not embedder.configured() or not rt.center:
        return
    cands = [*st.out.values(), *st.pool.values()]
    now = asyncio.get_event_loop().time()
    keys = [c.key for c in cands if c.key not in rt.psim and rt.asked.get(f"p:{c.key}", -1e9) < now - wave.ASK_AGAIN_S]
    if not keys:
        return
    try:
        async with embedder.EmbedderClient() as e:
            sim = await e.similarity(keys, rt.center)
            rt.psim.update(sim.scores)
            for k in keys:
                rt.asked[f"p:{k}"] = now
            by_key = {c.key: c for c in cands}
            todo = sorted((by_key[k] for k in sim.missing if k in by_key and by_key[k].track.source == "dz"
                           and f"e:{k}" not in rt.asked), key=lambda c: -c.score)[:wave.EMBED_NEW]
            urls = await asyncio.gather(*(build._preview(c.track) for c in todo))
            items = [{"key": c.key, "url": u} for c, u in zip(todo, urls, strict=True) if u]
            for c in todo:
                rt.asked[f"e:{c.key}"] = now
            if items:
                await e.embed(items)
    except embedder.EmbedderDown as e:
        log.info("smart: no sound term: %s", e)


def _neutral(rt: SmartRuntime) -> float:
    known = sorted(rt.psim.values())
    return known[len(known) // 2] if known else 0.0


def rec_value(rt: SmartRuntime, c: build.Cand, vectors: dict[str, list[float]], pos, neg, neutral: float) -> float:
    """The wave's value (the mix's score, the session's early skips, closeness to what it liked) plus the
    closeness to the context by sound (a track without a vector counts as the middle one), minus the penalty of a
    track recent runs offered."""
    v = wave.value(c, rt.state, vectors, pos, neg)
    v += PLAYLIST_SOUND * rt.psim.get(c.key, neutral)
    if wave.fp_of(c) in rt.offered:
        v -= OFFERED_PENALTY
    return v


async def _fill(rt: SmartRuntime, slots: list[int]) -> list[tuple[int, build.Cand, float]]:
    """Picks a recommendation for each free slot (the caller holds the lock); they become `out` of the session."""
    st = rt.state
    if not slots or rt.profile is None:
        return []
    p = rt.profile
    from app.recommendation_policy import load_policy
    from app.recommendations import adapted_weights, explain_candidate

    policy = await load_policy(rt.user_id)
    weights = await adapted_weights(rt.user_id, wave.weights())
    if isinstance(p, V2Profile):
        cfg = get_settings().cfg.mixes
        for pool in (st.pool, st.out):
            for ref, c in list(pool.items()):
                if not build.keep(c, p, cfg):
                    del pool[ref]
        wave._session_weights(st, p)
        for key in rt.accepted:
            st.positive_weights.setdefault(key, 1.0)
    for key in rt.accepted:
        if key not in st.pos:
            wave._signal(st.pos, key)
    vectors = await wave._vectors(rt)
    await _sound(rt)
    pos = wave._weighted_center(st.positive_weights, vectors) if st.taste_model == "v2" else wave._center(st.pos, vectors)
    neg = None if st.taste_model == "v2" else wave._center(st.neg, vectors)
    neutral = _neutral(rt)
    pool = [c for c in st.pool.values() if not wave._disliked(c, p) and not _blocked(rt)(c)
            and not policy.blocked(c)]
    vals = {}
    for c in pool:
        build.apply_score(c, weights, p)
        c.known = build.known_of(c, p)
        raw = rec_value(rt, c, vectors, pos, neg, neutral)
        multiplier = policy.multiplier(c)
        value = raw * multiplier if raw >= 0 else raw / multiplier
        vals[c.track.ref] = value
        c.parts.update(temporary_multiplier=multiplier, selection_value=value,
                       playlist_similarity=rt.psim.get(c.key),
                       playlist_sound=PLAYLIST_SOUND * rt.psim.get(c.key, neutral))
        c.explanation = explain_candidate(c)
    ranked = sorted(pool, key=lambda c: -vals[c.track.ref])
    n = len(rt.own)
    out: list[tuple[int, build.Cand, float]] = []
    for k in slots:
        after = smart_order.slot_after(n, k)
        near = [rt.own_artist.get(rt.own[i], "") for i in (after, after + 1) if 0 <= i < n]
        i = smart_order.pick_rec([(c.track.ref, c.artist) for c in ranked], rt.rec_artists, near)
        if i is None:
            break
        c = ranked.pop(i)
        st.pool.pop(c.track.ref, None)
        st.out[c.track.ref] = c
        rt.filled[str(k)] = c.track.ref
        rt.rec_artists.append(c.artist)
        out.append((k, c, vals[c.track.ref]))
    return out


def _refill(rt: SmartRuntime) -> None:
    """The pool runs low: radios of the next songs of the playlist, in the background."""
    if rt.refilling or rt.profile is None or not rt.seed_pool:
        return
    pool = rt.seed_pool
    chosen = [CatalogTrack.model_validate(pool[(rt.seed_next + i) % len(pool)]) for i in range(min(SEED_REFILL, len(pool)))]
    rt.seed_next = (rt.seed_next + len(chosen)) % len(pool)
    seeds = [wave.taste_of(t) for t in chosen]
    profile = rt.profile
    rt.refilling = True

    async def run() -> None:
        try:
            gens = build.generators(profile, seeds)
            results = await build.run_generators({k: gens[k] for k in ("ytm_radio", "lastfm_similar")})
            async with rt.lock:
                await wave.absorb(rt, results)
                await _save(rt.sid, rt)
        except Exception:
            log.exception("smart %s: refill failed", rt.sid)
        finally:
            rt.refilling = False

    t = asyncio.create_task(run())
    rt.tasks.add(t)
    t.add_done_callback(rt.tasks.discard)


async def _save_items(rt: SmartRuntime, picked: list[tuple[int, build.Cand, float]]) -> None:
    if not picked:
        return
    try:
        async with session_scope() as s:
            nxt = (await s.execute(select(func.coalesce(func.max(SmartItem.position) + 1, 0)).where(
                SmartItem.session_id == rt.sid))).scalar_one()
            rows = [dict(session_id=rt.sid, user_id=rt.user_id, handed_at=utcnow(), position=nxt + i, slot=k,
                         ref=c.track.ref, nd_song_id=c.nd_song_id, fingerprint=wave.fp_of(c)[:1024],
                         artist=(c.track.artist or "")[:256], title=(c.track.title or "")[:256],
                         generator=c.generator[:32], gens=dict(c.gens), familiar=c.familiar, score=c.score,
                         sim=rt.psim.get(c.key), value=v, state="handed", decision_json=c.features())
                    for i, (k, c, v) in enumerate(picked)]
            await s.execute(sqlite_insert(SmartItem).values(rows).on_conflict_do_nothing(
                index_elements=["session_id", "ref"]))
    except Exception:
        log.exception("smart %s: handed out tracks not recorded", rt.sid)


async def _hand_out(rt: SmartRuntime, slots: list[int]) -> list[tuple[int, build.Cand]]:
    from app import listen

    picked = await _fill(rt, slots)
    await _save(rt.sid, rt)
    await _save_items(rt, picked)
    stream = [c.track.ref for _, c, _ in picked if not c.nd_song_id and c.track.source != "nd"]
    if stream:
        listen.prefetch(rt.user_id, stream)
    listen.warm_light([f"nd:{c.nd_song_id}" if c.nd_song_id else c.track.ref for _, c, _ in picked
                       if c.nd_song_id or c.track.source != "nd"])
    return [(k, c) for k, c, _ in picked]


# --- the API's calls ------------------------------------------------------------------------------------------------


@dataclass
class Plan:
    session_id: str
    context: str
    title: str | None
    own: list[str]
    recs: list[tuple[int, str, build.Cand]]  # slot, the own song it goes after, the track
    pending: bool
    per_rec: int
    recorded: Any = None


def _after(rt: SmartRuntime, slot: int) -> str:
    return rt.own[smart_order.slot_after(len(rt.own), slot)]


async def clear_old(now: datetime | None = None) -> int:
    """Sessions not asked for `KEEP`: their state becomes `{"cleared": true}`, the rows and the items stay."""
    now = now or utcnow()
    async with session_scope() as s:
        rows = list((await s.execute(select(SmartSession).where(SmartSession.updated_at < now - KEEP))).scalars())
        n = 0
        for row in rows:
            if (row.state_json or {}).get("cleared"):
                continue
            row.state_json = {"cleared": True}
            n += 1
            if (rt := _rt.pop(row.id, None)) is not None:
                for t in rt.tasks:
                    t.cancel()
    return n


async def start(user: User, context: str, songs: list[str], current: str | None, *, title: str | None = None,
                origin: str | None = None, client: str | None = None, rng: random.Random | None = None,
                now: datetime | None = None) -> Plan:
    if not valid_context(context):
        raise SmartError(422, "context: liked или playlist:<id>")
    ids = list(dict.fromkeys(songs))
    if not ids:
        raise SmartError(422, "В списке нет песен")
    rng = rng or random.Random()
    now = now or utcnow()
    cfg = get_settings().cfg.mixes
    await clear_old(now)
    infos, heard, hidden, offered = await asyncio.gather(
        own_infos(ids), last_heard(user.id, ids, now), hidden_in(user.id, context), offered_in(user.id, context, now))
    songs_o = [smart_order.OwnSong(i, artist_key(infos[i].artist) if infos[i].artist else "") for i in ids]
    order = smart_order.fewer_repeats(songs_o, heard, now, rng, first=current)
    own = [x.id for x in order]
    seed_ids = smart_order.choose_seeds(songs_o, rng, newest_last=context != "liked")
    async with session_scope() as s:
        profile = await build_profile(s, user, model=cfg.taste_model)
    if isinstance(profile, V2Profile):
        from app.mixes.taste_select import prepare
        await prepare(profile)
    sid = uuid.uuid4().hex[:16]
    state = wave.State(taste_model=cfg.taste_model)
    rt = _wire(SmartRuntime(user_id=user.id, mode="normal", state=state, profile=profile, sid=sid, context=context,
                            own=own, own_artist={x.id: x.artist for x in songs_o}, hidden=hidden, offered=offered))
    rt.own_refs = {x.ref for x in infos.values() if x.ref}
    rt.own_nd = {x.nd_song_id for x in infos.values() if x.nd_song_id}
    state.heard = {x.fp for x in infos.values() if x.fp} | hidden
    keys = [embedder.song_key(infos[i].nd_song_id) for i in ids if infos[i].nd_song_id][:CENTER_MAX]
    rt.center = dict.fromkeys(keys, 1.0)
    seeds = _seed_tracks(rt, seed_ids, infos)
    rt.seed_pool = [t.track.model_dump(mode="json") for t in seeds if t.track is not None]
    rt.seed_next = 0
    async with session_scope() as s:
        s.add(SmartSession(id=sid, user_id=user.id, context=context, title=(title or "")[:256] or None,
                           own_count=len(own), per_rec=smart_order.own_per_rec(len(own)), first_id=current,
                           taste_model=cfg.taste_model, origin=origin, client=client, state_json={}))
    _rt[sid] = rt
    async with rt.lock:
        await wave._gather(sid, rt, list(generators(profile, seeds).items()),
                           lambda: len(rt.state.pool) >= 4 * wave.BATCH)
        slots = smart_order.slots_ahead(len(own), 0, AHEAD)
        handed = await _hand_out(rt, slots)
        pending = bool(rt.tasks)
    return Plan(sid, context, title, own, [(k, _after(rt, k), c) for k, c in handed], pending, smart_order.own_per_rec(len(own)))


async def record(rt: SmartRuntime, raw: list[Any]):
    """The events into the history; one about a recommendation of this session gets the context `smart:<id>` (also
    when the client stored it already under another). Returns what `record_events` says and the stored rows."""
    from app.plays import record_events

    st = rt.state
    refs = {c.track.ref for c in st.out.values()} | {x.ref for x in st.played}
    nds = {c.nd_song_id for c in st.out.values() if c.nd_song_id} | {x.nd_song_id for x in st.played if x.nd_song_id}
    context = f"smart:{rt.sid}"
    events = []
    for x in raw:
        if isinstance(x, dict) and ((x.get("ref") in refs) or (x.get("nd_song_id") in nds)):
            x = {**x, "context": context}
        events.append(x)
    async with session_scope() as s:
        out = await record_events(s, rt.user_id, events)
        ids = [x["event_id"] for x in events if isinstance(x, dict) and x.get("context") == context
               and isinstance(x.get("event_id"), str)]
        rows: list[PlayEvent] = []
        if ids:
            await s.execute(update(PlayEvent).where(PlayEvent.user_id == rt.user_id, PlayEvent.event_id.in_(ids))
                            .values(context=context))
            rows = list((await s.execute(select(PlayEvent).where(
                PlayEvent.user_id == rt.user_id, PlayEvent.event_id.in_(ids), PlayEvent.context == context)
                .order_by(PlayEvent.ts, PlayEvent.id))).scalars())
        if rt.state.taste_model == "v2":
            rows = list((await s.execute(select(PlayEvent).where(
                PlayEvent.user_id == rt.user_id, PlayEvent.context == context, PlayEvent.ts <= utcnow())
                .order_by(PlayEvent.ts, PlayEvent.id))).scalars())
        row = await s.get(SmartSession, rt.sid)
        if row is not None:
            row.next_calls = (row.next_calls or 0) + 1
    return out, rows


async def next_batch(user: User, sid: str, played: list[Any], at: str | None) -> Plan:
    rt = await _runtime(sid, user)
    if not rt.state.pool and rt.tasks:  # nothing to pick from yet: the sources still answer (outside the lock)
        await asyncio.wait(set(rt.tasks), timeout=get_settings().wave_wait_s)
    async with rt.lock:
        recorded, events = await record(rt, played)
        await wave._profile(rt, user)  # after the events were stored: it sees them
        await wave.learn(sid, rt, events)
        if len(rt.state.pool) < wave.POOL_LOW:
            _refill(rt)
        index = rt.own.index(at) if at in rt.own else 0
        slots = [k for k in smart_order.slots_ahead(len(rt.own), index, AHEAD) if str(k) not in rt.filled]
        handed = await _hand_out(rt, slots)
        pending = bool(rt.tasks) or len(handed) < len(slots)
        async with session_scope() as s:
            title = (await s.get(SmartSession, sid)).title  # type: ignore[union-attr]
    return Plan(sid, rt.context, title, [], [(k, _after(rt, k), c) for k, c in handed], pending,
                smart_order.own_per_rec(len(rt.own)), recorded)


async def _item(sid: str, ref: str | None, nd_song_id: str | None) -> SmartItem:
    async with session_scope() as s:
        q = select(SmartItem).where(SmartItem.session_id == sid)
        q = q.where(or_(*([SmartItem.ref == ref] if ref else []), *([SmartItem.nd_song_id == nd_song_id]
                                                                    if nd_song_id else [])))
        item = (await s.execute(q.limit(1))).scalar_one_or_none()
        if item is None:
            raise SmartError(404, "Такой рекомендации в этом перемешивании не было")
        s.expunge(item)
        return item


async def _mark(sid: str, item_id: int, state: str) -> None:
    async with session_scope() as s:
        await s.execute(update(SmartItem).where(SmartItem.id == item_id).values(state=state))


async def hide(user: User, sid: str, ref: str | None, nd_song_id: str | None) -> None:
    """«−»: the track is not offered in this context any more (and the session's skips count it as an early
    skip, so what is like it goes down); it stays everywhere else, unlike «Не нравится»."""
    rt = await _runtime(sid, user)
    item = await _item(sid, ref, nd_song_id)
    async with rt.lock:
        async with session_scope() as s:
            await s.execute(sqlite_insert(SmartHidden).values(
                user_id=user.id, context=rt.context, fingerprint=item.fingerprint, ref=item.ref,
                nd_song_id=item.nd_song_id, artist=item.artist, title=item.title, created_at=utcnow())
                .on_conflict_do_nothing(index_elements=["user_id", "context", "fingerprint"]))
        await _mark(sid, item.id, "hidden")
        rt.hidden.add(item.fingerprint)
        st = rt.state
        akey = artist_key(item.artist)
        st.skips[akey] = st.skips.get(akey, 0) + 1
        for pool in (st.pool, st.out):
            for r, c in list(pool.items()):
                if wave.fp_of(c) == item.fingerprint:
                    wave._signal(st.neg, c.key)
                    del pool[r]
        await _save(sid, rt)


async def accept(user: User, sid: str, ref: str | None, nd_song_id: str | None) -> tuple[SmartItem, str | None]:
    """«+»: marks the recommendation as taken (what it raises in the next picks). Returns the item and the
    Navidrome playlist the track has to be put in (`None` in «Понравившиеся»: the client likes it itself)."""
    rt = await _runtime(sid, user)
    item = await _item(sid, ref, nd_song_id)
    async with rt.lock:
        await _mark(sid, item.id, "added")
        key = embedder.song_key(item.nd_song_id) if item.nd_song_id else item.ref
        if key not in rt.accepted:
            rt.accepted.append(key)
        await _save(sid, rt)
    return item, rt.context[9:] if rt.context.startswith("playlist:") else None


async def settle(sid: str) -> None:
    """Waits for the session's background sources (tests)."""
    rt = _rt.get(sid)
    while rt is not None and rt.tasks:
        tasks = list(rt.tasks)
        await asyncio.gather(*tasks, return_exceptions=True)
        rt.tasks.difference_update(tasks)


async def reset() -> None:
    for rt in _rt.values():
        for t in rt.tasks:
            t.cancel()
    _rt.clear()


__all__ = ["Plan", "SmartError", "accept", "hide", "next_batch", "reset", "settle", "start", "valid_context"]

