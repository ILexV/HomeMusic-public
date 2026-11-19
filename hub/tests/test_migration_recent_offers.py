"""Legacy mixes have no per-item ready time; only active, available rows have evidence of an offer."""
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config

from app import db


def test_only_playable_active_rows_are_backfilled(db_file):
    root = Path(db.__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file.as_posix()}")
    cfg.attributes["configure_logger"] = False
    command.downgrade(cfg, "4f9b2c8d1e30")
    created = "2026-11-01 12:00:00.000000"
    with sqlite3.connect(db_file) as c:
        c.execute("INSERT INTO users (id,nd_username,nd_password_enc,is_admin,created_at) VALUES (1,'me','',0,?)", (created,))
        c.execute("INSERT INTO mixes (id,user_id,created_at,title,description,mode,status,stats_json) "
                  "VALUES (1,1,?,'Mix','','normal','closed','{}')", (created,))
        rows = [(0, "active", "s"), (1, "active", None), (2, "reserve", "r"), (3, "failed", "f")]
        c.executemany("INSERT INTO mix_items (mix_id,position,state,nd_song_id,familiar,generator,score,features_json,downloaded_for_mix) "
                      "VALUES (1,?,?,?,0,'stash',1,'{}',0)", rows)
    command.upgrade(cfg, "7d2f16b9c840")
    with sqlite3.connect(db_file) as c:
        assert c.execute("SELECT offered_at FROM mix_items ORDER BY position").fetchall() == [(created,), (None,), (None,), (None,)]
