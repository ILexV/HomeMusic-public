"""Download history filters and counters must cover all visible, account-owned rows."""

import pytest

from app import db
from app.auth import current_user
from app.db import get_session
from app.library.owned import ACTIVE_STATUSES, quiet
from app.models import DownloadBatch, DownloadItem, User
from app.security import encrypt
from tests.support import app_client


@pytest.fixture
async def page_env(hub_env):
    from app.main import create_app

    db.init_engine()
    try:
        async with db.session_scope() as s:
            owner = User(nd_username="owner", nd_password_enc=encrypt("password"))
            other = User(nd_username="other", nd_password_enc=encrypt("password"))
            s.add_all([owner, other])
            await s.flush()
            batches = [DownloadBatch(user_id=user.id, origin="catalog") for user in (owner, other)]
            s.add_all(batches)
            await s.flush()
            app = create_app()
            app.dependency_overrides[current_user] = lambda: owner
            app.dependency_overrides[get_session] = lambda: s
            async with app_client(app, login=None) as client:
                yield s, client, owner, other, batches
    finally:
        await db.dispose()


def item(user, batch, status, **flags):
    return DownloadItem(user_id=user.id, batch_id=batch.id, status=status,
                        query_json={"query": {"artist": "Artist", "title": "Track"}, **flags})


async def test_old_failure_is_filtered_before_limit_and_legacy_lookup_is_unchanged(page_env):
    s, client, owner, other, batches = page_env
    old = item(owner, batches[0], "failed")
    s.add(old)
    await s.flush()
    s.add_all([item(owner, batches[0], "done") for _ in range(250)])
    s.add(item(other, batches[1], "failed"))
    await s.flush()

    response = await client.get("/api/downloads/page", params={"filter": "problems", "limit": 1})
    # Also guards registration order: /page must not be parsed as /{item_id}.
    assert response.status_code == 200, response.text
    page = response.json()
    assert [row["id"] for row in page["items"]] == [old.id]
    assert page["next_cursor"] is None
    assert page["counts"] == {"all": 251, "active": 0, "problems": 1, "finished": 250}

    legacy = await client.get("/api/downloads", params={"limit": 200})
    assert legacy.status_code == 200, legacy.text
    assert isinstance(legacy.json(), list)
    assert len(legacy.json()) == 200
    assert old.id not in {row["id"] for row in legacy.json()}


@pytest.mark.parametrize("filter", ["all", "active", "problems"])
async def test_filter_pages_and_counts_are_account_scoped_and_cursor_exclusive(page_env, filter):
    s, client, owner, other, batches = page_env
    statuses = ["queued", "needs_review", "cancelled", "searching", "failed", "done", "downloading", "tagging", "importing"]
    rows = [item(owner, batches[0], status) for status in statuses]
    foreign = [item(other, batches[1], status) for status in statuses]
    s.add_all([*rows, *foreign])
    await s.flush()
    expected = [row.id for row in reversed(rows) if filter == "all"
                or (filter == "active" and row.status in ACTIVE_STATUSES)
                or (filter == "problems" and row.status in ("failed", "needs_review"))]
    counts = {"all": 9, "active": 5, "problems": 2, "finished": 2}
    seen = []
    before = None
    while True:
        params = {"filter": filter, "limit": 1}
        if before is not None:
            params["before_id"] = before
        response = await client.get("/api/downloads/page", params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["counts"] == counts
        ids = [row["id"] for row in page["items"]]
        assert ids and (before is None or all(id_ < before for id_ in ids))
        assert not set(ids).intersection(seen)
        seen.extend(ids)
        if page["next_cursor"] is None:
            break
        assert page["next_cursor"] == ids[-1]
        before = page["next_cursor"]
    assert seen == expected

    # A cursor is only an exclusive id bound, never authorization for the cursor owner's history.
    response = await client.get("/api/downloads/page", params={"before_id": foreign[-1].id, "filter": filter})
    assert [row["id"] for row in response.json()["items"]] == expected
    empty = (await client.get("/api/downloads/page", params={"filter": filter, "before_id": 1})).json()
    assert empty == {"items": [], "next_cursor": None, "counts": counts}


async def test_new_arrivals_do_not_duplicate_or_skip_an_existing_cursor(page_env):
    s, client, owner, _, batches = page_env
    rows = [item(owner, batches[0], "failed") for _ in range(5)]
    s.add_all(rows)
    await s.flush()
    first = (await client.get("/api/downloads/page", params={"filter": "problems", "limit": 2})).json()
    inserted = item(owner, batches[0], "failed")
    s.add(inserted)
    await s.flush()
    second = (await client.get("/api/downloads/page", params={
        "filter": "problems", "limit": 2, "before_id": first["next_cursor"],
    })).json()
    third = (await client.get("/api/downloads/page", params={
        "filter": "problems", "limit": 2, "before_id": second["next_cursor"],
    })).json()
    assert [row["id"] for page in (first, second, third) for row in page["items"]] == [row.id for row in reversed(rows)]
    assert third["next_cursor"] is None
    assert second["counts"] == third["counts"] == {"all": 6, "active": 0, "problems": 6, "finished": 0}


async def test_visibility_and_counts_match_quiet_favorite_import_mix_and_wave_semantics(page_env):
    s, client, owner, _, batches = page_env
    flags = [
        {}, {"star": True}, {"star": True, "star_failed": True}, {"star": True, "star_cancelled": True},
        {"star": True, "requested": True}, {"import": {"id": 7}}, {"import": {"id": 7}, "requested": True},
        {"mix": {"id": 8}}, {"mix": {"id": 8}, "requested": True},
        {"wave": {"id": 9}}, {"wave": {"id": 9}, "requested": True},
        {"star": False, "import": None, "mix": {}, "wave": []},
        {"star": "", "requested": False}, {"star": 0, "requested": None},
        {"star": True, "requested": []}, {"star": True, "requested": {}},
        {"star": "0"}, {"star": "{}"}, {"star": True, "requested": "yes"},
    ]
    statuses = ("done", "failed", "needs_review", "queued", "cancelled")
    rows = [item(owner, batches[0], statuses[index % len(statuses)], **values) for index, values in enumerate(flags)]
    s.add_all(rows)
    await s.flush()
    visible = [row for row in rows if not quiet(row.query_json)]
    counts = {
        "all": len(visible),
        "active": sum(row.status in ACTIVE_STATUSES for row in visible),
        "problems": sum(row.status in ("failed", "needs_review") for row in visible),
        "finished": sum(row.status in ("done", "cancelled") for row in visible),
    }
    for filter in ("all", "active", "problems"):
        response = await client.get("/api/downloads/page", params={"filter": filter})
        assert response.status_code == 200, response.text
        page = response.json()
        expected = [row.id for row in reversed(visible) if filter == "all"
                    or (filter == "active" and row.status in ACTIVE_STATUSES)
                    or (filter == "problems" and row.status in ("failed", "needs_review"))]
        assert [row["id"] for row in page["items"]] == expected
        assert all(not row["star"] for row in page["items"])
        assert page["counts"] == counts
        assert page["next_cursor"] is None


async def test_page_defaults_bounds_and_empty_history(page_env):
    s, client, owner, _, batches = page_env
    response = await client.get("/api/downloads/page")
    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "next_cursor": None,
                               "counts": {"all": 0, "active": 0, "problems": 0, "finished": 0}}
    s.add_all([item(owner, batches[0], "done") for _ in range(51)])
    await s.flush()
    page = (await client.get("/api/downloads/page")).json()
    assert len(page["items"]) == 50
    assert page["next_cursor"] == page["items"][-1]["id"]
    assert page["counts"]["all"] == 51
    for params in ({"limit": 0}, {"limit": 201}, {"before_id": 0}, {"before_id": -1}, {"filter": "done"}):
        invalid = await client.get("/api/downloads/page", params=params)
        assert invalid.status_code == 422, invalid.text
