import json
import os
import tempfile
import uuid
from datetime import datetime, timezone

from flask import (
    Blueprint,
    abort,
    after_this_request,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from . import audit, db, socketio
from .access import ROLE_LEVEL, accessible_projects, load_project, project_role, project_route, role_allows
from .maintenance import delete_project_data, file_sha256, project_upload_dir
from .models import (
    PROJECT_ROLES,
    AuditLog,
    GraphEdge,
    GraphNode,
    NodeRevision,
    Project,
    ProjectMember,
    TimelineEvent,
    Upload,
    User,
    utcnow,
)
from .revisions import record_revision
from .settings import get_setting

main_bp = Blueprint("main", __name__)

# Served inline; everything else (SVG included) is sent as a download so it can't run script in our origin
INLINE_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
                      ".webp": "image/webp"}
IMAGE_EXTENSIONS = set(INLINE_IMAGE_TYPES) | {".svg"}
UPLOAD_CSP = "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox"
# Per-project beta features (Project.<name>_enabled): label, and what's kept while it's off
BETA_FEATURES = {"findings": ("Findings", "Existing findings are"), "report": ("Report", "Report details are")}


def parse_iso_datetime(value):
    if not value:
        return datetime.now(timezone.utc)
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def project_room(project_id):
    return f"project:{project_id}"


@main_bp.app_context_processor
def project_context():
    project = g.get("project")
    role = g.get("project_role")
    nav_projects = accessible_projects(current_user) if current_user.is_authenticated else []
    return {
        "project": project,
        "project_role": role,
        "can_edit": bool(project and role_allows(role, project, "editor")),
        "is_owner": bool(project and role_allows(role, project, "owner")),
        "nav_projects": nav_projects,
    }


def last_project():
    project_id = session.get("last_project_id")
    project = db.session.get(Project, project_id) if project_id else None
    if project and project.archived_at is None and project_role(current_user, project):
        return project
    projects = accessible_projects(current_user)
    return projects[0] if projects else None


@main_bp.route("/")
def index():
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login"))
    project = last_project()
    if not project:
        return redirect(url_for("main.projects"))
    return redirect(url_for("main.notes", project_id=project.id))


# --- Project list, creation, import -----------------------------------------

def can_create_projects():
    return current_user.is_admin or not get_setting("only_admins_create_projects")


@main_bp.get("/projects")
@login_required
def projects():
    show_archived = request.args.get("archived") == "1"
    all_projects = accessible_projects(current_user, include_archived=True)
    visible = [p for p in all_projects if (p.archived_at is not None) == show_archived]
    roles = {p.id: project_role(current_user, p) for p in visible}
    archived_count = sum(1 for p in all_projects if p.archived_at is not None)
    member_counts = dict(db.session.query(ProjectMember.project_id, db.func.count(ProjectMember.id))
                         .group_by(ProjectMember.project_id).all())
    return render_template("projects.html", projects=visible, roles=roles, show_archived=show_archived,
                           archived_count=archived_count, member_counts=member_counts,
                           can_create=can_create_projects(), active_page="projects")


@main_bp.post("/projects")
@login_required
def create_project():
    if not can_create_projects():
        abort(403)
    name = request.form.get("name", "").strip()[:120]
    if not name:
        flash("Give the project a name.", "error")
        return redirect(url_for("main.projects"))
    project = Project(name=name, description=request.form.get("description", "").strip(), created_by_id=current_user.id)
    db.session.add(project)
    db.session.flush()
    db.session.add(ProjectMember(project_id=project.id, user_id=current_user.id, role="owner"))
    db.session.add(GraphNode(project_id=project.id, title="Untitled page", in_graph=False, user_id=current_user.id))
    db.session.commit()
    audit.record("project.created", project_id=project.id, target_type="project", target_id=project.id, name=name)
    return redirect(url_for("main.notes", project_id=project.id))


@main_bp.post("/projects/import")
@login_required
def import_project_upload():
    from .transfer import TransferError, import_project

    if not can_create_projects():
        abort(403)
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Choose an export file to import.", "error")
        return redirect(url_for("main.projects"))
    handle, path = tempfile.mkstemp(suffix=".zip")
    os.close(handle)
    try:
        file.save(path)
        project = import_project(path, request.form.get("password", ""), current_user,
                                 request.form.get("name", "").strip() or None)
    except TransferError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.projects"))
    finally:
        os.remove(path)
    audit.record("project.imported", project_id=project.id, target_type="project", target_id=project.id,
                 name=project.name, source=secure_filename(file.filename))
    flash(f"Imported “{project.name}”.", "success")
    return redirect(url_for("main.notes", project_id=project.id))


# Old, session-based URLs keep working by sending people to the matching page of their last project
def _legacy(path, endpoint):
    @login_required
    def view():
        project = last_project()
        if not project:
            return redirect(url_for("main.projects"))
        return redirect(url_for(endpoint, project_id=project.id))
    view.__name__ = f"legacy_{path.strip('/')}"
    return view


for _path, _endpoint in (("/notes", "main.notes"), ("/graph", "main.graph"), ("/timeline", "main.timeline"),
                         ("/images", "main.images"), ("/files", "main.files_page"), ("/dashboard", "main.notes")):
    main_bp.add_url_rule(_path, view_func=_legacy(_path, _endpoint))


@main_bp.get("/projects/select/<int:project_id>")
@login_required
def select_project(project_id):
    return redirect(url_for("main.notes", project_id=project_id))


@main_bp.get("/nodes/<int:node_id>")
@login_required
def legacy_node_page(node_id):
    node = GraphNode.query.get_or_404(node_id)
    return redirect(url_for("main.node_page", project_id=node.project_id, node_id=node.id))


# --- Project pages -----------------------------------------------------------

@main_bp.get("/p/<int:project_id>/")
@project_route()
def project_home(project):
    return redirect(url_for("main.notes", project_id=project.id))


@main_bp.get("/p/<int:project_id>/notes")
@project_route()
def notes(project):
    return render_template("notes.html", active_page="notes")


@main_bp.get("/p/<int:project_id>/graph")
@project_route()
def graph(project):
    return render_template("graph.html", active_page="graph")


@main_bp.get("/p/<int:project_id>/timeline")
@project_route()
def timeline(project):
    return render_template("timeline.html", active_page="timeline")


@main_bp.get("/p/<int:project_id>/images")
@project_route()
def images(project):
    return render_template("images.html", active_page="files", files_tab="images")


@main_bp.get("/p/<int:project_id>/files")
@project_route()
def files_page(project):
    return render_template("files.html", active_page="files", files_tab="files")


def project_node(project, node_id, include_deleted=False):
    node = db.session.get(GraphNode, node_id)
    if not node or node.project_id != project.id or (node.deleted_at is not None and not include_deleted):
        abort(404)
    return node


@main_bp.get("/p/<int:project_id>/nodes/<int:node_id>")
@project_route()
def node_page(project, node_id):
    node = project_node(project, node_id)
    return render_template("node.html", active_page="notes", node_id=node.id)


@main_bp.get("/p/<int:project_id>/nodes/<int:node_id>/history")
@project_route()
def node_history(project, node_id):
    node = project_node(project, node_id)
    revisions = NodeRevision.query.filter_by(node_id=node.id).order_by(NodeRevision.id.desc()).all()
    selected = None
    selected_id = request.args.get("rev", type=int)
    if selected_id:
        selected = next((r for r in revisions if r.id == selected_id), None)
    selected = selected or (revisions[0] if revisions else None)
    previous = None
    if selected:
        index = revisions.index(selected)
        previous = revisions[index + 1] if index + 1 < len(revisions) else None
    return render_template("history.html", node=node, revisions=revisions, selected=selected, previous=previous,
                           active_page="notes")


@main_bp.post("/p/<int:project_id>/nodes/<int:node_id>/history/<int:revision_id>/restore")
@project_route("editor")
def restore_revision(project, node_id, revision_id):
    node = project_node(project, node_id)
    revision = db.session.get(NodeRevision, revision_id)
    if not revision or revision.node_id != node.id:
        abort(404)
    before = (node.title, node.caption, node.notes)
    node.title, node.caption, node.notes = revision.title, revision.caption, revision.notes
    record_revision(node, current_user, before=before, force_new=True)
    db.session.commit()
    socketio.emit("node:updated", {**node.to_dict(), "origin": None, "edited_by": current_user.username},
                  to=project_room(project.id))
    audit.record("page.restored_revision", project_id=project.id, target_type="page", target_id=node.id,
                 revision=revision.id)
    flash("Page restored to the selected version.", "success")
    return redirect(url_for("main.node_history", project_id=project.id, node_id=node.id))


@main_bp.get("/p/<int:project_id>/api/state")
@project_route()
def state(project):
    events = TimelineEvent.query.filter_by(project_id=project.id).order_by(TimelineEvent.order_index.asc()).all()
    nodes = GraphNode.query.filter_by(project_id=project.id, deleted_at=None).order_by(GraphNode.id.asc()).all()
    live_ids = {node.id for node in nodes}
    edges = [
        edge for edge in GraphEdge.query.filter_by(project_id=project.id).order_by(GraphEdge.id.asc()).all()
        if edge.source_id in live_ids and edge.target_id in live_ids
    ]
    return jsonify({
        "user": current_user.username,
        "project_id": project.id,
        "role": g.project_role,
        "can_edit": role_allows(g.project_role, project, "editor"),
        "events": [event.to_dict() for event in events],
        "nodes": [node.to_dict() for node in nodes],
        "edges": [edge.to_dict() for edge in edges],
    })


@main_bp.get("/js/<path:filename>")
@login_required
def serve_js_module(filename):
    return send_from_directory(os.path.join(current_app.root_path, "protected_static", "js"), filename)


# --- Uploads -----------------------------------------------------------------

def upload_url(project_id, filename):
    return url_for("main.serve_upload", project_id=project_id, filename=filename)


def send_upload(upload):
    ext = os.path.splitext(upload.filename)[1].lower()
    directory = project_upload_dir(upload.project_id, create=False)
    if ext in INLINE_IMAGE_TYPES:
        response = send_from_directory(directory, upload.filename, mimetype=INLINE_IMAGE_TYPES[ext])
    else:
        response = send_from_directory(directory, upload.filename, as_attachment=True,
                                       download_name=upload.original_name or upload.filename)
    response.headers["Content-Security-Policy"] = UPLOAD_CSP
    response.headers["Cache-Control"] = "private, max-age=3600"
    return response


@main_bp.get("/p/<int:project_id>/uploads/<filename>")
@project_route()
def serve_upload(project, filename):
    upload = Upload.query.filter_by(filename=filename, project_id=project.id).first()
    if not upload:
        abort(404)
    return send_upload(upload)


@main_bp.get("/uploads/<filename>")
@login_required
def legacy_serve_upload(filename):
    """Links written before uploads were project-scoped; the stored filename is unique, so it finds its project."""
    upload = Upload.query.filter_by(filename=filename).first()
    if not upload:
        abort(404)
    load_project(upload.project_id)
    return send_upload(upload)


def upload_listing(project, images):
    uploads = Upload.query.filter_by(project_id=project.id).order_by(Upload.created_at.desc()).all()
    listing = []
    for upload in uploads:
        is_image = os.path.splitext(upload.filename)[1].lower() in IMAGE_EXTENSIONS
        if is_image != images:
            continue
        listing.append({
            "filename": upload.filename,
            "name": upload.original_name,
            "url": upload_url(project.id, upload.filename),
            "created_at": upload.created_at.timestamp(),
            "size": upload.size,
            "uploaded_by": upload.user.username if upload.user else "",
        })
    return listing


@main_bp.get("/p/<int:project_id>/api/images")
@project_route()
def list_images(project):
    return jsonify(upload_listing(project, images=True))


@main_bp.get("/p/<int:project_id>/api/files")
@project_route()
def list_files(project):
    return jsonify(upload_listing(project, images=False))


@main_bp.post("/p/<int:project_id>/api/upload")
@project_route("editor")
def upload_file(project):
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file selected"}), 400
    original = secure_filename(file.filename) or "file"
    name, ext = os.path.splitext(original)
    filename = f"{name[:80]}_{uuid.uuid4().hex}{ext.lower()[:16]}"
    path = os.path.join(project_upload_dir(project.id), filename)
    file.save(path)
    upload = Upload(project_id=project.id, filename=filename, original_name=file.filename[:255],
                    size=os.path.getsize(path), sha256=file_sha256(path), user_id=current_user.id)
    db.session.add(upload)
    audit.record("upload.created", project_id=project.id, target_type="upload", target_id=filename,
                 name=file.filename[:255], size=upload.size, sha256=upload.sha256)
    return jsonify({"url": upload_url(project.id, filename), "filename": filename})


@main_bp.delete("/p/<int:project_id>/api/images/<filename>")
@main_bp.delete("/p/<int:project_id>/api/files/<filename>")
@project_route("editor")
def delete_file(project, filename):
    upload = Upload.query.filter_by(filename=filename, project_id=project.id).first()
    if not upload:
        return jsonify({"error": "File not found"}), 404
    path = os.path.join(project_upload_dir(project.id, create=False), upload.filename)
    if os.path.isfile(path):
        os.remove(path)
    db.session.delete(upload)
    audit.record("upload.deleted", project_id=project.id, target_type="upload", target_id=filename,
                 name=upload.original_name)
    return jsonify({"message": "File deleted"})


# --- Project settings, members, archive, export, delete ----------------------

@main_bp.get("/p/<int:project_id>/settings")
@project_route()
def project_settings(project):
    members = (ProjectMember.query.filter_by(project_id=project.id).join(User)
               .order_by(User.username).all())
    member_ids = {member.user_id for member in members}
    candidates = User.query.filter(User.disabled.is_(False)).order_by(User.username).all()
    candidates = [user for user in candidates if user.id not in member_ids]
    return render_template("project_settings.html", members=members, candidates=candidates, roles=PROJECT_ROLES,
                           active_page="settings")


@main_bp.get("/p/<int:project_id>/activity")
@project_route()
def project_activity(project):
    if ROLE_LEVEL.get(g.project_role, 0) < ROLE_LEVEL["owner"]:
        abort(403)
    page = max(1, request.args.get("page", 1, type=int))
    entries = (AuditLog.query.filter_by(project_id=project.id).order_by(AuditLog.id.desc())
               .offset((page - 1) * 100).limit(101).all())
    return render_template("project_activity.html", entries=entries[:100], page=page, has_more=len(entries) > 100,
                           active_page="settings")


def owner_count(project_id):
    return ProjectMember.query.filter_by(project_id=project_id, role="owner").count()


@main_bp.post("/p/<int:project_id>/settings")
@project_route("owner")
def update_project(project):
    name = request.form.get("name", "").strip()[:120]
    if not name:
        flash("The project needs a name.", "error")
        return redirect(url_for("main.project_settings", project_id=project.id))
    changes = {}
    if name != project.name:
        changes["name"] = [project.name, name]
    description = request.form.get("description", "").strip()
    if description != (project.description or ""):
        changes["description"] = "changed"
    project.name, project.description = name, description
    db.session.commit()
    if changes:
        audit.record("project.updated", project_id=project.id, target_type="project", target_id=project.id, **changes)
    flash("Project details saved.", "success")
    return redirect(url_for("main.project_settings", project_id=project.id))


@main_bp.post("/p/<int:project_id>/members")
@project_route("owner")
def add_member(project):
    user = db.session.get(User, request.form.get("user_id", type=int) or 0)
    role = request.form.get("role", "editor")
    if not user or role not in PROJECT_ROLES:
        flash("Choose a user and a role.", "error")
    elif ProjectMember.query.filter_by(project_id=project.id, user_id=user.id).first():
        flash(f"{user.username} is already a member.", "error")
    else:
        db.session.add(ProjectMember(project_id=project.id, user_id=user.id, role=role))
        audit.record("member.added", project_id=project.id, target_type="user", target_id=user.id,
                     member=user.username, role=role)
        flash(f"Added {user.username} as {role}.", "success")
    return redirect(url_for("main.project_settings", project_id=project.id))


@main_bp.post("/p/<int:project_id>/members/<int:user_id>")
@project_route("owner")
def update_member(project, user_id):
    member = ProjectMember.query.filter_by(project_id=project.id, user_id=user_id).first_or_404()
    action = request.form.get("action")
    username = member.user.username
    if action == "remove":
        if member.role == "owner" and owner_count(project.id) <= 1:
            flash("A project needs at least one owner.", "error")
        else:
            db.session.delete(member)
            audit.record("member.removed", project_id=project.id, target_type="user", target_id=user_id,
                         member=username)
            # Drop their live connection out of the project room straight away
            socketio.emit("project:access_revoked", {"user_id": user_id}, to=project_room(project.id))
            flash(f"Removed {username}.", "success")
    else:
        role = request.form.get("role")
        if role not in PROJECT_ROLES:
            abort(400)
        if member.role == "owner" and role != "owner" and owner_count(project.id) <= 1:
            flash("A project needs at least one owner.", "error")
        elif role != member.role:
            audit.record("member.role_changed", project_id=project.id, target_type="user", target_id=user_id,
                         member=username, old=member.role, new=role, commit=False)
            member.role = role
            db.session.commit()
            flash(f"{username} is now {role}.", "success")
    return redirect(url_for("main.project_settings", project_id=project.id))


@main_bp.post("/p/<int:project_id>/features")
@project_route("owner")
def update_features(project):
    # Each form posts one switch (feature=<name>) so toggling one never changes the other
    feature = request.form.get("feature")
    if feature not in BETA_FEATURES:
        abort(400)
    label, kept = BETA_FEATURES[feature]
    flag = f"{feature}_enabled"
    enabled = request.form.get(flag) == "on"
    if enabled != getattr(project, flag):
        setattr(project, flag, enabled)
        db.session.commit()
        audit.record(f"project.{feature}_{'enabled' if enabled else 'disabled'}", project_id=project.id,
                     target_type="project", target_id=project.id)
        flash(f"{label} (beta) turned on. It's now in the project tabs." if enabled
              else f"{label} (beta) turned off. {kept} kept and come back if you turn it on again.", "success")
    return redirect(url_for("main.project_settings", project_id=project.id))


@main_bp.post("/p/<int:project_id>/archive")
@project_route()
def archive_project(project):
    if ROLE_LEVEL.get(g.project_role, 0) < ROLE_LEVEL["owner"]:
        abort(403)
    archive = request.form.get("archive") == "1"
    project.archived_at = utcnow() if archive else None
    db.session.commit()
    audit.record("project.archived" if archive else "project.unarchived", project_id=project.id,
                 target_type="project", target_id=project.id)
    flash("Project archived. It is now read-only." if archive else "Project restored from the archive.", "success")
    return redirect(url_for("main.project_settings", project_id=project.id))


@main_bp.post("/p/<int:project_id>/export")
@project_route()
def export_project_download(project):
    from .transfer import TransferError, export_project

    if ROLE_LEVEL.get(g.project_role, 0) < ROLE_LEVEL["owner"]:
        abort(403)
    password = request.form.get("password", "")
    if password != request.form.get("confirm_password", ""):
        flash("The export passwords don't match.", "error")
        return redirect(url_for("main.project_settings", project_id=project.id))
    handle, path = tempfile.mkstemp(suffix=".zip")
    os.close(handle)
    try:
        export_project(project, path, password)
    except TransferError as exc:
        os.remove(path)
        flash(str(exc), "error")
        return redirect(url_for("main.project_settings", project_id=project.id))
    audit.record("project.exported", project_id=project.id, target_type="project", target_id=project.id)

    @after_this_request
    def cleanup(response):
        response.call_on_close(lambda: os.path.exists(path) and os.remove(path))
        return response

    safe_name = secure_filename(project.name) or f"project-{project.id}"
    return send_file(path, as_attachment=True, download_name=f"{safe_name}-{utcnow():%Y%m%d}.zip",
                     mimetype="application/zip")


@main_bp.post("/p/<int:project_id>/delete")
@project_route()
def delete_project(project):
    if ROLE_LEVEL.get(g.project_role, 0) < ROLE_LEVEL["owner"]:
        abort(403)
    if request.form.get("confirm_name", "").strip() != project.name:
        flash("The name you typed doesn't match the project name.", "error")
        return redirect(url_for("main.project_settings", project_id=project.id))
    name, project_id = project.name, project.id
    socketio.emit("project:deleted", {}, to=project_room(project_id))
    delete_project_data(project)
    audit.record("project.deleted", project_id=project_id, target_type="project", target_id=project_id, name=name)
    if session.get("last_project_id") == project_id:
        session.pop("last_project_id", None)
    flash(f"Deleted “{name}”.", "success")
    return redirect(url_for("main.projects"))


@main_bp.app_template_global()
def line_diff(old, new):
    """(kind, line) pairs for a simple line diff: kind is "same", "add" or "del"."""
    import difflib

    rows = []
    for line in difflib.ndiff((old or "").splitlines(), (new or "").splitlines()):
        marker, text = line[:2], line[2:]
        if marker == "  ":
            rows.append(("same", text))
        elif marker == "+ ":
            rows.append(("add", text))
        elif marker == "- ":
            rows.append(("del", text))
    return rows


@main_bp.app_template_filter("fromjson")
def fromjson_filter(value):
    try:
        return json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
