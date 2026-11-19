"""Record playable Daily Mix offers and index both weekly exposure windows."""

import sqlalchemy as sa
from alembic import op

revision = "7d2f16b9c840"
down_revision = "4f9b2c8d1e30"
branch_labels = depends_on = None


def upgrade():
    op.add_column("mix_items", sa.Column("offered_at", sa.DateTime(), nullable=True))
    # Historical rows did not record when a reserve was promoted or its download became ready.
    op.execute("""UPDATE mix_items SET offered_at = (SELECT created_at FROM mixes WHERE mixes.id = mix_items.mix_id)
                  WHERE state = 'active' AND nd_song_id IS NOT NULL AND nd_song_id != ''""")
    op.create_index("ix_mix_items_mix_offered", "mix_items", ["mix_id", "offered_at"])
    op.create_index("ix_wave_items_user_first_handed", "wave_items", ["user_id", "first_handed_at"])
    op.drop_index("ix_wave_items_user_id", table_name="wave_items")


def downgrade():
    op.create_index("ix_wave_items_user_id", "wave_items", ["user_id"])
    op.drop_index("ix_wave_items_user_first_handed", table_name="wave_items")
    op.drop_index("ix_mix_items_mix_offered", table_name="mix_items")
    op.drop_column("mix_items", "offered_at")
