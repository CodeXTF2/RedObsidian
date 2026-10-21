import csv
import io
import os
import zipfile
from datetime import datetime, timedelta

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, stream_with_context, url_for
from flask_login import current_user

from . import audit, db
from .access import admin_required
from .models import PROJECT_ROLES, AuditLog, Finding, GraphEdge, GraphNode, Project, ProjectMember, TimelineEvent, Upload, User
from .security import password_policy_text, password_problems, random_password
from .settings import DEFAULTS, SECRET_KEYS, all_settings, set_settings

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

AUDIT_PAGE_SIZE = 100
MAX_TEMPLATE_BYTES = 20 * 1024 * 1024


@admin_bp.get("/")
@admin_required
def index():
    return redirect(url_for("admin.users"))


# --- Users -----------------------------------------------------------------------

def active_admin_count():
    return User.query.filter_by(is_admin=True, disabled=False).count()


@admin_bp.get("/users")
@admin_required
def users():
    all_users = User.query.order_by(User.username).all()
    memberships = dict(db.session.query(ProjectMember.user_id, db.func.count(ProjectMember.id))
                       .group_by(ProjectMember.user_id).all())
    return render_template("admin/users.html", users=all_users, memberships=memberships,
                           policy=password_policy_text(), settings=all_settings(), admin_page="users")


@admin_bp.post("/users")
@admin_required
def create_user():
    username = request.form.get("username", "").strip()[:64]
    if not username or any(ch.isspace() for ch in username):
        flash("Usernames can't be empty or contain spaces.", "error")
        return redirect(url_for("admin.users"))
    if User.query.filter_by(username=username).first():
        flash(f"User “{username}” already exists.", "error")
        return redirect(url_for("admin.users"))
    password = request.form.get("password", "")
    generated = not password
    if generated:
        password = random_password()
    else:
        problems = password_problems(password, username)
        if problems:
            for problem in problems:
                flash(problem, "error")
            return redirect(url_for("admin.users"))
    user = User(username=username, is_admin=request.form.get("is_admin") == "on")
    user.set_password(password)
    user.must_change_password = request.form.get("must_change") == "on" or generated
    db.session.add(user)
    db.session.flush()
    audit.record("user.created", target_type="user", target_id=user.id, target=username, is_admin=user.is_admin)
    return render_template("admin/credential.html", user=user, password=password, created=True, admin_page="users")


def target_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    return user


@admin_bp.get("/users/<int:user_id>")
@admin_required
def user_detail(user_id):
    user = target_user(user_id)
    memberships = ProjectMember.query.filter_by(user_id=user.id).join(Project).order_by(Project.name).all()
    member_project_ids = {m.project_id for m in memberships}
    other_projects = [p for p in Project.query.order_by(Project.name).all() if p.id not in member_project_ids]
    recent = AuditLog.query.filter_by(user_id=user.id).order_by(AuditLog.id.desc()).limit(25).all()
    return render_template("admin/user_detail.html", user=user, memberships=memberships, other_projects=other_projects,
                           roles=PROJECT_ROLES, recent=recent, admin_page="users")


@admin_bp.post("/users/<int:user_id>/action")
@admin_required
def user_action(user_id):
    user = target_user(user_id)
    action = request.form.get("action")
    is_self = user.id == current_user.id
    back = redirect(url_for("admin.user_detail", user_id=user.id))

    if action == "reset_password":
        password = random_password()
        user.set_password(password)
        user.must_change_password = True
        db.session.commit()
        audit.record("user.password_reset", target_type="user", target_id=user.id, target=user.username)
        return render_template("admin/credential.html", user=user, password=password, created=False, admin_page="users")

    if action in ("disable", "demote") and is_self:
        flash("You can't do that to your own account.", "error")
        return back
    if action in ("disable", "demote") and user.is_admin and not user.disabled and active_admin_count() <= 1:
        flash("At least one active administrator must remain.", "error")
        return back

    if action == "disable":
        user.disabled = True
        user.rotate_session_token()
        message = f"{user.username} is disabled and has been signed out."
    elif action == "enable":
        user.disabled = False
        message = f"{user.username} can sign in again."
    elif action == "promote":
        user.is_admin = True
        message = f"{user.username} is now an administrator."
    elif action == "demote":
        user.is_admin = False
        message = f"{user.username} is no longer an administrator."
    elif action == "reset_2fa":
        user.totp_enabled = False
        user.totp_secret = None
        user.totp_last_step = None
        user.recovery_codes_json = "[]"
        user.rotate_session_token()
        message = f"Two-factor authentication was reset for {user.username}."
    elif action == "revoke_sessions":
        user.rotate_session_token()
        message = f"All of {user.username}'s sessions were signed out."
    elif action == "force_password_change":
        user.must_change_password = True
        message = f"{user.username} must change their password at next sign-in."
    else:
        abort(400)
    db.session.commit()
    audit.record(f"user.{action}", target_type="user", target_id=user.id, target=user.username)
    flash(message, "success")
    return back


def authored_content_count(user_id):
    return sum(model.query.filter_by(user_id=user_id).count()
               for model in (GraphNode, GraphEdge, TimelineEvent, Finding))


@admin_bp.post("/users/<int:user_id>/delete")
@admin_required
def delete_user(user_id):
    user = target_user(user_id)
    if user.id == current_user.id:
        flash("You can't delete your own account.", "error")
        return redirect(url_for("admin.user_detail", user_id=user.id))
    if authored_content_count(user.id) or Project.query.filter_by(created_by_id=user.id).count():
        flash(f"{user.username} has written project content, so the account can't be deleted. Disable it instead.", "error")
        return redirect(url_for("admin.user_detail", user_id=user.id))
    if user.is_admin and active_admin_count() <= 1:
        flash("At least one active administrator must remain.", "error")
        return redirect(url_for("admin.user_detail", user_id=user.id))
    username = user.username
    ProjectMember.query.filter_by(user_id=user.id).delete()
    Upload.query.filter_by(user_id=user.id).update({"user_id": None})
    db.session.delete(user)
    db.session.commit()
    audit.record("user.deleted", target_type="user", target_id=user_id, target=username)
    flash(f"Deleted {username}.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.post("/users/<int:user_id>/memberships")
@admin_required
def user_membership(user_id):
    user = target_user(user_id)
    project = db.session.get(Project, request.form.get("project_id", type=int) or 0)
    role = request.form.get("role")
    if not project:
        abort(404)
    member = ProjectMember.query.filter_by(project_id=project.id, user_id=user.id).first()
    if role == "remove":
        if member:
            if member.role == "owner" and ProjectMember.query.filter_by(project_id=project.id, role="owner").count() <= 1:
                flash("A project needs at least one owner.", "error")
                return redirect(url_for("admin.user_detail", user_id=user.id))
            db.session.delete(member)
            audit.record("member.removed", project_id=project.id, target_type="user", target_id=user.id,
                         member=user.username)
    elif role in PROJECT_ROLES:
        if member:
            if member.role == "owner" and role != "owner" and ProjectMember.query.filter_by(project_id=project.id, role="owner").count() <= 1:
                flash("A project needs at least one owner.", "error")
                return redirect(url_for("admin.user_detail", user_id=user.id))
            old = member.role
            member.role = role
            audit.record("member.role_changed", project_id=project.id, target_type="user", target_id=user.id,
                         member=user.username, old=old, new=role)
        else:
            db.session.add(ProjectMember(project_id=project.id, user_id=user.id, role=role))
            audit.record("member.added", project_id=project.id, target_type="user", target_id=user.id,
                         member=user.username, role=role)
    else:
        abort(400)
    flash("Project access updated.", "success")
    return redirect(url_for("admin.user_detail", user_id=user.id))


# --- Settings ----------------------------------------------------------------------

@admin_bp.get("/settings")
@admin_required
def settings():
    from .auth import sso_configured

    values = dict(all_settings())
    secret_set = {key: bool(values[key]) for key in SECRET_KEYS}
    for key in SECRET_KEYS:
        values[key] = ""
    return render_template("admin/settings.html", values=values, secret_set=secret_set,
                           sso_ready=sso_configured(), callback_url=url_for("auth.sso_callback", _external=True),
                           admin_page="settings")


@admin_bp.post("/settings")
@admin_required
def save_settings():
    updates = {}
    for key, default in DEFAULTS.items():
        if isinstance(default, bool):
            updates[key] = request.form.get(key) == "on"
        elif key in request.form:
            if key in SECRET_KEYS and not request.form.get(key) and request.form.get(f"{key}__clear") != "on":
                continue  # blank secret field means "keep the current one"
            updates[key] = request.form.get(key)
    changed = set_settings(updates)
    if changed:
        audit.record("settings.changed", target_type="settings", changes={
            key: ("(secret changed)" if key in SECRET_KEYS else {"old": old, "new": new})
            for key, (old, new) in changed.items()
        })
        flash(f"Saved {len(changed)} setting{'s' if len(changed) != 1 else ''}.", "success")
    else:
        flash("No changes.", "success")
    return redirect(url_for("admin.settings"))


@admin_bp.post("/report-template")
@admin_required
def report_template():
    from .reports import template_path

    path = template_path()
    if request.form.get("action") == "remove":
        if os.path.isfile(path):
            os.remove(path)
            audit.record("settings.report_template_removed", target_type="settings")
        flash("Report template removed; reports use the built-in layout.", "success")
        return redirect(url_for("admin.settings"))
    file = request.files.get("template")
    if not file or not file.filename.lower().endswith(".docx"):
        flash("Choose a .docx file.", "error")
        return redirect(url_for("admin.settings"))
    data = file.read(MAX_TEMPLATE_BYTES + 1)
    if len(data) > MAX_TEMPLATE_BYTES:
        flash("The template is too large (20 MB limit).", "error")
        return redirect(url_for("admin.settings"))
    try:
        from docx import Document
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            archive.getinfo("word/document.xml")
        Document(io.BytesIO(data))
    except Exception:
        flash("That file isn't a valid Word document.", "error")
        return redirect(url_for("admin.settings"))
    with open(path, "wb") as handle:
        handle.write(data)
    audit.record("settings.report_template_uploaded", target_type="settings", name=file.filename[:255])
    flash("Report template uploaded.", "success")
    return redirect(url_for("admin.settings"))


@admin_bp.app_template_global()
def report_template_exists():
    from .reports import template_path
    return os.path.isfile(template_path())


# --- Projects ------------------------------------------------------------------------

@admin_bp.get("/projects")
@admin_required
def projects():
    all_projects = Project.query.order_by(Project.archived_at.isnot(None), Project.created_at.desc()).all()
    owners = {}
    for member in ProjectMember.query.filter_by(role="owner").all():
        owners.setdefault(member.project_id, []).append(member.user.username)
    counts = dict(db.session.query(ProjectMember.project_id, db.func.count(ProjectMember.id))
                  .group_by(ProjectMember.project_id).all())
    return render_template("admin/projects.html", projects=all_projects, owners=owners, counts=counts,
                           settings=all_settings(), admin_page="projects")


# --- Audit log ----------------------------------------------------------------------

def audit_query():
    query = AuditLog.query
    username = request.args.get("user", "").strip()
    action = request.args.get("action", "").strip()
    project_id = request.args.get("project", type=int)
    since = request.args.get("since", "").strip()
    until = request.args.get("until", "").strip()
    if username:
        query = query.filter(AuditLog.username == username)
    if action:
        query = query.filter(AuditLog.action.like(f"{action.replace('%', '')}%"))
    if project_id:
        query = query.filter(AuditLog.project_id == project_id)
    try:
        if since:
            query = query.filter(AuditLog.at >= datetime.fromisoformat(since))
        if until:
            query = query.filter(AuditLog.at < datetime.fromisoformat(until) + timedelta(days=1))
    except ValueError:
        flash("Dates must look like 2026-01-31.", "error")
    return query.order_by(AuditLog.id.desc())


@admin_bp.get("/audit")
@admin_required
def audit_log():
    page = max(1, request.args.get("page", 1, type=int))
    entries = audit_query().offset((page - 1) * AUDIT_PAGE_SIZE).limit(AUDIT_PAGE_SIZE + 1).all()
    project_names = dict(db.session.query(Project.id, Project.name).all())
    actions = sorted({action.split(".")[0] for (action,) in db.session.query(AuditLog.action).distinct()})
    return render_template("admin/audit.html", entries=entries[:AUDIT_PAGE_SIZE], has_more=len(entries) > AUDIT_PAGE_SIZE,
                           page=page, project_names=project_names, actions=actions, args=request.args,
                           admin_page="audit")


def csv_cell(value):
    """Stop spreadsheet apps from treating user-written text as a formula."""
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@admin_bp.get("/audit.csv")
@admin_required
def audit_csv():
    def rows():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["time_utc", "user", "ip", "project_id", "action", "target_type", "target_id", "details"])
        yield buffer.getvalue()
        for entry in audit_query().yield_per(500):
            buffer.seek(0)
            buffer.truncate()
            writer.writerow([csv_cell(value) for value in (
                entry.at.isoformat(), entry.username or "", entry.ip or "", entry.project_id or "",
                entry.action, entry.target_type or "", entry.target_id or "", entry.details_json)])
            yield buffer.getvalue()

    audit.record("audit.exported", target_type="audit")
    return Response(stream_with_context(rows()), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=redobsidian-audit.csv"})
