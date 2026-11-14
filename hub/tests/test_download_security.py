"""Provider files cross the public API only as references to server-held search results."""
import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import select

from app import db, listen
from app.api import downloads, search
from app.api.schemas import AssistantChoice
from app.auth import current_user
from app.models import DownloadItem, User
from app.providers.base import Candidate, contained_path, ProviderError
from app.services import svc
from tests.support import FakeProvider


@pytest.fixture
async def client(hub_env, monkeypatch):
    db.init_engine()
    app = FastAPI()
    app.include_router(downloads.router)
    app.include_router(search.router)
    async with db.session_scope() as session:
        user = User(nd_username="owner", nd_password_enc="unused")
        session.add(user)
        await session.flush()
    app.dependency_overrides[current_user] = lambda: user
    monkeypatch.setattr(listen, "_choices", {})
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://hub.test") as client:
            yield client
    finally:
        await db.dispose()


async def test_forged_candidate_is_rejected_not_ignored(client):
    candidate = {"provider": "hitmos_fm", "provider_id": "../../outside", "artist": "A", "title": "T",
                 "page_url": "http://127.0.0.1/", "download_url": "http://169.254.169.254/latest/meta-data"}
    response = await client.post("/api/downloads", json={"items": [{"artist": "A", "title": "T", "candidate": candidate}]})
    assert response.status_code == 422
    assert any(error["loc"][-1] == "candidate" for error in response.json()["detail"])
    async with db.session_scope() as session:
        assert (await session.execute(select(DownloadItem))).first() is None
    with pytest.raises(ValidationError):
        AssistantChoice(kind="candidate", candidate=candidate)


async def test_unknown_file_reference_requires_new_search(client):
    response = await client.post("/api/downloads", json={"track_refs": ["file:hitmos_fm:never-seen"]})
    assert response.status_code == 404


async def test_search_file_reference_queues_only_the_server_candidate(client, monkeypatch):
    candidate = Candidate(provider="hitmos_fm", provider_id="123", artist="Artist", title="Track",
                          page_url="https://hitmos.fm/song/123", download_url="https://cdn.test/audio.mp3")
    monkeypatch.setattr(svc, "providers", {"hitmos_fm": FakeProvider("hitmos_fm", [candidate])})
    response = await client.get("/api/search/providers", params={"q": "Artist - Track", "provider": "hitmos_fm"})
    assert response.status_code == 200 and response.json()["candidates"][0]["provider_id"] == "123"
    remembered = await listen.file_candidate("file:hitmos_fm:123")
    assert remembered is not None and remembered.download_url == candidate.download_url
    response = await client.post("/api/downloads", json={"track_refs": ["file:hitmos_fm:123"]})
    assert response.status_code == 200 and response.json()["queued"] == 1
    async with db.session_scope() as session:
        item = (await session.execute(select(DownloadItem))).scalar_one()
        assert item.chosen_candidate_json["download_url"] == candidate.download_url
        assert item.query_json["query"]["artist"] == candidate.artist


def test_staging_containment_rejects_escape_and_symlink(tmp_path):
    root = tmp_path / "staging"
    root.mkdir()
    with pytest.raises(ProviderError):
        contained_path(root, root / "../outside.mp3")
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"not staging")
    link = root / "escape.mp3"
    link.symlink_to(outside)
    with pytest.raises(ProviderError):
        contained_path(root, link)
