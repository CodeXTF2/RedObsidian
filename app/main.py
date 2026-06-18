import os
import uuid
from datetime import datetime, timezone

from flask import (Blueprint, abort, current_app, jsonify, redirect,
                   render_template, request, url_for, send_from_directory, session)
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from .models import GraphEdge, GraphNode, TimelineEvent, Project, db

main_bp = Blueprint("main", __name__)


def get_current_project_id():
    project_id = session.get("project_id")
    if not project_id:
        project = Project.query.order_by(Project.created_at.asc()).first()
        if project:
            session["project_id"] = project.id
            return project.id
    return project_id


def get_upload_dir():
    project_id = get_current_project_id()
    if not project_id:
        upload_dir = os.path.join(current_app.instance_path, "uploads", "default")
    else:
        upload_dir = os.path.join(current_app.instance_path, "uploads", str(project_id))
    os.makedirs(upload_dir, exist_ok=True)
    return upload_dir


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
        if not get_current_project_id():
            return redirect(url_for("main.projects"))
        return redirect(url_for("main.notes"))
    return redirect(url_for("auth.login"))


@main_bp.get("/projects")
@login_required
def projects():
    all_projects = Project.query.order_by(Project.created_at.desc()).all()
    return render_template("projects.html", projects=all_projects, active_page="projects")


@main_bp.post("/projects")
@login_required
def create_project():
    name = request.form.get("name", "").strip()
    if not name:
        return redirect(url_for("main.projects"))
    
    project = Project(name=name, description=request.form.get("description"), created_by_id=current_user.id)
    db.session.add(project)
    db.session.commit()
    
    # Create initial Untitled page
    initial_node = GraphNode(
        project_id=project.id,
        title="Untitled",
        in_graph=False,
        user_id=current_user.id
    )
    db.session.add(initial_node)
    db.session.commit()
    
    session["project_id"] = project.id
    return redirect(url_for("main.notes"))


@main_bp.get("/projects/select/<int:project_id>")
@login_required
def select_project(project_id):
    project = Project.query.get_or_404(project_id)
    session["project_id"] = project.id
    return redirect(url_for("main.notes"))


@main_bp.post("/projects/delete/<int:project_id>")
@login_required
def delete_project(project_id):
    project = Project.query.get_or_404(project_id)
    confirm_name = request.form.get("confirm_name", "").strip()
    
    if confirm_name != project.name:
        return redirect(url_for("main.projects"))
    
    # Delete associated data
    TimelineEvent.query.filter_by(project_id=project.id).delete()
    
    # Handle graph nodes and their children
    nodes = GraphNode.query.filter_by(project_id=project.id).all()
    for node in nodes:
        # Clear children references to avoid foreign key issues during bulk delete
        for child in node.children:
            child.parent_id = None
    
    GraphEdge.query.filter_by(project_id=project.id).delete()
    GraphNode.query.filter_by(project_id=project.id).delete()
    
    # Delete project
    db.session.delete(project)
    db.session.commit()
    
    # Clean up files
    project_upload_dir = os.path.join(current_app.instance_path, "uploads", str(project.id))
    if os.path.exists(project_upload_dir):
        import shutil
        shutil.rmtree(project_upload_dir)
        
    # Clear session if deleted project was active
    if session.get("project_id") == project.id:
        session.pop("project_id", None)
        
    return redirect(url_for("main.projects"))


@main_bp.get("/dashboard")
@login_required
def dashboard():
    return redirect(url_for("main.notes"))


@main_bp.get("/notes")
@login_required
def notes():
    if not get_current_project_id():
        return redirect(url_for("main.projects"))
    return render_template("notes.html", active_page="notes")


@main_bp.get("/graph")
@login_required
def graph():
    if not get_current_project_id():
        return redirect(url_for("main.projects"))
    return render_template("graph.html", active_page="graph")


@main_bp.get("/timeline")
@login_required
def timeline():
    if not get_current_project_id():
        return redirect(url_for("main.projects"))
    return render_template("timeline.html", active_page="timeline")


@main_bp.get("/nodes/<int:node_id>")
@login_required
def node_page(node_id):
    node = GraphNode.query.get_or_404(node_id)
    if node.project_id != get_current_project_id():
        abort(403)
    return render_template("node.html", active_page="notes", node_id=node.id)


@main_bp.get("/api/state")
@login_required
def state():
    project_id = get_current_project_id()
    if not project_id:
        return jsonify({"error": "No project selected"}), 400
        
    events = TimelineEvent.query.filter_by(project_id=project_id).order_by(TimelineEvent.order_index.asc()).all()
    nodes = GraphNode.query.filter_by(project_id=project_id).order_by(GraphNode.id.asc()).all()
    edges = GraphEdge.query.filter_by(project_id=project_id).order_by(GraphEdge.id.asc()).all()
    return jsonify(
        {
            "user": current_user.username,
            "project_id": project_id,
            "events": [event.to_dict() for event in events],
            "nodes": [node.to_dict() for node in nodes],
            "edges": [edge.to_dict() for edge in edges],
        }
    )


@main_bp.get("/app.js")
@login_required
def serve_js():
    return send_from_directory(os.path.join(current_app.root_path, "protected_static", "js"), "app.js")


@main_bp.get("/uploads/<filename>")
@login_required
def serve_upload(filename):
    return send_from_directory(get_upload_dir(), filename)


@main_bp.get("/api/images")
@login_required
def list_images():
    upload_dir = get_upload_dir()
    
    files = []
    extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
    for filename in os.listdir(upload_dir):
        if os.path.isfile(os.path.join(upload_dir, filename)):
            ext = os.path.splitext(filename)[1].lower()
            if ext in extensions:
                url = url_for("main.serve_upload", filename=filename)
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
    upload_dir = get_upload_dir()
    
    files = []
    image_extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
    for filename in os.listdir(upload_dir):
        if os.path.isfile(os.path.join(upload_dir, filename)):
            ext = os.path.splitext(filename)[1].lower()
            if ext not in image_extensions:
                url = url_for("main.serve_upload", filename=filename)
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
        
        upload_dir = get_upload_dir()
        
        filepath = os.path.join(upload_dir, filename)
        file.save(filepath)
        
        url = url_for("main.serve_upload", filename=filename)
        return jsonify({"url": url, "filename": filename})
    
    return jsonify({"error": "Upload failed"}), 500


@main_bp.delete("/api/images/<filename>")
@main_bp.delete("/api/files/<filename>")
@login_required
def delete_file(filename):
    filename = secure_filename(filename)
    filepath = os.path.join(get_upload_dir(), filename)
    
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
