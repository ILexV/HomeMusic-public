"""Account-owned recording bindings and reversible candidate feedback."""
import sqlalchemy as sa
from alembic import op

revision = "4f9b2c8d1e30"
down_revision = "3e8c1a7d9f20"
branch_labels = depends_on = None


def upgrade():
    op.create_table("recording_bindings",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ref", sa.String(512), nullable=False),
        sa.Column("provider", sa.String(64), nullable=True),
        sa.Column("provider_id", sa.String(512), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("served_at", sa.DateTime(), nullable=True),
        sa.Column("full_verified", sa.Boolean(), nullable=True),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.Column("provenance", sa.String(64), nullable=False))
    op.create_index("ix_recording_bindings_user_ref", "recording_bindings", ["user_id", "ref", "created_at"])
    op.create_table("recording_feedback",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ref", sa.String(512), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("provider_id", sa.String(512), nullable=False),
        sa.Column("binding_id", sa.String(64), sa.ForeignKey("recording_bindings.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("user_id", "ref", "provider", "provider_id", name="uq_recording_feedback_candidate"))


def downgrade():
    op.drop_table("recording_feedback")
    op.drop_index("ix_recording_bindings_user_ref", table_name="recording_bindings")
    op.drop_table("recording_bindings")
