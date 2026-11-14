"""Timezone expiry, verified aliases, reversibility and account isolation."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db import session_scope
from app.models import FavoriteSubject, User, utcnow
from app.recommendation_models import RecommendationPause
from app.recommendation_policy import Policy, PolicyError, expires_at, load_policy, resolve_target
from tests.test_mixes import add_track, add_user
from tests.test_wave import cand, env


def test_today_uses_next_local_midnight_including_dst():
    now = datetime(2026, 3, 8, 5, 30)  # New York 00:30 before the DST jump
    assert expires_at("today", now, "America/New_York") == datetime(2026, 3, 9, 4)
    assert expires_at("today", datetime(2026, 11, 13, 22), "Europe/Moscow") == datetime(2026, 11, 14, 21)
    aware = datetime(2026, 11, 13, 22, tzinfo=UTC)
    assert expires_at("week", aware) == datetime(2026, 11, 20, 22)
    assert expires_at("less_artist", aware) == datetime(2026, 12, 13, 22)


def test_soft_artist_pause_is_not_track_block_or_global_mutation():
    c = cand(1, "Кино")
    policy = Policy(quiet_artists={c.artist})
    assert policy.multiplier(c) == 0.25 and not policy.blocked(c)
    assert Policy().multiplier(c) == 1.0
    assert policy.multiplier(cand(2, "Другой")) == 1.0


async def test_alias_expiry_user_isolation_and_reversible_removal(env):
    await add_track(12)
    me, other = env["admin"], await add_user("listener")
    now = utcnow()
    async with session_scope() as s:
        s.add(FavoriteSubject(target_type="track", canonical_key="ref:dz:12", ref="dz:12", nd_id="nd12",
                              aliases=["ref:dz:12", "nd:nd12", "nd:old12"], data={}))
        active = RecommendationPause(user_id=me, action="week", label="song", ref="dz:12", nd_song_id=None,
                                     artist="Свой 12", artist_key="свой 12", aliases=["ref:dz:12"],
                                     expires_at=now + timedelta(days=7))
        expired = RecommendationPause(user_id=me, action="week", label="old", ref="dz:13", nd_song_id=None,
                                      artist="Other", artist_key="other", aliases=["ref:dz:13"],
                                      expires_at=now - timedelta(seconds=1))
        s.add_all([active, expired])
        await s.flush()
        pause_id = active.id
    policy = await load_policy(me)
    assert policy.blocked(cand(12))
    old = cand(500)
    old.nd_song_id = "old12"
    assert policy.blocked(old)
    assert not policy.blocked(cand(13))
    assert not (await load_policy(other)).blocked(cand(12))
    response = await env["client"].get("/api/recommendations/pauses")
    assert response.status_code == 200 and [x["id"] for x in response.json()["items"]] == [pause_id]
    assert response.json()["items"][0]["expires_at"].endswith("+00:00")
    response = await env["client"].delete(f"/api/recommendations/pauses/{pause_id}")
    assert response.status_code == 204
    assert not (await load_policy(me)).blocked(old)


async def test_server_resolves_metadata_rejects_arbitrary_alias_and_deduplicates(env):
    await add_track(12)
    await add_track(13)
    c = env["client"]
    response = await c.post("/api/recommendations/pauses", json={"action": "week", "ref": "dz:12",
                                                                  "nd_song_id": "nd13"})
    assert response.status_code == 422
    response = await c.post("/api/recommendations/pauses", json={"action": "less_artist", "ref": "dz:12",
                                                                  "artist": "Injected artist"})
    assert response.status_code == 422
    first = await c.post("/api/recommendations/pauses", json={"action": "week", "ref": "dz:12"})
    assert first.status_code == 201, first.text
    item = first.json()
    assert item["artist"] == "Свой 12" and item["nd_song_id"] == "nd12"
    again = await c.post("/api/recommendations/pauses", json={"action": "week", "nd_song_id": "nd12"})
    assert again.status_code == 201 and again.json()["id"] == item["id"]
    soft = await c.post("/api/recommendations/pauses", json={"action": "less_artist", "ref": "dz:12"})
    assert soft.status_code == 201 and soft.json()["label"] == "Свой 12"
    policy = await load_policy(env["admin"])
    assert policy.multiplier(cand(90, "Свой 12")) == 0.25
    await c.delete(f"/api/recommendations/pauses/{soft.json()['id']}")
    assert (await load_policy(env["admin"])).multiplier(cand(90, "Свой 12")) == 1
    async with session_scope() as s:
        rows = list((await s.execute(select(RecommendationPause).where(RecommendationPause.user_id == env["admin"]))).scalars())
        assert len(rows) == 1


async def test_cannot_delete_another_accounts_pause(env):
    other = await add_user("other")
    async with session_scope() as s:
        row = RecommendationPause(user_id=other, action="week", label="other", ref="dz:1", artist="A",
                                  artist_key="a", aliases=["ref:dz:1"], expires_at=utcnow() + timedelta(days=7))
        s.add(row)
        await s.flush()
        id_ = row.id
    response = await env["client"].delete(f"/api/recommendations/pauses/{id_}")
    assert response.status_code == 404
    async with session_scope() as s:
        assert await s.get(RecommendationPause, id_) is not None


async def test_unknown_track_is_not_accepted_as_resolved_identity(env):
    async with session_scope() as s:
        user = await s.get(User, env["admin"])
        with pytest.raises(PolicyError) as error:
            await resolve_target(s, user, "unsupported:missing", None)
        assert error.value.status == 404
