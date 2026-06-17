from pathlib import Path

from flask import Flask
from flask_login import LoginManager
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text

db = SQLAlchemy()
login_manager = LoginManager()
socketio = SocketIO(cors_allowed_origins=[], async_mode="threading")


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
    if "graph_node" not in inspector.get_table_names():
        return

    graph_node_columns = {column["name"] for column in inspector.get_columns("graph_node")}
    if "parent_id" not in graph_node_columns:
        db.session.execute(text("ALTER TABLE graph_node ADD COLUMN parent_id INTEGER"))
        db.session.commit()
    if "order_index" not in graph_node_columns:
        db.session.execute(text("ALTER TABLE graph_node ADD COLUMN order_index FLOAT DEFAULT 0 NOT NULL"))
        db.session.commit()

    timeline_columns = {column["name"] for column in inspector.get_columns("timeline_event")}
    if "order_index" not in timeline_columns:
        db.session.execute(text("ALTER TABLE timeline_event ADD COLUMN order_index FLOAT DEFAULT 0 NOT NULL"))
        db.session.execute(text("UPDATE timeline_event SET order_index = -strftime('%s', occurred_at)"))
        db.session.commit()
    if "manual_order" not in timeline_columns:
        db.session.execute(text("ALTER TABLE timeline_event ADD COLUMN manual_order BOOLEAN DEFAULT 0 NOT NULL"))
        db.session.commit()
