"""Client of the `embedder` service (MIXES.md 4.2): audio vectors of library songs and catalog candidates.

Keys: `nd:<song_id>` for a library song (its file, `path` relative to the library), a catalog `ref` (`dz:…`) for a
track known by its Deezer preview. The service did not answer — `EmbedderDown`: mixes are built without the audio
term.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field, FiniteFloat

from app.config import get_settings

log = logging.getLogger(__name__)

MAX_ITEMS = 5000  # the service's limit per request


class EmbedderDown(Exception):
    pass


class EmbedderSpaceExpired(EmbedderDown):
    """Refresh the profile before retrying; cached centers must never cross spaces."""


class GeometrySpace(BaseModel):
    model: str
    space_id: str = Field(min_length=1)
    dim: int | None


class TasteFacet(BaseModel):
    id: str
    weight: FiniteFloat = Field(gt=0, le=1)
    members: list[str]
    center: list[FiniteFloat]


class GeometryDiagnostics(BaseModel):
    available: int
    zero_residual: list[str]
    invalid: list[str]


class GeometryProfile(GeometrySpace):
    facets: list[TasteFacet]
    negatives: list[str]
    missing: list[str]
    diagnostics: GeometryDiagnostics


class GeometryScore(BaseModel):
    key: str
    pos: FiniteFloat
    neg: FiniteFloat
    score: FiniteFloat


class GeometrySimilarity(GeometrySpace):
    results: list[GeometryScore]
    missing: list[str]
    diagnostics: GeometryDiagnostics

    @property
    def scores(self) -> dict[str, float]:
        return {r.key: r.score for r in self.results}


class ShadowSound(GeometrySpace):
    v1: dict[str, FiniteFloat]
    v2: dict[str, FiniteFloat]
    missing_v1: list[str]
    missing_v2: list[str]
    missing_baseline: list[str]
    missing_positive: list[str]
    missing_negative: list[str]
    raw_vectors: dict[str, list[FiniteFloat]] | None = None
    centered_vectors: dict[str, list[FiniteFloat]] | None = None


class CenteredVectors(GeometrySpace):
    vectors: dict[str, list[FiniteFloat]]
    missing: list[str]
    diagnostics: GeometryDiagnostics


class GeometryProjection(GeometrySpace):
    """`/v2/project`: 2D coordinates of centered vectors (PCA of the snapshot or of the given keys)."""
    basis: str
    explained: list[FiniteFloat]
    points: dict[str, list[FiniteFloat]]
    facets: dict[str, list[FiniteFloat]]
    facet_cos: dict[str, list[FiniteFloat]]
    missing: list[str]
    diagnostics: GeometryDiagnostics


def _validate_coverage(requested: list[str], found: list[str], missing: list[str],
                       *, extra_missing: list[str] | None = None) -> None:
    wanted, present, absent = set(requested), set(found), set(missing)
    # Similarity metadata also reports missing negative keys, which have no score of their own.
    allowed_missing = wanted | set(extra_missing or [])
    if (len(present) != len(found) or len(absent) != len(missing)
            or not present <= wanted or not absent <= allowed_missing
            or present & absent or not wanted <= present | absent):
        raise ValueError("geometry response must cover each requested key exactly once as found or missing")


def configured() -> bool:
    return bool(get_settings().embedder_url.strip())


def song_key(nd_song_id: str) -> str:
    return f"nd:{nd_song_id}"


@dataclass
class Similarity:
    scores: dict[str, float] = field(default_factory=dict)  # key -> pos − 0.5·neg
    missing: set[str] = field(default_factory=set)


class EmbedderClient:
    def __init__(self, client: httpx.AsyncClient | None = None):
        s = get_settings()
        self.base_url = s.embedder_url.rstrip("/")
        headers = {"Authorization": f"Bearer {s.embedder_token}"} if s.embedder_token else {}
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5), headers=headers)
        self._own = client is None

    async def __aenter__(self) -> EmbedderClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._own:
            await self._client.aclose()

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            r = await self._client.post(f"{self.base_url}{path}", json=body)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise EmbedderDown(f"{path}: {e!r}") from e

    async def health(self) -> dict[str, Any]:
        try:
            r = await self._client.get(f"{self.base_url}/healthz")
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise EmbedderDown(f"/healthz: {e!r}") from e

    async def has_vector_or_clip(self, key: str) -> bool:
        try:
            r = await self._client.get(f"{self.base_url}/embed/{quote(key, safe='')}")
            if r.status_code == 404:
                return False
            r.raise_for_status()
            status = r.json()
            return bool(status["has_vector"] or status.get("has_clip"))
        except (httpx.HTTPError, ValueError, KeyError) as e:
            raise EmbedderDown(f"embed status: {e!r}") from e

    async def submit_clip(self, key: str, wav: bytes) -> None:
        try:
            r = await self._client.post(f"{self.base_url}/embed/clip", params={"key": key},
                                        headers={"Content-Type": "audio/wav"}, content=wav)
            r.raise_for_status()
            result = r.json()
            if not (result.get("queued") or result.get("known")):
                raise ValueError("embedder did not accept the recording")
        except (httpx.HTTPError, ValueError) as e:
            raise EmbedderDown(f"embed clip: {e!r}") from e

    async def embed(self, items: list[dict[str, str]]) -> int:
        """`[{key, url|path}]` into the service's queue (known ones are skipped there). Returns how many were queued."""
        queued = 0
        for i in range(0, len(items), MAX_ITEMS):
            body = await self._post("/embed", {"items": items[i:i + MAX_ITEMS]})
            queued += int(body.get("queued") or 0)
            for r in body.get("rejected") or []:
                log.info("embedder rejected %s: %s", r.get("key"), r.get("error"))
        return queued

    async def similarity(self, candidates: list[str], profile: dict[str, float]) -> Similarity:
        """Each candidate's `pos − 0.5·neg` against the weighted centers of the profile (positive and negative
        weights); keys without a vector are `missing`."""
        if not candidates or not profile:
            return Similarity(missing=set(candidates))
        body = await self._post("/similarity", {
            "candidates": candidates[:MAX_ITEMS],
            "profile": [{"key": k, "weight": w} for k, w in list(profile.items())[:MAX_ITEMS]], "top": 0})
        return Similarity(scores={r["key"]: float(r["score"]) for r in body.get("results") or []},
                          missing=set(body.get("missing") or []))

    async def vectors(self, keys: list[str]) -> dict[str, list[float]]:
        if not keys:
            return {}
        return dict((await self._post("/vectors", {"keys": keys[:MAX_ITEMS]})).get("vectors") or {})

    async def _geometry(self, path: str, body: dict[str, Any], schema, *, space: GeometrySpace | None = None):
        try:
            response = await self._client.post(f"{self.base_url}{path}", json=body)
            if response.status_code == 409:
                raise EmbedderSpaceExpired(f"{path}: stale space; rebuild the profile")
            response.raise_for_status()
            result = schema.model_validate(response.json())
            if space is not None and (result.space_id, result.model, result.dim) != (space.space_id, space.model, space.dim):
                raise EmbedderSpaceExpired(f"{path}: response changed space; rebuild the profile")
            vectors = result.vectors.values() if isinstance(result, CenteredVectors) else (
                f.center for f in result.facets) if isinstance(result, GeometryProfile) else []
            if any(len(v) != result.dim for v in vectors):
                raise ValueError("vector dimension differs from geometry space")
            if isinstance(result, GeometrySimilarity):
                _validate_coverage(body["candidates"], [r.key for r in result.results], result.missing,
                                   extra_missing=body["negatives"])
            elif isinstance(result, CenteredVectors):
                _validate_coverage(body["keys"], list(result.vectors), result.missing)
            elif isinstance(result, GeometryProjection):
                _validate_coverage(body["keys"], list(result.points), result.missing)
                if (len(result.explained) != 2 or any(len(v) != 2 for v in [*result.points.values(),
                                                                         *result.facets.values()])
                        or set(result.facets) != {f["id"] for f in body["facets"]}
                        or set(result.facet_cos) != set(result.points)
                        or any(len(v) != len(body["facets"]) for v in result.facet_cos.values())):
                    raise ValueError("projection response does not match the request")
            return result
        except (httpx.HTTPError, ValueError) as e:
            # An older service (404), unavailable service, or malformed response all use the usual down fallback.
            raise EmbedderDown(f"{path}: {e!r}") from e

    async def profile_v2(self, positives: dict[str, float], negatives: list[str]) -> GeometryProfile:
        """Compute facets from s>=1 tracks. Unsupported v2 raises EmbedderDown for caller fallback."""
        if len(positives) > MAX_ITEMS or len(negatives) > MAX_ITEMS:
            raise EmbedderDown("/v2/profile: profile exceeds request limit")
        return await self._geometry("/v2/profile", {
            "positives": [{"key": k, "weight": w} for k, w in positives.items()], "negatives": negatives,
        }, GeometryProfile)

    async def similarity_v2(self, candidates: list[str], profile: GeometryProfile) -> GeometrySimilarity:
        """No implicit v1 retry: callers can explicitly drop sound or refresh an expired profile."""
        results, missing = [], []
        diagnostics = GeometryDiagnostics(available=profile.diagnostics.available, zero_residual=[], invalid=[])
        for i in range(0, len(candidates), MAX_ITEMS):
            result = await self._geometry("/v2/similarity", {
                "space_id": profile.space_id, "candidates": candidates[i:i + MAX_ITEMS],
                "facets": [f.model_dump() for f in profile.facets], "negatives": profile.negatives,
            }, GeometrySimilarity, space=profile)
            results.extend(result.results)
            missing.extend(result.missing)
            diagnostics.available = result.diagnostics.available
            diagnostics.zero_residual.extend(result.diagnostics.zero_residual)
            diagnostics.invalid.extend(result.diagnostics.invalid)
        return GeometrySimilarity(model=profile.model, space_id=profile.space_id, dim=profile.dim,
                                  results=results, missing=list(dict.fromkeys(missing)),
                                  diagnostics=diagnostics)

    async def vectors_v2(self, keys: list[str], space: GeometrySpace) -> CenteredVectors:
        vectors, missing = {}, []
        diagnostics = GeometryDiagnostics(available=0, zero_residual=[], invalid=[])
        for i in range(0, len(keys), MAX_ITEMS):
            result = await self._geometry("/v2/vectors", {
                "space_id": space.space_id, "keys": keys[i:i + MAX_ITEMS],
            }, CenteredVectors, space=space)
            vectors.update(result.vectors)
            missing.extend(result.missing)
            diagnostics.available = result.diagnostics.available
            diagnostics.zero_residual.extend(result.diagnostics.zero_residual)
            diagnostics.invalid.extend(result.diagnostics.invalid)
        return CenteredVectors(model=space.model, space_id=space.space_id, dim=space.dim, vectors=vectors,
                               missing=list(dict.fromkeys(missing)), diagnostics=diagnostics)

    async def project_v2(self, keys: list[str], profile: GeometryProfile, *, basis: str = "global",
                         fit_keys: list[str] | None = None) -> GeometryProjection:
        """2D coordinates of `keys` and of the profile's facet centers, plus the cosine of each key to every facet.
        `basis` "global" — axes of the whole snapshot; "keys" — axes fitted on `fit_keys`. An expired space raises
        `EmbedderSpaceExpired`: rebuild the profile and call again."""
        fit = list(dict.fromkeys(fit_keys or []))
        if len(keys) > MAX_ITEMS or len(fit) > MAX_ITEMS:
            raise EmbedderDown("/v2/project: request exceeds limit")
        body: dict[str, Any] = {
            "space_id": profile.space_id, "keys": list(dict.fromkeys(keys)), "basis": basis,
            "facets": [{"id": f.id, "center": f.center} for f in profile.facets]}
        if basis == "keys":
            body["fit_keys"] = fit
        return await self._geometry("/v2/project", body, GeometryProjection, space=profile)

