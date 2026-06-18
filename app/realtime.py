import json
from flask import session
from flask_login import current_user
from flask_socketio import disconnect, emit
from sqlalchemy.exc import IntegrityError

from . import db
from .main import parse_iso_datetime
from .models import GraphEdge, GraphNode, TimelineEvent, as_utc


def get_current_project_id():
    return session.get("project_id")


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
    try:
        return parse_iso_datetime(value)
    except (TypeError, ValueError):
        return None


def register_socket_handlers(socketio):
    @socketio.on("connect")
    def connect():
        if not current_user.is_authenticated:
            disconnect()
            return
        emit("presence", {"message": f"{current_user.username} connected"}, broadcast=True)

    @socketio.on("timeline:create")
    def create_timeline_event(payload):
        project_id = get_current_project_id()
        if not current_user.is_authenticated or not project_id:
            disconnect()
            return

        title = (payload or {}).get("title", "").strip()
        body = (payload or {}).get("body", "").strip()
        if not title:
            emit("error:message", {"message": "Timeline title is required."})
            return
        occurred_at = parse_timeline_datetime((payload or {}).get("occurred_at"))
        if occurred_at is None:
            emit("error:message", {"message": "Timeline date and time is invalid."})
            return

        event = TimelineEvent(
            project_id=project_id,
            title=title[:160],
            body=body,
            files_json=json.dumps((payload or {}).get("files", [])),
            occurred_at=occurred_at,
            user_id=current_user.id,
        )
        event.order_index = automatic_timeline_order(event.occurred_at, project_id)
        db.session.add(event)
        db.session.commit()
        emit("timeline:created", event.to_dict(), broadcast=True)

    @socketio.on("timeline:update")
    def update_timeline_event(payload):
        project_id = get_current_project_id()
        if not current_user.is_authenticated or not project_id:
            disconnect()
            return

        event = db.session.get(TimelineEvent, (payload or {}).get("id"))
        if not event or event.project_id != project_id:
            emit("error:message", {"message": "Timeline event not found."})
            return

        if "title" in payload:
            title = (payload.get("title") or "").strip()
            if not title:
                emit("error:message", {"message": "Timeline title is required."})
                return
            event.title = title[:160]
        if "body" in payload:
            event.body = (payload.get("body") or "").strip()
        if "files" in payload:
            event.files_json = json.dumps(payload.get("files", []))
        if "occurred_at" in payload:
            occurred_at = parse_timeline_datetime(payload.get("occurred_at"))
            if occurred_at is None:
                emit("error:message", {"message": "Timeline date and time is invalid."})
                return
            event.occurred_at = occurred_at
            if not event.manual_order:
                event.order_index = automatic_timeline_order(occurred_at, project_id, exclude_id=event.id)

        db.session.commit()
        emit("timeline:updated", event.to_dict(), broadcast=True)

    @socketio.on("timeline:reorder")
    def reorder_timeline_event(payload):
        project_id = get_current_project_id()
        if not current_user.is_authenticated or not project_id:
            disconnect()
            return

        event = db.session.get(TimelineEvent, (payload or {}).get("id"))
        if not event or event.project_id != project_id:
            emit("error:message", {"message": "Timeline event not found."})
            return

        try:
            event.order_index = float((payload or {}).get("order_index", event.order_index))
        except (TypeError, ValueError):
            emit("error:message", {"message": "Timeline order is invalid."})
            return
        event.manual_order = True
        db.session.commit()
        emit("timeline:updated", event.to_dict(), broadcast=True)

    @socketio.on("timeline:auto_sort")
    def auto_sort_timeline_events():
        project_id = get_current_project_id()
        if not current_user.is_authenticated or not project_id:
            disconnect()
            return

        events = reset_timeline_auto_order(project_id)
        db.session.commit()
        emit("timeline:reset", {"events": [event.to_dict() for event in events]}, broadcast=True)

    @socketio.on("timeline:delete")
    def delete_timeline_event(payload):
        project_id = get_current_project_id()
        if not current_user.is_authenticated or not project_id:
            disconnect()
            return

        event_id = (payload or {}).get("id")
        event = db.session.get(TimelineEvent, event_id)
        if not event or event.project_id != project_id:
            emit("error:message", {"message": f"Timeline event {event_id} not found."})
            return

        try:
            db.session.delete(event)
            db.session.commit()
            emit("timeline:deleted", {"id": event_id}, broadcast=True)
        except Exception as e:
            db.session.rollback()
            emit("error:message", {"message": f"Deletion failed: {str(e)}"})

    @socketio.on("node:create")
    def create_node(payload):
        project_id = get_current_project_id()
        if not current_user.is_authenticated or not project_id:
            disconnect()
            return

        title = (payload or {}).get("title", "").strip() or "Untitled node"
        parent_id = (payload or {}).get("parent_id")
        if parent_id:
            parent = db.session.get(GraphNode, int(parent_id))
            if not parent or parent.project_id != project_id:
                emit("error:message", {"message": "Parent page not found."})
                return

        node = GraphNode(
            project_id=project_id,
            title=title[:120],
            parent_id=int(parent_id) if parent_id else None,
            caption=((payload or {}).get("caption", "").strip())[:280],
            color=((payload or {}).get("color", "").strip())[:20],
            files_json=json.dumps((payload or {}).get("files", [])),
            in_graph=bool((payload or {}).get("in_graph", True)),
            order_index=float((payload or {}).get("order_index", 0)),
            x=float((payload or {}).get("x", 360)),
            y=float((payload or {}).get("y", 240)),
            user_id=current_user.id,
        )
        db.session.add(node)
        db.session.commit()
        emit("node:created", node.to_dict(), broadcast=True)

    @socketio.on("node:update")
    def update_node(payload):
        project_id = get_current_project_id()
        if not current_user.is_authenticated or not project_id:
            disconnect()
            return

        node = db.session.get(GraphNode, (payload or {}).get("id"))
        if not node or node.project_id != project_id:
            emit("error:message", {"message": "Node not found."})
            return

        for field, limit in (("title", 120), ("caption", 280), ("notes", None), ("color", 20)):
            if field in payload:
                value = (payload.get(field) or "")
                if field not in ("notes", "color"):
                    value = value.strip()
                setattr(node, field, value[:limit] if limit else value)

        if "files" in payload:
            node.files_json = json.dumps(payload.get("files", []))

        if "in_graph" in payload:
            node.in_graph = bool(payload["in_graph"])

        if "parent_id" in payload:
            parent_id = payload.get("parent_id")
            if parent_id and int(parent_id) == node.id:
                emit("error:message", {"message": "A page cannot contain itself."})
                return
            if parent_id and is_descendant(node.id, int(parent_id)):
                emit("error:message", {"message": "A page cannot be moved inside its own child."})
                return
            if parent_id:
                parent = db.session.get(GraphNode, int(parent_id))
                if not parent or parent.project_id != project_id:
                    emit("error:message", {"message": "Parent page not found."})
                    return
            node.parent_id = int(parent_id) if parent_id else None

        if "order_index" in payload:
            node.order_index = float(payload["order_index"])

        if "x" in payload:
            node.x = float(payload["x"])
        if "y" in payload:
            node.y = float(payload["y"])

        db.session.commit()
        emit("node:updated", node.to_dict(), broadcast=True)

    @socketio.on("node:delete")
    def delete_node(payload):
        project_id = get_current_project_id()
        if not current_user.is_authenticated or not project_id:
            disconnect()
            return

        node_id = (payload or {}).get("id")
        node = db.session.get(GraphNode, node_id)
        if not node or node.project_id != project_id:
            emit("error:message", {"message": f"Node {node_id} not found."})
            return

        try:
            # Handle children: re-parent to parent of deleted node or null
            new_parent_id = node.parent_id
            for child in node.children:
                child.parent_id = new_parent_id

            # Delete edges
            GraphEdge.query.filter_by(project_id=project_id).filter((GraphEdge.source_id == node.id) | (GraphEdge.target_id == node.id)).delete()

            db.session.delete(node)
            db.session.commit()
            emit("node:deleted", {"id": node_id}, broadcast=True)

            # Ensure at least one page exists
            if GraphNode.query.filter_by(project_id=project_id).count() == 0:
                new_node = GraphNode(
                    project_id=project_id,
                    title="Untitled",
                    in_graph=False,
                    user_id=current_user.id
                )
                db.session.add(new_node)
                db.session.commit()
                emit("node:created", new_node.to_dict(), broadcast=True)
        except Exception as e:
            db.session.rollback()
            emit("error:message", {"message": f"Deletion failed: {str(e)}"})

    @socketio.on("edge:create")
    def create_edge(payload):
        project_id = get_current_project_id()
        if not current_user.is_authenticated or not project_id:
            disconnect()
            return

        source_id = int((payload or {}).get("source_id", 0))
        target_id = int((payload or {}).get("target_id", 0))
        if source_id == target_id:
            emit("error:message", {"message": "Choose two different nodes."})
            return

        source = db.session.get(GraphNode, source_id)
        target = db.session.get(GraphNode, target_id)
        if not source or not target or source.project_id != project_id or target.project_id != project_id:
            emit("error:message", {"message": "Both nodes must exist."})
            return

        edge = GraphEdge(project_id=project_id, source_id=source_id, target_id=target_id, user_id=current_user.id)
        db.session.add(edge)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            emit("error:message", {"message": "Those nodes are already linked."})
            return

        emit("edge:created", edge.to_dict(), broadcast=True)

    @socketio.on("edge:delete")
    def delete_edge(payload):
        project_id = get_current_project_id()
        if not current_user.is_authenticated or not project_id:
            disconnect()
            return

        edge_id = (payload or {}).get("id")
        edge = db.session.get(GraphEdge, edge_id)
        if not edge or edge.project_id != project_id:
            emit("error:message", {"message": f"Edge {edge_id} not found."})
            return

        db.session.delete(edge)
        db.session.commit()
        emit("edge:deleted", {"id": edge_id}, broadcast=True)
