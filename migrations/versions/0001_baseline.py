"""Baseline: the schema created by versions before migrations were introduced

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-21

"""
from alembic import op
import sqlalchemy as sa

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_user_username", "user", ["username"], unique=True)

    op.create_table(
        "project",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
    )

    op.create_table(
        "timeline_event",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("files_json", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("order_index", sa.Float(), nullable=False),
        sa.Column("manual_order", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
    )
    op.create_index("ix_timeline_event_project_id", "timeline_event", ["project_id"])
    op.create_index("ix_timeline_event_occurred_at", "timeline_event", ["occurred_at"])
    op.create_index("ix_timeline_event_ended_at", "timeline_event", ["ended_at"])

    op.create_table(
        "graph_node",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("graph_node.id"), nullable=True),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("caption", sa.String(280), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("files_json", sa.Text(), nullable=False),
        sa.Column("order_index", sa.Float(), nullable=False),
        sa.Column("color", sa.String(20), nullable=True),
        sa.Column("in_graph", sa.Boolean(), nullable=False),
        sa.Column("x", sa.Float(), nullable=False),
        sa.Column("y", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
    )
    op.create_index("ix_graph_node_project_id", "graph_node", ["project_id"])
    op.create_index("ix_graph_node_parent_id", "graph_node", ["parent_id"])
    op.create_index("ix_graph_node_deleted_at", "graph_node", ["deleted_at"])

    op.create_table(
        "graph_edge",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("graph_node.id"), nullable=False),
        sa.Column("target_id", sa.Integer(), sa.ForeignKey("graph_node.id"), nullable=False),
        sa.Column("joint_x", sa.Float(), nullable=True),
        sa.Column("joint_y", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.CheckConstraint("source_id != target_id", name="no_self_edge"),
        sa.UniqueConstraint("source_id", "target_id", name="unique_directed_edge"),
    )
    op.create_index("ix_graph_edge_project_id", "graph_edge", ["project_id"])


def downgrade():
    op.drop_table("graph_edge")
    op.drop_table("graph_node")
    op.drop_table("timeline_event")
    op.drop_table("project")
    op.drop_table("user")
