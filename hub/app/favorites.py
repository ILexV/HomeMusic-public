"""Typed favorite decisions, causal rollback, verified aliases and Navidrome reconciliation.

The journal is the source of own state. Imported stars remain display-only provenance.
No scoring window is applied when folding state; consumers decide how old a contribution may be.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import FavoriteTarget
from app.models import (
    Album,
    DownloadItem,
    FavoriteSubject,
    Import,
    ImportCollection,
    ImportItem,
    NavidromeSync,
    PlayEvent,
    Track,
    User,
    utcnow,
)

log = logging.getLogger(__name__)
ND_FIELDS = {"track": "nd_song_id", "album": "nd_album_id", "artist": "nd_artist_id"}
ND_LISTS = {"track": "song", "album": "album", "artist": "artist"}
GRACE = timedelta(hours=24)


def decision_id(operation: str, target_type: str, key: str) -> str:
    return "fav:" + hashlib.sha256(f"{operation}\0{target_type}\0{key}".encode()).hexdigest()[:48]


def subject_data(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Keep the shared directory small even after many different client snapshots."""
    raw = raw or {}
    data = {k: str(raw[k])[:512] for k in ("name", "title", "artist", "artist_ref", "album_ref",
                                         "album_title", "cover_url", "cover_small_url") if raw.get(k)}
    for k in ("duration", "duration_s"):
        if isinstance(raw.get(k), (int, float)):
            data[k] = raw[k]
    if isinstance(raw.get("track_refs"), list):
        data["track_refs"] = [x[:64] for x in raw["track_refs"][:100] if isinstance(x, str)]
    return data


def aliases(obj: Any) -> set[str]:
    typ = obj.target_type or "track"
    nd = getattr(obj, ND_FIELDS[typ], None)
    return {x for x in (f"ref:{obj.ref}" if obj.ref else None, f"nd:{nd}" if nd else None) if x}


class AliasIndex:
    """Union of explicit ref/ND pairs and persisted verified aliases, isolated by type."""

    def __init__(self, events: Iterable[Any] = (), subjects: Iterable[FavoriteSubject] = ()):
        self.parent: dict[tuple[str, str], tuple[str, str]] = {}
        for subject in subjects:
            self.join(subject.target_type, set(subject.aliases or []))
        for ev in events:
            self.join(ev.target_type or "track", aliases(ev))

    def root(self, key: tuple[str, str]) -> tuple[str, str]:
        p = self.parent.setdefault(key, key)
        if p != key:
            self.parent[key] = self.root(p)
        return self.parent[key]

    def join(self, typ: str, keys: set[str]):
        roots = sorted({self.root((typ, k)) for k in keys})
        if roots:
            for r in roots[1:]:
                self.parent[r] = roots[0]

    def key(self, obj: Any) -> tuple[str, str]:
        return self.root((obj.target_type or "track", min(aliases(obj))))


@dataclass
class State:
    target_type: str
    key: str
    liked: bool
    since: datetime | None
    event: PlayEvent  # latest effective independent decision
    received_at: datetime  # latest arrival including causal compensation
    ref: str | None = None
    nd_id: str | None = None
    data: dict[str, Any] | None = None
    aliases: list[str] = field(default_factory=list)


def causal_activity(rows: list[PlayEvent], index: AliasIndex):
    """Resolve a functional causal graph iteratively, including untrusted cycles/long chains.

    Only chains ending at a same-user, same-subject independent decision have an effect.
    A node is active iff none of its active retractors remain; parallel retractors are OR.
    """
    from collections import deque

    by_id = {(e.user_id, e.event_id): e for e in rows}
    parents = {}
    for key, e in by_id.items():
        target = (e.user_id, e.reverts_event_id)
        if e.reverts_event_id and target in by_id and index.key(e) == index.key(by_id[target]):
            parents[key] = target
    roots = {}
    for start in by_id:
        path, visiting = [], set()
        key = start
        while key not in roots and key not in visiting:
            visiting.add(key)
            path.append(key)
            e = by_id[key]
            if not e.reverts_event_id:
                roots[key] = key
                break
            if key not in parents:
                roots[key] = None
                break
            key = parents[key]
        root = roots.get(key)  # A cycle (and every chain into it) is inert.
        for key in path:
            roots[key] = root
    remaining = {key: 0 for key, root in roots.items() if root is not None}
    for child, parent in parents.items():
        if child in remaining:
            remaining[parent] += 1
    queue = deque(key for key, count in remaining.items() if not count)
    active, blocked = set(), set()
    while queue:
        key = queue.popleft()
        if key not in blocked:
            active.add(key)
        if key in parents:
            parent = parents[key]
            if key in active:
                blocked.add(parent)
            remaining[parent] -= 1
            if not remaining[parent]:
                queue.append(parent)
    return active, roots


def _decision_inputs(events, subjects, now):
    """Shared causal/redundancy resolution for current state and activity consumers."""
    rows = list({(e.user_id, e.event_id): e for e in events
                 if e.kind in ("like", "unlike") and (now is None or e.ts <= now)}.values())
    index = AliasIndex(rows, subjects)
    active, roots = causal_activity(rows, index)
    def identity(e):
        return e.user_id, e.event_id
    ordered = sorted(rows, key=lambda e: (e.ts, e.received_at, e.id or 0, e.event_id))
    last_kind: dict[tuple[int, tuple[str, str]], str] = {}
    series: dict[tuple[int, tuple[str, str]], tuple[int, str]] = {}
    redundant = set()
    for e in ordered:
        if e.reverts_event_id:
            key = (e.user_id, index.key(e))
            if roots[identity(e)] == (e.user_id, e.reverts_event_id) and series.get(key) == (e.user_id, e.reverts_event_id):
                last_kind.pop(key, None)
                series.pop(key, None)
            continue
        key = (e.user_id, index.key(e))
        if last_kind.get(key) == e.kind:
            redundant.add(identity(e))
        else:
            series[key] = identity(e)
        last_kind[key] = e.kind
    return rows, index, active, roots, ordered, redundant


def effective_decisions(events: Iterable[PlayEvent], subjects: Iterable[FavoriteSubject] = (),
                        now: datetime | None = None) -> list[PlayEvent]:
    """Effective independent rows only; causal nodes and legacy redundant retries are not actions."""
    _rows, _index, active, _roots, ordered, redundant = _decision_inputs(events, list(subjects), now)
    return [e for e in ordered if not e.reverts_event_id and
            (e.user_id, e.event_id) in active and (e.user_id, e.event_id) not in redundant]


def fold(events: Iterable[PlayEvent], subjects: Iterable[FavoriteSubject] = (),
         now: datetime | None = None) -> dict[tuple[str, str], State]:
    """Pure fold. Rollbacks remove their exact decision, even if delivered before it.

    Repeated states don't renew `since`. A rollback never becomes an independent opposite decision.
    Its target is checked as well as its id, so an unrelated object cannot be rolled back.
    """
    subjects = list(subjects)
    rows, index, active, roots, ordered, redundant = _decision_inputs(events, subjects, now)
    def identity(e):
        return e.user_id, e.event_id
    arrivals: dict[tuple[str, str], datetime] = {}
    for e in rows:
        if identity(e) in redundant or (e.reverts_event_id and
                (roots[identity(e)] is None or roots[identity(e)] in redundant)):
            continue
        k = index.key(e)
        arrivals[k] = max(arrivals.get(k, e.received_at), e.received_at)
    out: dict[tuple[str, str], State] = {}
    for e in ordered:
        if e.reverts_event_id or identity(e) not in active or identity(e) in redundant:
            continue
        k = index.key(e)
        prev = out.get(k)
        liked = e.kind == "like"
        since = e.ts if liked else None
        out[k] = State(*k, liked, since, e, arrivals[k],
                       e.ref or (prev.ref if prev else None),
                       getattr(e, ND_FIELDS[k[0]]) or (prev.nd_id if prev else None),
                       {**(prev.data or {} if prev else {}), **(e.target_data or {})})
    # A rolled-back first like still has a neutral state and must not bootstrap from ND as a new like.
    for e in reversed(ordered):
        k = index.key(e)
        if k not in out and not e.reverts_event_id and identity(e) not in redundant:
            out[k] = State(*k, False, None, e, arrivals[k], e.ref, getattr(e, ND_FIELDS[k[0]]), e.target_data)
    for subject in subjects:
        k = index.root((subject.target_type, min(subject.aliases))) if subject.aliases else None
        if k in out:
            st = out[k]
            st.ref, st.nd_id = subject.ref or st.ref, subject.nd_id or st.nd_id
            st.data = {**(subject.data or {}), **(st.data or {})}
    for key, st in out.items():
        st.aliases = sorted(alias for typ, alias in index.parent if typ == st.target_type and
                            index.root((typ, alias)) == key)
    return out


async def snapshot(s: AsyncSession, user_id: int, now: datetime | None = None):
    now = now or utcnow()
    q = select(PlayEvent).where(PlayEvent.user_id == user_id, PlayEvent.kind.in_(("like", "unlike")))
    q = q.where(PlayEvent.ts <= now)
    events = list((await s.execute(q)).scalars())
    subjects = list((await s.execute(select(FavoriteSubject))).scalars())
    partial = [(e.ref, e.nd_song_id) for e in events if e.target_type == "track" and not (e.ref and e.nd_song_id)]
    if partial:
        from app.plays import Ids
        fill = Ids(s)
        await fill.prime(partial)
        for ref, nd in set(partial):
            ref, nd = await fill.complete(ref, nd)
            if ref and nd:
                subjects.append(FavoriteSubject(target_type="track", ref=ref, nd_id=nd,
                    aliases=[f"ref:{ref}", f"nd:{nd}"], canonical_key=f"ref:{ref}", data={}))
    return fold(events, subjects, now)


async def remember(s: AsyncSession, target: FavoriteTarget, *, extra_aliases: Iterable[str] = ()) -> FavoriteSubject:
    typ = target.target_type
    keys = aliases(target) | set(extra_aliases)
    # Usually one indexed hit; JSON aliases also retain replaced ND song ids.
    subjects = list((await s.execute(select(FavoriteSubject).where(FavoriteSubject.target_type == typ))).scalars())
    matches = [x for x in subjects if keys.intersection(x.aliases or [])]
    data: dict[str, Any] = {}
    for x in matches:
        keys.update(x.aliases or [])
        data.update(x.data or {})
    data = subject_data({**data, **(target.target_data or {})})
    if matches:
        row = matches[0]
        for other in matches[1:]:
            row.ref, row.nd_id = row.ref or other.ref, row.nd_id or other.nd_id
            await s.delete(other)
    else:
        row = FavoriteSubject(target_type=typ, canonical_key=min(keys), aliases=[], data={})
        s.add(row)
    if target.ref and (not row.ref or target.ref.startswith("dz:") or row.ref.startswith("file:")):
        row.ref = target.ref
    # A snapshot can still contain an old replaced song: observing that alias alone
    # must not move the canonical library id back from the verified replacement.
    if nd_id := getattr(target, ND_FIELDS[typ]):
        if not row.nd_id or target.ref or extra_aliases:
            row.nd_id = nd_id
    row.aliases, row.data, row.enriched_at = sorted(keys), data, utcnow()
    await s.flush()
    return row


def analyze_liked_track(user_id: int, subject: FavoriteSubject) -> None:
    if subject.target_type == "track" and subject.ref and not subject.nd_id:
        from app.mixes.heard_audio import schedule

        schedule(user_id, subject.ref)


async def record(s: AsyncSession, user_id: int, target: FavoriteTarget, liked: bool, *,
                 operation: str | None = None, ts: datetime | None = None, client: str = "hub",
                 context: str | None = None, reverts_event_id: str | None = None,
                 mirror_pending: bool = False, normalize: bool = True, event_id: str | None = None) -> PlayEvent:
    from app.plays import Ids, event_time

    target = target.model_copy()
    if target.target_type == "track":
        target.ref, target.nd_song_id = await Ids(s).complete(target.ref, target.nd_song_id)
    row = await remember(s, target)
    eid = event_id or decision_id(operation or uuid.uuid4().hex, target.target_type, row.canonical_key)
    now = utcnow()
    inserted = await s.execute(insert(PlayEvent).values(
        **target.model_dump(), user_id=user_id, event_id=eid, ts=event_time(ts or now, now) if normalize else ts or now,
        kind="like" if liked else "unlike", client=client, context=context, received_at=now,
        reverts_event_id=reverts_event_id, mirror_pending=mirror_pending).on_conflict_do_nothing())
    if inserted.rowcount and liked and not reverts_event_id:
        analyze_liked_track(user_id, row)
    return (await s.execute(select(PlayEvent).where(PlayEvent.user_id == user_id, PlayEvent.event_id == eid))).scalar_one()


def target_of(state: State) -> FavoriteTarget:
    return FavoriteTarget(target_type=state.target_type, ref=state.ref,
                          **{ND_FIELDS[state.target_type]: state.nd_id}, target_data=state.data)


async def command(s: AsyncSession, user: User, targets: list[FavoriteTarget], *, liked: bool,
                  operation: str | None = None, ts: datetime | None = None, client: str = "pwa",
                  context: str | None = None, nd=None) -> list[PlayEvent]:
    """Each successful chunk is journaled immediately; callers may commit successes before reporting failure."""
    from app.auth import user_navidrome

    own_nd = nd is None
    nd = nd or user_navidrome(user)
    operation = operation or uuid.uuid4().hex
    events = []
    try:
        # Chunk by type so a partial network failure never claims unperformed objects.
        for typ in ND_FIELDS:
            group = [t.model_copy() for t in targets if t.target_type == typ]
            event_ids = {id(t): operation if len(targets) == 1 else decision_id(operation, typ, min(aliases(t)))
                         for t in group}
            for t in group:
                if typ == "track":
                    from app.plays import Ids
                    t.ref, t.nd_song_id = await Ids(s).complete(t.ref, t.nd_song_id)
            active = []
            for target in group:
                subject = await remember(s, target)
                if not getattr(target, ND_FIELDS[typ]) and subject.nd_id:
                    setattr(target, ND_FIELDS[typ], subject.nd_id)
                eid = event_ids[id(target)]
                previous = (await s.execute(select(PlayEvent).where(
                    PlayEvent.user_id == user.id, PlayEvent.event_id == eid))).scalar_one_or_none()
                if previous:
                    events.append(previous)
                    continue  # Retried old commands must not undo a newer decision in Navidrome.
                from app.plays import event_time
                at = event_time(ts or utcnow())
                states = await snapshot(s, user.id)
                current = next((x for x in states.values() if x.target_type == typ and
                    ((x.ref and x.ref == target.ref) or (x.nd_id and x.nd_id == getattr(target, ND_FIELDS[typ])))), None)
                if current and current.event.ts > at:
                    events.append(await record(s, user.id, target, liked, ts=at, event_id=eid,
                                               client=client, context=context))
                    continue  # Delayed commands enter history without overwriting the live mirror.
                active.append(target)
            group = active
            for start in range(0, len(group), 200):
                chunk = group[start:start + 200]
                ids = list(dict.fromkeys(getattr(t, ND_FIELDS[typ]) for t in chunk if getattr(t, ND_FIELDS[typ])))
                if ids:
                    await (nd.star if liked else nd.unstar)(**{typ + "_ids" if typ != "track" else "song_ids": ids})
                for target in chunk:
                    events.append(await record(s, user.id, target, liked, operation=operation, ts=ts,
                                               client=client, context=context,
                                               event_id=event_ids[id(target)],
                                               mirror_pending=not bool(getattr(target, ND_FIELDS[typ]))))
                    if not liked and typ == "track" and target.ref:
                        await clear_pending(s, user.id, target.ref)
    finally:
        if own_nd:
            await nd.aclose()
    return events


async def clear_pending(s: AsyncSession, user_id: int, ref: str):
    from app.imports.apply import unlike_refs
    from app.jobs.downloads import item_ref

    await unlike_refs(s, user_id, [ref])
    items = (await s.execute(select(DownloadItem).where(DownloadItem.user_id == user_id))).scalars()
    for item in items:
        if item_ref(item) != ref:
            continue
        q = dict(item.query_json or {})
        was_star_only = q.get("star") and not q.get("requested") and not any(q.get(k) for k in ("import", "mix", "wave"))
        q.update(star=False, star_cancelled=True)
        item.query_json = q
        if was_star_only and item.status == "queued":
            item.status = "cancelled"
    await s.flush()


async def download_requested(s: AsyncSession, item: DownloadItem):
    from app.jobs.downloads import item_ref
    ref = item_ref(item)
    if not ref:
        return
    event = await record(s, item.user_id, FavoriteTarget(ref=ref, target_data=item.query_json.get("query")), True,
                         operation=f"download:{item.id}:{uuid.uuid4().hex}",
                         client="assistant" if item.query_json.get("favorite_client") == "assistant" else "hub",
                         context=item.query_json.get("favorite_context") or "download", mirror_pending=True)
    item.query_json = {**item.query_json, "star_event_id": event.event_id, "star_cancelled": False, "star_failed": False}


async def download_failed(s: AsyncSession, item: DownloadItem):
    q = item.query_json or {}
    if not q.get("star") or q.get("star_cancelled") or not q.get("star_event_id"):
        return
    original = (await s.execute(select(PlayEvent).where(PlayEvent.user_id == item.user_id,
                                                       PlayEvent.event_id == q["star_event_id"]))).scalar_one_or_none()
    if original:
        await record(s, item.user_id, FavoriteTarget(ref=original.ref, nd_song_id=original.nd_song_id), False,
                     operation=f"failed:{original.event_id}", reverts_event_id=original.event_id,
                     client="hub", context="download:failed")
        item.query_json = {**q, "star_failed": True}


async def imported_targets(s: AsyncSession, user_id: int) -> set[tuple[str, str]]:
    """All known import provenance, including old album/artist imports conservatively matched by catalog ref."""
    keys = set()
    imports = list((await s.execute(select(Import).where(Import.user_id == user_id))).scalars())
    for imp in imports:
        opts = imp.options_json or {}
        for typ, opt in (("track", "liked"), ("album", "liked_album_ids"), ("artist", "liked_artist_ids")):
            keys.update((typ, f"nd:{x}") for x in opts.get(opt, []))
    rows = (await s.execute(select(ImportItem.ref, ImportCollection.kind).join(
        ImportCollection, ImportCollection.id == ImportItem.collection_id).where(
        ImportItem.import_id.in_([i.id for i in imports]), ImportCollection.selected.is_(True),
        ImportCollection.kind.in_(("likes", "albums", "artists")), ImportItem.ref.is_not(None)))).all()
    kinds = {"likes": "track", "albums": "album", "artists": "artist"}
    keys.update((kinds[k], f"ref:{r}") for r, k in rows)
    return keys


def parse_time(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt
    except (ValueError, TypeError):
        return None


async def reconcile(s: AsyncSession, user: User, body: dict, *, now: datetime | None = None, nd=None) -> int:
    """A complete, successful getStarred2 snapshot; callers must not pass transport failures as {}."""
    now = now or utcnow()
    if not isinstance(body, dict) or any(k in body and not isinstance(body[k], list) for k in ND_LISTS.values()):
        raise ValueError("incomplete starred2 snapshot")
    before = await snapshot(s, user.id, now)
    provenance = await imported_targets(s, user.id)
    # Old imports did not persist verified ND album/artist ids. Unknown objects of such
    # a type are quarantined until verified enrichment or a real own decision, never guessed.
    subjects = list((await s.execute(select(FavoriteSubject))).scalars())
    index = AliasIndex([x.event for x in before.values()], subjects)
    verified_refs = {(x.target_type, f"ref:{x.ref}") for x in subjects if x.ref and x.nd_id}
    legacy_types = {typ for typ, key in provenance if typ != "track" and key.startswith("ref:") and
                    not any(index.root((typ, key)) == index.root(verified) for verified in verified_refs)}
    imported = {index.root(k) for k in provenance}
    have: dict[tuple[str, str], FavoriteTarget] = {}
    dates = {}
    for typ, list_name in ND_LISTS.items():
        for obj in body.get(list_name, []):
            if not isinstance(obj, dict) or not obj.get("id"):
                raise ValueError("incomplete starred2 object")
            target = FavoriteTarget(target_type=typ, **{ND_FIELDS[typ]: str(obj["id"])},
                                    target_data={k: obj[k] for k in ("name", "artist", "title", "duration") if k in obj})
            key = index.key(target)
            have[key], dates[key] = target, parse_time(obj.get("starred"))
            await remember(s, target)
    changed = 0
    for key in sorted(before.keys() | have.keys()):
        state, target = before.get(key), have.get(key)
        if state and state.event.mirror_pending:
            # A failed own delivery is retried, never interpreted as an outside unstar.
            if nd is not None and state.nd_id:
                await (nd.star if state.liked else nd.unstar)(**{
                    "song_ids" if state.target_type == "track" else state.target_type + "_ids": [state.nd_id]})
                state.event.mirror_pending = False
            continue
        if state and now - state.received_at < GRACE:
            continue
        if target and state is None and (key in imported or key[0] in legacy_types):
            continue
        if state is None and target:
            at = min(dates.get(key) or now, now)
            await record(s, user.id, target, True, ts=at, normalize=False,
                         operation=f"ndbaseline:{key}", client="navidrome", context="favorite:bootstrap")
            changed += 1
        elif state and bool(target) != state.liked and state.nd_id:
            # A known unstar is not overwritten by the same stale baseline on restart.
            at = dates.get(key) if target else now
            if target and (at is None or at <= state.event.ts):
                continue  # A pre-existing ND star cannot resurrect a known own unlike.
            at = at if at and state.event.ts < at <= now else now
            await record(s, user.id, target or target_of(state), bool(target), ts=at, normalize=False,
                         operation=f"ndsync:{state.event.event_id}:{int(bool(target))}", client="navidrome",
                         context="favorite:reconcile")
            changed += 1
    sync = await s.get(NavidromeSync, user.id)
    if sync is None:
        sync = NavidromeSync(user_id=user.id, last_scrobble_id=0)
        s.add(sync)
    sync.favorites_synced_at = now
    sync.favorites_bootstrapped_at = sync.favorites_bootstrapped_at or now
    return changed


async def sync_user(user: User) -> int:
    from app.auth import user_navidrome
    from app.db import session_scope

    nd = user_navidrome(user)
    try:
        body = await nd.get_starred2()
        async with session_scope() as s:
            await enrich_pending_subjects(s, nd)
            await recover_import_subjects(s, user.id, body, nd)
            return await reconcile(s, user, body, nd=nd)
    finally:
        await nd.aclose()


async def enrich_pending_subjects(s: AsyncSession, nd) -> None:
    """Verified late binding of catalog album/artist decisions when their library objects arrive."""
    from app.catalog.models import split_ref
    from app.matching.normalize import normalize_artist

    rows = list((await s.execute(select(FavoriteSubject).where(FavoriteSubject.nd_id.is_(None),
                    FavoriteSubject.target_type.in_(("album", "artist"))))).scalars())
    for row in rows:
        try:
            nd_id = None
            if row.target_type == "album" and row.ref:
                src, id_ = split_ref(row.ref)
                if src != "dz":
                    continue
                song = (await s.execute(select(Track.nd_song_id).join(Album, Album.id == Track.album_id).where(
                    Album.deezer_album_id == id_, Track.nd_song_id.is_not(None)).limit(1))).scalar_one_or_none()
                if song:
                    nd_id = (await nd.get_song(song)).get("albumId")
            elif name := (row.data or {}).get("name"):
                found = (await nd.search3(name, song_count=0, artist_count=5)).get("artist", [])
                matches = [a for a in found if normalize_artist(a.get("name")).text == normalize_artist(name).text]
                if len(matches) == 1:
                    nd_id = matches[0].get("id")
            if nd_id:
                await remember(s, FavoriteTarget(target_type=row.target_type, ref=row.ref,
                    **{ND_FIELDS[row.target_type]: nd_id}, target_data=row.data))
        except Exception as exc:
            log.info("favorite subject %s/%s deferred: %r", row.target_type, row.ref, exc)


async def recover_import_subjects(s: AsyncSession, user_id: int, body: dict, nd) -> None:
    """Recover legacy import provenance through verified ND responses, never a guessed song-derived id."""
    from app.catalog.models import split_ref
    from app.matching.normalize import normalize_artist

    rows = (await s.execute(select(Import, ImportItem, ImportCollection.kind).join(
        ImportItem, ImportItem.import_id == Import.id).join(ImportCollection,
        ImportCollection.id == ImportItem.collection_id).where(Import.user_id == user_id,
        ImportCollection.selected.is_(True), ImportCollection.kind.in_(("albums", "artists")),
        ImportItem.ref.is_not(None)))).all()
    subjects = list((await s.execute(select(FavoriteSubject))).scalars())
    verified = {(x.target_type, x.ref) for x in subjects if x.ref and x.nd_id}
    for imp, item, kind in rows:
        typ = "album" if kind == "albums" else "artist"
        if (typ, item.ref) in verified:
            continue
        nd_id = None
        try:
            if typ == "album":
                src, catalog_id = split_ref(item.ref)
                if src != "dz":
                    continue
                song = (await s.execute(select(Track.nd_song_id).join(Album, Album.id == Track.album_id).where(
                    Album.deezer_album_id == catalog_id, Track.nd_song_id.is_not(None)).limit(1))).scalar_one_or_none()
                if song:
                    nd_id = (await nd.get_song(song)).get("albumId")
            else:
                name = (item.raw_json or {}).get("name")
                if not name:
                    continue
                found = (await nd.search3(name, song_count=0, artist_count=5)).get("artist", [])
                matches = [a for a in found if normalize_artist(a.get("name")).text == normalize_artist(name).text]
                if len(matches) == 1:
                    nd_id = matches[0].get("id")
            if not nd_id:
                continue
            await remember(s, FavoriteTarget(target_type=typ, ref=item.ref, **{ND_FIELDS[typ]: nd_id},
                                              target_data=item.raw_json))
            verified.add((typ, item.ref))
            # A verified relationship is metadata even when the imported star has already been removed.
            if any(obj.get("id") == nd_id for obj in body.get(ND_LISTS[typ], [])):
                opt = "liked_album_ids" if typ == "album" else "liked_artist_ids"
                opts = imp.options_json or {}
                imp.options_json = {**opts, opt: list(dict.fromkeys([*(opts.get(opt) or []), nd_id]))}
        except Exception as exc:
            log.info("favorite import metadata %s/%s deferred: %r", typ, item.ref, exc)
