"""Recommendation controls must not reset queues, listening history or old explanations."""
import json
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


def test_recommendation_upgrade_preserves_old_sessions_and_history(tmp_path):
    import app.db

    path = tmp_path / "hub.sqlite3"
    root = Path(app.db.__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "2d9a7c6e4b10")
    queue = json.dumps({"owner": "desktop", "position": 42, "queue": ["nd:song"]})
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO users (id,nd_username,nd_password_enc,is_admin,created_at) "
                  "VALUES (1,'qa','unused',0,'2026-11-01')")
        c.execute("INSERT INTO playback_sessions (user_id,payload) VALUES (1,?)", (queue,))
        c.execute("INSERT INTO wave_sessions (id,user_id,created_at,updated_at,mode,state_json) "
                  "VALUES ('old',1,'2026-11-01','2026-11-01','normal','{}')")
        c.execute("INSERT INTO wave_items (session_id,user_id,first_handed_at,last_handed_at,position,ref,key,generator) "
                  "VALUES ('old',1,'2026-11-01','2026-11-01',0,'dz:1','dz:1','seed')")
        c.execute("INSERT INTO play_events (user_id,event_id,ts,kind,ref,client,received_at) "
                  "VALUES (1,'like-1','2026-11-01','like','dz:1','pwa','2026-11-01')")
    command.upgrade(cfg, "head")
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT payload FROM playback_sessions").fetchone()[0] == queue
        assert c.execute("SELECT ref,decision_json FROM wave_items").fetchall() == [("dz:1", None)]
        assert c.execute("SELECT event_id,kind FROM play_events").fetchall() == [("like-1", "like")]
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("INSERT INTO release_seen (user_id,ref,seen_at) VALUES (1,'dz:10','2026-11-01')")
        c.execute("INSERT INTO recommendation_pauses "
                  "(user_id,action,label,ref,artist,artist_key,aliases,created_at,expires_at) "
                  "VALUES (1,'week','Track','dz:1','Artist','artist','[\"dz:1\"]','2026-11-01','2026-11-08')")
        c.execute("UPDATE wave_items SET decision_json=?", (json.dumps({"explanation": ["Recorded reason"]}),))
    command.downgrade(cfg, "2d9a7c6e4b10")
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT payload FROM playback_sessions").fetchone()[0] == queue
        assert c.execute("SELECT ref FROM wave_items").fetchall() == [("dz:1",)]
        assert c.execute("SELECT event_id,kind FROM play_events").fetchall() == [("like-1", "like")]
    command.upgrade(cfg, "head")
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT payload FROM playback_sessions").fetchone()[0] == queue
        assert c.execute("SELECT ref,decision_json FROM wave_items").fetchall() == [("dz:1", None)]
