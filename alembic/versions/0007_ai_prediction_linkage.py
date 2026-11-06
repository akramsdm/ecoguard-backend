"""Add AI prediction linkage to reviews and priority fields.

Revision ID: 0007_ai_prediction_linkage
Revises: 0006_areas_osm_districts
Create Date: 2026-11-04
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_ai_prediction_linkage"
down_revision = "0006_areas_osm_districts"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("reviews", sa.Column("ai_prediction_id", sa.String(36), sa.ForeignKey("predictions.id"), nullable=True))
    op.add_column("reviews", sa.Column("ai_species", sa.String(100), nullable=True))
    op.add_column("reviews", sa.Column("ai_confidence", sa.Float(), nullable=True))

    op.add_column("predictions", sa.Column("priority", sa.String(16), nullable=True))
    op.add_column("predictions", sa.Column("suggested_decision", sa.String(24), nullable=True))

    op.add_column("reports", sa.Column("ai_priority", sa.String(16), nullable=True))
    op.create_index("ix_reports_ai_priority", "reports", ["ai_priority"])


def downgrade():
    op.drop_index("ix_reports_ai_priority", table_name="reports")
    op.drop_column("reports", "ai_priority")
    op.drop_column("predictions", "suggested_decision")
    op.drop_column("predictions", "priority")
    op.drop_column("reviews", "ai_confidence")
    op.drop_column("reviews", "ai_species")
    op.drop_column("reviews", "ai_prediction_id")