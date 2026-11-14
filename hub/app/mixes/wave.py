"""«Моя волна» (MIXES.md 7): an endless stream for the user's taste, handed out 5 tracks at a time and picked again
after every action.

- A session (`WaveSession`) keeps a pool of candidates made by the generators of «Микс дня» (`app.mixes.build`:
  the same sources, filters and score), the tracks handed out and not started yet, the last 50 played, and the
  session's own skips and likes. Started from a track or an artist («Волна по треку») the pool comes from that
  seed's radios and the seed plays first; without one — from the taste profile, as a mix. A wave by a Navidrome
  artist (the app's «Радиостанции») adds the artist's library songs to its Deezer radios; its artist plays once in 3.
- Every `next` brings the events since the previous one and picks again: an early skip (< 30 s) or «Не нравится»
  lowers the artist and what sounds like the track (the embedder's vectors); a ♥ or a track played to its end adds
  its YouTube Music radio to the pool (`session_radio`) and raises what sounds like it. An artist plays at most once
  in 8 tracks; the mode keeps the familiar share of a mix.
- The tracks the client still has ahead (`queued`) are picked again with the rest: the answer replaces them.
- Sources answer at their own pace: a request waits for them `wave_wait_s`, what comes later joins the pool in the
  background (one lock per session). A pool running low is refilled from the radios of the next seeds.
- Tracks play as a stream from the Hub (their files are prefetched); one played to its end is downloaded quietly,
  as an economy import downloads a track once listened to (a ♥ downloads it anyway: the clients' ♥).
- For the admin's analytics (INSIGHTS.md 6.2–6.4) a session keeps where and by what client it was started, and every
  track handed out goes to `wave_items`; a session not asked for `KEEP` has its heavy state cleared to a summary,
  the row stays. The wave itself never reads any of it.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog.models import CatalogTrack, split_ref
from app.api.schemas import WavePreferences
from app.config import get_settings
from app.db import session_scope
from app.jobs.queue import enqueue, notify
from app.library.owned import ACTIVE_STATUSES
from app.matching.normalize import fingerprint
from app.mixes import build, embedder, sources
from app.mixes.resolve import catalog_track, library_track
from app.mixes.taste import EARLY_SKIP_S, Profile, TasteTrack, artist_key, build_profile, skip_weight
from app.mixes.taste_math import V2Profile, saturation
from app.mixes.taste_snapshot import TasteEvent
from app.models import DownloadBatch, DownloadItem, PlayEvent, Track, User, WaveItem, WaveSession, utcnow
from app.services import svc

log = logging.getLogger(__name__)

PLAY_KINDS = ("start", "skip", "finish")

BATCH = 5
HISTORY_MAX = 50
ARTIST_GAP = 8  # an artist at most once in this many tracks
SEED_ARTIST_GAP = 3  # ... the artist a wave started from: at most once in this many
POOL_LOW = 25  # fewer candidates left: the pool is refilled in the background
SIGNALS_MAX = 10  # the session's latest likes / skips its sound centers are made of
SKIP_ARTIST_PENALTY = 0.6  # per early skip of the artist in this session
SOUND_WEIGHT = 1.0  # × (closeness to the session's likes − to its skips)
QUEUED_BONUS = 0.3  # a track the client has queued already is not swapped for a hair better one
SESSION_RADIO = "session_radio"
ARTIST_SONGS = "artist_songs"
ARTIST_SONGS_MAX = 60  # the seed artist's library songs put in the pool
ARTIST_ALBUMS_MAX = 30  # ... read from this many of their albums
SESSION_WEIGHTS = {"seed": 10.0, SESSION_RADIO: 1.2, ARTIST_SONGS: 1.2}
TITLES = {"seed": "С чего началась волна", SESSION_RADIO: "Похоже на понравившееся",
          ARTIST_SONGS: "Песни исполнителя"}
IDLE = timedelta(hours=24)  # a session not asked for this long is over
KEEP = timedelta(days=7)  # ... and this long: its state is cleared to a summary (`CLEARED_KEYS`), the row stays
CLEARED_KEYS = ("version", "taste_model", "gens", "started", "started_familiar", "skips", "seeds_used", "downloads")
EMBED_NEW = 60  # new candidates without a vector whose previews go to the embedder per refill
VECTORS_TOP = 80  # best candidates whose vectors are asked for the session's sound centers
ASK_AGAIN_S = 60  # a vector the embedder did not have is asked again after this
REFILL_SEEDS = 3
PREPARE_POOL = 10  # bounded reserve: failed audio never turns into a taste signal
PREFERENCE_WEIGHT = 2.0
PREFERENCE_PROMPTS = {
    "energy": ("calm, gentle, relaxing music with low energy", "energetic, intense, driving music"),
    "vocals": ("instrumental music without singing or vocals", "music with a singer and prominent vocals"),
}


class WaveError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# --- state --------------------------------------------------------------------------------------------------------


def cand_json(c: build.Cand) -> dict[str, Any]:
    return {"track": c.track.model_dump(mode="json"), "gens": c.gens, "familiar": c.familiar,
            "nd_song_id": c.nd_song_id, "library_track_id": c.library_track_id, "sim": c.sim,
            "score": c.score, "parts": c.parts, "known": c.known, "explanation": c.explanation}


def cand_of(d: dict[str, Any]) -> build.Cand:
    return build.Cand(track=CatalogTrack.model_validate(d["track"]), gens=dict(d.get("gens") or {}),
                      familiar=bool(d.get("familiar")), nd_song_id=d.get("nd_song_id"),
                      library_track_id=d.get("library_track_id"), sim=d.get("sim"), score=float(d.get("score") or 0),
                      parts=d.get("parts"), known=d.get("known"), explanation=list(d.get("explanation") or []))


def fp_of(c: build.Cand) -> str:
    return fingerprint(c.track.artist, c.track.title)


@dataclass
class Played:
    """A track of the session that started playing (the latest `HISTORY_MAX`)."""

    ref: str
    nd_song_id: str | None
    key: str  # the vector key
    artist: str  # artist key
    familiar: bool
    kind: str = "start"  # start/skip/finish (how it ended), like on top
    position_s: float | None = None
    track: dict[str, Any] | None = None  # the catalog track (the radio of a liked one, the download)


@dataclass
class State:
    version: int = 2
    taste_model: str = "v1"  # Old JSON without a version always stays v1, regardless of current config.
    seen: set[str] = field(default_factory=set)
    signals: dict[str, dict[str, Any]] = field(default_factory=dict)
    positive_weights: dict[str, float] = field(default_factory=dict)
    space: dict[str, Any] | None = None
    pool: dict[str, build.Cand] = field(default_factory=dict)  # by ref: not handed out
    out: dict[str, build.Cand] = field(default_factory=dict)  # handed out, not started (the client's queue)
    played: list[Played] = field(default_factory=list)
    heard: set[str] = field(default_factory=set)  # fingerprints of every track started: never again
    skips: dict[str, int] = field(default_factory=dict)  # artist key -> early skips in the session
    pos: list[str] = field(default_factory=list)  # vector keys of the session's likes and finished tracks
    neg: list[str] = field(default_factory=list)  # ... of its early skips and dislikes
    radios: list[str] = field(default_factory=list)  # refs whose session radio was asked
    seeds_used: list[str] = field(default_factory=list)  # taste keys the refill took radios of
    started: int = 0
    started_familiar: int = 0
    downloads: list[str] = field(default_factory=list)  # refs downloaded for the session
    gens: dict[str, Any] = field(default_factory=dict)  # generator -> {tracks, seconds, error?, off?}
    seed_artist: str | None = None  # artist key of a wave by an artist: it plays more often (`SEED_ARTIST_GAP`)
    preferences: dict[str, str] = field(default_factory=lambda: WavePreferences().model_dump())
    preferences_status: str | None = None
    preparation: dict[str, int] = field(default_factory=lambda: {"ready": 0, "pending": 0, "unavailable": 0})
    awaiting_seed: str | None = None
    unavailable: set[str] = field(default_factory=set)
    refills_since_play: int = 0
    more_to_prepare: bool = False

    def dump(self) -> dict[str, Any]:
        return {"version": self.version, "taste_model": self.taste_model, "seen": sorted(self.seen),
                "signals": self.signals, "positive_weights": self.positive_weights, "space": self.space,
                "pool": [cand_json(c) for c in self.pool.values()], "out": [cand_json(c) for c in self.out.values()],
                "played": [p.__dict__ for p in self.played], "heard": sorted(self.heard), "skips": self.skips,
                "pos": self.pos, "neg": self.neg, "radios": self.radios, "seeds_used": self.seeds_used,
                "started": self.started, "started_familiar": self.started_familiar, "downloads": self.downloads,
                "gens": self.gens, "seed_artist": self.seed_artist, "preferences": self.preferences,
                "preferences_status": self.preferences_status, "preparation": self.preparation,
                "awaiting_seed": self.awaiting_seed, "unavailable": sorted(self.unavailable),
                "refills_since_play": self.refills_since_play, "more_to_prepare": self.more_to_prepare}

    @classmethod
    def load(cls, d: dict[str, Any]) -> State:
        st = cls(version=int(d.get("version") or 1), taste_model="v2" if d.get("version") and
                 d.get("taste_model") == "v2" else "v1", seen=set(d.get("seen") or []),
                 signals=dict(d.get("signals") or {}), positive_weights=dict(d.get("positive_weights") or {}),
                 space=d.get("space"), skips=dict(d.get("skips") or {}),
                 pos=list(d.get("pos") or []), neg=list(d.get("neg") or []),
                 radios=list(d.get("radios") or []), seeds_used=list(d.get("seeds_used") or []),
                 started=int(d.get("started") or 0), started_familiar=int(d.get("started_familiar") or 0),
                 downloads=list(d.get("downloads") or []), gens=dict(d.get("gens") or {}),
                 heard=set(d.get("heard") or []), seed_artist=d.get("seed_artist"),
                 preferences=WavePreferences.model_validate(d.get("preferences") or {}).model_dump(),
                 preferences_status=d.get("preferences_status"),
                 preparation=d.get("preparation") or {"ready": 0, "pending": 0, "unavailable": 0},
                 awaiting_seed=d.get("awaiting_seed"), unavailable=set(d.get("unavailable") or []),
                 refills_since_play=int(d.get("refills_since_play") or 0),
                 more_to_prepare=bool(d.get("more_to_prepare")))
        st.pool = {c.track.ref: c for c in map(cand_of, d.get("pool") or [])}
        st.out = {c.track.ref: c for c in map(cand_of, d.get("out") or [])}
        st.played = [Played(**p) for p in d.get("played") or []]
        return st

    def known_fps(self) -> set[str]:
        return self.heard | {fp_of(c) for c in [*self.pool.values(), *self.out.values()]}


@dataclass
class Runtime:
    """What a session keeps in memory besides its row: the profile, the vectors asked, the lock, its tasks."""

    user_id: int
    mode: str
    state: State
    profile: Profile | None = None
    vectors: dict[str, list[float]] = field(default_factory=dict)
    asked: dict[str, float] = field(default_factory=dict)  # key -> when it was asked (monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: set[asyncio.Task] = field(default_factory=set)
    refilling: bool = False
    # a session of another kind (the smart shuffle, app.mixes.smart) keeps its state in its own row, and a track
    # played to its end is not downloaded for it
    save: Callable[[str, Runtime], Awaitable[None]] | None = None
    download_finished: bool = True
    blocked: Callable[[build.Cand], bool] | None = None  # candidates the session never takes (its own songs...)


_rt: dict[str, Runtime] = {}


async def _save(sid: str, rt: Runtime) -> None:
    async with session_scope() as s:
        row = await s.get(WaveSession, sid)
        if row is not None:
            row.state_json, row.updated_at = rt.state.dump(), utcnow()


async def _runtime(sid: str, user: User) -> Runtime:
    rt = _rt.get(sid)
    if rt is None:
        async with session_scope() as s:
            row = await s.get(WaveSession, sid)
            if row is None or row.user_id != user.id or row.updated_at < utcnow() - IDLE:
                raise WaveError(404, "Волна закончилась: запустите новую")
            rt = Runtime(user_id=user.id, mode=row.mode, state=State.load(row.state_json or {}))
            if "seen" not in (row.state_json or {}):
                # Legacy state has no event ledger. Preserve its already-saved learning
                # rather than applying the old journal again when an offline client retries.
                rt.state.seen.update((await s.execute(select(PlayEvent.event_id).where(
                    PlayEvent.user_id == user.id, PlayEvent.context == f"wave:{sid}",
                    PlayEvent.received_at <= row.updated_at))).scalars())
        rt = _rt.setdefault(sid, rt)
    elif rt.user_id != user.id:
        raise WaveError(404, "Волна закончилась: запустите новую")
    return rt


async def _profile(rt: Runtime, user: User) -> Profile:
    async with session_scope() as s:
        rt.profile = await build_profile(s, user, model=rt.state.taste_model)
    if isinstance(rt.profile, V2Profile):
        from app.mixes.taste_select import prepare
        await prepare(rt.profile)
    return rt.profile


# --- generators ---------------------------------------------------------------------------------------------------


def taste_of(t: CatalogTrack) -> TasteTrack:
    return TasteTrack(key=t.ref, ref=t.ref, artist=t.artist, title=t.title, duration_s=t.duration_s, track=t)


async def artist_radio(artist_ref: str) -> list[CatalogTrack]:
    if svc.deezer is None:
        raise build.Off("Deezer")
    return await svc.deezer.get_artist_radio(split_ref(artist_ref)[1])


async def radio_of(t: CatalogTrack) -> list[CatalogTrack]:
    """YouTube Music's radio of a track the user liked or played to its end in this session."""
    return await build._by_seeds([taste_of(t)], lambda x: sources.ytm_radio(x.artist, x.title), "ytm")


def generators(p: Profile, seed: CatalogTrack | None, seed_artist: dict[str, Any] | None,
               seeds: list[TasteTrack]) -> dict[str, build.Gen]:
    """The sources of a new session's pool: the seed's radios, or the mix's generators for the profile."""
    if seed is not None:
        gens = build.generators(p, [taste_of(seed)])
        out = {k: gens[k] for k in ("ytm_radio", "lastfm_similar", "yandex_similar")}
        if seed.artist_ref and seed.artist_ref.startswith("dz:"):
            out["deezer_radio"] = lambda: artist_radio(seed.artist_ref or "")
        return out
    if seed_artist is not None:
        ref = seed_artist.get("ref")
        songs: list[CatalogTrack] = list(seed_artist.get("songs") or [])

        async def top() -> list[CatalogTrack]:
            if svc.deezer is None:
                raise build.Off("Deezer")
            return await svc.deezer.get_artist_top(split_ref(ref)[1], limit=10)

        async def own() -> list[CatalogTrack]:
            return random.sample(songs, len(songs))

        async def radios() -> list[CatalogTrack]:
            tracks = (await top())[:2] if ref else random.sample(songs, min(2, len(songs)))
            return await build._by_seeds([taste_of(t) for t in tracks],
                                         lambda x: sources.ytm_radio(x.artist, x.title), "ytm")

        out = {"deezer_radio": lambda: artist_radio(ref), "favorites": top} if ref else {}
        if songs:
            out[ARTIST_SONGS] = own
        return {**out, "ytm_radio": radios}
    return build.generators(p, seeds)


def refill_generators(p: Profile, st: State) -> dict[str, build.Gen]:
    """More of the pool: the radios of the next profile seeds the session has not used (round and round)."""
    cfg = get_settings().cfg.mixes
    pool = [t for t in p.seeds(cfg.seeds * 4) if t.key not in st.seeds_used]
    if not pool:
        st.seeds_used.clear()
        pool = p.seeds(cfg.seeds * 4)
    seeds = pool[:REFILL_SEEDS]
    st.seeds_used += [t.key for t in seeds]
    if not seeds:
        return {}
    gens = build.generators(p, seeds)
    return {k: gens[k] for k in ("ytm_radio", "lastfm_similar")}


# --- the pool -----------------------------------------------------------------------------------------------------


def weights() -> dict[str, float]:
    return {**get_settings().cfg.mixes.weights, **SESSION_WEIGHTS}


async def absorb(rt: Runtime, results: list[build.GenResult]) -> int:
    """New candidates into the pool: one per song (a song already there adds the generator), library songs marked,
    the mix's filters, its score with the closeness to the taste by sound. Returns how many were added."""
    p = rt.profile
    assert p is not None
    st = rt.state
    for r in results:
        g = st.gens.setdefault(r.name, {"tracks": 0, "seconds": 0.0})
        g["tracks"] += len(r.tracks)
        g["seconds"] = round(g["seconds"] + r.seconds, 2)
        if r.error:
            g["error"], g["off"] = r.error, r.off
    merged = build.merge(results)
    by_fp = {fp_of(c): c for c in st.pool.values()}
    known = st.known_fps()
    fresh: dict[str, build.Cand] = {}
    for c in merged.values():
        fp = fp_of(c)
        if (have := by_fp.get(fp)) is not None:
            for g, pos in c.gens.items():
                have.gens[g] = min(have.gens.get(g, pos), pos)
            have.score = build.score(have, weights(), p)
        elif fp not in known and c.track.ref not in st.unavailable:
            fresh[c.track.ref] = c
    if not fresh:
        return 0
    await build.link_library(fresh)
    cfg = get_settings().cfg.mixes
    kept = [c for c in fresh.values() if not (rt.blocked and rt.blocked(c)) and build.keep(c, p, cfg)]
    if not isinstance(p, V2Profile):
        await _sound(p, kept)
    for c in kept:
        c.score = build.score(c, weights(), p)
        st.pool[c.track.ref] = c
    return len(kept)


async def _sound(p: Profile, cands: list[build.Cand]) -> None:
    """`sim` of new candidates (the embedder); the previews of the best ones without a vector are embedded for the
    picks to come (nothing waits for them). The service down — no audio term."""
    if not cands or not embedder.configured():
        return
    try:
        async with embedder.EmbedderClient() as e:
            sim = await e.similarity([c.key for c in cands], p.vector_profile())
            for c in cands:
                c.sim = sim.scores.get(c.key)
            todo = sorted((c for c in cands if c.key in sim.missing and c.track.source == "dz"),
                          key=lambda c: -build.score(c, weights(), p))[:EMBED_NEW]
            urls = await asyncio.gather(*(build._preview(c.track) for c in todo))
            items = [{"key": c.key, "url": u} for c, u in zip(todo, urls, strict=True) if u]
            if items:
                await e.embed(items)
    except embedder.EmbedderDown as e:
        log.info("wave: embedder unavailable: %s", e)


async def _gather(sid: str, rt: Runtime, gens: list[tuple[str, build.Gen]], enough: Callable[[], bool]) -> None:
    """Runs the generators (a name may repeat: the session's radios); waits for them `wave_wait_s` (or until
    `enough`), absorbing each as it answers. Those still running then join the pool in the background. The caller
    holds the session's lock."""
    if not gens:
        return
    tasks = {asyncio.create_task(build.run_generators({n: f})) for n, f in gens}
    deadline = time.monotonic() + get_settings().wave_wait_s
    pending = tasks
    while pending:
        done, pending = await asyncio.wait(pending, timeout=max(0.0, deadline - time.monotonic()),
                                           return_when=asyncio.FIRST_COMPLETED)
        if not done:
            break
        await absorb(rt, [r for t in done for r in t.result()])
        if enough():
            break
    for t in pending:
        late = asyncio.create_task(_late(sid, rt, t))
        rt.tasks.add(late)
        late.add_done_callback(rt.tasks.discard)


async def _late(sid: str, rt: Runtime, task: asyncio.Task) -> None:
    try:
        results = await task
        async with rt.lock:
            added = await absorb(rt, results)
            await (rt.save or _save)(sid, rt)
        log.info("wave %s: %s joined later (+%s)", sid, [r.name for r in results], added)
    except asyncio.CancelledError:
        task.cancel()
        raise
    except Exception:
        log.exception("wave %s: a late source failed", sid)


def _refill(sid: str, rt: Runtime) -> None:
    """The pool runs low: the radios of the next seeds, in the background."""
    if rt.refilling or rt.profile is None:
        return
    v2 = isinstance(rt.profile, V2Profile)
    profile = rt.profile
    chosen = []
    if v2:
        pool = [t for t in profile.seeds(get_settings().cfg.mixes.seeds * 4) if t.key not in rt.state.seeds_used]
        if not pool:
            rt.state.seeds_used.clear()
            pool = profile.seeds(get_settings().cfg.mixes.seeds * 4)
        chosen = pool[:REFILL_SEEDS]
        rt.state.seeds_used.extend(t.key for t in chosen)
    gens = {} if v2 else refill_generators(rt.profile, rt.state)
    if not gens and not v2:
        return
    rt.refilling = True

    async def run() -> None:
        try:
            current = gens
            if v2:
                from app.mixes.taste_select import enrich_selected
                enriched = await enrich_selected(profile, chosen)
                all_gens = build.generators(profile, enriched) if enriched else {}
                current = {k: all_gens[k] for k in ("ytm_radio", "lastfm_similar") if k in all_gens}
            results = await build.run_generators(current)
            async with rt.lock:
                await absorb(rt, results)
                await (rt.save or _save)(sid, rt)
        except Exception:
            log.exception("wave %s: refill failed", sid)
        finally:
            rt.refilling = False

    t = asyncio.create_task(run())
    rt.tasks.add(t)
    t.add_done_callback(rt.tasks.discard)


# --- picking --------------------------------------------------------------------------------------------------------


def _center(keys: list[str], vectors: dict[str, list[float]]) -> list[float] | None:
    vs = [vectors[k] for k in keys if vectors.get(k)]
    if not vs:
        return None
    c = [sum(x) / len(vs) for x in zip(*vs, strict=False)]
    n = math.sqrt(sum(x * x for x in c)) or 1.0
    return [x / n for x in c]


def _weighted_center(weights: dict[str, float], vectors: dict[str, list[float]]) -> list[float] | None:
    rows = [(vectors[k], weight) for k, weight in weights.items() if k in vectors and weight > 0]
    if not rows:
        return None
    center = [sum(v[i] * weight for v, weight in rows) for i in range(len(rows[0][0]))]
    norm = math.sqrt(sum(x * x for x in center))
    return [x / norm for x in center] if norm > 1e-12 else None


def _session_weights(st: State, profile: V2Profile) -> None:
    from app.favorites import fold
    from app.mixes.taste_snapshot import Subject

    events, by_id, positives, negative, skips = [], {}, {}, [], {}
    for eid, data in sorted(st.signals.items(), key=lambda x: (x[1]["ts"], x[0])):
        values = {k: data[k] for k in TasteEvent.__dataclass_fields__ if k in data}
        values["ts"] = datetime.fromisoformat(values["ts"])
        values["received_at"] = datetime.fromisoformat(values["received_at"])
        e = TasteEvent(**values)
        t = profile.find(ref=e.ref, nd_song_id=e.nd_song_id)
        key = t.key if t else data["key"]
        events.append(e)
        by_id[eid] = key, data["artist"]
        if e.reverts_event_id:
            continue
        if e.kind == "finish":
            positives[key] = positives.get(key, 0) + 2
        elif e.kind == "skip" and e.position_s is not None:
            if e.position_s < EARLY_SKIP_S:
                negative.append(key)
                skips[data["artist"]] = skips.get(data["artist"], 0) + 1
            else:
                positives[key] = positives.get(key, 0) + skip_weight(e.position_s, e.duration_s or data.get("length"))
    subjects = [Subject(x.target_type, tuple(x.aliases), x.ref, x.nd_id, x.data or {})
                for x in profile.favorite_states.values()]
    for state in fold(events, subjects).values():
        if not state.liked:
            continue
        key, _ = by_id[state.event.event_id]
        current = profile.find(ref=state.ref, nd_song_id=state.nd_id)
        # A fresh global unlike removes only the session like, never finish/late-skip evidence.
        journalled = any(x.target_type == "track" and
                         ((state.ref and x.ref == state.ref) or (state.nd_id and x.nd_id == state.nd_id))
                         for x in profile.favorite_states.values())
        if not journalled or (current and current.liked):
            positives[key] = positives.get(key, 0) + 1
    explicit = {}
    for e in events:
        if e.kind in ("dislike", "undislike"):
            explicit[by_id[e.event_id][0]] = e
    for key, e in explicit.items():
        if e.kind == "dislike":
            negative.append(key)
            artist = by_id[e.event_id][1]
            skips[artist] = skips.get(artist, 0) + 1
    st.positive_weights = {key: saturation(value) for key, value in positives.items() if value > 0}
    st.pos = list(st.positive_weights)[-SIGNALS_MAX:]
    st.positive_weights = {key: st.positive_weights[key] for key in st.pos}
    st.neg = list(dict.fromkeys(reversed(negative)))[:SIGNALS_MAX]
    st.skips = skips


async def _learn_v2(sid: str, rt: Runtime, events: list[PlayEvent]) -> None:
    st, liked = rt.state, []
    assert isinstance(rt.profile, V2Profile)
    for e in events:
        if e.user_id != rt.user_id or not e.event_id or e.event_id in st.seen:
            continue
        # Future/delayed events remain unconsumed until their occurrence time arrives.
        if e.ts > utcnow() or (e.client or "").lower().endswith("-debug"):
            continue
        st.seen.add(e.event_id)
        if (e.target_type or "track") != "track":
            continue
        matched = _match(st, e.ref, e.nd_song_id)
        if matched is None:
            old = next((d for d in reversed(list(st.signals.values())) if
                        (e.ref and d.get("ref") == e.ref) or
                        (e.nd_song_id and d.get("nd_song_id") == e.nd_song_id)), None)
            if old is None:
                continue
            matched = "signal", Played(old["ref"], old.get("nd_song_id"), old["key"], old["artist"],
                                        bool(old.get("familiar")), track=old.get("track"))
        x = matched[1]
        if isinstance(x, build.Cand):
            x = _start(st, x) if e.kind in PLAY_KINDS else _as_played(x)
        if e.kind in ("skip", "finish"):
            x.kind, x.position_s = e.kind, e.position_s
        values = {k: getattr(e, k) for k in TasteEvent.__dataclass_fields__}
        values["ts"], values["received_at"] = e.ts.isoformat(), e.received_at.isoformat()
        st.signals[e.event_id] = {**values, "key": x.key, "artist": x.artist,
                                  "length": (x.track or {}).get("duration_s"), "track": x.track,
                                  "familiar": x.familiar}
        if e.kind in ("finish", "like") and not e.reverts_event_id:
            liked.append(x)
            if e.kind == "finish":
                await _download(sid, rt, x)
    _session_weights(st, rt.profile)
    radios = []
    for x in liked:
        if x.ref not in st.radios and x.track:
            st.radios.append(x.ref)
            track = CatalogTrack.model_validate(x.track)
            radios.append((SESSION_RADIO, lambda track=track: radio_of(track)))
    await _gather(sid, rt, radios, lambda: False)


def value(c: build.Cand, st: State, vectors: dict[str, list[float]], pos: list[float] | None,
          neg: list[float] | None, queued: bool = False) -> float:
    """The mix's score, minus the session's early skips of the artist, plus closeness by sound to what the session
    liked minus to what it skipped."""
    v = c.score - SKIP_ARTIST_PENALTY * st.skips.get(c.artist, 0) + (QUEUED_BONUS if queued else 0.0)
    vec = vectors.get(c.key)
    if vec is not None:
        if st.taste_model == "v2":
            nearest = max((max(0.0, build._cos(vec, vectors.get(key))) for key in st.neg), default=0.0)
            v += SOUND_WEIGHT * (build._cos(vec, pos) - .5 * nearest)
        else:
            v += SOUND_WEIGHT * (build._cos(vec, pos) - build._cos(vec, neg))
    parts = c.parts or {}
    v += parts.get("session_preferences", {}).get("contribution", 0.0)
    multiplier = parts.get("pause_multiplier", 1.0)
    # A soft restriction must also lower a negative score, never make it more attractive.
    return v * multiplier if v >= 0 else v / multiplier


def pick(st: State, p: Profile, mode: str, vectors: dict[str, list[float]], n: int = BATCH,
         queued: set[str] = frozenset(), first: build.Cand | None = None,
         values: dict[str, float] | None = None, eligible: set[str] | None = None) -> list[build.Cand]:
    """The next `n`: the best by `value` keeping the mode's familiar share, an artist at most once in `ARTIST_GAP`
    tracks (counting the played ones; the artist of a wave by an artist — once in `SEED_ARTIST_GAP`); with nothing else left, the gap gives way (never the same artist twice in a
    row). «Не нравится» set since the candidate came in drops it. `queued` — refs the client has ahead already.
    `values` gets the `value` of every candidate ranked, by ref (the analytics of `wave_items`)."""
    pos = _weighted_center(st.positive_weights, vectors) if st.taste_model == "v2" else _center(st.pos, vectors)
    neg = None if st.taste_model == "v2" else _center(st.neg, vectors)
    pool = [c for c in [*st.out.values(), *st.pool.values()] if c is not first and not _disliked(c, p)
            and (eligible is None or c.track.ref in eligible)]
    vals = {id(c): value(c, st, vectors, pos, neg, c.track.ref in queued) for c in pool}
    if values is not None:
        values.update((c.track.ref, vals[id(c)]) for c in pool)
    ranked = sorted(pool, key=lambda c: -vals[id(c)])
    share = {"familiar": 0.8, "new": 0.1}.get(st.preferences.get("discovery"),
                                               build.MODES.get(mode, build.MODES["normal"]))
    out: list[build.Cand] = [first] if first is not None else []
    recent = [x.artist for x in st.played]
    while len(out) < n and ranked:
        seq = recent + [c.artist for c in out]
        gap = set(seq[-(ARTIST_GAP - 1):])
        seed_gap = set(seq[-(SEED_ARTIST_GAP - 1):])
        total, fam = st.started + len(out), st.started_familiar + sum(c.familiar for c in out)
        want_fam = fam + 1 <= share * (total + 1) + 0.5
        ok = [c for c in ranked if c.artist not in (seed_gap if c.artist == st.seed_artist else gap)]
        best = (next((c for c in ok if c.familiar == want_fam), None) or (ok[0] if ok else None)
                or next((c for c in ranked if not seq or c.artist != seq[-1]), None))
        if best is None:
            break
        ranked.remove(best)
        out.append(best)
    return out


def _disliked(c: build.Cand, p: Profile) -> bool:
    t = c.track
    return p.disliked(ref=t.ref if t.source in ("dz", "file") else None, nd_song_id=c.nd_song_id, artist=t.artist,
                      artist_ref=t.artist_ref)


async def _vectors(rt: Runtime) -> dict[str, list[float]]:
    """Vectors of the best candidates and of the session's likes and skips (asked once, missing ones again a minute
    later)."""
    st = rt.state
    if st.taste_model == "v2":
        from app.mixes.taste_select import geometry_scores
        p = rt.profile
        assert isinstance(p, V2Profile)
        candidates = [*st.out.values(), *st.pool.values()]
        rt.vectors = await geometry_scores(p, candidates, [*st.pos, *st.neg])
        st.space = {"model": p.geometry.model, "space_id": p.geometry.space_id,
                    "dim": p.geometry.dim} if p.geometry else None
        for c in candidates:
            c.score = build.score(c, weights(), p)
        if p.geometry:
            todo = sorted((c for c in candidates if c.sim is None and c.track.source == "dz"),
                          key=lambda c: -c.score)[:EMBED_NEW]
            urls = await asyncio.gather(*(build._preview(c.track) for c in todo))
            items = [{"key": c.key, "url": url} for c, url in zip(todo, urls, strict=True) if url]
            if items:
                try:
                    async with embedder.EmbedderClient() as client:
                        await client.embed(items)
                except embedder.EmbedderDown:
                    pass
        return rt.vectors
    if not embedder.configured() or not (st.pos or st.neg):
        return rt.vectors
    best = sorted([*st.out.values(), *st.pool.values()], key=lambda c: -c.score)[:VECTORS_TOP]
    now = time.monotonic()
    keys = [k for k in dict.fromkeys([*st.pos, *st.neg, *(c.key for c in best)])
            if k not in rt.vectors and rt.asked.get(k, 0) < now - ASK_AGAIN_S]
    if keys:
        try:
            async with embedder.EmbedderClient() as e:
                rt.vectors.update(await e.vectors(keys))
        except embedder.EmbedderDown as e:
            log.info("wave: no vectors: %s", e)
        for k in keys:
            rt.asked[k] = now
    return rt.vectors


# --- the session's events ----------------------------------------------------------------------------------------


def _match(st: State, ref: str | None, nd: str | None) -> tuple[str, build.Cand | Played] | None:
    """The handed out track an event names (by its ref or Navidrome song; `nd:<song>` refs too)."""
    if ref and ref.startswith("nd:"):
        ref, nd = None, nd or ref[3:]
    for c in st.out.values():
        if (ref and c.track.ref == ref) or (nd and c.nd_song_id == nd):
            return "out", c
    for x in reversed(st.played):
        if (ref and x.ref == ref) or (nd and x.nd_song_id == nd):
            return "played", x
    return None


def _start(st: State, c: build.Cand) -> Played:
    st.out.pop(c.track.ref, None)
    st.pool.pop(c.track.ref, None)
    x = _as_played(c)
    st.played = [*st.played, x][-HISTORY_MAX:]
    st.heard.add(fp_of(c))
    st.started += 1
    st.refills_since_play = 0
    st.started_familiar += c.familiar
    return x


def _signal(keys: list[str], key: str) -> None:
    if key in keys:
        keys.remove(key)
    keys.append(key)
    del keys[:-SIGNALS_MAX]


def _as_played(c: build.Cand) -> Played:
    return Played(ref=c.track.ref, nd_song_id=c.nd_song_id, key=c.key, artist=c.artist, familiar=c.familiar,
                  track=c.track.model_dump(mode="json"))


async def learn(sid: str, rt: Runtime, events: list[PlayEvent]) -> None:
    """What the session's events say: starts, early skips, likes, tracks played to the end."""
    if rt.state.taste_model == "v2":
        await _learn_v2(sid, rt, events)
        return
    st = rt.state
    liked: list[Played] = []
    for e in events:
        if e.event_id and e.event_id in st.seen:
            continue
        if e.event_id:
            st.seen.add(e.event_id)
        if (e.target_type or "track") != "track" or e.reverts_event_id:
            continue  # Typed subjects and causal rollbacks are journal data, not a track playback signal.
        m = _match(st, e.ref, e.nd_song_id)
        if m is None:
            continue
        x = m[1]
        if isinstance(x, build.Cand):  # handed out: it starts now (a ♥ of a track ahead doesn't start it)
            x = _start(st, x) if e.kind in PLAY_KINDS else _as_played(x)
        if e.kind in ("skip", "finish"):
            x.kind, x.position_s = e.kind, e.position_s
        if (e.kind == "skip" and (e.position_s or 0) < EARLY_SKIP_S) or e.kind == "dislike":
            st.skips[x.artist] = st.skips.get(x.artist, 0) + 1
            _signal(st.neg, x.key)
            if x.key in st.pos:
                st.pos.remove(x.key)
        elif e.kind in ("finish", "like"):
            _signal(st.pos, x.key)
            if x.key in st.neg:
                st.neg.remove(x.key)
            liked.append(x)
            if e.kind == "finish":
                await _download(sid, rt, x)
    radios: list[tuple[str, build.Gen]] = []
    for x in liked:
        if x.ref in st.radios or not x.track:
            continue
        st.radios.append(x.ref)
        t = CatalogTrack.model_validate(x.track)
        radios.append((SESSION_RADIO, lambda t=t: radio_of(t)))
    await _gather(sid, rt, radios, lambda: False)


async def _download(sid: str, rt: Runtime, x: Played) -> None:
    """A track played to its end that the library lacks: a quiet download (`query_json.wave`), unless one is under
    way already (the ♥ one, another session's)."""
    from app.jobs.downloads import query_from_track

    st = rt.state
    if not rt.download_finished or x.nd_song_id or not x.track or x.ref in st.downloads or not x.ref.startswith("dz:"):
        return
    st.downloads.append(x.ref)
    t = CatalogTrack.model_validate(x.track)
    async with session_scope() as s:
        active = (await s.execute(select(DownloadItem.id).where(
            DownloadItem.status.in_(ACTIVE_STATUSES),
            func.json_extract(DownloadItem.query_json, "$.meta.track_ref") == x.ref).limit(1))).scalar_one_or_none()
        if active is not None:
            return
        from app.library.owned import find_existing

        if await find_existing(s, artist=t.artist, title=t.title, duration_s=t.duration_s, isrc=t.isrc,
                               deezer_track_id=t.id) is not None:
            return
        row = await s.get(WaveSession, sid)
        if row is None:
            return
        if row.batch_id is None:
            batch = DownloadBatch(user_id=rt.user_id, origin="wave", title="Моя волна")
            s.add(batch)
            await s.flush()
            row.batch_id = batch.id
        item = DownloadItem(batch_id=row.batch_id, user_id=rt.user_id, query_json={
            "query": query_from_track(t).model_dump(),
            "meta": {"track_ref": x.ref, "deezer_track_id": t.id, "artists": t.artists or [t.artist]},
            "wave": {"session": sid}})
        s.add(item)
        await s.flush()
        await enqueue(s, "download_item", {"item_id": item.id})
    notify()
    log.info("wave %s: played to the end, downloading %s — %s", sid, t.artist, t.title)


async def record(s: AsyncSession, sid: str, user_id: int, raw: list[Any]):
    """The session's events into the listening history (`play_events`, context `wave:<id>`); one the client stored
    already through `/api/plays/events` (the same `event_id`) gets that context. Returns the stored events too."""
    from app.plays import record_events

    context = f"wave:{sid}"
    events = [{**x, "context": context} if isinstance(x, dict) else x for x in raw]
    out = await record_events(s, user_id, events)
    ids = [x["event_id"] for x in events if isinstance(x, dict) and isinstance(x.get("event_id"), str)]
    rows: list[PlayEvent] = []
    if ids:
        await s.execute(update(PlayEvent).where(PlayEvent.user_id == user_id, PlayEvent.event_id.in_(ids),
                        or_(PlayEvent.context.is_(None), ~PlayEvent.context.like("wave:%"), PlayEvent.context == context))
                        .values(context=context))
        rows = list((await s.execute(select(PlayEvent).where(PlayEvent.user_id == user_id,
                                                             PlayEvent.event_id.in_(ids), PlayEvent.context == context)
                                     .order_by(PlayEvent.ts, PlayEvent.id))).scalars())
    return out, rows


# --- the API's two calls --------------------------------------------------------------------------------------------


def title_of(seed: dict[str, Any] | None) -> str:
    if not seed:
        return "Моя волна"
    return f"Волна по треку «{seed.get('title')}»" if seed.get("ref") else f"Волна: {seed.get('artist')}"


@dataclass
class Batch:
    session_id: str
    title: str
    mode: str
    seed: dict[str, Any] | None
    tracks: list[build.Cand]
    pending: bool  # sources still answering in the background
    recorded: Any = None
    preferences: dict[str, str] = field(default_factory=lambda: WavePreferences().model_dump())
    preferences_status: str | None = None
    preparation: dict[str, int] = field(default_factory=lambda: {"ready": 0, "pending": 0, "unavailable": 0})


async def _artist_songs(nd, artist_id: str, name: str) -> list[CatalogTrack]:
    """The Navidrome artist's songs, as catalog-like tracks (their Deezer record when the Hub has one)."""
    artist = await nd.get_artist(artist_id)
    albums = [a["id"] for a in artist.get("album") or [] if a.get("id")][:ARTIST_ALBUMS_MAX]
    found = await asyncio.gather(*(nd.get_album(a) for a in albums), return_exceptions=True)
    key = artist_key(name)
    songs: dict[str, dict[str, Any]] = {}
    for album in found:
        if isinstance(album, BaseException):
            continue
        for x in album.get("song") or []:
            ids = {x.get("artistId"), *(a.get("id") for a in x.get("artists") or [])}
            if x.get("id") and (artist_id in ids or artist_key(x.get("artist")) == key):
                songs.setdefault(x["id"], x)
    picked = random.sample(list(songs), min(ARTIST_SONGS_MAX, len(songs)))
    async with session_scope() as s:
        rows = {t.nd_song_id: t for t in (await s.execute(select(Track).where(Track.nd_song_id.in_(picked)))).scalars()}
    out = []
    for sid in picked:
        if (t := rows.get(sid)) is not None:
            out.append(library_track(t))
        else:
            x = songs[sid]
            out.append(CatalogTrack(ref=f"nd:{sid}", source="nd", id=sid, title=x.get("title") or "",
                                    artist=x.get("artist") or name, artists=[x.get("artist") or name],
                                    album_title=x.get("album"), duration_s=x.get("duration")))
    return out


async def _catalog_artist(name: str, songs: list[CatalogTrack]) -> str | None:
    """The Deezer artist of a library artist: from a song's catalog record, else the search by the exact name."""
    if svc.deezer is None:
        return None
    key = artist_key(name)
    for t in songs:
        if t.source == "dz" and (full := await catalog_track(t.ref)) is not None and full.artist_ref                 and artist_key(full.artist) == key:
            return full.artist_ref
    try:
        found = await svc.deezer.search(name, kinds=("artist",), limit=5)
    except Exception as e:  # The wave goes on with the library songs.
        log.warning("wave: Deezer artist search %r failed: %s", name, e)
        return None
    return next((a.ref for a in found.artists if artist_key(a.name) == key), None)


async def _seed(seed: dict[str, Any], user: User | None = None) -> tuple[CatalogTrack | None, dict[str, Any] | None]:
    """The track or the artist a wave starts from."""
    if seed.get("by_artist"):
        nd_id = seed.get("nd_song_id")
        ref = seed.get("ref") or ""
        if ref.startswith("nd:"):
            nd_id = ref[3:]
        if nd_id and user is not None:
            from app.auth import user_navidrome

            nd = user_navidrome(user)
            try:
                artist_id = (await nd.get_song(nd_id)).get("artistId")
            except Exception as e:
                raise WaveError(404, "Исполнитель трека не найден") from e
            finally:
                await nd.aclose()
            if artist_id:
                return await _seed({"nd_artist_id": artist_id}, user)
        elif ref.startswith("dz:"):
            track = await catalog_track(ref)
            if track and track.artist_ref:
                return await _seed({"artist_ref": track.artist_ref}, user)
        raise WaveError(404, "Исполнитель трека не найден")
    if nd := seed.get("nd_song_id"):
        async with session_scope() as s:
            t = (await s.execute(select(Track).where(Track.nd_song_id == nd).limit(1))).scalar_one_or_none()
            lt = library_track(t) if t is not None else None
        if lt is None:
            raise WaveError(404, "Песни нет в библиотеке")
        if lt.source == "dz" and (full := await catalog_track(lt.ref)) is not None:
            lt = full  # the artist's ref for its radio
        return lt, None
    if ref := seed.get("ref"):
        if ref.startswith("nd:"):
            return await _seed({"nd_song_id": ref[3:]}, user)
        if ref.startswith("file:"):
            from app import listen

            c = await listen.file_candidate(ref)
            if c is None or not c.artist.strip() or not c.title.strip():
                raise WaveError(404, "Файл не найден: повторите поиск")
            return CatalogTrack(ref=ref, source="file", id=c.provider_id, title=c.title, artist=c.artist,
                                artists=[c.artist], album_title=c.album, duration_s=c.duration_s,
                                explicit=c.explicit, cover_url=c.cover_url, cover_small_url=c.cover_url), None
        if not ref.startswith("dz:") or (t := await catalog_track(ref)) is None:
            raise WaveError(404, "Трек не найден в каталоге")
        return t, None
    if (ref := seed.get("artist_ref")) and svc.deezer is not None:
        try:
            a = await svc.deezer.get_artist(split_ref(ref)[1])
        except Exception as e:
            raise WaveError(404, "Исполнитель не найден") from e
        return None, {"ref": a.ref, "name": a.name}
    if (nd_id := seed.get("nd_artist_id")) and user is not None:
        from app.auth import user_navidrome

        nd = user_navidrome(user)
        try:
            name = (await nd.get_artist(nd_id)).get("name")
            if not name:
                raise WaveError(404, "Исполнитель не найден")
            songs = await _artist_songs(nd, nd_id, name)
        except WaveError:
            raise
        except Exception as e:
            raise WaveError(404, "Исполнитель не найден") from e
        finally:
            await nd.aclose()
        return None, {"ref": await _catalog_artist(name, songs), "name": name, "nd_artist_id": nd_id, "songs": songs}
    raise WaveError(400, "Нужен ref, nd_song_id, artist_ref или nd_artist_id")


async def clear_old(s: AsyncSession, now: datetime | None = None) -> int:
    """Sessions not asked for `KEEP`: their state becomes a summary (`CLEARED_KEYS`; the pool, the queue and the
    played tracks are heavy and no longer needed), the row and its `wave_items` stay for the history. Over already
    (`IDLE`): such a session answers 404 as before. Returns how many were cleared."""
    now = now or utcnow()
    rows = list((await s.execute(select(WaveSession).where(WaveSession.updated_at < now - KEEP,
                                                           WaveSession.cleared_at.is_(None)))).scalars())
    for row in rows:
        old = row.state_json or {}
        row.state_json = {"cleared": True, **{k: old[k] for k in CLEARED_KEYS if k in old}}
        row.cleared_at = now
        if (rt := _rt.pop(row.id, None)) is not None:
            for t in rt.tasks:
                t.cancel()
    return len(rows)


async def start(user: User, seed: dict[str, Any] | None, mode: str | None, *, origin: str | None = None,
                client: str | None = None, restart_of: str | None = None,
                request: dict[str, Any] | None = None, preferences: dict[str, str] | None = None) -> Batch:
    """A new session. `origin`, `client`, `restart_of` and `request` (the start's body as it came) are kept for the
    analytics only."""
    from app.mixes.core import settings_of

    cfg = get_settings().cfg.mixes
    async with session_scope() as s:
        await clear_old(s)
        mode = mode or (await settings_of(s, user.id)).mode
    seed_track, seed_artist = await _seed(seed, user) if seed else (None, None)
    async with session_scope() as s:
        profile = await build_profile(s, user, model=cfg.taste_model)
    if isinstance(profile, V2Profile):
        from app.mixes.taste_select import prepare
        await prepare(profile)
    seeds: list[TasteTrack] = []
    if seed_track is None and seed_artist is None:
        if isinstance(profile, V2Profile):
            from app.mixes.taste_select import seeds as select_seeds
            seeds = await select_seeds(profile, cfg.seeds)
        else:
            seeds = profile.seeds(cfg.seeds) or await build.cold_seeds(profile, cfg.seeds)
        if profile.empty or not seeds:
            raise WaveError(409, "Волне не от чего оттолкнуться: послушайте что-нибудь или выберите исполнителей")
    sid = uuid.uuid4().hex[:16]
    rt = Runtime(user_id=user.id, mode=mode, state=State(taste_model=cfg.taste_model,
                 preferences=WavePreferences.model_validate(preferences or {}).model_dump()), profile=profile)
    rt.state.seeds_used = [t.key for t in seeds]
    rt.state.seed_artist = artist_key(seed_artist["name"]) if seed_artist else None
    seed_json = ({"ref": seed_track.ref, "artist": seed_track.artist, "title": seed_track.title} if seed_track
                 else {k: v for k, v in (("artist_ref", seed_artist.get("ref")), ("artist", seed_artist["name"]),
                                         ("nd_artist_id", seed_artist.get("nd_artist_id"))) if v}
                 if seed_artist else None)
    async with session_scope() as s:
        s.add(WaveSession(id=sid, user_id=user.id, mode=mode, seed_json=seed_json, state_json={}, origin=origin,
                          client=client, restart_of=restart_of, taste_model=cfg.taste_model, request_json=request))
    _rt[sid] = rt
    async with rt.lock:
        first = None
        if seed_track is not None:
            first = build.Cand(track=seed_track, gens={"seed": 0})
            await build.link_library({seed_track.ref: first})
            if _disliked(first, profile):
                raise WaveError(409, "Начальный трек исключён из рекомендаций")
            first.familiar = build.keep(first, profile, cfg) and first.familiar
            first.score = build.score(first, weights(), profile)
        if first is not None:
            rt.state.heard.add(fp_of(first))  # the radios' copies of the seed stay out
        await _gather(sid, rt, list(generators(profile, seed_track, seed_artist, seeds).items()),
                      lambda: len(rt.state.pool) >= 4 * BATCH)
        tracks = await _hand_out(sid, rt, set(), first)
        pending = bool(rt.tasks) or rt.state.preparation["pending"] > 0 or rt.state.more_to_prepare
    return Batch(sid, title_of(seed_json), mode, seed_json, tracks, pending,
                 preferences=rt.state.preferences, preferences_status=rt.state.preferences_status,
                 preparation=rt.state.preparation)


async def next_batch(user: User, sid: str, played: list[Any], queued: list[str],
                     preferences: dict[str, str] | None = None) -> Batch:
    rt = await _runtime(sid, user)
    if not rt.state.pool and rt.tasks:  # nothing to pick from yet: the sources still answer (outside the lock)
        await asyncio.wait(set(rt.tasks), timeout=get_settings().wave_wait_s)
    async with rt.lock:
        async with session_scope() as s:
            recorded, events = await record(s, sid, user.id, played)
            # Playback may have arrived through /plays before a preferences-only refresh.
            events = list((await s.execute(select(PlayEvent).where(PlayEvent.user_id == user.id,
                PlayEvent.context == f"wave:{sid}", PlayEvent.ts <= utcnow())
                .order_by(PlayEvent.ts, PlayEvent.id))).scalars())
            row = await s.get(WaveSession, sid)
            seed_json = row.seed_json if row else None
            if row is not None:
                row.next_calls = (row.next_calls or 0) + 1
        await _profile(rt, user)  # Must follow the event transaction, including external state changes.
        await learn(sid, rt, events)
        st = rt.state
        if preferences is not None:
            st.preferences = WavePreferences.model_validate(preferences).model_dump()
        # No actual playback between calls: try only a bounded number of new source batches.
        # Repeated empty/all-unavailable results must not keep clients polling forever.
        if len(st.pool) < POOL_LOW and not rt.refilling and st.refills_since_play < REFILL_SEEDS:
            _refill(sid, rt)
            st.refills_since_play += 1
        tracks = await _hand_out(sid, rt, {q for q in queued if q in st.out})
        pending = bool(rt.tasks) or st.preparation["pending"] > 0 or st.more_to_prepare
    return Batch(sid, title_of(seed_json), rt.mode, seed_json, tracks, pending, recorded,
                 st.preferences, st.preferences_status, st.preparation)


async def _preferences(rt: Runtime, candidates: list[build.Cand]) -> None:
    """Contrast text prompts against actual MuQ audio vectors, never inferred genre/tag labels."""
    st = rt.state
    st.preferences_status = None
    for c in candidates:
        if c.parts is None:
            c.parts = {}
        c.parts.pop("session_preferences", None)
    active = {axis: st.preferences[axis] for axis in PREFERENCE_PROMPTS if st.preferences[axis] != "any"}
    if not active:
        return
    if not embedder.configured():
        st.preferences_status = "Аудиомодель недоступна: энергия и вокал пока не учитываются."
        return
    best = sorted(candidates, key=lambda c: -c.score)[:VECTORS_TOP]
    keys = list(dict.fromkeys(c.key for c in best))
    if not keys:
        st.preferences_status = "Нет аудиовекторов для оценки энергии и вокала."
        return
    try:
        # MuQ loads lazily (about 9 s on the CPU server); leave headroom within the desktop's 25 s read deadline.
        async with asyncio.timeout(20.0), embedder.EmbedderClient() as client:
            prompts = [(axis, text) for axis in active for text in PREFERENCE_PROMPTS[axis]]
            responses = await asyncio.gather(*(client._post("/text", {
                "text": text, "keys": keys, "limit": len(keys)}) for _, text in prompts))
        models = {r.get("model") for r in responses}
        if len(models) != 1 or not next(iter(models)) or next(iter(models)) == "fake":
            raise embedder.EmbedderDown("no consistent real text/audio model")
        model = next(iter(models))
        scores = []
        for response in responses:
            scores.append({r["key"]: float(r["sim"]) for r in response.get("results", [])
                           if r.get("key") in keys and isinstance(r.get("sim"), (float, int))
                           and math.isfinite(r["sim"]) and -1.0001 <= r["sim"] <= 1.0001})
        complete = set(keys)
        for i, (axis, preference) in enumerate(active.items()):
            low, high = scores[2 * i:2 * i + 2]
            available = low.keys() & high.keys()
            complete.intersection_update(available)
            sign = 1 if preference in ("energetic", "vocal") else -1
            for c in best:
                if c.key not in available:
                    continue
                evidence = c.parts.setdefault("session_preferences", {"model": model, "contribution": 0.0})
                contrast = sign * (high[c.key] - low[c.key])
                evidence[axis] = {"preference": preference, "contrast": contrast}
                evidence["contribution"] += PREFERENCE_WEIGHT * contrast
        if len(complete) < len({c.key for c in candidates}):
            st.preferences_status = "Энергия и вокал учитываются только для треков с доступными аудиовекторами."
    except (embedder.EmbedderDown, TimeoutError, TypeError, ValueError, KeyError) as exc:
        log.info("wave: session audio preferences unavailable: %r", exc)
        st.preferences_status = "Аудиомодель недоступна: энергия и вокал пока не учитываются."


async def _hand_out(sid: str, rt: Runtime, queued: set[str], first: build.Cand | None = None) -> list[build.Cand]:
    """Rank once, check a bounded full-file reserve, then hand out only verified ready recordings."""
    from app import listen, recommendations
    from app.recommendation_policy import load_policy

    assert rt.profile is not None
    st = rt.state
    if first is not None:
        st.awaiting_seed = first.track.ref
        st.pool[first.track.ref] = first
    elif st.awaiting_seed:
        first = st.pool.get(st.awaiting_seed)
        if first is None:
            raise WaveError(409, "Полная запись начального трека недоступна")
    policy = await load_policy(rt.user_id)
    if first is not None and (policy.blocked(first) or _disliked(first, rt.profile)):
        raise WaveError(409, "Начальный трек временно исключён из рекомендаций")
    if isinstance(rt.profile, V2Profile):
        cfg = get_settings().cfg.mixes
        for pool in (st.pool, st.out):
            for ref, c in list(pool.items()):
                if c is not first and not build.keep(c, rt.profile, cfg):
                    del pool[ref]
        _session_weights(st, rt.profile)
    vectors = await _vectors(rt)
    source_weights = await recommendations.adapted_weights(rt.user_id, weights())
    candidates = [*st.out.values(), *st.pool.values()]
    for c in candidates:
        c.parts = build.score_parts(c, source_weights, rt.profile)
        c.score = c.parts["total"]
        c.known = build.known_of(c, rt.profile)
        c.parts["pause_multiplier"] = policy.multiplier(c)
        c.parts["preferences"] = dict(st.preferences)
    await _preferences(rt, candidates)
    eligible = {c.track.ref for c in candidates if not policy.blocked(c) and not _disliked(c, rt.profile)
                and not (rt.blocked and rt.blocked(c))}
    values: dict[str, float] = {}
    reserve = pick(st, rt.profile, rt.mode, vectors, n=PREPARE_POOL, queued=queued, first=first,
                   values=values, eligible=eligible)
    st.more_to_prepare = len(reserve) == PREPARE_POOL and len(eligible) > len(reserve)
    refs = {c.track.ref: f"nd:{c.nd_song_id}" if c.nd_song_id else c.track.ref for c in reserve}
    statuses = await listen.prepare_full(rt.user_id, list(dict.fromkeys(refs.values())), timeout_s=2.0)
    ready = {ref for ref, playback_ref in refs.items() if statuses.get(playback_ref) == "ready"}
    unavailable = {ref for ref, playback_ref in refs.items() if statuses.get(playback_ref) == "unavailable"}
    pending = refs.keys() - ready - unavailable
    st.preparation = {"ready": len(ready), "pending": len(pending), "unavailable": len(unavailable)}
    for ref in unavailable:
        st.pool.pop(ref, None)
        st.out.pop(ref, None)
    st.unavailable.update(unavailable)
    if first is not None and first.track.ref in unavailable:
        await _save(sid, rt)
        raise WaveError(409, "Полная запись начального трека недоступна; другой трек не подставлен")
    tracks = ([] if first is not None and first.track.ref not in ready else
              pick(st, rt.profile, rt.mode, vectors, queued=queued, first=first, values=values, eligible=ready))
    if tracks and first is not None:
        st.awaiting_seed = None
    for ref in list(st.out):
        st.pool[ref] = st.out.pop(ref)
    for c in tracks:
        c.parts["selection_value"] = values.get(c.track.ref)
        c.explanation = recommendations.explain_candidate(c)
        if c.gens.get("seed") == 0:
            c.explanation.insert(0, "Выбран начальным треком волны")
        evidence = c.parts.get("session_preferences", {})
        for axis, names in (("energy", {"calm": "спокойному", "energetic": "энергичному"}),
                            ("vocals", {"instrumental": "инструментальному", "vocal": "вокальному"})):
            feature = evidence.get(axis)
            if feature and feature["contrast"] > 0:
                c.explanation.append(f"По аудиомодели ближе к выбранному {names[feature['preference']]} звучанию")
        discovery = st.preferences["discovery"]
        if discovery == "familiar" and c.familiar:
            c.explanation.append("В этой волне увеличена доля знакомой музыки")
        elif discovery == "new" and not c.familiar:
            c.explanation.append("В этой волне увеличена доля незнакомой музыки")
        if c.parts["pause_multiplier"] < 1:
            c.explanation.append("Приоритет исполнителя временно снижен на 30 дней")
        st.pool.pop(c.track.ref, None)
        st.out[c.track.ref] = c
    await _save(sid, rt)
    try:
        async with session_scope() as s:
            await save_items(s, sid, rt.user_id, tracks, values)
    except Exception:
        log.exception("wave %s: handed out tracks not recorded", sid)
    listen.warm_light([f"nd:{c.nd_song_id}" if c.nd_song_id else c.track.ref for c in tracks])
    return tracks


async def save_items(s: AsyncSession, sid: str, user_id: int, tracks: list[build.Cand],
                     values: dict[str, float], now: datetime | None = None) -> None:
    """What the wave handed out into `wave_items` (INSIGHTS.md 6.4): a track new to the session gets the next
    position; one handed out again (it went back to the pool, or the client still has it queued) counts once more,
    with its latest score, closeness and value."""
    if not tracks:
        return
    now = now or utcnow()
    refs = [c.track.ref for c in tracks]
    have = set((await s.execute(select(WaveItem.ref).where(WaveItem.session_id == sid,
                                                          WaveItem.ref.in_(refs)))).scalars())
    nxt = (await s.execute(select(func.coalesce(func.max(WaveItem.position) + 1, 0))
                           .where(WaveItem.session_id == sid))).scalar_one()
    rows = []
    for c in tracks:
        ref = c.track.ref
        rows.append(dict(session_id=sid, user_id=user_id, first_handed_at=now, last_handed_at=now, handed_count=1,
                         position=-1 if ref in have else nxt, ref=ref, nd_song_id=c.nd_song_id, key=c.key,
                         artist=(c.track.artist or "")[:256], title=(c.track.title or "")[:256],
                         generator=top_generator(c)[:32], gens=dict(c.gens), familiar=c.familiar,
                         score=c.score, sim=c.sim, value=values.get(ref),
                         decision_json={**c.features(),
                                        "source_weights": (c.parts or {}).get("generator_weights", {}),
                                        "preferences": (c.parts or {}).get("preferences", {})}))
        if ref not in have:
            have.add(ref)
            nxt += 1
    q = sqlite_insert(WaveItem).values(rows)
    await s.execute(q.on_conflict_do_update(index_elements=["session_id", "ref"], set_={
        "handed_count": WaveItem.handed_count + 1, "last_handed_at": q.excluded.last_handed_at,
        "score": q.excluded.score, "sim": q.excluded.sim, "value": q.excluded.value,
        "generator": q.excluded.generator, "gens": q.excluded.gens, "familiar": q.excluded.familiar,
        "decision_json": q.excluded.decision_json}))


async def settle(sid: str) -> None:
    """Waits for the session's background sources (tests)."""
    rt = _rt.get(sid)
    while rt is not None and rt.tasks:
        tasks = list(rt.tasks)
        await asyncio.gather(*tasks, return_exceptions=True)
        # gather() can finish synchronously for done tasks, before their discard callbacks run.
        # Remove this completed snapshot ourselves; tasks spawned while awaiting remain tracked.
        rt.tasks.difference_update(tasks)


async def reset() -> None:
    """Forget the sessions in memory and stop their tasks (shutdown, tests): they load from the database again."""
    for rt in _rt.values():
        for t in rt.tasks:
            t.cancel()
    _rt.clear()


def generator_title(name: str) -> str:
    return TITLES.get(name) or build.GENERATOR_TITLES.get(name, name)


def top_generator(c: build.Cand) -> str:
    return c.generator if c.gens else "seed"


__all__ = ["BATCH", "Batch", "WaveError", "generator_title", "next_batch", "reset", "settle", "start"]
