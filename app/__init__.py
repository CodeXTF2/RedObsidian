from pathlib import Path

from flask import Flask
from flask_login import LoginManager
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text

db = SQLAlchemy()
login_manager = LoginManager()
socketio = SocketIO(cors_allowed_origins=[], async_mode="gevent")


def create_app():
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY="dev-change-me",
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{Path(app.instance_path) / 'redteam_obsidian.sqlite'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)
    socketio.init_app(app)

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

    # Ensure all projects have at least one page
    for project in Project.query.all():
        if GraphNode.query.filter_by(project_id=project.id).count() == 0:
            initial_node = GraphNode(
                project_id=project.id,
                title="Untitled",
                in_graph=False,
                user_id=project.created_by_id
            )
            db.session.add(initial_node)
    db.session.commit()

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
