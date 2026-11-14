"""Building a «Микс дня» (MIXES.md 6.1): generators → filters → score → 50 songs (+ reserve) → order → name.

- Generators give ranked lists of catalog tracks: «Любимое» (`favorites`), «Тайник» (`stash`), «Похожее»
  (`ytm_radio`, `lastfm_similar`, `deezer_radio`), «Незнакомое» (`yandex_similar`, `similar_artists`), «Новинки»
  (`new_releases`), «Моя волна» Яндекса (`yandex_wave`). A generator that fails is left out (its error goes to the
  mix's stats); the mix is made of the others.
- Familiar — the track is in the user's history, or its artist was listened to more than 5 times; new — neither
  (the «Незнакомое» rule of Yandex). Familiar songs heard in the last 3 days are left out.
- Score: Σ weight / log2(position + 2) over the generators that gave it, + 1.5·sim (the embedder: closeness to the
  taste by sound), + 0.2·popularity (Deezer rank, log); a picked artist's next song is worth less.
- Mode shares (familiar / new): normal 40/60, favorites 80/20, discover 10/90; ≤ 2 songs of an artist; order —
  a strong familiar song first, then the shares interleaved, the next song the closest by sound of a few best ones,
  never the same artist twice in a row.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.catalog.models import CatalogTrack, split_ref
from app.config import get_settings
from app.db import session_scope
from app.matching.normalize import fingerprint, normalize_artist, split_version, version_kind
from app.mixes import embedder, sources
from app.mixes.resolve import resolve_hits
from app.mixes.taste import Profile, TasteTrack, artist_key
from app.mixes.taste_math import V2Profile
from app.models import Import, Track, utcnow
from app.services import svc

log = logging.getLogger(__name__)

MODES = {"normal": 0.4, "favorites": 0.8, "discover": 0.1}  # share of familiar songs
MODE_TITLES = {"normal": "Обычный", "favorites": "Любимое", "discover": "Незнакомое"}
GENERATOR_TITLES = {
    "favorites": "Любимое", "stash": "Тайник", "ytm_radio": "Похожее: радио YouTube Music",
    "lastfm_similar": "Похожее: Last.fm", "deezer_radio": "Похожее: радио Deezer",
    "yandex_similar": "Незнакомое: похожие Яндекса", "similar_artists": "Незнакомое: похожие исполнители",
    "new_releases": "Новинки", "yandex_wave": "Моя волна Яндекса",
}
BANNED_VERSIONS = {"karaoke", "instrumental", "cover", "slowed", "sped up", "8d", "bass boosted", "remix", "live"}
REJECT_REASONS = ("disliked", "version", "duration", "recent", "negative_score")  # `keep_reason`
UNFAMILIAR_MAX_LISTENS = 5
RECENT_DAYS = 3
FAVORITE_REST_DAYS = 7
STASH_REST_DAYS = 60
SIM_WEIGHT, POP_WEIGHT, ARTIST_PENALTY = 1.5, 0.2, 0.3
ORDER_LOOKAHEAD = 4
EMBED_CANDIDATES = 300  # candidates sent to the embedder (best by score without sound)
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAYS_ACC = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]


@dataclass
class Cand:
    track: CatalogTrack
    gens: dict[str, int] = field(default_factory=dict)  # generator -> best position in its list
    familiar: bool = False
    nd_song_id: str | None = None
    library_track_id: int | None = None
    sim: float | None = None
    score: float = 0.0
    # analytics of a mix's song (INSIGHTS.md 6.6), set by `build`: the parts of its score, what the profile knew of it
    parts: dict[str, Any] | None = None
    known: dict[str, Any] | None = None
    explanation: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        """The audio vector key: the library song, else the catalog track."""
        return embedder.song_key(self.nd_song_id) if self.nd_song_id else self.track.ref

    @property
    def artist(self) -> str:
        return artist_key(self.track.artist)

    @property
    def generator(self) -> str:
        contributions = (self.parts or {}).get("generator_contributions")
        if contributions:
            return max(contributions, key=contributions.get)
        w = get_settings().cfg.mixes.weights
        return max(self.gens, key=lambda g: w.get(g, 0.5) / math.log2(self.gens[g] + 2)) if self.gens else "seed"

    def features(self) -> dict[str, Any]:
        return {"track": self.track.model_dump(mode="json"), "generators": self.gens, "sim": self.sim,
                "rank": self.track.rank, "familiar": self.familiar, "score_parts": self.parts, "profile": self.known,
                "explanation": list(self.explanation)}


@dataclass
class GenResult:
    name: str
    tracks: list[CatalogTrack] = field(default_factory=list)
    error: str | None = None
    off: bool = False  # not configured / not available (no key, no token)
    seconds: float = 0.0


@dataclass
class Built:
    active: list[Cand]
    reserve: list[Cand]
    title: str
    description: str
    stats: dict[str, Any]


Gen = Callable[[], Awaitable[list[CatalogTrack]]]


class Off(Exception):
    """The generator is not available (no Last.fm key, no Yandex token)."""


# --- generators -----------------------------------------------------------------------------------------------


def taste_track(t: TasteTrack) -> CatalogTrack | None:
    if t.track is not None:
        return t.track
    if t.ref and t.ref.startswith("dz:") and t.artist:
        return CatalogTrack(ref=t.ref, source="dz", id=split_ref(t.ref)[1], title=t.title, artist=t.artist,
                            artists=[t.artist], duration_s=t.duration_s)
    if t.nd_song_id and t.artist:
        return CatalogTrack(ref=f"nd:{t.nd_song_id}", source="nd", id=t.nd_song_id, title=t.title, artist=t.artist,
                            artists=[t.artist], duration_s=t.duration_s)
    return None


def _stable(key: str, salt: str) -> str:
    return hashlib.sha1(f"{salt}|{key}".encode()).hexdigest()


def favorites(p: Profile) -> list[CatalogTrack]:
    """The best of the profile, not heard for a week."""
    rest = p.now - timedelta(days=FAVORITE_REST_DAYS)
    best = sorted((t for t in p.tracks.values() if t.score > 0 and
                   (not isinstance(p, V2Profile) or t.familiar) and
                   ((t.last_heard if isinstance(p, V2Profile) else t.last_played) is None or
                    (t.last_heard if isinstance(p, V2Profile) else t.last_played) < rest)),
                  key=lambda t: -t.score)
    return [x for t in best if (x := taste_track(t)) is not None]


def stash(p: Profile) -> list[CatalogTrack]:
    """Own ♥ and downloads not heard for 60 days or never, in an order of the day."""
    rest = p.now - timedelta(days=STASH_REST_DAYS)
    if isinstance(p, V2Profile):
        from datetime import UTC
        day = p.now.replace(tzinfo=UTC).astimezone(ZoneInfo(p.timezone)).date().isoformat()
    else:
        day = p.now.date().isoformat()
    pool = [t for t in p.tracks.values()
            if (t.liked or t.downloaded or (isinstance(p, V2Profile) and t.album_liked)) and
            (isinstance(p, V2Profile) or t.score >= 0) and
            ((t.last_heard if isinstance(p, V2Profile) else t.last_played) is None or
             (t.last_heard if isinstance(p, V2Profile) else t.last_played) < rest)]
    pool.sort(key=lambda t: _stable(t.key, day))
    return [x for t in pool if (x := taste_track(t)) is not None]


async def _by_seeds(seeds: list[TasteTrack], fetch: Callable[[TasteTrack], Awaitable[list[sources.Hit]]],
                    source: str) -> list[CatalogTrack]:
    """A source asked by every seed; the lists interleaved (each seed's first, then its second...)."""
    lists = await asyncio.gather(*(fetch(t) for t in seeds), return_exceptions=True)
    errors = [x for x in lists if isinstance(x, BaseException)]
    ok = [x for x in lists if not isinstance(x, BaseException)]
    if errors and not ok:
        raise errors[0]
    resolved = await resolve_hits(source, _interleave(ok))
    return [t for t in resolved if t is not None]


def _interleave(lists: list[list[Any]]) -> list[Any]:
    out: list[Any] = []
    for i in range(max((len(x) for x in lists), default=0)):
        out += [x[i] for x in lists if i < len(x)]
    return out


async def artist_ref(p: Profile, key: str) -> str | None:
    """The Deezer artist of an artist the profile knows by name."""
    if isinstance(p, V2Profile):
        for state in p.favorite_states.values():
            data = state.data or {}
            if state.liked and state.target_type == "artist" and artist_key(data.get("name")) == key and state.ref:
                return state.ref
            if state.liked and state.target_type == "album" and artist_key(data.get("artist")) == key and data.get("artist_ref"):
                return data["artist_ref"]
    for t in p.tracks.values():
        if t.track is not None and t.track.artist_ref and artist_key(t.track.artist) == key:
            return t.track.artist_ref
    for a in p.picked_artists:
        if artist_key(a.get("name")) == key and a.get("ref"):
            return a["ref"]
    return await artist_by_name(p.artist_names.get(key) or key)


async def artist_by_name(name: str) -> str | None:
    if svc.deezer is None or not name:
        return None
    from app.catalog.cache import cached

    async def produce() -> str:
        found = await svc.deezer.search(name, kinds=("artist",), limit=5)
        want = normalize_artist(name)
        for a in found.artists:
            got = normalize_artist(a.name)
            if (got.text and got.text == want.text) or (got.latin and got.latin == want.latin):
                return a.ref
        return ""

    return await cached(f"mix:dz_artist:{artist_key(name)}", sources.TTL_S, produce) or None


async def _artist_refs(p: Profile, n: int) -> list[str]:
    keys = p.top_artists(n) or [artist_key(a.get("name")) for a in p.picked_artists][:n]
    refs = await asyncio.gather(*(artist_ref(p, k) for k in keys))
    return [r for r in refs if r and r.startswith("dz:")]


async def deezer_radio(p: Profile) -> list[CatalogTrack]:
    if svc.deezer is None:
        raise Off("Deezer")
    refs = await _artist_refs(p, 3)
    lists = await asyncio.gather(*(svc.deezer.get_artist_radio(split_ref(r)[1]) for r in refs))
    return _interleave(list(lists))


async def similar_artists(p: Profile) -> list[CatalogTrack]:
    """Top tracks of artists similar to the favourite ones that the user has not listened to."""
    if svc.deezer is None:
        raise Off("Deezer")
    refs = await _artist_refs(p, 3)
    related = await asyncio.gather(*(svc.deezer.get_artist_related(split_ref(r)[1]) for r in refs))
    names = [a.name for a in _interleave(list(related))]
    if sources.lastfm_configured():
        top = [p.artist_names.get(k, k) for k in p.top_artists(3)]
        try:
            more = await asyncio.gather(*(sources.lastfm_similar_artists(a) for a in top))
            names = _interleave([names, _interleave(list(more))])
        except sources.SourceDown as e:
            log.info("mix: last.fm similar artists: %s", e)
    picked: list[str] = []
    seen: set[str] = set()
    for name in names:
        k = artist_key(name)
        if k in seen or k in p.artists or p.artist_listens.get(k):
            continue
        seen.add(k)
        picked.append(name)
        if len(picked) >= 15:
            break
    found = await asyncio.gather(*(artist_by_name(n) for n in picked))
    tops = await asyncio.gather(*(svc.deezer.get_artist_top(split_ref(r)[1], limit=3) for r in found if r))
    return _interleave(list(tops))


async def new_releases(p: Profile) -> list[CatalogTrack]:
    """Releases of the favourite artists of the last 30 days."""
    if svc.deezer is None:
        raise Off("Deezer")
    since = (p.now - timedelta(days=30)).date()
    refs = await _artist_refs(p, 10)
    pages = await asyncio.gather(*(svc.deezer.get_artist_albums(split_ref(r)[1]) for r in refs))
    fresh = [a for albums in pages for a in albums if a.release_date and a.release_date >= since][:10]
    albums = await asyncio.gather(*(svc.deezer.get_album(a.id) for a in fresh))
    return _interleave([list(a.tracks or [])[:3] for a in albums])


async def yandex_token(user_id: int) -> str | None:
    """The token of the user's Yandex import kept for «Обновить» (M7)."""
    from app.security import decrypt

    async with session_scope() as s:
        enc = (await s.execute(select(Import.token_enc).where(
            Import.user_id == user_id, Import.source == "yandex", Import.token_enc.is_not(None))
            .order_by(Import.id.desc()).limit(1))).scalar_one_or_none()
    try:
        return decrypt(enc) if enc else None
    except RuntimeError:
        return None


async def yandex_wave(p: Profile) -> list[CatalogTrack]:
    token = await yandex_token(p.user_id)
    if not token:
        raise Off("нет токена Яндекса")
    hits = await sources.yandex_wave(token)
    return [t for t in await resolve_hits("yandex", hits) if t is not None]


def generators(p: Profile, seeds: list[TasteTrack]) -> dict[str, Gen]:
    async def lastfm() -> list[CatalogTrack]:
        if not sources.lastfm_configured():
            raise Off("нет LASTFM_API_KEY")
        return await _by_seeds(seeds, lambda t: sources.lastfm_similar_tracks(t.artist, t.title, limit=30), "lastfm")

    async def const(x: list[CatalogTrack]) -> list[CatalogTrack]:
        return x

    return {
        "favorites": lambda: const(favorites(p)),
        "stash": lambda: const(stash(p)),
        "ytm_radio": lambda: _by_seeds(seeds, lambda t: sources.ytm_radio(t.artist, t.title), "ytm"),
        "lastfm_similar": lastfm,
        "deezer_radio": lambda: deezer_radio(p),
        "yandex_similar": lambda: _by_seeds(seeds, lambda t: sources.yandex_similar(t.artist, t.title, t.duration_s),
                                            "yandex"),
        "similar_artists": lambda: similar_artists(p),
        "new_releases": lambda: new_releases(p),
        "yandex_wave": lambda: yandex_wave(p),
    }


async def run_generators(gens: dict[str, Gen]) -> list[GenResult]:
    async def one(name: str, fn: Gen) -> GenResult:
        t0 = time.monotonic()
        try:
            out = GenResult(name, tracks=await fn())
        except Off as e:
            out = GenResult(name, error=str(e), off=True)
        except Exception as e:
            log.warning("mix generator %s failed: %r", name, e)
            out = GenResult(name, error=repr(e)[:300])
        out.seconds = round(time.monotonic() - t0, 2)
        return out

    return list(await asyncio.gather(*(one(n, f) for n, f in gens.items())))


async def cold_seeds(p: Profile, n: int) -> list[TasteTrack]:
    """A new user: the top tracks of the artists picked on «Что вы слушаете?»."""
    if svc.deezer is None or not p.picked_artists:
        return []
    refs = [a["ref"] for a in p.picked_artists if str(a.get("ref", "")).startswith("dz:")]
    tops = await asyncio.gather(*(svc.deezer.get_artist_top(split_ref(r)[1], limit=2) for r in refs),
                                return_exceptions=True)
    tracks = _interleave([x for x in tops if not isinstance(x, BaseException)])
    return [TasteTrack(key=t.ref, ref=t.ref, artist=t.artist, title=t.title, duration_s=t.duration_s, track=t)
            for t in tracks][:n]


# --- candidates ---------------------------------------------------------------------------------------------------


def merge(results: list[GenResult]) -> dict[str, Cand]:
    """One candidate per song (the same song from several generators adds up)."""
    out: dict[str, Cand] = {}
    by_fp: dict[str, str] = {}
    for r in results:
        for pos, t in enumerate(r.tracks):
            fp = fingerprint(t.artist, t.title)
            key = by_fp.get(fp) or t.ref
            c = out.get(key)
            if c is None:
                c = out[key] = Cand(track=t)
                by_fp[fp] = key
            elif c.track.source == "nd" and t.source == "dz":
                c.track = t  # the catalog record is the better one
            c.gens[r.name] = min(c.gens.get(r.name, pos), pos)
    return out


async def link_library(cands: dict[str, Cand]) -> None:
    """Which candidates the library has (its Navidrome song): those need no download."""
    from app.library.owned import annotate_tracks

    dz = [c for c in cands.values() if c.track.source == "dz"]
    async with session_scope() as s:
        for c, o in zip(dz, await annotate_tracks(s, [c.track for c in dz]), strict=True):
            if o.owned and o.nd_song_id:
                c.nd_song_id, c.library_track_id = o.nd_song_id, o.library_track_id
        nd = {c.track.id: c for c in cands.values() if c.track.source == "nd"}
        if nd:
            for tid, song in (await s.execute(select(Track.id, Track.nd_song_id)
                                              .where(Track.nd_song_id.in_(list(nd))))).all():
                nd[song].nd_song_id, nd[song].library_track_id = song, tid


def keep_reason(c: Cand, p: Profile, cfg) -> str | None:
    """Return the first rejection reason, or None when kept; sets `familiar`."""
    t = c.track
    ref = t.ref if t.source == "dz" else None
    if p.disliked(ref=ref, nd_song_id=c.nd_song_id, artist=t.artist, artist_ref=t.artist_ref):
        return "disliked"
    if version_kind(split_version(t.title)[1]) in BANNED_VERSIONS:
        return "version"
    if t.duration_s and not cfg.min_duration_s <= t.duration_s <= cfg.max_duration_s:
        return "duration"
    known = p.find(ref=ref, nd_song_id=c.nd_song_id, fp=fingerprint(t.artist, t.title))
    if isinstance(p, V2Profile):
        c.familiar = bool(known and known.familiar) or p.artist_listens.get(c.artist, 0) > UNFAMILIAR_MAX_LISTENS
        if known and known.familiar and known.last_heard and known.last_heard >= p.now - timedelta(days=RECENT_DAYS):
            return "recent"
        return None
    if known is not None:
        if known.score < 0:
            return "negative_score"
        if known.last_played and known.last_played >= p.now - timedelta(days=RECENT_DAYS):
            return "recent"
        c.familiar = True
        return None
    c.familiar = p.artist_listens.get(c.artist, 0) > UNFAMILIAR_MAX_LISTENS
    return None


def keep(c: Cand, p: Profile, cfg) -> bool:
    """Filters (MIXES.md 6.1.2); sets `familiar`."""
    return keep_reason(c, p, cfg) is None


def popularity(rank: int | None) -> float:
    return min(1.0, math.log10(rank + 1) / 6) if rank else 0.0


def score_parts(c: Cand, weights: dict[str, float], profile: Profile | None = None) -> dict[str, Any]:
    """The score and what it is made of: the generators' Σ weight / log2(position + 2), the closeness by sound
    (1.5·m·sim for v2, 1.5·sim for v1), the popularity, v2's penalties of a negative track and artist; `maturity` —
    the v2 profile's m (none for v1). `total` is added up in the order `score` always did."""
    contributions = {g: weights.get(g, 0.5) / math.log2(pos + 2) for g, pos in c.gens.items()}
    gens = sum(contributions.values())
    pop = POP_WEIGHT * popularity(c.track.rank)
    if isinstance(profile, V2Profile):
        t = profile.find(ref=c.track.ref, nd_song_id=c.nd_song_id, fp=fingerprint(c.track.artist, c.track.title))
        track_penalty = .5 * max(t.score, -4) if t and t.score < 0 else 0
        artist_score = profile.artists.get(c.artist, 0)
        artist_penalty = .3 * max(artist_score, -3) if artist_score < 0 else 0
        sound = SIM_WEIGHT * profile.maturity * (c.sim or 0)
        total = gens + sound + pop + track_penalty + artist_penalty
        maturity: float | None = profile.maturity
    else:
        track_penalty = artist_penalty = 0
        sound = SIM_WEIGHT * (c.sim or 0.0)
        total = gens + sound + pop
        maturity = None
    return {"generators": gens, "sound": sound, "popularity": pop, "track_penalty": track_penalty,
            "artist_penalty": artist_penalty, "maturity": maturity, "total": total,
            "generator_weights": {g: weights.get(g, 0.5) for g in c.gens},
            "generator_contributions": contributions}


def score(c: Cand, weights: dict[str, float], profile: Profile | None = None) -> float:
    return score_parts(c, weights, profile)["total"]


def apply_score(c: Cand, weights: dict[str, float], profile: Profile | None, multiplier: float = 1.0) -> None:
    """Store the exact ranked inputs; reducing priority must also demote negative scores."""
    c.parts = score_parts(c, weights, profile)
    raw = c.parts["total"]
    c.score = raw * multiplier if raw >= 0 else raw / multiplier
    c.parts.update(temporary_multiplier=multiplier, total=c.score)


def known_of(c: Cand, p: Profile) -> dict[str, Any] | None:
    """What the profile knew of a candidate when the mix was built (none: an unknown track)."""
    t = c.track
    known = p.find(ref=t.ref if t.source == "dz" else None, nd_song_id=c.nd_song_id, fp=fingerprint(t.artist, t.title))
    if known is None:
        return None
    v2 = isinstance(p, V2Profile)
    return {"key": known.key, "s": known.score, "positive": known.positive_raw if v2 else None,
            "negative": known.negative if v2 else None, "artist_score": p.artists.get(c.artist),
            "familiar": known.familiar if v2 else True}


# --- the audio term ----------------------------------------------------------------------------------------------


async def library_items() -> list[dict[str, str]]:
    async with session_scope() as s:
        rows = (await s.execute(select(Track.nd_song_id, Track.path).where(Track.nd_song_id.is_not(None)))).all()
    return [{"key": embedder.song_key(song), "path": path} for song, path in rows]


async def _preview(c: CatalogTrack) -> str | None:
    if c.source != "dz" or svc.deezer is None:
        return None
    try:
        return await svc.deezer.preview_url(c.id)
    except Exception:
        return None


async def sound(p: Profile, cands: list[Cand], stats: dict[str, Any]) -> dict[str, list[float]]:
    """`sim` of the candidates (the embedder: closeness to the taste by sound). New candidates and catalog songs of
    the profile are embedded from their Deezer previews first; the build waits for them `mix_embed_wait_s`.
    Returns the vectors of the candidates (for the order). The service down — no audio term."""
    if isinstance(p, V2Profile):
        return await _sound_v2(p, cands, stats)
    if not embedder.configured():
        stats["embedder"] = "off"
        return {}
    st = get_settings()
    t0 = time.monotonic()
    profile = p.vector_profile()
    try:
        async with embedder.EmbedderClient() as e:
            await e.embed(await library_items())
            keys = [c.key for c in cands]
            sim = await e.similarity(keys, profile)
            need = [k for k in profile if k in sim.missing and not k.startswith("nd:")]
            todo = [c for c in cands if c.key in sim.missing and c.track.source == "dz"]
            by_key = {t.key: t for t in p.tracks.values()}
            urls = await asyncio.gather(*(_preview(c.track) for c in todo),
                                        *(_preview(by_key[k].track) if by_key[k].track else _none() for k in need))
            items = [{"key": c.key, "url": u} for c, u in zip(todo, urls[:len(todo)], strict=True) if u]
            items += [{"key": k, "url": u} for k, u in zip(need, urls[len(todo):], strict=True) if u]
            if items:
                await e.embed(items)
                waiting = {x["key"] for x in items}
                deadline = time.monotonic() + st.mix_embed_wait_s
                while waiting & sim.missing and time.monotonic() < deadline:
                    await asyncio.sleep(st.mix_poll_s)
                    sim = await e.similarity(keys, profile)
            for c in cands:
                c.sim = sim.scores.get(c.key)
            vectors = await e.vectors(keys)
    except embedder.EmbedderDown as e:
        log.warning("mix: embedder unavailable, no audio term: %s", e)
        stats["embedder"] = f"down: {e}"[:300]
        return {}
    stats["embedder"] = {"scored": sum(c.sim is not None for c in cands), "embedded": len(items),
                         "seconds": round(time.monotonic() - t0, 1)}
    return vectors


async def _sound_v2(p: V2Profile, cands: list[Cand], stats: dict[str, Any]) -> dict[str, list[float]]:
    from app.mixes.taste_select import facet_names, geometry_scores

    if embedder.configured():
        try:
            async with embedder.EmbedderClient() as client:
                await client.embed(await library_items())
        except embedder.EmbedderDown:
            pass
    vectors = await geometry_scores(p, cands)
    todo = [c for c in cands if c.sim is None and c.track.source == "dz"]
    urls = await asyncio.gather(*(_preview(c.track) for c in todo)) if p.geometry else []
    items = [{"key": c.key, "url": url} for c, url in zip(todo, urls, strict=False) if url]
    if items:
        try:
            async with embedder.EmbedderClient() as client:
                await client.embed(items)
            settings = get_settings()
            deadline = time.monotonic() + settings.mix_embed_wait_s
            while time.monotonic() < deadline:
                await asyncio.sleep(settings.mix_poll_s)
                vectors = await geometry_scores(p, cands)
                if all(c.sim is not None for c in todo):
                    break
        except embedder.EmbedderDown:
            pass
    stats["embedder"] = {"space_id": p.geometry.space_id, "model": p.geometry.model} if p.geometry else "down/off"
    stats["facets"], stats["maturity"] = facet_names(p), p.maturity
    return vectors


async def _none() -> None:
    return None


# --- assembling -----------------------------------------------------------------------------------------------


def _take(pool: list[Cand], n: int, per_artist: dict[str, int], cap: int) -> list[Cand]:
    """Best first; an artist's next song is worth less, never more than `cap` of an artist. `pool` is sorted by
    score, best first."""
    out: list[Cand] = []
    while len(out) < n:
        best, best_v = None, -math.inf
        for c in pool:
            if c.score <= best_v:
                break  # the penalty only lowers: nothing further on can win
            k = per_artist.get(c.artist, 0)
            if k >= cap:
                continue
            v = c.score - ARTIST_PENALTY * k
            if v > best_v:
                best, best_v = c, v
        if best is None:
            break
        pool.remove(best)
        per_artist[best.artist] = per_artist.get(best.artist, 0) + 1
        out.append(best)
    return out


def select_songs(cands: list[Cand], mode: str, size: int, reserve: int, cap: int) -> tuple[list[Cand], list[Cand]]:
    """The mix's songs by the mode's shares (a pool that runs short is made up by the other) and the reserve."""
    fam = sorted((c for c in cands if c.familiar), key=lambda c: -c.score)
    new = sorted((c for c in cands if not c.familiar), key=lambda c: -c.score)
    n_fam = round(size * MODES.get(mode, MODES["normal"]))
    per: dict[str, int] = {}
    a_fam = _take(fam, n_fam, per, cap)
    a_new = _take(new, size - len(a_fam), per, cap)
    a_fam += _take(fam, size - len(a_fam) - len(a_new), per, cap)
    r_fam = _take(fam, round(reserve * MODES.get(mode, MODES["normal"])), per, cap)
    r_new = _take(new, reserve - len(r_fam), per, cap)
    r_fam += _take(fam, reserve - len(r_fam) - len(r_new), per, cap)
    return a_fam + a_new, sorted(r_fam + r_new, key=lambda c: -c.score)


def _cos(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b:
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=False))


def order(songs: list[Cand], vectors: dict[str, list[float]]) -> list[Cand]:
    """A strong familiar song first; the shares interleaved evenly; of the next few best of a pool the one closest
    by sound to the previous song; the same artist never twice in a row (when there is a choice)."""
    fam = sorted((c for c in songs if c.familiar), key=lambda c: -c.score)
    new = sorted((c for c in songs if not c.familiar), key=lambda c: -c.score)
    out: list[Cand] = []
    share = len(fam) / len(songs) if songs else 0
    placed = 0  # familiar songs placed so far
    while fam or new:
        # familiar when it keeps the familiar share so far at or under the mix's (the first song is familiar)
        want_fam = not out or placed + 1 <= share * (len(out) + 1) + 0.5
        pool = fam if (want_fam and fam) or not new else new
        prev = out[-1] if out else None
        window = pool[:ORDER_LOOKAHEAD]
        fresh = [c for c in window if prev is None or c.artist != prev.artist] or window
        if prev is not None and vectors.get(prev.key):
            pick = max(fresh, key=lambda c: _cos(vectors.get(c.key), vectors.get(prev.key)) + 0.01 * -fresh.index(c))
        else:
            pick = fresh[0]
        pool.remove(pick)
        out.append(pick)
        placed += pick.familiar
    return out


# --- name --------------------------------------------------------------------------------------------------------

NAME_TOOL = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Название микса по-русски, 2–6 слов, без кавычек и эмодзи"},
        "description": {"type": "string", "description": "Одно предложение по-русски: что в миксе, до 150 символов"},
    },
    "required": ["title", "description"],
}


def time_of_day(h: int) -> str:
    return "утро" if 5 <= h < 12 else "день" if 12 <= h < 17 else "вечер" if 17 <= h < 23 else "ночь"


def fallback_name(local: datetime, songs: list[Cand]) -> tuple[str, str]:
    artists = list(dict.fromkeys(c.track.artist for c in songs if c.track.artist))[:3]
    fam = sum(c.familiar for c in songs)
    desc = f"{', '.join(artists)} и другие: {fam} знакомых и {len(songs) - fam} новых песен" if artists else ""
    return f"Микс дня · {WEEKDAYS[local.weekday()]}", desc


async def name_mix(user_id: int, songs: list[Cand], local: datetime, stats: dict[str, Any]) -> tuple[str, str]:
    """A daylist-like name by an LLM from the songs' tags (Last.fm), the weekday and the time; no AI — a template."""
    from app.ai import llm

    title, desc = fallback_name(local, songs)
    if not llm.configured():
        return title, desc
    counts: dict[str, int] = {}
    for c in songs:
        counts[c.track.artist] = counts.get(c.track.artist, 0) + 1
    top = [a for a, _ in sorted(counts.items(), key=lambda x: -x[1])][:6]
    tags: list[str] = []
    if sources.lastfm_configured():
        try:
            for row in await asyncio.gather(*(sources.lastfm_artist_tags(a) for a in top[:5])):
                tags += row
        except sources.SourceDown as e:
            log.info("mix name: tags: %s", e)
    tags = [t for t, _ in sorted({t.lower(): tags.count(t) for t in tags}.items(), key=lambda x: -x[1])][:10]
    fam = sum(c.familiar for c in songs)
    prompt = (f"День: {WEEKDAYS[local.weekday()]}, время: {time_of_day(local.hour)}.\n"
              f"Исполнители: {', '.join(top)}.\nТеги Last.fm: {', '.join(tags) or 'нет'}.\n"
              f"Знакомых песен {fam}, новых {len(songs) - fam}.")
    try:
        async with llm.LLMClient() as client:
            out, usage = await client.call_tool(
                system=("Ты придумываешь название ежедневному музыкальному миксу в духе Spotify daylist: жанр или "
                        "настроение + день недели или время суток, например «Ночной русский пост-панк на "
                        f"{WEEKDAYS_ACC[4]}». По-русски, живо, без кавычек и эмодзи."),
                user_content=prompt, tool_name="mix_name", tool_description="Название и описание микса дня",
                parameters=NAME_TOOL, max_tokens=300)
    except Exception as e:
        log.info("mix name: llm failed: %r", e)
        return title, desc
    from app.models import AiUsage

    async with session_scope() as s:
        s.add(AiUsage(user_id=user_id, kind="mix", tokens_in=usage.tokens_in, tokens_out=usage.tokens_out,
                      cost_usd=usage.cost_usd))
    stats["name_cost_usd"] = usage.cost_usd
    t, d = str(out.get("title") or "").strip()[:120], str(out.get("description") or "").strip()[:300]
    return (t or title), (d or desc)


# --- all together -------------------------------------------------------------------------------------------------


def local_now(now: datetime | None = None) -> datetime:
    """Users' local time (`Settings.timezone`), naive."""
    from datetime import UTC

    utc = (now or utcnow()).replace(tzinfo=UTC)
    try:
        return utc.astimezone(ZoneInfo(get_settings().timezone)).replace(tzinfo=None)
    except Exception:  # no tz database (Windows without tzdata): the machine's own zone
        return utc.astimezone().replace(tzinfo=None)


async def build(p: Profile, mode: str, gens: dict[str, Gen] | None = None,
                shadow_pool: list[Cand] | None = None) -> Built:
    cfg = get_settings().cfg.mixes
    from app.recommendation_policy import load_policy
    from app.recommendations import adapted_weights, explain_candidate

    weights = await adapted_weights(p.user_id, cfg.weights)
    policy = await load_policy(p.user_id)
    t0 = time.monotonic()
    stats: dict[str, Any] = {"mode": mode, "taste_model": "v2" if isinstance(p, V2Profile) else "v1"}
    stats["source_weights"] = weights
    if isinstance(p, V2Profile):
        from app.mixes.taste_select import prepare
        from app.mixes.taste_select import seeds as select_seeds
        await prepare(p)
        seeds = await select_seeds(p, cfg.seeds)
    else:
        seeds = p.seeds(cfg.seeds) or await cold_seeds(p, cfg.seeds)
    stats["seeds"] = [f"{t.artist} — {t.title}" for t in seeds]
    stats["seed_keys"] = [t.key for t in seeds]
    results = await run_generators(gens if gens is not None else generators(p, seeds))
    cands = merge(results)
    await link_library(cands)
    if shadow_pool is not None:
        try:
            from copy import deepcopy
            shadow_pool.extend(deepcopy(list(cands.values())[:500]))
        except Exception:
            shadow_pool.append(None)  # comparison reports failure without affecting the build
    rejected = dict.fromkeys(REJECT_REASONS, 0)
    kept = []
    for c in cands.values():
        reason = "temporary_pause" if policy.blocked(c) else keep_reason(c, p, cfg)
        if reason is None:
            kept.append(c)
        else:
            rejected[reason] = rejected.get(reason, 0) + 1
    stats["rejected"] = rejected
    for c in kept:
        apply_score(c, weights, p, policy.multiplier(c))
    kept.sort(key=lambda c: -c.score)
    vectors = await sound(p, kept[:EMBED_CANDIDATES], stats)
    for c in kept:
        apply_score(c, weights, p, policy.multiplier(c))
    active, reserve = select_songs(kept, mode, cfg.size, cfg.reserve, cfg.max_per_artist)
    active = order(active, vectors)
    for c in active + reserve:
        c.known = known_of(c, p)
        c.explanation = explain_candidate(c)
    used: dict[str, int] = {}
    for c in active:
        used[c.generator] = used.get(c.generator, 0) + 1
    stats["generators"] = {r.name: {"tracks": len(r.tracks), "used": used.get(r.name, 0), "seconds": r.seconds,
                                    **({"error": r.error} if r.error else {}), **({"off": True} if r.off else {})}
                           for r in results}
    stats["candidates"], stats["kept"] = len(cands), len(kept)
    stats["familiar"] = sum(c.familiar for c in active)
    title, description = await name_mix(p.user_id, active, local_now(p.now), stats)
    stats["build_seconds"] = round(time.monotonic() - t0, 1)
    return Built(active=active, reserve=reserve, title=title, description=description, stats=stats)


__all__ = ["GENERATOR_TITLES", "MODES", "MODE_TITLES", "Built", "Cand", "GenResult", "build", "local_now"]
