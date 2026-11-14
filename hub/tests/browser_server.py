"""Real Hub/SQLite for browser regressions; only the external Navidrome API is simulated.

Run from hub/. Every invocation owns a temporary library and never reads the deployment's .env.
The server binds loopback only. No test-only routes are installed in the production application.
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import wave
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import respx
import uvicorn
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ACCOUNTS = ("offline", "expiry", "alice", "bob", "handoff", "pagination", "operator")
PASSWORD = "browser-test-password"
ND = "http://navidrome.browser.test"


def navidrome(request: httpx.Request) -> httpx.Response:
    user = request.url.params.get("u", "")
    salt = request.url.params.get("s", "")
    token = hashlib.md5((PASSWORD + salt).encode()).hexdigest()
    if user not in ACCOUNTS or request.url.params.get("t") != token:
        return httpx.Response(200, json={"subsonic-response": {
            "status": "failed", "error": {"code": 40, "message": "Invalid credentials"}}})
    song = {"id": "browser-tone", "title": "Browser tone", "artist": "HomeMusic test",
            "album": "Browser regression", "duration": 120, "starred": "2026-01-01T00:00:00Z"}
    endpoint = request.url.path.rsplit("/", 1)[-1].removesuffix(".view")
    responses = {
        "ping": {}, "getUser": {"user": {"username": user, "adminRole": user == "operator"}},
        "getStarred2": {"starred2": {"song": [song] if user != "bob" else [], "album": [], "artist": []}},
        "getSong": {"song": song}, "getAlbumList2": {"albumList2": {"album": []}},
        "getArtists": {"artists": {"index": []}}, "getPlaylists": {"playlists": {"playlist": []}},
        "scrobble": {}, "getScanStatus": {"scanStatus": {"scanning": False}},
    }
    if endpoint not in responses:
        raise AssertionError(f"Unexpected Navidrome endpoint: {endpoint}")
    return httpx.Response(200, json={"subsonic-response": {"status": "ok", **responses[endpoint]}})


def run(root: Path) -> None:
    os.environ.update({
        "MUSIC_ROOT": str(root / "music"), "DATA_DIR": str(root / "data"),
        "CONFIG_FILE": str(root / "config.yaml"), "STATIC_DIR": str(root / "no-static"),
        "NAVIDROME_URL": ND, "NAVIDROME_ADMIN_USER": "alice", "NAVIDROME_ADMIN_PASSWORD": PASSWORD,
        "HUB_SECRET_KEY": Fernet.generate_key().decode(), "DISABLE_BACKGROUND": "true",
        "LLM_API_KEY": "", "EMBEDDER_URL": "", "LASTFM_API_KEY": "", "SMTP_HOST": "",
        "OUTBOUND_PROXY": "", "OUTBOUND_PROXY_HOSTS": "", "PUBLIC_HUB_URL": "", "PUBLIC_MUSIC_URL": "",
    })
    from app import db
    from app.main import create_app
    from app.models import DownloadBatch, DownloadItem, Track, User
    from app.security import encrypt

    app = create_app()
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        with respx.mock(assert_all_called=False, assert_all_mocked=True) as external:
            external.route(host="navidrome.browser.test").mock(side_effect=navidrome)
            async with original_lifespan(application):
                library = root / "music" / "library"
                with wave.open(str(library / "tone.wav"), "wb") as audio:
                    audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
                    audio.writeframes(b"\0\0" * 8000 * 120)
                async with db.session_scope() as session:
                    users = {}
                    for name in ACCOUNTS:
                        user = User(nd_username=name, nd_password_enc=encrypt(PASSWORD))
                        session.add(user)
                        await session.flush()
                        users[name] = user
                    session.add(Track(uid="browser-tone", path="tone.wav", nd_song_id="browser-tone",
                                      artist="HomeMusic test", title="Browser tone", duration_s=120,
                                      fingerprint="homemusic test|browser tone", codec="wav", source="upload"))
                    for name, user in users.items():
                        batch = DownloadBatch(user_id=user.id, origin="search")
                        session.add(batch)
                        await session.flush()
                        # The oldest failure must remain discoverable beyond the former latest-200 cutoff.
                        total = 225 if name == "pagination" else 1
                        for number in range(total):
                            session.add(DownloadItem(user_id=user.id, batch_id=batch.id,
                                status="failed" if number == 0 else "done",
                                error="Source unavailable" if number == 0 else None,
                                query_json={"query": {"artist": name, "title": f"{name} download {number}"}}))
                yield

    app.router.lifespan_context = lifespan
    uvicorn.run(app, host="127.0.0.1", port=18080, log_level="warning")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="homemusic-browser-") as directory:
        run(Path(directory))
