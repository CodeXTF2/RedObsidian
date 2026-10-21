import json
import logging

from flask import has_request_context, request
from flask_login import current_user

from . import db
from .models import AuditLog

log = logging.getLogger("redobsidian.audit")


def client_ip():
    if not has_request_context():
        return None
    return (request.remote_addr or "")[:64] or None


def record(action, project_id=None, target_type=None, target_id=None, user=None, commit=True, **details):
    """Append an audit entry. `user` defaults to the signed-in user."""
    if user is None and has_request_context() and current_user and current_user.is_authenticated:
        user = current_user
    entry = AuditLog(
        user_id=getattr(user, "id", None),
        username=getattr(user, "username", None) or details.pop("username", None),
        project_id=project_id,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        details_json=json.dumps(details, default=str),
        ip=client_ip(),
    )
    db.session.add(entry)
    if commit:
        db.session.commit()
    log.info("%s user=%s project=%s target=%s:%s %s", action, entry.username, project_id, target_type, target_id, details or "")
    return entry
