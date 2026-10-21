"""Per-project toggle for the Findings (beta) feature, off by default

Revision ID: 0003_findings_toggle
Revises: 0002_team_features
Create Date: 2026-10-21

"""
from alembic import op
import sqlalchemy as sa

revision = "0003_findings_toggle"
down_revision = "0002_team_features"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("project") as batch:
        batch.add_column(sa.Column("findings_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    with op.batch_alter_table("project") as batch:
        batch.drop_column("findings_enabled")
