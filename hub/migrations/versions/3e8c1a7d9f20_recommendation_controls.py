"""Recommendation decision snapshots, temporary pauses and release read marks."""
import sqlalchemy as sa
from alembic import op

revision = "3e8c1a7d9f20"
down_revision = "2d9a7c6e4b10"
branch_labels = depends_on = None


def upgrade():
    op.add_column("wave_items", sa.Column("decision_json", sa.JSON(), nullable=True))
    op.add_column("smart_items", sa.Column("decision_json", sa.JSON(), nullable=True))
    op.create_table(
        "recommendation_pauses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("label", sa.String(1024), nullable=False),
        sa.Column("ref", sa.String(512), nullable=True),
        sa.Column("nd_song_id", sa.String(64), nullable=True),
        sa.Column("artist", sa.String(512), nullable=False),
        sa.Column("artist_key", sa.String(512), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_recommendation_pauses_user_expires", "recommendation_pauses", ["user_id", "expires_at"])
    op.create_table(
        "release_seen",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("ref", sa.String(64), primary_key=True),
        sa.Column("seen_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table("release_seen")
    op.drop_index("ix_recommendation_pauses_user_expires", table_name="recommendation_pauses")
    op.drop_table("recommendation_pauses")
    with op.batch_alter_table("smart_items") as batch:
        batch.drop_column("decision_json")
    with op.batch_alter_table("wave_items") as batch:
        batch.drop_column("decision_json")
