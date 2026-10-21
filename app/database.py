"""Schema setup: Alembic migrations, plus a bridge for databases created before migrations existed."""
import logging

from flask_migrate import stamp, upgrade
from sqlalchemy import inspect, text

from . import db

log = logging.getLogger(__name__)

# The migration that matches the schema the pre-Alembic code created
BASELINE_REVISION = "0001_baseline"


def init_database(app):
    with app.app_context():
        tables = set(inspect(db.engine).get_table_names())
        if tables and "alembic_version" not in tables:
            log.info("Existing database without migration history; bringing it to the baseline schema")
            legacy_upgrade(tables)
            stamp(revision=BASELINE_REVISION)
        upgrade()
        post_migrate_fixups(app)


def legacy_upgrade(table_names):
    """The column additions the old ensure_schema() made at startup, so any old database reaches the baseline."""
    inspector = inspect(db.engine)

    def columns(table):
        return {column["name"] for column in inspector.get_columns(table)}

    statements = []
    for table in ("timeline_event", "graph_node", "graph_edge"):
        if table in table_names and "project_id" not in columns(table):
            statements.append(f"ALTER TABLE {table} ADD COLUMN project_id INTEGER REFERENCES project(id)")
            statements.append(f"UPDATE {table} SET project_id = (SELECT MIN(id) FROM project)")  # noqa: S608 - fixed table names
    if "graph_node" in table_names:
        existing = columns("graph_node")
        for name, ddl in (
            ("parent_id", "INTEGER"),
            ("order_index", "FLOAT DEFAULT 0 NOT NULL"),
            ("files_json", "TEXT DEFAULT '[]' NOT NULL"),
            ("deleted_at", "DATETIME"),
        ):
            if name not in existing:
                statements.append(f"ALTER TABLE graph_node ADD COLUMN {name} {ddl}")
    if "timeline_event" in table_names:
        existing = columns("timeline_event")
        if "order_index" not in existing:
            statements.append("ALTER TABLE timeline_event ADD COLUMN order_index FLOAT DEFAULT 0 NOT NULL")
            statements.append("UPDATE timeline_event SET order_index = -strftime('%s', occurred_at)")
        for name, ddl in (
            ("manual_order", "BOOLEAN DEFAULT 0 NOT NULL"),
            ("files_json", "TEXT DEFAULT '[]' NOT NULL"),
            ("ended_at", "DATETIME"),
        ):
            if name not in existing:
                statements.append(f"ALTER TABLE timeline_event ADD COLUMN {name} {ddl}")
    if "graph_edge" in table_names:
        existing = columns("graph_edge")
        for name in ("joint_x", "joint_y"):
            if name not in existing:
                statements.append(f"ALTER TABLE graph_edge ADD COLUMN {name} FLOAT")
    # Columns added by ALTER TABLE never got the indexes create_all() would have made
    for name, table, column in (
        ("ix_timeline_event_project_id", "timeline_event", "project_id"),
        ("ix_timeline_event_occurred_at", "timeline_event", "occurred_at"),
        ("ix_timeline_event_ended_at", "timeline_event", "ended_at"),
        ("ix_graph_node_project_id", "graph_node", "project_id"),
        ("ix_graph_node_parent_id", "graph_node", "parent_id"),
        ("ix_graph_node_deleted_at", "graph_node", "deleted_at"),
        ("ix_graph_edge_project_id", "graph_edge", "project_id"),
    ):
        if table in table_names:
            statements.append(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({column})")
    if "user" in table_names:
        statements.append('CREATE UNIQUE INDEX IF NOT EXISTS ix_user_username ON "user" (username)')
    for statement in statements:
        db.session.execute(text(statement))
    db.session.commit()


def post_migrate_fixups(app):
    from .maintenance import register_untracked_uploads
    from .models import GraphNode, Project

    # Every project needs at least one page for the Vault to open on
    for project in Project.query.all():
        if GraphNode.query.filter_by(project_id=project.id, deleted_at=None).count() == 0:
            db.session.add(GraphNode(project_id=project.id, title="Untitled page", in_graph=False,
                                     user_id=project.created_by_id))
    db.session.commit()
    register_untracked_uploads(app)
