"""Who can see and change which project."""
from functools import wraps

from flask import abort, g, session
from flask_login import current_user, login_required

from . import db
from .models import Project, ProjectMember

ROLE_LEVEL = {"viewer": 1, "editor": 2, "owner": 3}


def project_role(user, project):
    """The user's role in the project ("owner", "editor", "viewer") or None. Admins act as owners."""
    if not user or not user.is_authenticated or not project:
        return None
    if user.is_admin:
        return "owner"
    member = ProjectMember.query.filter_by(project_id=project.id, user_id=user.id).first()
    return member.role if member else None


def role_allows(role, project, needed):
    if role is None:
        return False
    # Archived projects are read-only for everyone
    if project.archived_at is not None and needed != "viewer":
        return False
    return ROLE_LEVEL.get(role, 0) >= ROLE_LEVEL[needed]


def accessible_projects(user, include_archived=False):
    query = Project.query
    if not user.is_admin:
        query = query.join(ProjectMember).filter(ProjectMember.user_id == user.id)
    if not include_archived:
        query = query.filter(Project.archived_at.is_(None))
    return query.order_by(Project.created_at.desc()).all()


def load_project(project_id, needed="viewer"):
    """The project if the current user may access it at the needed level; aborts otherwise.

    A project the user isn't a member of is reported as missing, so its existence isn't revealed.
    """
    project = db.session.get(Project, project_id)
    role = project_role(current_user, project)
    if project is None or role is None:
        abort(404)
    if not role_allows(role, project, needed):
        abort(403)
    g.project = project
    g.project_role = role
    session["last_project_id"] = project.id
    return project


def project_route(needed="viewer"):
    """Decorator for routes taking <int:project_id>; passes the loaded Project as `project`."""
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapper(project_id, *args, **kwargs):
            project = load_project(project_id, needed)
            return view(project, *args, **kwargs)
        return wrapper
    return decorator


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapper
