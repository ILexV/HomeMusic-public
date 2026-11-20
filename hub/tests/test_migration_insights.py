"""The expanding insights migration (INSIGHTS.md 6.1) keeps old wave sessions and events readable."""
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config


def test_insights_upgrade_keeps_old_wave_sessions_and_events(tmp_path):
    import app.db

    db = tmp_path / "hub.sqlite3"
    root = Path(app.db.__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "6a3d8c2e710f")
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO users (id,nd_username,nd_password_enc,is_admin,created_at) "
                  "VALUES (1,'qa','unused',0,'2020-01-01')")
        c.execute("INSERT INTO wave_sessions (id,user_id,created_at,updated_at,mode,seed_json,state_json) "
                  "VALUES ('old',1,'2026-10-01','2026-10-02','normal','{\"ref\": \"dz:1\"}','{\"started\": 2}')")
        c.execute("INSERT INTO play_events (user_id,event_id,ts,kind,ref,client,received_at,context) "
                  "VALUES (1,'e1','2026-10-01','start','dz:1','pwa','2026-10-01','wave:old')")

    command.upgrade(cfg, "head")

    with sqlite3.connect(db) as c:
        assert c.execute("SELECT seed_json,state_json,origin,client,restart_of,taste_model,request_json,next_calls,"
                         "cleared_at FROM wave_sessions").fetchall() == [
            ('{"ref": "dz:1"}', '{"started": 2}', None, None, None, None, None, 0, None)]
        assert c.execute("SELECT event_id,launch_id,launch_origin,play_trigger FROM play_events").fetchall() == [("e1", None, None, None)]
        c.execute("INSERT INTO wave_items (session_id,user_id,first_handed_at,last_handed_at,position,ref,key,"
                  "generator) VALUES ('old',1,'2026-10-01','2026-10-01',0,'dz:1','dz:1','seed')")
        assert c.execute("SELECT handed_count,familiar,score,artist FROM wave_items").fetchall() == [(1, 0, 0, "")]
        indexes = {r[1] for r in c.execute("SELECT * FROM sqlite_master WHERE type='index'")}
        assert {"ix_wave_sessions_user_created", "ix_play_events_user_launch", "ix_wave_items_session_id"} <= indexes
        assert {"ix_wave_items_user_last_handed", "ix_smart_items_user_handed"} <= indexes
    command.downgrade(cfg, "6a3d8c2e710f")
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT id,state_json FROM wave_sessions").fetchall() == [("old", '{"started": 2}')]
        assert c.execute("SELECT event_id FROM play_events").fetchall() == [("e1",)]
        assert not c.execute("SELECT name FROM sqlite_master WHERE name='wave_items'").fetchall()
