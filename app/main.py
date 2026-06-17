import os
import uuid
from datetime import datetime, timezone

from flask import (Blueprint, abort, current_app, jsonify, redirect,
                   render_template, request, url_for)
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from .models import GraphEdge, GraphNode, TimelineEvent

main_bp = Blueprint("main", __name__)


def parse_iso_datetime(value):
    if not value:
        return datetime.now(timezone.utc)
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@main_bp.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("main.notes"))
    return redirect(url_for("auth.login"))


@main_bp.get("/dashboard")
@login_required
def dashboard():
    return redirect(url_for("main.notes"))


@main_bp.get("/notes")
@login_required
def notes():
    return render_template("notes.html", active_page="notes")


@main_bp.get("/graph")
@login_required
def graph():
    return render_template("graph.html", active_page="graph")


@main_bp.get("/timeline")
@login_required
def timeline():
    return render_template("timeline.html", active_page="timeline")


@main_bp.get("/nodes/<int:node_id>")
@login_required
def node_page(node_id):
    node = GraphNode.query.get_or_404(node_id)
    return render_template("node.html", active_page="notes", node_id=node.id)


@main_bp.get("/api/state")
@login_required
def state():
    events = TimelineEvent.query.order_by(TimelineEvent.order_index.asc()).all()
    nodes = GraphNode.query.order_by(GraphNode.id.asc()).all()
    edges = GraphEdge.query.order_by(GraphEdge.id.asc()).all()
    return jsonify(
        {
            "user": current_user.username,
            "events": [event.to_dict() for event in events],
            "nodes": [node.to_dict() for node in nodes],
            "edges": [edge.to_dict() for edge in edges],
        }
    )


@main_bp.get("/api/images")
@login_required
def list_images():
    upload_dir = os.path.join(current_app.root_path, "static", "uploads")
    if not os.path.exists(upload_dir):
        return jsonify([])
    
    files = []
    extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
    for filename in os.listdir(upload_dir):
        if os.path.isfile(os.path.join(upload_dir, filename)):
            ext = os.path.splitext(filename)[1].lower()
            if ext in extensions:
                url = url_for("static", filename=f"uploads/{filename}")
                files.append({
                    "filename": filename,
                    "url": url,
                    "created_at": os.path.getctime(os.path.join(upload_dir, filename))
                })
    
    files.sort(key=lambda x: x["created_at"], reverse=True)
    return jsonify(files)


@main_bp.get("/api/files")
@login_required
def list_files():
    upload_dir = os.path.join(current_app.root_path, "static", "uploads")
    if not os.path.exists(upload_dir):
        return jsonify([])
    
    files = []
    image_extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
    for filename in os.listdir(upload_dir):
        if os.path.isfile(os.path.join(upload_dir, filename)):
            ext = os.path.splitext(filename)[1].lower()
            if ext not in image_extensions:
                url = url_for("static", filename=f"uploads/{filename}")
                files.append({
                    "filename": filename,
                    "url": url,
                    "created_at": os.path.getctime(os.path.join(upload_dir, filename)),
                    "size": os.path.getsize(os.path.join(upload_dir, filename))
                })
    
    files.sort(key=lambda x: x["created_at"], reverse=True)
    return jsonify(files)


@main_bp.post("/api/upload")
@login_required
def upload_file():
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No selected file"}), 400
    
    if file:
        filename = secure_filename(file.filename)
        # Add a UUID to prevent collisions
        name, ext = os.path.splitext(filename)
        filename = f"{name}_{uuid.uuid4().hex}{ext}"
        
        upload_dir = os.path.join(current_app.root_path, "static", "uploads")
        os.makedirs(upload_dir, exist_ok=True)
        
        filepath = os.path.join(upload_dir, filename)
        file.save(filepath)
        
        url = url_for("static", filename=f"uploads/{filename}")
        return jsonify({"url": url, "filename": filename})
    
    return jsonify({"error": "Upload failed"}), 500


@main_bp.delete("/api/images/<filename>")
@main_bp.delete("/api/files/<filename>")
@login_required
def delete_file(filename):
    filename = secure_filename(filename)
    filepath = os.path.join(current_app.root_path, "static", "uploads", filename)
    
    if os.path.exists(filepath):
        os.remove(filepath)
        return jsonify({"message": "File deleted"})
    return jsonify({"error": "File not found"}), 404


@main_bp.get("/images")
@login_required
def images():
    return render_template("images.html", active_page="images")


@main_bp.get("/files")
@login_required
def files_page():
    return render_template("files.html", active_page="files")
