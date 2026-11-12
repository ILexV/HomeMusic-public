"""Saved rule playlists and account-scoped shared playback sessions."""
import sqlalchemy as sa
from alembic import op

revision = "2d9a7c6e4b10"
down_revision = "a3c8e5f17b92"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "smart_playlists",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_smart_playlists_user_id", "smart_playlists", ["user_id"])
    op.create_table(
        "playback_sessions",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  primary_key=True, nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )


def downgrade():
    op.drop_table("playback_sessions")
    op.drop_index("ix_smart_playlists_user_id", table_name="smart_playlists")
    op.drop_table("smart_playlists")
