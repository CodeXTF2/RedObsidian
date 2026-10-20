import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, flash, jsonify, redirect, request, url_for
from flask_login import LoginManager
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFError, CSRFProtect
from sqlalchemy import inspect, text

db = SQLAlchemy()
login_manager = LoginManager()
csrf = CSRFProtect()
socketio = SocketIO(async_mode="gevent")

# Soft-deleted pages are kept this long so they can be restored with undo
DELETED_NODE_RETENTION = timedelta(days=30)


def load_secret_key(instance_path):
    """Use REDOBSIDIAN_SECRET_KEY if set, otherwise a random key persisted in the instance folder."""
    env_key = os.environ.get("REDOBSIDIAN_SECRET_KEY")
    if env_key:
        return env_key
    key_file = Path(instance_path) / "secret_key"
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip()
    key = secrets.token_hex(32)
    key_file.write_text(key, encoding="utf-8")
    try:
        key_file.chmod(0o600)
    except OSError:
        pass
    return key


def allowed_socket_origins():
    """Same-origin only by default. REDOBSIDIAN_ALLOWED_ORIGINS adds a comma-separated allowlist (e.g. behind a proxy)."""
    value = os.environ.get("REDOBSIDIAN_ALLOWED_ORIGINS", "").strip()
    if not value:
        return None
    return [origin.strip().rstrip("/") for origin in value.split(",") if origin.strip()]


def create_app():
    app = Flask(__name__, instance_relative_config=True)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    app.config.from_mapping(
        SECRET_KEY=load_secret_key(app.instance_path),
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{Path(app.instance_path) / 'redteam_obsidian.sqlite'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        REMEMBER_COOKIE_HTTPONLY=True,
        REMEMBER_COOKIE_SAMESITE="Lax",
        # Tokens live as long as the session, so long-open tabs can still upload
        WTF_CSRF_TIME_LIMIT=None,
    )
    if os.environ.get("REDOBSIDIAN_SECURE_COOKIES") == "1":
        app.config.update(SESSION_COOKIE_SECURE=True, REMEMBER_COOKIE_SECURE=True)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    socketio.init_app(app, cors_allowed_origins=allowed_socket_origins())

    @app.errorhandler(CSRFError)
    def handle_csrf_error(error):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Your session expired. Reload the page and try again."}), 400
        flash("Your session expired. Please try again.", "error")
        return redirect(request.referrer or url_for("main.index"))

    login_manager.login_view = "auth.login"
    login_manager.login_message_category = "error"

    from .models import User

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    from .auth import auth_bp
    from .main import main_bp
    from .realtime import register_socket_handlers

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    register_socket_handlers(socketio)

    with app.app_context():
        db.create_all()
        ensure_schema()

    return app


def ensure_schema():
    inspector = inspect(db.engine)
    table_names = inspector.get_table_names()

    from .models import Project, User, GraphNode

    # Ensure Project table exists (db.create_all() should handle it, but for safety)
    if "project" not in table_names:
        db.create_all()
        table_names = inspector.get_table_names()

    # Create default project if none exists
    if Project.query.count() == 0:
        admin = User.query.first()
        if admin:
            default_project = Project(name="Default Project", description="Auto-generated default project", created_by_id=admin.id)
            db.session.add(default_project)
            db.session.commit()

    default_project = Project.query.first()
    default_project_id = default_project.id if default_project else None

    # Update other tables
    for table_name in ["timeline_event", "graph_node", "graph_edge"]:
        if table_name in table_names:
            columns = {column["name"] for column in inspector.get_columns(table_name)}
            if "project_id" not in columns:
                db.session.execute(text(f"ALTER TABLE {table_name} ADD COLUMN project_id INTEGER REFERENCES project(id)"))
                if default_project_id:
                    db.session.execute(text(f"UPDATE {table_name} SET project_id = {default_project_id}"))
                db.session.commit()

    if "graph_node" in table_names:
        graph_node_columns = {column["name"] for column in inspector.get_columns("graph_node")}
        if "parent_id" not in graph_node_columns:
            db.session.execute(text("ALTER TABLE graph_node ADD COLUMN parent_id INTEGER"))
            db.session.commit()
        if "order_index" not in graph_node_columns:
            db.session.execute(text("ALTER TABLE graph_node ADD COLUMN order_index FLOAT DEFAULT 0 NOT NULL"))
            db.session.commit()
        if "files_json" not in graph_node_columns:
            db.session.execute(text("ALTER TABLE graph_node ADD COLUMN files_json TEXT DEFAULT '[]' NOT NULL"))
            db.session.commit()

    if "timeline_event" in table_names:
        timeline_columns = {column["name"] for column in inspector.get_columns("timeline_event")}
        if "order_index" not in timeline_columns:
            db.session.execute(text("ALTER TABLE timeline_event ADD COLUMN order_index FLOAT DEFAULT 0 NOT NULL"))
            db.session.execute(text("UPDATE timeline_event SET order_index = -strftime('%s', occurred_at)"))
            db.session.commit()
        if "manual_order" not in timeline_columns:
            db.session.execute(text("ALTER TABLE timeline_event ADD COLUMN manual_order BOOLEAN DEFAULT 0 NOT NULL"))
            db.session.commit()
        if "files_json" not in timeline_columns:
            db.session.execute(text("ALTER TABLE timeline_event ADD COLUMN files_json TEXT DEFAULT '[]' NOT NULL"))
            db.session.commit()
        if "ended_at" not in timeline_columns:
            db.session.execute(text("ALTER TABLE timeline_event ADD COLUMN ended_at DATETIME"))
            db.session.commit()

    if "graph_node" in table_names:
        graph_node_columns = {column["name"] for column in inspector.get_columns("graph_node")}
        if "deleted_at" not in graph_node_columns:
            db.session.execute(text("ALTER TABLE graph_node ADD COLUMN deleted_at DATETIME"))
            db.session.commit()

    if "graph_edge" in table_names:
        graph_edge_columns = {column["name"] for column in inspector.get_columns("graph_edge")}
        for column in ("joint_x", "joint_y"):
            if column not in graph_edge_columns:
                db.session.execute(text(f"ALTER TABLE graph_edge ADD COLUMN {column} FLOAT"))
                db.session.commit()

    purge_deleted_nodes()

    # Ensure all projects have at least one page (runs after migrations so the
    # GraphNode query only touches columns that exist)
    for project in Project.query.all():
        if GraphNode.query.filter_by(project_id=project.id, deleted_at=None).count() == 0:
            initial_node = GraphNode(
                project_id=project.id,
                title="Untitled page",
                in_graph=False,
                user_id=project.created_by_id
            )
            db.session.add(initial_node)
    db.session.commit()


def purge_deleted_nodes():
    from .models import GraphEdge, GraphNode

    cutoff = datetime.now(timezone.utc) - DELETED_NODE_RETENTION
    expired = GraphNode.query.filter(GraphNode.deleted_at.isnot(None), GraphNode.deleted_at < cutoff).all()
    if not expired:
        return
    expired_ids = [node.id for node in expired]
    GraphEdge.query.filter(GraphEdge.source_id.in_(expired_ids) | GraphEdge.target_id.in_(expired_ids)).delete(synchronize_session=False)
    GraphNode.query.filter(GraphNode.parent_id.in_(expired_ids)).update({"parent_id": None}, synchronize_session=False)
    GraphNode.query.filter(GraphNode.id.in_(expired_ids)).delete(synchronize_session=False)
    db.session.commit()
