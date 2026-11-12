"""Current-track download status must not depend on the global history page."""
from app import db
from app.api.downloads import list_items
from app.models import DownloadBatch, DownloadItem, User
from app.security import encrypt


async def test_reference_filter_precedes_limit_and_keeps_account_scope(hub_env):
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
            expected = DownloadItem(user_id=owner.id, batch_id=batches[0].id, status="failed",
                query_json={"query": {"artist": "Artist", "title": "Wanted"}, "meta": {"track_ref": "dz:1"}})
            s.add(expected)
            await s.flush()
            # Newer unrelated downloads and another user's matching ref must not hide or replace it.
            s.add_all([
                DownloadItem(user_id=owner.id, batch_id=batches[0].id, status="done",
                    query_json={"query": {"artist": "Artist", "title": "Other"}, "meta": {"track_ref": "dz:2"}}),
                DownloadItem(user_id=other.id, batch_id=batches[1].id, status="downloading",
                    query_json={"query": {"artist": "Artist", "title": "Wanted"}, "meta": {"track_ref": "dz:1"}}),
            ])
            await s.flush()
            result = await list_items(status=None, limit=1, user=owner, s=s, ref="dz:1")
            assert [(item.id, item.status, item.track_ref) for item in result] == [(expected.id, "failed", "dz:1")]
            assert await list_items(status=None, limit=1, user=owner, s=s, ref="dz:missing") == []
    finally:
        await db.dispose()
