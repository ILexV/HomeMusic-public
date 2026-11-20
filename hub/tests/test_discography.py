"""Discography is read-only; artist identities come from track credits, not punctuation or search rank."""

import httpx
import pytest
import respx

from tests.support import app_client


@pytest.fixture
async def env(hub_env):
    from app.main import create_app

    state = {"artists": [{"name": "AC/DC", "id": "local-1"}, {"name": "Guest", "id": "local-2"}], "fail": 0}

    def nd(req):
        method = req.url.path.rsplit("/", 1)[1]
        body = {"status": "ok"}
        if method == "getUser":
            body["user"] = {"adminRole": True}
        elif method == "getSong":
            if state["fail"]:
                body.update(status="failed", error={"code": state["fail"], "message": "source error"})
            else:
                body["song"] = {"id": "s", "artist": "AC/DC & Guest", "artistId": "local-1", "artists": state["artists"]}
        elif method == "getArtists":
            body["artists"] = {"index": [{"artist": state["artists"]}]}
        elif method == "getArtist":
            body["artist"] = {"id": "local-1", "name": "AC/DC"}
        elif method != "ping":
            pytest.fail(f"Unexpected Navidrome action: {method}")
        return httpx.Response(200, json={"subsonic-response": body})

    app = create_app()
    async with app.router.lifespan_context(app):
        with respx.mock(assert_all_called=False) as mock:
            mock.route(host="navidrome.test").mock(side_effect=nd)
            mock.get("https://api.deezer.com/track/7").respond(json={
                "id": 7, "artist": {"id": 10, "name": "AC/DC"}, "contributors": [
                    {"id": 10, "name": "AC/DC", "role": "Main"},
                    {"id": 20, "name": "Guest", "role": "Featured"},
                    {"id": 21, "name": "Guest", "role": "Featured"}]})
            async with app_client(app) as client:
                yield client, state


async def test_discography_merges_only_unambiguous_credits_and_keeps_every_contributor_id(env):
    client, _ = env
    response = await client.get("/api/discography/credits", params={"nd_song_id": "s", "ref": "dz:7"})
    assert response.status_code == 200, response.text
    assert response.json() == {"artists": [
        {"name": "AC/DC", "nd_artist_id": "local-1", "ref": "dz:10"},
        {"name": "Guest", "nd_artist_id": "local-2", "ref": None},
        {"name": "Guest", "nd_artist_id": None, "ref": "dz:20"},
        {"name": "Guest", "nd_artist_id": None, "ref": "dz:21"}], "warning": None}


async def test_discography_library_legacy_primary_id_uses_canonical_name(env):
    client, state = env
    state["artists"] = []
    response = await client.get("/api/discography/credits?ref=nd:s")
    assert response.status_code == 200, response.text
    assert response.json()["artists"] == [{"name": "AC/DC", "nd_artist_id": "local-1", "ref": None}]
    assert (await client.get("/api/discography/credits?ref=nd:s&nd_song_id=other")).status_code == 422


async def test_discography_cross_catalog_lookup_never_uses_substring_or_first_homonym(env):
    client, state = env
    resolve = "/api/discography/artist"
    assert (await client.get(resolve, params={"scope": "library", "name": "AC"})).json() is None
    assert (await client.get(resolve, params={"scope": "library", "name": "ac/dc"})).json()["nd_artist_id"] == "local-1"
    state["artists"].append({"name": "AC/DC", "id": "another"})
    assert (await client.get(resolve, params={"scope": "library", "name": "AC/DC"})).status_code == 409


async def test_discography_reports_partial_outage_not_silent_empty_or_permission_bypass(env):
    client, state = env
    state["fail"] = 0x50
    assert (await client.get("/api/discography/credits?nd_song_id=s")).status_code == 502
    response = await client.get("/api/discography/credits?nd_song_id=s&ref=dz:7")
    assert response.status_code == 200, response.text
    assert response.json()["warning"] and len(response.json()["artists"]) == 3
    state["fail"] = 50
    assert (await client.get("/api/discography/credits?nd_song_id=s&ref=dz:7")).status_code == 403


async def test_discography_catalog_lookup_respects_whole_name_not_search_order(env, monkeypatch):
    from app.catalog.models import CatalogArtist, SearchResults
    from app.services import svc

    async def search(name, kinds, limit):
        assert kinds == ("artist",)
        return SearchResults(artists=[
            CatalogArtist(id="1", source="dz", ref="dz:1", name="AC/DC Tribute"),
            CatalogArtist(id="2", source="dz", ref="dz:2", name="AC/DC"),
        ])

    client, _ = env
    monkeypatch.setattr(svc.deezer, "search", search)
    response = await client.get("/api/discography/artist", params={"scope": "catalog", "name": "ac/DC"})
    assert response.status_code == 200, response.text
    assert response.json() == {"name": "AC/DC", "ref": "dz:2", "nd_artist_id": None}
