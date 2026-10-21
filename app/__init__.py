import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, flash, g, jsonify, redirect, request, session, url_for
from flask_login import LoginManager, current_user, logout_user
from flask_migrate import Migrate
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFError, CSRFProtect
from sqlalchemy import event, text
from sqlalchemy.engine import Engine

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
csrf = CSRFProtect()
socketio = SocketIO(async_mode="gevent")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Pages a signed-in user can still reach while a password change or 2FA enrolment is being forced on them
FORCED_FLOW_ENDPOINTS = {
    "auth.logout", "auth.account", "auth.change_password", "auth.totp_setup", "auth.totp_confirm",
    "static", "healthz",
}


def env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


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


def database_uri(instance_path):
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return f"sqlite:///{Path(instance_path) / 'redteam_obsidian.sqlite'}"
    # Hosting platforms often hand out postgres:// URLs; SQLAlchemy wants an explicit driver
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def allowed_socket_origins():
    """Same-origin only by default. REDOBSIDIAN_ALLOWED_ORIGINS adds a comma-separated allowlist (e.g. behind a proxy)."""
    value = os.environ.get("REDOBSIDIAN_ALLOWED_ORIGINS", "").strip()
    if not value:
        return None
    return [origin.strip().rstrip("/") for origin in value.split(",") if origin.strip()]


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record):
    # WAL lets readers work while a write is in progress; busy_timeout waits instead of failing with "database is locked"
    if type(dbapi_connection).__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()


def configure_logging():
    level = os.environ.get("REDOBSIDIAN_LOG_LEVEL", "INFO").upper()
    root = logging.getLogger()
    if not any(getattr(handler, "_redobsidian", False) for handler in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handler._redobsidian = True
        root.addHandler(handler)
    root.setLevel(level)


def create_app(test_config=None):
    configure_logging()
    instance_path = os.environ.get("REDOBSIDIAN_INSTANCE_PATH") or None
    app = Flask(__name__, instance_relative_config=True, instance_path=instance_path)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    secure_cookies = env_flag("REDOBSIDIAN_SECURE_COOKIES")
    app.config.from_mapping(
        SECRET_KEY=load_secret_key(app.instance_path),
        SQLALCHEMY_DATABASE_URI=database_uri(app.instance_path),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=secure_cookies,
        REMEMBER_COOKIE_HTTPONLY=True,
        REMEMBER_COOKIE_SAMESITE="Lax",
        REMEMBER_COOKIE_SECURE=secure_cookies,
        # Tokens live as long as the session, so long-open tabs can still upload
        WTF_CSRF_TIME_LIMIT=None,
        # Hard cap on any request body; the per-upload limit is an admin setting
        MAX_CONTENT_LENGTH=int(os.environ.get("REDOBSIDIAN_MAX_REQUEST_MB", "1024")) * 1024 * 1024,
        UPLOAD_ROOT=os.environ.get("REDOBSIDIAN_UPLOAD_DIR") or str(Path(app.instance_path) / "uploads"),
        AUTO_MIGRATE=env_flag("REDOBSIDIAN_AUTO_MIGRATE", True),
        SECURE_COOKIES=secure_cookies,
        REDIS_URL=os.environ.get("REDIS_URL", "").strip() or None,
    )
    if test_config:
        app.config.update(test_config)

    if env_flag("REDOBSIDIAN_BEHIND_PROXY"):
        # Trust one proxy hop for the client address and scheme (needed for audit IPs, lockout and SSO callbacks)
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    migrate.init_app(app, db, directory=str(PROJECT_ROOT / "migrations"), render_as_batch=True)
    login_manager.init_app(app)
    csrf.init_app(app)
    socketio.init_app(
        app,
        cors_allowed_origins=allowed_socket_origins(),
        message_queue=app.config["REDIS_URL"],
        # Socket events are re-authorised per event, so a slightly larger buffer for big notes is fine
        max_http_buffer_size=16 * 1024 * 1024,
    )

    login_manager.login_view = "auth.login"
    login_manager.login_message_category = "error"
    login_manager.session_protection = "basic"

    from .models import User

    @login_manager.user_loader
    def load_user(user_id):
        user_pk, _, token = str(user_id).partition(":")
        try:
            user = db.session.get(User, int(user_pk))
        except (TypeError, ValueError):
            return None
        # A rotated token (password change, disable, sign out everywhere) ends the session
        if not user or user.disabled or not token or not secrets.compare_digest(token, user.session_token):
            return None
        return user

    register_request_hooks(app)

    from .admin import admin_bp
    from .auth import auth_bp
    from .findings import findings_bp
    from .main import main_bp
    from .realtime import register_socket_handlers

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(findings_bp)
    app.register_blueprint(admin_bp)
    register_socket_handlers(socketio)

    @app.get("/healthz")
    def healthz():
        try:
            db.session.execute(text("SELECT 1"))
        except Exception:
            app.logger.exception("Health check failed")
            return jsonify({"status": "error"}), 503
        return jsonify({"status": "ok"})

    if app.config["AUTO_MIGRATE"]:
        from .database import init_database
        init_database(app)

    return app


def register_request_hooks(app):
    from .settings import get_setting

    @app.errorhandler(CSRFError)
    def handle_csrf_error(error):
        if request.path.startswith("/api/") or "/api/" in request.path:
            return jsonify({"error": "Your session expired. Reload the page and try again."}), 400
        flash("Your session expired. Please try again.", "error")
        return redirect(url_for("main.index"))

    @app.errorhandler(413)
    def handle_too_large(error):
        message = f"File is too large (limit {get_setting('max_upload_mb')} MB)."
        if "/api/" in request.path:
            return jsonify({"error": message}), 413
        flash(message, "error")
        return redirect(request.referrer or url_for("main.index"))

    @app.before_request
    def before_request():
        g.csp_nonce = secrets.token_urlsafe(16)
        if request.endpoint == "healthz":
            return None

        # Per-upload limit (admin setting); the hard cap is MAX_CONTENT_LENGTH
        if request.method == "POST" and request.endpoint == "main.upload_file":
            limit = get_setting("max_upload_mb")
            if limit and request.content_length and request.content_length > limit * 1024 * 1024:
                abort(413)

        if not current_user.is_authenticated:
            return None

        timeout = get_setting("session_idle_timeout_minutes")
        now = int(datetime.now(timezone.utc).timestamp())
        last_seen = session.get("last_seen")
        if timeout and last_seen and now - last_seen > timeout * 60:
            logout_user()
            session.clear()
            flash("You were signed out after a period of inactivity.", "error")
            return redirect(url_for("auth.login"))
        session["last_seen"] = now

        if request.endpoint in FORCED_FLOW_ENDPOINTS:
            return None
        from .security import password_expired
        if current_user.must_change_password or password_expired(current_user):
            if "/api/" in request.path:
                return jsonify({"error": "You must change your password first."}), 403
            return redirect(url_for("auth.change_password"))
        if get_setting("totp_required") and not current_user.totp_enabled and not session.get("sso_login"):
            if "/api/" in request.path:
                return jsonify({"error": "You must set up two-factor authentication first."}), 403
            return redirect(url_for("auth.totp_setup"))
        return None

    @app.after_request
    def security_headers(response):
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "same-origin")
        headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        nonce = getattr(g, "csp_nonce", "")
        headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        if app.config["SECURE_COOKIES"]:
            headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if current_user.is_authenticated and response.mimetype == "text/html":
            headers.setdefault("Cache-Control", "no-store")
        return response

    @app.context_processor
    def template_globals():
        return {"csp_nonce": getattr(g, "csp_nonce", "")}
