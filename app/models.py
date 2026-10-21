import json
import secrets
from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from . import db


def utcnow():
    return datetime.now(timezone.utc)


def as_utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def isoformat_utc(value):
    return as_utc(value).isoformat().replace("+00:00", "Z")


def load_json_list(value):
    try:
        loaded = json.loads(value) if value else []
    except (TypeError, ValueError):
        return []
    return loaded if isinstance(loaded, list) else []


def new_session_token():
    return secrets.token_hex(16)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    # Empty for accounts that only sign in through SSO
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    disabled = db.Column(db.Boolean, default=False, nullable=False)
    must_change_password = db.Column(db.Boolean, default=False, nullable=False)
    password_changed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # Rotated on password change / disable / "sign out everywhere" to kill existing sessions
    session_token = db.Column(db.String(32), default=new_session_token, nullable=False)
    totp_secret = db.Column(db.String(64), nullable=True)
    totp_enabled = db.Column(db.Boolean, default=False, nullable=False)
    # Last accepted TOTP time step, so a code can't be replayed
    totp_last_step = db.Column(db.Integer, nullable=True)
    recovery_codes_json = db.Column(db.Text, default="[]", nullable=False)
    sso_subject = db.Column(db.String(255), nullable=True, unique=True)
    last_login_at = db.Column(db.DateTime(timezone=True), nullable=True)

    def get_id(self):
        return f"{self.id}:{self.session_token}"

    @property
    def is_active(self):
        return not self.disabled

    @property
    def has_password(self):
        return bool(self.password_hash)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
        self.password_changed_at = utcnow()
        self.session_token = new_session_token()

    def check_password(self, password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    def rotate_session_token(self):
        self.session_token = new_session_token()


class Setting(db.Model):
    """Admin-editable settings, stored as JSON values. Defaults live in app/settings.py."""
    key = db.Column(db.String(64), primary_key=True)
    value_json = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class LoginFailure(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), nullable=False, index=True)
    ip = db.Column(db.String(64), nullable=False, index=True)
    at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False, index=True)


class Project(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    # Archived projects are read-only and purged after the configured retention period
    archived_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # Report front matter (client, dates, executive summary...) as JSON
    report_json = db.Column(db.Text, default="{}", nullable=False)
    # Findings is a beta feature that project owners opt into
    findings_enabled = db.Column(db.Boolean, default=False, nullable=False)
    report_enabled = db.Column(db.Boolean, default=False, nullable=False)

    created_by = db.relationship("User")
    members = db.relationship("ProjectMember", back_populates="project", cascade="all, delete-orphan")

    @property
    def report(self):
        try:
            data = json.loads(self.report_json or "{}")
        except (TypeError, ValueError):
            data = {}
        return data if isinstance(data, dict) else {}

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description or "",
            "created_at": isoformat_utc(self.created_at),
            "created_by": self.created_by.username if self.created_by else "",
            "archived": self.archived_at is not None,
        }


PROJECT_ROLES = ("owner", "editor", "viewer")


class ProjectMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    role = db.Column(db.String(16), default="editor", nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (db.UniqueConstraint("project_id", "user_id", name="unique_project_member"),)

    project = db.relationship("Project", back_populates="members")
    user = db.relationship("User")


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    user_id = db.Column(db.Integer, nullable=True, index=True)
    # Copied so the log stays readable after a user is deleted
    username = db.Column(db.String(64), nullable=True)
    project_id = db.Column(db.Integer, nullable=True, index=True)
    action = db.Column(db.String(64), nullable=False, index=True)
    target_type = db.Column(db.String(32), nullable=True)
    target_id = db.Column(db.String(64), nullable=True)
    details_json = db.Column(db.Text, default="{}", nullable=False)
    ip = db.Column(db.String(64), nullable=True)

    @property
    def details(self):
        try:
            return json.loads(self.details_json or "{}")
        except (TypeError, ValueError):
            return {}


class Upload(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False, unique=True)
    original_name = db.Column(db.String(255), nullable=False)
    size = db.Column(db.Integer, default=0, nullable=False)
    sha256 = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)

    user = db.relationship("User")


class TimelineEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False, index=True)
    title = db.Column(db.String(160), nullable=False)
    body = db.Column(db.Text, nullable=True)
    files_json = db.Column(db.Text, default="[]", nullable=False)
    occurred_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    ended_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)
    order_index = db.Column(db.Float, default=0, nullable=False)
    manual_order = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    project = db.relationship("Project")
    user = db.relationship("User")

    def to_dict(self):
        return {
            "id": self.id,
            "project_id": self.project_id,
            "title": self.title,
            "body": self.body or "",
            "files": load_json_list(self.files_json),
            "occurred_at": isoformat_utc(self.occurred_at),
            "ended_at": isoformat_utc(self.ended_at) if self.ended_at else None,
            "order_index": self.order_index,
            "manual_order": self.manual_order,
            "created_at": isoformat_utc(self.created_at),
            "author": self.user.username if self.user else "",
        }


class GraphNode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False, index=True)
    parent_id = db.Column(db.Integer, db.ForeignKey("graph_node.id"), nullable=True, index=True)
    title = db.Column(db.String(120), nullable=False)
    caption = db.Column(db.String(280), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    files_json = db.Column(db.Text, default="[]", nullable=False)
    order_index = db.Column(db.Float, default=0, nullable=False)
    color = db.Column(db.String(20), nullable=True)
    in_graph = db.Column(db.Boolean, default=True, nullable=False)
    x = db.Column(db.Float, default=360, nullable=False)
    y = db.Column(db.Float, default=240, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    # Set when the page is deleted; kept for a while so the delete can be undone
    deleted_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    project = db.relationship("Project")
    user = db.relationship("User")
    parent = db.relationship("GraphNode", remote_side=[id], backref="children")

    def to_dict(self):
        return {
            "id": self.id,
            "project_id": self.project_id,
            "parent_id": self.parent_id,
            "title": self.title,
            "caption": self.caption or "",
            "notes": self.notes or "",
            "files": load_json_list(self.files_json),
            "order_index": self.order_index,
            "color": self.color or "",
            "in_graph": self.in_graph,
            "x": self.x,
            "y": self.y,
            "author": self.user.username if self.user else "",
            "updated_at": isoformat_utc(self.updated_at),
        }


class NodeRevision(db.Model):
    """Snapshot of a page's text after an edit. Rapid autosaves by one user are folded into one revision."""
    id = db.Column(db.Integer, primary_key=True)
    node_id = db.Column(db.Integer, db.ForeignKey("graph_node.id"), nullable=False, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False, index=True)
    title = db.Column(db.String(120), nullable=False)
    caption = db.Column(db.String(280), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)

    user = db.relationship("User")


class GraphEdge(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False, index=True)
    source_id = db.Column(db.Integer, db.ForeignKey("graph_node.id"), nullable=False)
    target_id = db.Column(db.Integer, db.ForeignKey("graph_node.id"), nullable=False)
    # Optional bend point ("joint") of the elbow line, in graph world coordinates
    joint_x = db.Column(db.Float, nullable=True)
    joint_y = db.Column(db.Float, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    __table_args__ = (
        db.CheckConstraint("source_id != target_id", name="no_self_edge"),
        db.UniqueConstraint("source_id", "target_id", name="unique_directed_edge"),
    )

    project = db.relationship("Project")
    source = db.relationship("GraphNode", foreign_keys=[source_id])
    target = db.relationship("GraphNode", foreign_keys=[target_id])
    user = db.relationship("User")

    def to_dict(self):
        return {
            "id": self.id,
            "project_id": self.project_id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "joint_x": self.joint_x,
            "joint_y": self.joint_y,
            "author": self.user.username if self.user else "",
        }


SEVERITIES = ("critical", "high", "medium", "low", "info")
FINDING_STATUSES = ("draft", "open", "remediated", "accepted", "false_positive")


class Finding(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    severity = db.Column(db.String(16), default="medium", nullable=False)
    cvss_score = db.Column(db.Float, nullable=True)
    cvss_vector = db.Column(db.String(200), nullable=True)
    status = db.Column(db.String(24), default="draft", nullable=False)
    affected_assets = db.Column(db.Text, nullable=True)
    description = db.Column(db.Text, nullable=True)
    impact = db.Column(db.Text, nullable=True)
    remediation = db.Column(db.Text, nullable=True)
    references = db.Column(db.Text, nullable=True)
    # Pages in this project that hold the evidence for this finding
    evidence_node_ids_json = db.Column(db.Text, default="[]", nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    project = db.relationship("Project")
    user = db.relationship("User")

    @property
    def evidence_node_ids(self):
        return [int(value) for value in load_json_list(self.evidence_node_ids_json) if str(value).isdigit()]

    @property
    def severity_rank(self):
        return SEVERITIES.index(self.severity) if self.severity in SEVERITIES else len(SEVERITIES)
