"""Project deletion and the periodic retention purges."""
import hashlib
import logging
import os
import shutil
from datetime import timedelta

from flask import current_app

from . import db, socketio
from .models import AuditLog, Finding, GraphEdge, GraphNode, NodeRevision, Project, ProjectMember, TimelineEvent, Upload, utcnow

log = logging.getLogger(__name__)

MAINTENANCE_INTERVAL_SECONDS = 3600


def upload_root():
    return current_app.config["UPLOAD_ROOT"]


def project_upload_dir(project_id, create=True):
    path = os.path.join(upload_root(), str(int(project_id)))
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def delete_project_data(project):
    """Remove a project with all its pages, links, events, findings, uploads and memberships."""
    project_id = project.id
    node_ids = [node_id for (node_id,) in db.session.query(GraphNode.id).filter_by(project_id=project_id)]
    TimelineEvent.query.filter_by(project_id=project_id).delete(synchronize_session=False)
    GraphEdge.query.filter_by(project_id=project_id).delete(synchronize_session=False)
    Finding.query.filter_by(project_id=project_id).delete(synchronize_session=False)
    if node_ids:
        NodeRevision.query.filter(NodeRevision.node_id.in_(node_ids)).delete(synchronize_session=False)
    # Clear parent links first so the self-referencing rows can go in one statement
    GraphNode.query.filter_by(project_id=project_id).update({"parent_id": None}, synchronize_session=False)
    GraphNode.query.filter_by(project_id=project_id).delete(synchronize_session=False)
    Upload.query.filter_by(project_id=project_id).delete(synchronize_session=False)
    ProjectMember.query.filter_by(project_id=project_id).delete(synchronize_session=False)
    db.session.delete(project)
    db.session.commit()
    path = project_upload_dir(project_id, create=False)
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def purge_deleted_nodes():
    from .settings import get_setting

    days = get_setting("deleted_page_retention_days")
    if not days:
        return 0
    cutoff = utcnow() - timedelta(days=days)
    expired_ids = [node_id for (node_id,) in db.session.query(GraphNode.id).filter(
        GraphNode.deleted_at.isnot(None), GraphNode.deleted_at < cutoff)]
    if not expired_ids:
        return 0
    GraphEdge.query.filter(GraphEdge.source_id.in_(expired_ids) | GraphEdge.target_id.in_(expired_ids)).delete(synchronize_session=False)
    GraphNode.query.filter(GraphNode.parent_id.in_(expired_ids)).update({"parent_id": None}, synchronize_session=False)
    NodeRevision.query.filter(NodeRevision.node_id.in_(expired_ids)).delete(synchronize_session=False)
    GraphNode.query.filter(GraphNode.id.in_(expired_ids)).delete(synchronize_session=False)
    db.session.commit()
    return len(expired_ids)


def purge_archived_projects():
    from . import audit
    from .settings import get_setting

    days = get_setting("archived_project_retention_days")
    if not days:
        return 0
    cutoff = utcnow() - timedelta(days=days)
    expired = Project.query.filter(Project.archived_at.isnot(None), Project.archived_at < cutoff).all()
    for project in expired:
        name, project_id = project.name, project.id
        delete_project_data(project)
        audit.record("project.purged", project_id=project_id, target_type="project", target_id=project_id,
                     username="system", name=name, reason=f"archived more than {days} days")
    return len(expired)


def purge_audit_logs():
    from .settings import get_setting

    days = get_setting("audit_log_retention_days")
    if not days:
        return 0
    cutoff = utcnow() - timedelta(days=days)
    count = AuditLog.query.filter(AuditLog.at < cutoff).delete(synchronize_session=False)
    db.session.commit()
    return count


def register_untracked_uploads(app):
    """Add Upload rows for files already on disk (uploads from before tracking, or copied in by hand)."""
    root = app.config["UPLOAD_ROOT"]
    if not os.path.isdir(root):
        return
    known = {filename for (filename,) in db.session.query(Upload.filename)}
    project_ids = {project_id for (project_id,) in db.session.query(Project.id)}
    added = 0
    for entry in os.listdir(root):
        if not entry.isdigit() or int(entry) not in project_ids:
            continue
        folder = os.path.join(root, entry)
        for filename in os.listdir(folder):
            path = os.path.join(folder, filename)
            if filename in known or not os.path.isfile(path):
                continue
            db.session.add(Upload(project_id=int(entry), filename=filename, original_name=filename,
                                  size=os.path.getsize(path), sha256=file_sha256(path)))
            known.add(filename)
            added += 1
    if added:
        db.session.commit()
        log.info("Registered %d untracked upload(s)", added)


def run_maintenance(app):
    from .security import purge_old_login_failures

    with app.app_context():
        try:
            pages = purge_deleted_nodes()
            projects = purge_archived_projects()
            logs = purge_audit_logs()
            purge_old_login_failures()
            db.session.commit()
            if pages or projects or logs:
                log.info("Maintenance purged %d page(s), %d project(s), %d audit entries", pages, projects, logs)
        except Exception:
            db.session.rollback()
            log.exception("Maintenance run failed")
        finally:
            db.session.remove()


def start_maintenance(app):
    """Run the retention purges now and then hourly in the background."""
    def loop():
        while True:
            run_maintenance(app)
            socketio.sleep(MAINTENANCE_INTERVAL_SECONDS)

    socketio.start_background_task(loop)
