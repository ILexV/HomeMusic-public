"""HTTP API of the embedder (MIXES.md, section 4.2). Reachable only inside the compose network."""

import asyncio
import contextlib
import hmac
import logging
import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from urllib.parse import urlparse

import httpx
import numpy as np
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, model_validator

from .audio import AudioError, library_path
from .config import Settings
from .db import Store
from .geometry import MAX_DIM, Geometry, facet_cosines, facets, fit_axes, project
from .geometry import similarity as similarity_v2
from .models import Model, load_model
from .search import Index, _center, score
from .worker import Engine, Worker

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

MAX_ITEMS = 5000


class EmbedItem(BaseModel):
    key: str = Field(min_length=1, max_length=300)
    url: str | None = None
    path: str | None = None

    @model_validator(mode="after")
    def _one_source(self):
        if bool(self.url) == bool(self.path):
            raise ValueError("give exactly one of url, path")
        return self


class EmbedIn(BaseModel):
    items: list[EmbedItem] = Field(max_length=MAX_ITEMS)
    # embed again even if a vector from the same source exists
    force: bool = False


class Rejected(BaseModel):
    key: str
    error: str


class EmbedOut(BaseModel):
    queued: int
    known: int
    rejected: list[Rejected]


class ItemOut(BaseModel):
    key: str
    status: str
    error: str | None
    attempts: int
    has_vector: bool
    seconds: float | None
    updated_at: float


class Weighted(BaseModel):
    key: str
    weight: float


class SimilarityIn(BaseModel):
    candidates: list[str] = Field(max_length=MAX_ITEMS)
    profile: list[Weighted] = Field(max_length=MAX_ITEMS)
    top: int = Field(3, ge=0, le=50)


class NearOut(BaseModel):
    key: str
    sim: float


class ScoredOut(BaseModel):
    key: str
    pos: float | None
    neg: float | None
    score: float
    nearest: list[NearOut]
    nearest_neg: NearOut | None


class SimilarityOut(BaseModel):
    model: str
    results: list[ScoredOut]
    missing: list[str]


class TextIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    limit: int = Field(20, ge=1, le=500)
    prefix: str | None = None
    keys: list[str] | None = Field(None, max_length=MAX_ITEMS * 10)


class TextOut(BaseModel):
    model: str
    results: list[NearOut]


class VectorsIn(BaseModel):
    keys: list[str] = Field(max_length=MAX_ITEMS)


class VectorsOut(BaseModel):
    model: str
    dim: int | None
    vectors: dict[str, list[float]]
    missing: list[str]


KeyV2 = Annotated[str, Field(min_length=1, max_length=300)]
Coordinate = Annotated[float, Field(ge=-1, le=1, allow_inf_nan=False)]


class PositiveV2(BaseModel):
    key: KeyV2
    weight: float = Field(ge=1, le=1_000_000, allow_inf_nan=False)


class ProfileInV2(BaseModel):
    positives: list[PositiveV2] = Field(max_length=MAX_ITEMS)
    negatives: list[KeyV2] = Field(default_factory=list, max_length=MAX_ITEMS)


class FacetV2(BaseModel):
    id: KeyV2
    weight: float = Field(gt=0, le=1, allow_inf_nan=False)
    members: list[KeyV2] = Field(max_length=MAX_ITEMS)
    center: list[Coordinate] = Field(min_length=1, max_length=MAX_DIM)


class SpaceOutV2(BaseModel):
    model: str
    space_id: str
    dim: int | None


class DiagnosticsV2(BaseModel):
    available: int
    zero_residual: list[str]
    invalid: list[str]


class ProfileOutV2(SpaceOutV2):
    facets: list[FacetV2]
    negatives: list[str]
    missing: list[str]
    diagnostics: DiagnosticsV2


class SimilarityInV2(BaseModel):
    space_id: str = Field(min_length=1, max_length=100)
    candidates: list[KeyV2] = Field(max_length=MAX_ITEMS)
    facets: list[FacetV2] = Field(max_length=5)
    negatives: list[KeyV2] = Field(default_factory=list, max_length=MAX_ITEMS)


class ScoredV2(BaseModel):
    key: str
    pos: float
    neg: float
    score: float


class SimilarityOutV2(SpaceOutV2):
    results: list[ScoredV2]
    missing: list[str]
    diagnostics: DiagnosticsV2


class ShadowWeighted(BaseModel):
    key: KeyV2
    weight: float = Field(ge=-1_000_000, le=1_000_000, allow_inf_nan=False)


class ShadowInV2(BaseModel):
    candidates: list[KeyV2] = Field(max_length=500)
    include_vectors: bool = False
    baseline: list[ShadowWeighted] = Field(max_length=MAX_ITEMS)
    positives: list[PositiveV2] = Field(max_length=MAX_ITEMS)
    negatives: list[KeyV2] = Field(default_factory=list, max_length=MAX_ITEMS)


class ShadowOutV2(SpaceOutV2):
    v1: dict[str, float]
    v2: dict[str, float]
    missing_v1: list[str]
    missing_v2: list[str]
    missing_baseline: list[str]
    missing_positive: list[str]
    missing_negative: list[str]
    raw_vectors: dict[str, list[float]] | None = None
    centered_vectors: dict[str, list[float]] | None = None


class VectorsInV2(BaseModel):
    space_id: str = Field(min_length=1, max_length=100)
    keys: list[KeyV2] = Field(max_length=MAX_ITEMS)


class VectorsOutV2(SpaceOutV2):
    vectors: dict[str, list[float]]
    missing: list[str]
    diagnostics: DiagnosticsV2


class ProjectFacetV2(BaseModel):
    id: KeyV2
    center: list[Coordinate] = Field(min_length=1, max_length=MAX_DIM)


class ProjectInV2(BaseModel):
    space_id: str = Field(min_length=1, max_length=100)
    keys: list[KeyV2] = Field(max_length=MAX_ITEMS)
    facets: list[ProjectFacetV2] = Field(default_factory=list, max_length=5)
    basis: Literal["global", "keys"] = "global"
    fit_keys: list[KeyV2] = Field(default_factory=list, max_length=MAX_ITEMS)

    @model_validator(mode="after")
    def _fit_keys(self):
        if self.basis == "keys" and not self.fit_keys:
            raise ValueError("basis=keys needs fit_keys")
        return self


class ProjectOutV2(SpaceOutV2):
    basis: str
    explained: list[float]
    points: dict[str, list[float]]
    facets: dict[str, list[float]]
    facet_cos: dict[str, list[float]]
    missing: list[str]
    diagnostics: DiagnosticsV2


def model_name(settings: Settings) -> str:
    return "fake" if settings.model == "fake" else settings.model_repo


def create_app(settings: Settings | None = None, factory: Callable[[Settings], Model] = load_model) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = Store(settings.db_path)
        if n := store.recover():
            logging.getLogger("embedder").info("%d items back in the queue after a restart", n)
        engine = Engine(settings, factory, name=model_name(settings))
        client = httpx.AsyncClient(timeout=settings.download_timeout_s, follow_redirects=True)
        # the sample rate is the model's; known without loading it
        sr = 8000 if settings.model == "fake" else 24000
        worker = Worker(settings, store, engine, client, sr)
        app.state.store, app.state.engine, app.state.worker, app.state.index = store, engine, worker, Index()
        app.state.geometry = Geometry()
        task = None if settings.disable_worker else asyncio.create_task(worker.run())
        app.state.worker_task = task
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await client.aclose()
            if engine.model:
                engine.model.close()
            app.state.geometry.clear()
            store.close()

    app = FastAPI(title="HomeMusic embedder", lifespan=lifespan)

    def auth(authorization: Annotated[str | None, Header()] = None) -> None:
        if settings.token and not hmac.compare_digest(authorization or "", f"Bearer {settings.token}"):
            raise HTTPException(401, "bad token")

    def state():
        return app.state

    @app.get("/healthz")
    async def healthz(st=Depends(state)):
        e: Engine = st.engine
        w: Worker = st.worker
        worker_health = w.health()
        task = st.worker_task
        worker_health["status"] = "disabled" if task is None else "stopped" if task.done() else "running"
        return {
            "status": "degraded" if worker_health["stalled"] or worker_health["status"] == "stopped" else "ok",
            "model": e.name,
            # idle — not in memory (loaded on the next job), loading, ready, error
            "state": e.state,
            "loaded": e.model is not None,
            "device": e.model.device if e.model else None,
            "dim": e.model.dim if e.model else None,
            "error": e.error,
            "queue": st.store.counts(),
            "worker": worker_health,
            "songs_per_s": round(w.stats.last_rate, 2) if w.stats.last_rate else None,
        }

    @app.post("/embed", response_model=EmbedOut, dependencies=[Depends(auth)])
    async def embed(body: EmbedIn, st=Depends(state)):
        ok, rejected = [], []
        for it in body.items:
            if it.path:
                try:
                    library_path(settings.music_root, it.path)
                except AudioError as e:
                    rejected.append(Rejected(key=it.key, error=str(e)))
                    continue
                ok.append((it.key, "path", it.path))
            elif urlparse(it.url).scheme not in ("http", "https"):
                rejected.append(Rejected(key=it.key, error="url must be http(s)"))
            else:
                ok.append((it.key, "url", it.url))
        queued, known = st.store.enqueue(ok, force=body.force, model=st.engine.name)
        if queued:
            st.worker.wake.set()
        return EmbedOut(queued=queued, known=known, rejected=rejected)

    @app.get("/embed/{key:path}", response_model=ItemOut, dependencies=[Depends(auth)])
    async def embed_status(key: str, st=Depends(state)):
        it = st.store.get(key)
        if not it:
            raise HTTPException(404, "unknown key")
        return ItemOut(
            key=it.key, status=it.status, error=it.error, attempts=it.attempts,
            has_vector=it.status == "done" and it.model == st.engine.name, seconds=it.seconds,
            updated_at=it.updated_at,
        )

    @app.post("/similarity", response_model=SimilarityOut, dependencies=[Depends(auth)])
    async def similarity(body: SimilarityIn, st=Depends(state)):
        name = st.engine.name
        wanted = list(dict.fromkeys([*body.candidates, *(p.key for p in body.profile)]))
        vecs = st.store.vectors(wanted, name)
        weights: dict[str, float] = {}
        for p in body.profile:  # a key given twice: weights add up
            weights[p.key] = weights.get(p.key, 0.0) + p.weight
        profile = {k: (vecs[k], w) for k, w in weights.items() if k in vecs and w != 0}
        cands = {k: vecs[k] for k in dict.fromkeys(body.candidates) if k in vecs}
        results = await asyncio.to_thread(score, cands, profile, top=body.top)
        return SimilarityOut(
            model=name,
            results=[
                ScoredOut(
                    key=r.key, pos=r.pos, neg=r.neg, score=r.score,
                    nearest=[NearOut(key=n.key, sim=n.sim) for n in r.nearest],
                    nearest_neg=NearOut(key=r.nearest_neg.key, sim=r.nearest_neg.sim) if r.nearest_neg else None,
                )
                for r in results
            ],
            missing=[k for k in wanted if k not in vecs],
        )

    @app.post("/text", response_model=TextOut, dependencies=[Depends(auth)])
    async def text(body: TextIn, st=Depends(state)):
        e: Engine = st.engine
        try:
            q = (await e.run(lambda m, t: m.embed_text(t), [body.text]))[0]
        except Exception as ex:
            raise HTTPException(503, f"model unavailable: {e.error or ex}") from ex
        index: Index = st.index
        await asyncio.to_thread(index.refresh, st.store, e.name)
        found = index.nearest(
            np.asarray(q, np.float32), limit=body.limit, prefix=body.prefix,
            keys=set(body.keys) if body.keys is not None else None,
        )
        return TextOut(model=e.name, results=[NearOut(key=n.key, sim=n.sim) for n in found])

    @app.post("/vectors", response_model=VectorsOut, dependencies=[Depends(auth)])
    async def vectors(body: VectorsIn, st=Depends(state)):
        vecs = st.store.vectors(list(dict.fromkeys(body.keys)), st.engine.name)
        dim = len(next(iter(vecs.values()))) if vecs else None
        return VectorsOut(
            model=st.engine.name, dim=dim, vectors={k: v.tolist() for k, v in vecs.items()},
            missing=[k for k in dict.fromkeys(body.keys) if k not in vecs],
        )

    async def space_v2(st, space_id=None):
        space = await asyncio.to_thread(st.geometry.current, st.store, st.engine.name)
        if space_id is not None and space_id != space.space_id:
            raise HTTPException(409, {"code": "stale_space", "space_id": space.space_id, "model": space.model})
        return space

    def diagnostics_v2(space, wanted):
        return DiagnosticsV2(available=len(space.vectors), zero_residual=[k for k in wanted if k in space.zero],
                             invalid=[k for k in wanted if k in space.invalid])

    def metadata_v2(space, wanted):
        return {"model": space.model, "space_id": space.space_id, "dim": space.dim,
                "missing": [k for k in wanted if k not in space.vectors],
                "diagnostics": diagnostics_v2(space, wanted)}

    @app.post("/v2/profile", response_model=ProfileOutV2, dependencies=[Depends(auth)])
    async def profile_v2(body: ProfileInV2, st=Depends(state)):
        space = await space_v2(st)
        weights: dict[str, float] = {}
        for p in body.positives:
            weights[p.key] = weights.get(p.key, 0.0) + p.weight
        wanted = list(dict.fromkeys([*weights, *body.negatives]))
        found = await asyncio.to_thread(facets, space.vectors, weights)
        return ProfileOutV2(**metadata_v2(space, wanted),
                            facets=[FacetV2(**vars(f)) for f in found],
                            negatives=list(dict.fromkeys(k for k in body.negatives if k in space.vectors)))

    @app.post("/v2/similarity", response_model=SimilarityOutV2, dependencies=[Depends(auth)])
    async def compare_v2(body: SimilarityInV2, st=Depends(state)):
        space = await space_v2(st, body.space_id)
        if any(len(f.center) != space.dim or np.linalg.norm(f.center) < 1e-12 for f in body.facets):
            raise HTTPException(422, "facet center must be nonzero and match space dimension")
        wanted = list(dict.fromkeys([*body.candidates, *body.negatives]))
        cands = {k: space.vectors[k] for k in dict.fromkeys(body.candidates) if k in space.vectors}
        negs = {k: space.vectors[k] for k in body.negatives if k in space.vectors}
        results = await asyncio.to_thread(lambda: list(similarity_v2(cands, [f.center for f in body.facets], negs)))
        return SimilarityOutV2(**metadata_v2(space, wanted), results=results)

    @app.post("/v2/shadow", response_model=ShadowOutV2, dependencies=[Depends(auth)])
    async def shadow_v2(body: ShadowInV2, st=Depends(state)):
        # Read the raw rows and construct centered geometry from the very same immutable copy.
        def calculate():
            revision, rows = st.store.geometry_snapshot(st.engine.name)

            class FrozenStore:
                version = revision

                def geometry_snapshot(self, model):
                    return revision, rows

            space = Geometry().current(FrozenStore(), st.engine.name)
            raw = {k: v for k, v in rows if k not in space.invalid}
            weights = {}
            for item in body.baseline:
                weights[item.key] = weights.get(item.key, 0.0) + item.weight
            base = {k: (raw[k], w) for k, w in weights.items() if k in raw and w != 0}
            candidates = list(dict.fromkeys(body.candidates))
            valid_raw = {k: raw[k] for k in candidates if k in raw}
            positive_raw = [(v, w) for v, w in base.values() if w > 0]
            negative_raw = [(v, -w) for v, w in base.values() if w < 0]

            def center(group):
                return _center(np.stack([v for v, _ in group]), np.array([w for _, w in group])) if group else None

            pos_center, neg_center = center(positive_raw), center(negative_raw)
            v1 = {k: (float(v @ pos_center) if pos_center is not None else 0.0) -
                     .5 * (float(v @ neg_center) if neg_center is not None else 0.0)
                  for k, v in valid_raw.items()} if body.baseline else {}
            positive = {}
            for item in body.positives:
                positive[item.key] = positive.get(item.key, 0.0) + item.weight
            found = facets(space.vectors, positive)
            centered = {k: space.vectors[k] for k in candidates if k in space.vectors}
            negative = {k: space.vectors[k] for k in body.negatives if k in space.vectors}
            v2 = {r["key"]: r["score"] for r in similarity_v2(centered, [f.center for f in found], negative)}
            # Bound the temporary response; refuse vectors rather than returning a partial coordinate set.
            include_vectors = body.include_vectors and (space.dim or 0) * len(candidates) <= 512000
            return ShadowOutV2(model=space.model, space_id=space.space_id, dim=space.dim, v1=v1, v2=v2,
                               missing_v1=[k for k in candidates if k not in v1],
                               missing_v2=[k for k in candidates if k not in v2],
                               missing_baseline=[k for k in weights if k not in raw],
                               missing_positive=[k for k in positive if k not in space.vectors],
                               missing_negative=[k for k in body.negatives if k not in space.vectors],
                               raw_vectors={k: v.tolist() for k, v in valid_raw.items()} if include_vectors else None,
                               centered_vectors={k: v.tolist() for k, v in centered.items()} if include_vectors else None)

        return await asyncio.to_thread(calculate)

    @app.post("/v2/vectors", response_model=VectorsOutV2, dependencies=[Depends(auth)])
    async def vectors_v2(body: VectorsInV2, st=Depends(state)):
        space = await space_v2(st, body.space_id)
        wanted = list(dict.fromkeys(body.keys))
        return VectorsOutV2(**metadata_v2(space, wanted),
                            vectors={k: space.vectors[k].tolist() for k in wanted if k in space.vectors})

    @app.post("/v2/project", response_model=ProjectOutV2, dependencies=[Depends(auth)])
    async def project_v2(body: ProjectInV2, st=Depends(state)):
        space = await space_v2(st, body.space_id)
        if any(len(f.center) != space.dim or np.linalg.norm(f.center) < 1e-12 for f in body.facets):
            raise HTTPException(422, "facet center must be nonzero and match space dimension")
        wanted = list(dict.fromkeys(body.keys))

        def calculate():
            if body.basis == "global":
                axes = st.geometry.global_axes(space)
            else:
                axes = fit_axes({k: space.vectors[k] for k in dict.fromkeys(body.fit_keys) if k in space.vectors})
            found = {k: space.vectors[k] for k in wanted if k in space.vectors}
            centers = {f.id: np.asarray(f.center, dtype=np.float64) for f in body.facets}
            return ProjectOutV2(
                **metadata_v2(space, wanted), basis=body.basis, explained=list(axes.explained),
                points=project(axes, found), facets=project(axes, centers),
                facet_cos=facet_cosines(found, [f.center for f in body.facets]))

        return await asyncio.to_thread(calculate)

    return app
