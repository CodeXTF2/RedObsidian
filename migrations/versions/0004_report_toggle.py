"""Per-project toggle for the Report (beta) feature, off by default

Revision ID: 0004_report_toggle
Revises: 0003_findings_toggle
Create Date: 2026-10-21

"""
from alembic import op
import sqlalchemy as sa

revision = "0004_report_toggle"
down_revision = "0003_findings_toggle"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("project") as batch:
        batch.add_column(sa.Column("report_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    with op.batch_alter_table("project") as batch:
        batch.drop_column("report_enabled")
