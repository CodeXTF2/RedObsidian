import json
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


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Project(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    created_by = db.relationship("User")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description or "",
            "created_at": isoformat_utc(self.created_at),
            "created_by": self.created_by.username,
        }


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
        try:
            files = json.loads(self.files_json) if self.files_json else []
        except:
            files = []
        return {
            "id": self.id,
            "project_id": self.project_id,
            "title": self.title,
            "body": self.body or "",
            "files": files,
            "occurred_at": isoformat_utc(self.occurred_at),
            "ended_at": isoformat_utc(self.ended_at) if self.ended_at else None,
            "order_index": self.order_index,
            "manual_order": self.manual_order,
            "created_at": isoformat_utc(self.created_at),
            "author": self.user.username,
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
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    project = db.relationship("Project")
    user = db.relationship("User")
    parent = db.relationship("GraphNode", remote_side=[id], backref="children")

    def to_dict(self):
        try:
            files = json.loads(self.files_json) if self.files_json else []
        except:
            files = []
        return {
            "id": self.id,
            "project_id": self.project_id,
            "parent_id": self.parent_id,
            "title": self.title,
            "caption": self.caption or "",
            "notes": self.notes or "",
            "files": files,
            "order_index": self.order_index,
            "color": self.color or "",
            "in_graph": self.in_graph,
            "x": self.x,
            "y": self.y,
            "author": self.user.username,
            "updated_at": isoformat_utc(self.updated_at),
        }


class GraphEdge(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False, index=True)
    source_id = db.Column(db.Integer, db.ForeignKey("graph_node.id"), nullable=False)
    target_id = db.Column(db.Integer, db.ForeignKey("graph_node.id"), nullable=False)
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
            "author": self.user.username,
        }
