"""Dynamic rule evaluation, persistent CRUD and account isolation (no external services)."""
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.smart_playlists import router
from app.db import get_session
from app.models import PlayEvent, Session, User, utcnow
from app.security import hash_token
from app.smart_playlists import Definition, evaluate_tracks, library_songs, listening_history

NOW = datetime(2026, 11, 12, 12)


def definition(*rules, **options):
    return Definition.model_validate({"rules": list(rules), **options})


def song(id, **kwargs):
    return {"id": id, "title": id, "artist": "Artist", "album": "Album", "duration": 180,
            "created": "2026-11-10T12:00:00Z", "year": 2001, "genre": "Rock", **kwargs}


def ids(result):
    return [track.nd_song_id for track in result.tracks]


def test_combined_rules_count_before_limit_and_stable_sort():
    songs = [song("b"), song("a"), song("old", year=1980), song("jazz", genre="Jazz"), song("unliked")]
    rule = definition({"kind": "liked"}, {"kind": "genre", "value": "rock"},
                      {"kind": "year", "minimum": 1990, "maximum": 2001},
                      {"kind": "recently_added", "days": 3}, limit=1)
    result = evaluate_tracks(rule, songs, {"a", "b", "old", "jazz"}, {}, NOW)
    assert result.count == 2
    assert ids(result) == ["a"]
    reverse = rule.model_copy(update={"descending": True})
    assert ids(evaluate_tracks(reverse, songs, {"a", "b"}, {}, NOW)) == ["b"]


def test_any_rules_and_multigenre_metadata():
    rule = definition({"kind": "liked"}, {"kind": "genre", "value": "ambient"}, match="any")
    result = evaluate_tracks(rule, [song("a"), song("b", genres=[{"name": "Ambient"}]), song("c")], {"a"}, {}, NOW)
    assert ids(result) == ["a", "b"]


def test_not_listened_distinguishes_old_new_unheard_and_unknown_history():
    songs = [song("old"), song("new"), song("never"), song("unknown", playCount=3),
             song("nd-old", played="2025-01-01T10:00:00Z", playCount=1)]
    history = {"old": NOW - timedelta(days=900), "new": NOW - timedelta(hours=1)}
    stale = definition({"kind": "not_listened", "days": 30})
    assert ids(evaluate_tracks(stale, songs, set(), history, NOW)) == ["nd-old", "old"]
    include = definition({"kind": "not_listened", "days": 30, "include_unheard": True})
    assert ids(evaluate_tracks(include, songs, set(), history, NOW)) == ["nd-old", "never", "old"]
    unheard = definition({"kind": "unheard"})
    assert ids(evaluate_tracks(unheard, songs, set(), history, NOW)) == ["never"]


def test_current_listening_wins_over_stale_navidrome_date():
    rule = definition({"kind": "not_listened", "days": 30})
    songs = [song("a", played="2020-01-01T00:00:00Z")]
    assert evaluate_tracks(rule, songs, set(), {"a": NOW}, NOW).count == 0


def test_added_unknown_future_and_year_unknown_do_not_match():
    rule = definition({"kind": "recently_added", "days": 2}, {"kind": "year", "minimum": 2000, "maximum": 2002})
    songs = [song("exact"), song("unknown", created=None), song("future", created="2027-01-01T00:00:00Z"),
             song("no-year", year=None)]
    assert ids(evaluate_tracks(rule, songs, set(), {}, NOW)) == ["exact"]


@pytest.mark.parametrize("payload", [
    {"rules": []}, {"rules": [{"kind": "sql", "value": "1=1"}]},
    {"rules": [{"kind": "liked", "sql": "DROP TABLE users"}]},
    {"rules": [{"kind": "not_listened", "days": 0}]},
    {"rules": [{"kind": "genre", "value": "  "}]},
    {"rules": [{"kind": "year", "minimum": 2020, "maximum": 1990}]},
    {"rules": [{"kind": "liked"}], "sort": "users.id"},
    {"rules": [{"kind": "liked"}], "limit": 2001},
])
def test_only_validated_predicates_and_sort_keys_are_accepted(payload):
    with pytest.raises(ValidationError):
        Definition.model_validate(payload)


class Library:
    def __init__(self, data):
        self.data = data
        self.offsets = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def search3(self, query, *, song_count, song_offset):
        assert query == ""
        self.offsets.append(song_offset)
        # A server-side page cap must not silently truncate evaluation.
        return {"song": self.data["songs"][song_offset:song_offset + 2]}

    async def get_starred2(self):
        return {"song": [{"id": id} for id in self.data["liked"]]}


async def test_library_pagination_does_not_assume_requested_page_size():
    nd = Library({"songs": [song(str(i)) for i in range(5)]})
    assert len(await library_songs(nd)) == 5
    assert nd.offsets == [0, 2, 4, 5]


@pytest.fixture
async def api(db_file, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as s:
        users = [User(nd_username=name, nd_password_enc="not-used") for name in ("alice", "bob")]
        s.add_all(users)
        await s.flush()
        for user in users:
            s.add(Session(id=hash_token(user.nd_username), user_id=user.id, expires_at=utcnow() + timedelta(days=1)))
        await s.commit()
    data = {users[0].id: {"songs": [song("a"), song("b")], "liked": {"a"}},
            users[1].id: {"songs": [song("private")], "liked": {"private"}}}
    monkeypatch.setattr("app.smart_playlists.user_navidrome", lambda user: Library(data[user.id]))

    async def session():
        async with sessions() as s:
            yield s
            await s.commit()

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = session
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://hub") as client:
        yield client, sessions, users, data, engine
    await engine.dispose()


def account(name):
    return {"cookie": f"hm_session={name}"}


async def test_authenticated_crud_persists_and_isolates_accounts(api):
    client, _, _, _, engine = api
    root = "/api/smart-playlists"
    body = {"name": "Old favorites", "definition": {"rules": [{"kind": "liked"}]}}
    created = await client.post(root, json=body, headers=account("alice"))
    assert created.status_code == 201
    id = created.json()["id"]
    await engine.dispose()  # close every pooled connection; next request reloads persisted JSON
    rows = (await client.get(root, headers=account("alice"))).json()
    assert len(rows) == 1 and rows[0]["id"] == id
    assert (await client.get(root, headers=account("bob"))).json() == []
    for method, suffix, payload in [("GET", "", None), ("PUT", "", body),
                                     ("DELETE", "", None), ("POST", "/evaluate", None)]:
        response = await client.request(method, f"{root}/{id}{suffix}", json=payload, headers=account("bob"))
        assert response.status_code == 404
    body["name"] = "Changed"
    body["definition"] = {"rules": [{"kind": "genre", "value": "Rock"}], "limit": 1}
    changed = await client.put(f"{root}/{id}", json=body, headers=account("alice"))
    assert changed.status_code == 200 and changed.json()["definition"]["limit"] == 1
    assert (await client.delete(f"{root}/{id}", headers=account("alice"))).status_code == 200
    assert (await client.get(root, headers=account("alice"))).json() == []


async def test_every_route_requires_a_live_account_session(api):
    client, *_ = api
    body = {"name": "x", "definition": {"rules": [{"kind": "liked"}]}}
    for method, path, payload in [("GET", "", None), ("POST", "", body),
                                  ("POST", "/preview", body["definition"]), ("GET", "/missing", None),
                                  ("PUT", "/missing", body), ("DELETE", "/missing", None),
                                  ("POST", "/missing/evaluate", None)]:
        assert (await client.request(method, "/api/smart-playlists" + path, json=payload)).status_code == 401


async def test_saved_evaluation_changes_with_metadata_stars_and_listening(api):
    client, sessions, users, data, _ = api
    root = "/api/smart-playlists"
    body = {"name": "Dynamic", "definition": {"rules": [{"kind": "liked"}, {"kind": "unheard"},
                                                          {"kind": "genre", "value": "Rock"}]}}
    id = (await client.post(root, json=body, headers=account("alice"))).json()["id"]

    async def result():
        response = await client.post(f"{root}/{id}/evaluate", headers=account("alice"))
        assert response.status_code == 200
        return response.json()

    assert (await result())["count"] == 1
    data[users[0].id]["songs"][0]["genre"] = "Jazz"
    assert (await result())["count"] == 0
    data[users[0].id]["liked"].add("b")
    assert [track["nd_song_id"] for track in (await result())["tracks"]] == ["b"]
    async with sessions() as s:
        s.add(PlayEvent(user_id=users[0].id, event_id="heard", ts=utcnow(), kind="finish", nd_song_id="b",
                        listened_ms=180000, client="app/1"))
        await s.commit()
    assert (await result())["count"] == 0
    preview = await client.post(root + "/preview", json={"rules": [{"kind": "liked"}]}, headers=account("bob"))
    assert [track["nd_song_id"] for track in preview.json()["tracks"]] == ["private"]


async def test_history_is_all_time_per_account_and_ignores_non_listening_events(api):
    _, sessions, users, _, _ = api
    async with sessions() as s:
        for id, user, kind, age, ms in [("old", users[0], "finish", 900, None),
                                        ("liked", users[0], "like", 1, None),
                                        ("started", users[0], "start", 1, None),
                                        ("zero", users[0], "finish", 1, 0),
                                        ("progress", users[0], "progress", 1, 20000),
                                        ("other", users[1], "finish", 1, None)]:
            s.add(PlayEvent(user_id=user.id, event_id=id, ts=NOW - timedelta(days=age),
                            kind=kind, nd_song_id=id, listened_ms=ms, client="app/1"))
        await s.commit()
        history = await listening_history(s, users[0].id, NOW)
        assert set(history) == {"old", "progress"}
        assert history["old"] == NOW - timedelta(days=900)
