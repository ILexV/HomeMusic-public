"""Health, share-target fallback."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import HealthOut
from app.auth import admin_user
from app.config import get_settings
from app.db import get_session
from app.library.navidrome import admin_client
from app.models import User
from app import health as health_checks

log = logging.getLogger(__name__)
router = APIRouter(tags=["system"])


async def _navidrome_ok() -> bool:
    nd = None
    try:
        nd = admin_client()
        return bool(await asyncio.wait_for(nd.ping(), timeout=3))
    except Exception:
        return False
    finally:
        if nd is not None:
            try:
                await asyncio.wait_for(nd.aclose(), timeout=1)
            except Exception:
                pass


_BUILD_META = re.compile(r'<meta\s+name="hm-build"\s+content="([^"]+)"')
_build_cache: tuple[tuple[str, int, int], str | None] | None = None


def web_build(static_dir: Path) -> str | None:
    """Build id of the PWA in `static_dir` (stamped into index.html by web/vite.config.ts); None without a build."""
    global _build_cache
    index = static_dir / "index.html"
    try:
        st = index.stat()
    except OSError:
        return None
    key = (str(index), st.st_mtime_ns, st.st_size)
    if _build_cache is None or _build_cache[0] != key:
        m = _BUILD_META.search(index.read_text(encoding="utf-8", errors="replace"))
        _build_cache = (key, m.group(1) if m else None)
    return _build_cache[1]


@router.get("/healthz", response_model=HealthOut)
async def healthz():
    return HealthOut(ok=True, navidrome=health_checks.navidrome_cached, web_build=web_build(get_settings().static_dir))


@router.get("/readyz")
async def readyz():
    result = await health_checks.readiness(_navidrome_ok)
    return JSONResponse(result, status_code=200 if result["ok"] else 503)


@router.get("/api/system/health", response_model=health_checks.AdminHealthOut)
async def health(user: User = Depends(admin_user)):
    result, details = await asyncio.gather(health_checks.readiness(_navidrome_ok), health_checks.admin_details())
    return health_checks.AdminHealthOut(
        **result, **details, navidrome=health_checks.navidrome_cached,
        web_build=web_build(get_settings().static_dir),
    )


@router.post("/share-target")
async def share_target(request: Request, s: AsyncSession = Depends(get_session, scope="function")):
    """Fallback when the service worker did not intercept the share (e.g. first launch before SW install)."""
    from app.auth import COOKIE

    if not request.cookies.get(COOKIE):
        return RedirectResponse("/#/login?next=share", status_code=303)
    # the PWA re-shares from its own page; nothing is stored server-side without the SW
    return RedirectResponse("/#/share?fallback=1", status_code=303)


# --- Android app updates (stage 4.6): files live in MUSIC_ROOT/.apk (latest.json + the APK) ---------------


def _apk_dir():
    from app.config import get_settings

    return get_settings().music_root / ".apk"


_ARTIFACT = re.compile(r"^(apk|patch)-([0-9a-f]{64})\.(apk|bsdiff)$")


def _read_release() -> dict:
    from fastapi import HTTPException

    try:
        data = json.loads((_apk_dir() / "latest.json").read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("manifest must be an object")
        return data
    except (OSError, ValueError):
        raise HTTPException(404, "Нет сборки приложения") from None


def _artifact(name: str, size: int | None = None, digest: str | None = None, verify: bool = False) -> Path | None:
    match = _ARTIFACT.fullmatch(name)
    if not match or (match[1], match[3]) not in (("apk", "apk"), ("patch", "bsdiff")):
        return None
    if digest is not None and match[2] != digest:
        return None
    path = _apk_dir() / "artifacts" / name
    try:
        if path.is_symlink() or not path.is_file() or path.resolve().parent != (_apk_dir() / "artifacts").resolve():
            return None
        if size is not None and (type(size) is not int or size <= 0 or path.stat().st_size != size):
            return None
        if verify:
            with path.open("rb") as f:
                if hashlib.file_digest(f, "sha256").hexdigest() != match[2]:
                    return None
        return path
    except OSError:
        return None


def _valid_entry(entry: object, kind: str) -> bool:
    if not isinstance(entry, dict):
        return False
    url, size, digest = entry.get("url"), entry.get("size"), entry.get("sha256")
    prefix = "/app/artifacts/"
    if (not isinstance(url, str) or not url.startswith(prefix) or not isinstance(digest, str)
            or type(size) is not int or size <= 0):
        return False
    name = url[len(prefix):]
    if not name.startswith(kind + "-"):
        return False
    return _artifact(name, size, digest) is not None


@router.get("/app/latest.json")
async def apk_latest():
    data = _read_release()
    data["url"] = "/app/latest.apk"
    data.pop("file", None)
    full = data.get("full")
    if (not _valid_entry(full, "apk") or not isinstance(full, dict)
            or full.get("sha256") != data.get("sha256") or full.get("size") != data.get("size")):
        data.pop("full", None)
        data.pop("patches", None)
    else:
        patches = data.get("patches")
        data["patches"] = [p for p in patches if isinstance(p, dict)
                           and p.get("format") == "bsdiff40-v1"
                           and type(p.get("baseVersionCode")) is int and p["baseVersionCode"] > 0
                           and type(data.get("versionCode")) is int and p["baseVersionCode"] < data["versionCode"]
                           and type(p.get("size")) is int and p["size"] < full["size"]
                           and isinstance(p.get("baseSha256"), str)
                           and re.fullmatch(r"[0-9a-f]{64}", p["baseSha256"])
                           and _valid_entry(p, "patch")] if isinstance(patches, list) else []
    return data


@router.get("/app/latest.apk")
async def apk_file():
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    data = _read_release()
    name = data.get("file", "")
    if not isinstance(name, str) or not re.fullmatch(r"HomeMusic-\d+\.\d+\.\d+-\d+\.apk", name):
        raise HTTPException(404, "Нет сборки приложения")
    root = _apk_dir().resolve()
    path = (root / name).resolve()
    if path.parent != root or not path.is_file():
        raise HTTPException(404, "Нет сборки приложения")
    return FileResponse(path, media_type="application/vnd.android.package-archive", filename=path.name)


@router.get("/app/artifacts/{name}")
async def apk_artifact(name: str):
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    from starlette.concurrency import run_in_threadpool

    path = await run_in_threadpool(_artifact, name, verify=True)
    if path is None:
        raise HTTPException(404, "Нет сборки приложения")
    media_type = "application/vnd.android.package-archive" if name.endswith(".apk") else "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=name, headers={"Cache-Control": "public, max-age=31536000, immutable"})
