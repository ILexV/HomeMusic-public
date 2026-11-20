"""Preserve playback intent and index cross-mode offer history."""

import sqlalchemy as sa
from alembic import op

revision = "9b8c3d1f6a20"
down_revision = "7d2f16b9c840"
branch_labels = depends_on = None


def upgrade():
    # No backfill: a screen, launch or list position cannot prove a track was selected.
    op.add_column("play_events", sa.Column("play_trigger", sa.String(8), nullable=True))
    op.create_index("ix_wave_items_user_last_handed", "wave_items", ["user_id", "last_handed_at"])
    op.create_index("ix_smart_items_user_handed", "smart_items", ["user_id", "handed_at"])
    op.create_table("wave_reoffers",
                    sa.Column("item_id", sa.Integer(), sa.ForeignKey("wave_items.id", ondelete="CASCADE"), primary_key=True),
                    sa.Column("handed_at", sa.DateTime(), primary_key=True))


def downgrade():
    op.drop_table("wave_reoffers")
    op.drop_index("ix_smart_items_user_handed", table_name="smart_items")
    op.drop_index("ix_wave_items_user_last_handed", table_name="wave_items")
    op.drop_column("play_events", "play_trigger")
