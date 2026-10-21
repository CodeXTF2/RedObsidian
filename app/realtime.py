import json
import logging
from functools import wraps

from flask import request, session
from flask_login import current_user
from flask_socketio import disconnect, emit, join_room
from sqlalchemy.exc import IntegrityError

from . import audit, db
from .access import project_role, role_allows
from .main import parse_iso_datetime, project_room
from .models import GraphEdge, GraphNode, Project, TimelineEvent, as_utc, utcnow
from .revisions import record_revision

log = logging.getLogger(__name__)

MAX_ATTACHMENTS = 200


class InvalidInput(ValueError):
    """Bad client input; the message is safe to show to the user."""


def live_node(node_id, project_id):
    """A page in this project that hasn't been deleted, or None."""
    try:
        node = db.session.get(GraphNode, int(node_id))
    except (TypeError, ValueError):
        return None
    if not node or node.project_id != project_id or node.deleted_at is not None:
        return None
    return node


def with_origin(data):
    """Tag a broadcast with the connection and user that caused it, so clients can spot concurrent edits."""
    return {**data, "origin": request.sid, "edited_by": current_user.username}


def live_edges_for(node, project_id):
    edges = GraphEdge.query.filter_by(project_id=project_id).filter(
        (GraphEdge.source_id == node.id) | (GraphEdge.target_id == node.id)
    ).all()
    return [
        edge for edge in edges
        if live_node(edge.source_id, project_id) and live_node(edge.target_id, project_id)
    ]


def number(value, default=0.0, limit=1e7):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidInput("A number was invalid.") from exc
    if result != result or abs(result) == float("inf"):
        return default
    return max(-limit, min(limit, result))


def parse_joint(value):
    if value in (None, ""):
        return None
    return number(value, limit=10000.0)


def clean_files(value):
    """Attachments are a list of {name, url}; anything else from the client is dropped."""
    files = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            files.append({"name": str(item.get("name", ""))[:255], "url": str(item.get("url", ""))[:1024]})
    return json.dumps(files[:MAX_ATTACHMENTS])


def is_descendant(node_id, possible_parent_id):
    cursor = db.session.get(GraphNode, possible_parent_id)
    seen = set()
    while cursor and cursor.id not in seen:
        if cursor.parent_id == node_id:
            return True
        seen.add(cursor.id)
        cursor = db.session.get(GraphNode, cursor.parent_id) if cursor.parent_id else None
    return False


def automatic_timeline_order(occurred_at, project_id, exclude_id=None):
    auto_events = TimelineEvent.query.filter_by(manual_order=False, project_id=project_id).all()
    if exclude_id is not None:
        auto_events = [event for event in auto_events if event.id != exclude_id]
    auto_events.sort(key=lambda event: as_utc(event.occurred_at), reverse=True)
    if not auto_events:
        return 0

    insert_at = 0
    for index, event in enumerate(auto_events):
        event_time = as_utc(event.occurred_at)
        if as_utc(occurred_at) <= event_time:
            insert_at = index + 1

    previous_event = auto_events[insert_at - 1] if insert_at > 0 else None
    next_event = auto_events[insert_at] if insert_at < len(auto_events) else None

    if previous_event and next_event:
        return (previous_event.order_index + next_event.order_index) / 2
    if previous_event:
        return previous_event.order_index + 1
    if next_event:
        return next_event.order_index - 1
    return 0


def reset_timeline_auto_order(project_id):
    events = TimelineEvent.query.filter_by(project_id=project_id).all()
    events.sort(key=lambda event: as_utc(event.occurred_at), reverse=True)
    for index, event in enumerate(events):
        event.order_index = float(index)
        event.manual_order = False
    return events


def parse_timeline_datetime(value):
    if value in (None, ""):
        return None
    try:
        return parse_iso_datetime(value)
    except (TypeError, ValueError, AttributeError):
        return None


def parse_optional_timeline_datetime(value):
    if value in (None, ""):
        return None, True
    parsed = parse_timeline_datetime(value)
    return parsed, parsed is not None


def socket_project(needed):
    """The connection's project if the user may still act on it at the needed level, else None.

    Checked on every event so removed members and disabled accounts lose access immediately.
    """
    if not current_user.is_authenticated:
        disconnect()
        return None
    project = db.session.get(Project, session.get("ws_project_id") or 0)
    role = project_role(current_user, project)
    if project is None or role is None:
        emit("project:access_revoked", {"user_id": current_user.id})
        disconnect()
        return None
    if not role_allows(role, project, needed):
        message = "This project is archived and read-only." if project.archived_at else "You have read-only access to this project."
        emit("error:message", {"message": message})
        return None
    return project


def project_event(socketio, name, needed="editor"):
    """Register a socket handler that receives (project, payload) after the access check."""
    def decorator(handler):
        @socketio.on(name)
        @wraps(handler)
        def wrapper(payload=None):
            project = socket_project(needed)
            if project is None:
                return None
            try:
                return handler(project, payload if isinstance(payload, dict) else {})
            except InvalidInput as exc:
                db.session.rollback()
                emit("error:message", {"message": str(exc)})
            except Exception:
                db.session.rollback()
                log.exception("Socket event %s failed", name)
                emit("error:message", {"message": "Something went wrong saving that change. Reload and try again."})
            return None
        return wrapper
    return decorator


def register_socket_handlers(socketio):
    @socketio.on("connect")
    def connect(auth=None):
        if not current_user.is_authenticated:
            return False
        try:
            project_id = int(request.args.get("project", 0))
        except (TypeError, ValueError):
            return False
        project = db.session.get(Project, project_id)
        if not project or not project_role(current_user, project):
            return False
        # This session copy belongs to the socket connection only
        session["ws_project_id"] = project.id
        join_room(project_room(project.id))
        emit("presence", {"message": f"{current_user.username} connected"}, to=project_room(project.id))
        return None

    @project_event(socketio, "timeline:create")
    def create_timeline_event(project, payload):
        title = str(payload.get("title") or "").strip()
        body = str(payload.get("body") or "").strip()
        if not title:
            raise InvalidInput("Timeline title is required.")
        occurred_at = parse_timeline_datetime(payload.get("occurred_at"))
        if occurred_at is None:
            raise InvalidInput("Timeline date and time is invalid.")
        ended_at, valid_ended_at = parse_optional_timeline_datetime(payload.get("ended_at"))
        if not valid_ended_at:
            raise InvalidInput("Timeline end date and time is invalid.")
        if ended_at and as_utc(ended_at) < as_utc(occurred_at):
            raise InvalidInput("Timeline end date and time must be after the start.")

        event = TimelineEvent(
            project_id=project.id,
            title=title[:160],
            body=body,
            files_json=clean_files(payload.get("files")),
            occurred_at=occurred_at,
            ended_at=ended_at,
            user_id=current_user.id,
        )
        event.order_index = automatic_timeline_order(event.occurred_at, project.id)
        db.session.add(event)
        db.session.flush()
        audit.record("timeline.created", project_id=project.id, target_type="timeline_event", target_id=event.id,
                     title=event.title)
        emit("timeline:created", event.to_dict(), to=project_room(project.id))

    @project_event(socketio, "timeline:update")
    def update_timeline_event(project, payload):
        event = db.session.get(TimelineEvent, payload.get("id") or 0)
        if not event or event.project_id != project.id:
            raise InvalidInput("Timeline event not found.")

        if "title" in payload:
            title = str(payload.get("title") or "").strip()
            if not title:
                raise InvalidInput("Timeline title is required.")
            event.title = title[:160]
        if "body" in payload:
            event.body = str(payload.get("body") or "").strip()
        if "files" in payload:
            event.files_json = clean_files(payload.get("files"))
        if "occurred_at" in payload:
            occurred_at = parse_timeline_datetime(payload.get("occurred_at"))
            if occurred_at is None:
                raise InvalidInput("Timeline date and time is invalid.")
            event.occurred_at = occurred_at
            if not event.manual_order:
                event.order_index = automatic_timeline_order(occurred_at, project.id, exclude_id=event.id)
        if "ended_at" in payload:
            ended_at, valid_ended_at = parse_optional_timeline_datetime(payload.get("ended_at"))
            if not valid_ended_at:
                raise InvalidInput("Timeline end date and time is invalid.")
            event.ended_at = ended_at
        if event.ended_at and as_utc(event.ended_at) < as_utc(event.occurred_at):
            raise InvalidInput("Timeline end date and time must be after the start.")

        db.session.commit()
        emit("timeline:updated", with_origin(event.to_dict()), to=project_room(project.id))

    @project_event(socketio, "timeline:reorder")
    def reorder_timeline_event(project, payload):
        event = db.session.get(TimelineEvent, payload.get("id") or 0)
        if not event or event.project_id != project.id:
            raise InvalidInput("Timeline event not found.")
        event.order_index = number(payload.get("order_index", event.order_index))
        event.manual_order = True
        db.session.commit()
        emit("timeline:updated", with_origin(event.to_dict()), to=project_room(project.id))

    @project_event(socketio, "timeline:auto_sort")
    def auto_sort_timeline_events(project, payload):
        events = reset_timeline_auto_order(project.id)
        db.session.commit()
        emit("timeline:reset", {"events": [event.to_dict() for event in events]}, to=project_room(project.id))

    @project_event(socketio, "timeline:delete")
    def delete_timeline_event(project, payload):
        event_id = payload.get("id")
        event = db.session.get(TimelineEvent, event_id or 0)
        if not event or event.project_id != project.id:
            raise InvalidInput("Timeline event not found.")
        title = event.title
        db.session.delete(event)
        audit.record("timeline.deleted", project_id=project.id, target_type="timeline_event", target_id=event_id,
                     title=title)
        emit("timeline:deleted", {"id": event_id}, to=project_room(project.id))

    @project_event(socketio, "node:create")
    def create_node(project, payload):
        title = str(payload.get("title") or "").strip() or "Untitled node"
        parent_id = payload.get("parent_id")
        if parent_id and not live_node(parent_id, project.id):
            raise InvalidInput("Parent page not found.")

        node = GraphNode(
            project_id=project.id,
            title=title[:120],
            parent_id=int(parent_id) if parent_id else None,
            caption=str(payload.get("caption") or "").strip()[:280],
            color=str(payload.get("color") or "").strip()[:20],
            files_json=clean_files(payload.get("files")),
            in_graph=bool(payload.get("in_graph", True)),
            order_index=number(payload.get("order_index", 0)),
            x=number(payload.get("x", 360)),
            y=number(payload.get("y", 240)),
            user_id=current_user.id,
        )
        db.session.add(node)
        db.session.flush()
        audit.record("page.created", project_id=project.id, target_type="page", target_id=node.id, title=node.title)
        emit("node:created", node.to_dict(), to=project_room(project.id))
        return node.to_dict()

    @project_event(socketio, "node:update")
    def update_node(project, payload):
        node = live_node(payload.get("id"), project.id)
        if not node:
            raise InvalidInput("This page no longer exists.")

        before = (node.title, node.caption, node.notes)
        for field, limit in (("title", 120), ("caption", 280), ("notes", None), ("color", 20)):
            if field in payload:
                value = str(payload.get(field) or "")
                if field not in ("notes", "color"):
                    value = value.strip()
                setattr(node, field, value[:limit] if limit else value)

        if "files" in payload:
            node.files_json = clean_files(payload.get("files"))

        if "in_graph" in payload:
            node.in_graph = bool(payload["in_graph"])

        if "parent_id" in payload:
            parent_id = payload.get("parent_id")
            try:
                parent_id = int(parent_id) if parent_id else None
            except (TypeError, ValueError) as exc:
                raise InvalidInput("Parent page not found.") from exc
            if parent_id == node.id:
                raise InvalidInput("A page cannot contain itself.")
            if parent_id and is_descendant(node.id, parent_id):
                raise InvalidInput("A page cannot be moved inside its own child.")
            if parent_id and not live_node(parent_id, project.id):
                raise InvalidInput("Parent page not found.")
            node.parent_id = parent_id

        if "order_index" in payload:
            node.order_index = number(payload["order_index"])
        if "x" in payload:
            node.x = number(payload["x"])
        if "y" in payload:
            node.y = number(payload["y"])

        if any(field in payload for field in ("title", "caption", "notes")):
            record_revision(node, current_user, before=before)
        db.session.commit()
        emit("node:updated", with_origin(node.to_dict()), to=project_room(project.id))

    @project_event(socketio, "node:delete")
    def delete_node(project, payload):
        node_id = payload.get("id")
        node = live_node(node_id, project.id)
        if not node:
            raise InvalidInput("That page no longer exists.")

        # Children move up a level; node:restore moves them back
        new_parent_id = node.parent_id
        for child in node.children:
            if child.deleted_at is None:
                child.parent_id = new_parent_id

        # Soft delete: links are kept (but hidden) so an undo restores them too
        node.deleted_at = utcnow()
        audit.record("page.deleted", project_id=project.id, target_type="page", target_id=node.id, title=node.title)
        emit("node:deleted", {"id": node.id}, to=project_room(project.id))

        # Ensure at least one page exists
        if GraphNode.query.filter_by(project_id=project.id, deleted_at=None).count() == 0:
            new_node = GraphNode(project_id=project.id, title="Untitled page", in_graph=False, user_id=current_user.id)
            db.session.add(new_node)
            db.session.commit()
            emit("node:created", new_node.to_dict(), to=project_room(project.id))

    @project_event(socketio, "node:restore")
    def restore_node(project, payload):
        try:
            node = db.session.get(GraphNode, int(payload.get("id")))
        except (TypeError, ValueError):
            node = None
        if not node or node.project_id != project.id or node.deleted_at is None:
            raise InvalidInput("That page can no longer be restored.")

        node.deleted_at = None
        if node.parent_id and not live_node(node.parent_id, project.id):
            node.parent_id = None
        # Move children back, unless someone has rearranged them since
        restored_children = []
        for child_id in payload.get("children") or []:
            child = live_node(child_id, project.id)
            if child and child.parent_id == node.parent_id:
                child.parent_id = node.id
                restored_children.append(child)
        audit.record("page.restored", project_id=project.id, target_type="page", target_id=node.id, title=node.title)

        emit("node:restored", {
            "node": node.to_dict(),
            "children": [child.to_dict() for child in restored_children],
            "edges": [edge.to_dict() for edge in live_edges_for(node, project.id)],
        }, to=project_room(project.id))

    @project_event(socketio, "edge:create")
    def create_edge(project, payload):
        try:
            source_id = int(payload.get("source_id", 0))
            target_id = int(payload.get("target_id", 0))
        except (TypeError, ValueError) as exc:
            raise InvalidInput("Choose two nodes to link.") from exc
        if source_id == target_id:
            raise InvalidInput("Choose two different nodes.")
        if not live_node(source_id, project.id) or not live_node(target_id, project.id):
            raise InvalidInput("Both nodes must exist.")

        try:
            joint_x = parse_joint(payload.get("joint_x"))
            joint_y = parse_joint(payload.get("joint_y"))
        except InvalidInput:
            joint_x = joint_y = None

        edge = GraphEdge(project_id=project.id, source_id=source_id, target_id=target_id,
                         joint_x=joint_x, joint_y=joint_y, user_id=current_user.id)
        db.session.add(edge)
        try:
            db.session.commit()
        except IntegrityError as exc:
            raise InvalidInput("Those nodes are already linked.") from exc
        emit("edge:created", edge.to_dict(), to=project_room(project.id))

    @project_event(socketio, "edge:update")
    def update_edge(project, payload):
        edge = db.session.get(GraphEdge, payload.get("id") or 0)
        if not edge or edge.project_id != project.id:
            raise InvalidInput("Link not found.")
        if "joint_x" in payload:
            edge.joint_x = parse_joint(payload.get("joint_x"))
        if "joint_y" in payload:
            edge.joint_y = parse_joint(payload.get("joint_y"))
        db.session.commit()
        emit("edge:updated", edge.to_dict(), to=project_room(project.id))

    @project_event(socketio, "edge:delete")
    def delete_edge(project, payload):
        edge_id = payload.get("id")
        edge = db.session.get(GraphEdge, edge_id or 0)
        if not edge or edge.project_id != project.id:
            raise InvalidInput("Link not found.")
        db.session.delete(edge)
        db.session.commit()
        emit("edge:deleted", {"id": edge_id}, to=project_room(project.id))
