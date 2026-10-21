"""Page history. Each revision is the page's text after an edit; one user's edits within a short window share a revision."""
from datetime import timedelta

from . import db
from .models import NodeRevision, as_utc, utcnow

COALESCE_WINDOW = timedelta(minutes=10)
MAX_REVISIONS_PER_PAGE = 200


def _text(title, caption, notes):
    return (title or "", caption or "", notes or "")


def record_revision(node, user, before=None, force_new=False):
    """Snapshot the node's text if it changed. `before` is (title, caption, notes) prior to the edit."""
    after = _text(node.title, node.caption, node.notes)
    last = NodeRevision.query.filter_by(node_id=node.id).order_by(NodeRevision.id.desc()).first()
    # Only worth keeping when the page had real content (not just its default title)
    if last is None and before is not None and _text(*before) != after and any(before[1:]):
        # Keep the text from before history was tracked, attributed to the page's author
        db.session.add(NodeRevision(node_id=node.id, project_id=node.project_id, title=before[0] or "Untitled page",
                                    caption=before[1], notes=before[2], user_id=node.user_id,
                                    created_at=node.updated_at or utcnow(), updated_at=node.updated_at or utcnow()))
        db.session.flush()
        last = None
    elif last is not None and _text(last.title, last.caption, last.notes) == after:
        return None

    now = utcnow()
    if (not force_new and last is not None and last.user_id == getattr(user, "id", None)
            and now - as_utc(last.created_at) < COALESCE_WINDOW):
        last.title, last.caption, last.notes = node.title, node.caption, node.notes
        last.updated_at = now
        return last

    revision = NodeRevision(node_id=node.id, project_id=node.project_id, title=node.title, caption=node.caption,
                            notes=node.notes, user_id=getattr(user, "id", None), created_at=now, updated_at=now)
    db.session.add(revision)
    db.session.flush()
    prune(node.id)
    return revision


def prune(node_id):
    stale = (NodeRevision.query.filter_by(node_id=node_id).order_by(NodeRevision.id.desc())
             .offset(MAX_REVISIONS_PER_PAGE).all())
    for revision in stale:
        db.session.delete(revision)
