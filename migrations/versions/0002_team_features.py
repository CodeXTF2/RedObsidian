"""Team features: roles and membership, admin settings, 2FA, audit log, uploads, page history, findings

Revision ID: 0002_team_features
Revises: 0001_baseline
Create Date: 2026-10-21

"""
import secrets

from alembic import op
import sqlalchemy as sa

revision = "0002_team_features"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user") as batch:
        batch.add_column(sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("disabled", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("session_token", sa.String(32), nullable=False, server_default=""))
        batch.add_column(sa.Column("totp_secret", sa.String(64), nullable=True))
        batch.add_column(sa.Column("totp_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("totp_last_step", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("recovery_codes_json", sa.Text(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("sso_subject", sa.String(255), nullable=True))
        batch.add_column(sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_unique_constraint("uq_user_sso_subject", ["sso_subject"])

    with op.batch_alter_table("project") as batch:
        batch.add_column(sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("report_json", sa.Text(), nullable=False, server_default="{}"))

    op.create_table(
        "setting",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "login_failure",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("ip", sa.String(64), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_login_failure_username", "login_failure", ["username"])
    op.create_index("ix_login_failure_ip", "login_failure", ["ip"])
    op.create_index("ix_login_failure_at", "login_failure", ["at"])

    op.create_table(
        "project_member",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "user_id", name="unique_project_member"),
    )
    op.create_index("ix_project_member_project_id", "project_member", ["project_id"])
    op.create_index("ix_project_member_user_id", "project_member", ["user_id"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=True),
        sa.Column("target_id", sa.String(64), nullable=True),
        sa.Column("details_json", sa.Text(), nullable=False),
        sa.Column("ip", sa.String(64), nullable=True),
    )
    op.create_index("ix_audit_log_at", "audit_log", ["at"])
    op.create_index("ix_audit_log_user_id", "audit_log", ["user_id"])
    op.create_index("ix_audit_log_project_id", "audit_log", ["project_id"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])

    op.create_table(
        "upload",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False, unique=True),
        sa.Column("original_name", sa.String(255), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=True),
    )
    op.create_index("ix_upload_project_id", "upload", ["project_id"])

    op.create_table(
        "node_revision",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("node_id", sa.Integer(), sa.ForeignKey("graph_node.id"), nullable=False),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("caption", sa.String(280), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=True),
    )
    op.create_index("ix_node_revision_node_id", "node_revision", ["node_id"])
    op.create_index("ix_node_revision_project_id", "node_revision", ["project_id"])

    op.create_table(
        "finding",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("cvss_score", sa.Float(), nullable=True),
        sa.Column("cvss_vector", sa.String(200), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("affected_assets", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("impact", sa.Text(), nullable=True),
        sa.Column("remediation", sa.Text(), nullable=True),
        sa.Column("references", sa.Text(), nullable=True),
        sa.Column("evidence_node_ids_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
    )
    op.create_index("ix_finding_project_id", "finding", ["project_id"])

    migrate_existing_data()


def migrate_existing_data():
    connection = op.get_bind()
    user_ids = [row[0] for row in connection.execute(sa.text('SELECT id FROM "user" ORDER BY id'))]

    # Each existing account gets its own session token (this signs everyone out once)
    for user_id in user_ids:
        connection.execute(sa.text('UPDATE "user" SET session_token = :token WHERE id = :id'),
                           {"token": secrets.token_hex(16), "id": user_id})

    # The first account becomes the administrator
    if user_ids:
        connection.execute(sa.text('UPDATE "user" SET is_admin = :yes WHERE id = :id'), {"yes": True, "id": user_ids[0]})

    # Before membership existed every user could open every project; keep that access so nobody is locked out.
    # Creators become owners, everyone else editors. Owners can trim the list afterwards.
    now = sa.func.now()
    projects = list(connection.execute(sa.text("SELECT id, created_by_id FROM project")))
    member_table = sa.table("project_member", sa.column("project_id"), sa.column("user_id"), sa.column("role"),
                            sa.column("created_at"))
    for project_id, creator_id in projects:
        for user_id in user_ids:
            connection.execute(member_table.insert().values(
                project_id=project_id, user_id=user_id, role="owner" if user_id == creator_id else "editor",
                created_at=now))


def downgrade():
    op.drop_table("finding")
    op.drop_table("node_revision")
    op.drop_table("upload")
    op.drop_table("audit_log")
    op.drop_table("project_member")
    op.drop_table("login_failure")
    op.drop_table("setting")
    with op.batch_alter_table("project") as batch:
        batch.drop_column("report_json")
        batch.drop_column("archived_at")
    with op.batch_alter_table("user") as batch:
        batch.drop_constraint("uq_user_sso_subject", type_="unique")
        for column in ("last_login_at", "sso_subject", "recovery_codes_json", "totp_last_step", "totp_enabled",
                       "totp_secret", "session_token", "password_changed_at", "must_change_password", "disabled",
                       "is_admin"):
            batch.drop_column(column)
