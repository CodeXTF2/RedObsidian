#!/usr/bin/env python
"""Headless RedObsidian CLI for agents: read, edit and update one project without the web UI.

Every command runs inside the app's own models and rules (permissions, page history, audit log,
CVSS scoring, report builders), so changes look exactly like edits made in the browser.

    python redobsidian_agent.py --user <username> --project <name-or-id> <command> ...
    python redobsidian_agent.py help-agent            # quick reference for agents

Identity and scope
  --user     / REDOBSIDIAN_AGENT_USER       account the edits are made as (and audited as).
  --project  / REDOBSIDIAN_AGENT_PROJECT    project to work on (name or ID).
  REDOBSIDIAN_AGENT_PROJECTS                optional comma-separated allowlist of project names/IDs;
                                            anything else is refused, even for admins.
  The user's project role is enforced: viewer = read, editor = write, owner = settings/members/export.
  Archived projects are read-only. This is attribution and scoping, not authentication: whoever can run
  this script can already read the database.

Output
  JSON on stdout: {"ok": true, "result": ...} or {"ok": false, "error": "..."} (exit code 1).
  A few commands accept --raw to print plain text (page text, a section, a Markdown report).

Writes
  Each invocation is one transaction. `batch` runs many commands atomically (all or nothing).
  Open browser tabs update live only when REDIS_URL is set (the app's message queue); otherwise they
  pick up changes on reload. Use `page update --expect-updated-at` to avoid overwriting someone else's edit.
"""
import argparse
import difflib
import json
import os
import re
import shlex
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
# Never run migrations or start-up fixups from a CLI call, and keep app logging off stdout/stderr noise
os.environ.setdefault("REDOBSIDIAN_AUTO_MIGRATE", "0")
os.environ.setdefault("REDOBSIDIAN_LOG_LEVEL", "WARNING")

from werkzeug.utils import secure_filename  # noqa: E402

from app import audit, create_app, db, socketio  # noqa: E402
from app.access import ROLE_LEVEL, accessible_projects, project_role, role_allows  # noqa: E402
from app.findings import (  # noqa: E402
    FINDING_TEXT_FIELDS,
    REPORT_FIELDS,
    REPORT_FLAGS,
    clean_finding_fields,
    cvss31_score,
    severity_for_score,
    sorted_findings,
)
from app.main import IMAGE_EXTENSIONS, parse_iso_datetime, project_room  # noqa: E402
from app.maintenance import file_sha256, project_upload_dir  # noqa: E402
from app.models import (  # noqa: E402
    FINDING_STATUSES,
    PROJECT_ROLES,
    SEVERITIES,
    AuditLog,
    Finding,
    GraphEdge,
    GraphNode,
    NodeRevision,
    Project,
    ProjectMember,
    TimelineEvent,
    Upload,
    User,
    as_utc,
    isoformat_utc,
    load_json_list,
    utcnow,
)
from app.realtime import automatic_timeline_order, is_descendant, reset_timeline_auto_order  # noqa: E402
from app.revisions import record_revision  # noqa: E402

MAX_ATTACHMENTS = 200
PAGE_COLORS = ["red", "orange", "yellow", "green", "blue", "pink"]  # the graph's colour classes
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")


class CliError(Exception):
    """A problem with the request; the message is returned to the caller."""


# --- Context ---------------------------------------------------------------------

class Ctx:
    def __init__(self, app, args):
        self.app = app
        self.args = args
        self.user = None
        self.project = None
        self.role = None
        self.emits = []          # (event, payload) broadcast after commit
        self.on_commit = []      # callables run after a successful commit
        self.on_rollback = []    # callables run if the transaction is rolled back

    # identity / scope
    def load_user(self):
        if self.user:
            return self.user
        username = self.args.user or os.environ.get("REDOBSIDIAN_AGENT_USER")
        if not username:
            raise CliError("No user. Pass --user <username> or set REDOBSIDIAN_AGENT_USER.")
        user = User.query.filter_by(username=username).first()
        if not user:
            raise CliError(f"User '{username}' not found.")
        if user.disabled:
            raise CliError(f"User '{username}' is disabled.")
        self.user = user
        return user

    def load_project(self, needed="viewer"):
        user = self.load_user()
        if self.project is None:
            ref = self.args.project or os.environ.get("REDOBSIDIAN_AGENT_PROJECT")
            if not ref:
                raise CliError("No project. Pass --project <name-or-id> or set REDOBSIDIAN_AGENT_PROJECT.")
            project = find_project(ref)
            role = project_role(user, project) if project else None
            if project is None or role is None:
                raise CliError(f"Project '{ref}' not found (or {user.username} is not a member).")
            check_allowlist(ref, project)
            self.project, self.role = project, role
        if not role_allows(self.role, self.project, needed):
            if self.project.archived_at is not None and needed != "viewer":
                raise CliError("This project is archived and read-only. Unarchive it first (owner).")
            raise CliError(f"{user.username} is {self.role} of this project; this needs {needed}.")
        return self.project

    # side effects
    def audit(self, action, target_type=None, target_id=None, **details):
        audit.record(action, project_id=self.project.id if self.project else None, target_type=target_type,
                     target_id=target_id, user=self.user, commit=False, via="agent-cli", **details)

    def emit(self, event, payload):
        self.emits.append((event, payload))


def check_allowlist(ref, project=None):
    allow = [item.strip() for item in os.environ.get("REDOBSIDIAN_AGENT_PROJECTS", "").split(",") if item.strip()]
    if not allow:
        return
    keys = {str(ref)}
    if project is not None:
        keys |= {project.name, str(project.id)}
    if not keys & set(allow):
        raise CliError(f"Project '{ref}' is not in REDOBSIDIAN_AGENT_PROJECTS.")


def find_project(ref):
    ref = str(ref).strip()
    matches = Project.query.filter_by(name=ref).all()
    if not matches and ref.isdigit():
        project = db.session.get(Project, int(ref))
        matches = [project] if project else []
    if len(matches) > 1:
        raise CliError(f"Several projects are named '{ref}'; use the ID: " + ", ".join(str(p.id) for p in matches))
    return matches[0] if matches else None


# --- Helpers ---------------------------------------------------------------------

def read_text(value=None, file=None):
    """Text from --x VALUE or --x-file PATH ('-' = stdin). None if neither was given."""
    if file is not None:
        if file == "-":
            return sys.stdin.read()
        try:
            return Path(file).read_text(encoding="utf-8")
        except OSError as exc:
            raise CliError(f"Can't read {file}: {exc}") from exc
    return value


def on_off(value):
    if value is None:
        return None
    lowered = str(value).strip().lower()
    if lowered in ("on", "true", "1", "yes"):
        return True
    if lowered in ("off", "false", "0", "no"):
        return False
    raise CliError(f"Expected on/off, got '{value}'.")


def parse_time(value, label="time"):
    if value in (None, ""):
        return None
    if str(value).lower() == "now":
        return utcnow()
    try:
        return parse_iso_datetime(str(value))
    except (TypeError, ValueError) as exc:
        raise CliError(f"Invalid {label} '{value}'. Use ISO 8601, e.g. 2026-11-18T09:30:00Z.") from exc


def upload_url(project_id, filename):
    return f"/p/{project_id}/uploads/{filename}"


def clean_files(items):
    files = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            files.append({"name": str(item.get("name", ""))[:255], "url": str(item.get("url", ""))[:1024]})
    return json.dumps(files[:MAX_ATTACHMENTS])


def node_out(node, ctx, include_notes=True):
    """A page as JSON. Write commands leave out the body (it can be long); `page get` returns it."""
    data = node.to_dict()
    if not include_notes:
        data.pop("notes", None)
    data["notes_chars"] = len(node.notes or "")
    data["deleted_at"] = isoformat_utc(node.deleted_at) if node.deleted_at else None
    data["path"] = page_path(node)
    data["url"] = f"/p/{ctx.project.id}/nodes/{node.id}"
    return data


def page_path(node):
    parts, seen, cursor = [], set(), node
    while cursor is not None and cursor.id not in seen:
        parts.append(cursor.title)
        seen.add(cursor.id)
        cursor = db.session.get(GraphNode, cursor.parent_id) if cursor.parent_id else None
    return " / ".join(reversed(parts))


def live_pages(project_id):
    return GraphNode.query.filter_by(project_id=project_id, deleted_at=None).all()


def sibling_key(node):
    return (node.order_index or 0, (node.title or "").lower(), node.id)


def resolve_page(ctx, ref, include_deleted=False, deleted_only=False):
    """A page by ID, exact title, case-insensitive title, or 'Parent / Child' path."""
    ref = str(ref).strip()
    query = GraphNode.query.filter_by(project_id=ctx.project.id)
    if deleted_only:
        query = query.filter(GraphNode.deleted_at.isnot(None))
    elif not include_deleted:
        query = query.filter(GraphNode.deleted_at.is_(None))
    if ref.isdigit():
        node = query.filter(GraphNode.id == int(ref)).first()
        if node:
            return node
    candidates = query.all()
    for test in (lambda n: n.title == ref, lambda n: n.title.lower() == ref.lower(),
                 lambda n: page_path(n).lower() == ref.lower()):
        matches = [n for n in candidates if test(n)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise CliError(f"'{ref}' matches several pages; use an ID: "
                           + ", ".join(f"{n.id} ({page_path(n)})" for n in matches))
    state = "deleted page" if deleted_only else "page"
    raise CliError(f"No {state} '{ref}' in this project.")


def next_order_index(project_id, parent_id, exclude_id=None):
    siblings = [n for n in live_pages(project_id) if (n.parent_id or None) == (parent_id or None) and n.id != exclude_id]
    return max((n.order_index or 0) for n in siblings) + 1 if siblings else 0


def order_between(siblings, anchor, before):
    """order_index that places an item just before/after `anchor` in the sorted `siblings` list."""
    ordered = [s for s in siblings]
    index = next(i for i, s in enumerate(ordered) if s.id == anchor.id)
    if before:
        prev, nxt = (ordered[index - 1] if index > 0 else None), anchor
    else:
        prev, nxt = anchor, (ordered[index + 1] if index + 1 < len(ordered) else None)
    if prev and nxt:
        return ((prev.order_index or 0) + (nxt.order_index or 0)) / 2
    if prev:
        return (prev.order_index or 0) + 1
    return (nxt.order_index or 0) - 1


def place_page(ctx, node, parent_id, before=None, after=None):
    """Set node.order_index among its new siblings."""
    anchor_ref = before or after
    if not anchor_ref:
        node.order_index = next_order_index(ctx.project.id, parent_id, exclude_id=node.id)
        return
    anchor = resolve_page(ctx, anchor_ref)
    if (anchor.parent_id or None) != (parent_id or None):
        raise CliError(f"'{anchor.title}' is not a sibling under the chosen parent.")
    if anchor.id == node.id:
        raise CliError("A page can't be placed relative to itself.")
    siblings = sorted([n for n in live_pages(ctx.project.id)
                       if (n.parent_id or None) == (parent_id or None) and n.id != node.id], key=sibling_key)
    node.order_index = order_between(siblings, anchor, before=bool(before))


def check_parent(ctx, node_id, parent_ref):
    """Parent page ID for a 'parent' argument ('root' or '' means top level)."""
    if parent_ref is None or str(parent_ref).strip().lower() in ("", "root", "none", "null"):
        return None
    parent = resolve_page(ctx, parent_ref)
    if node_id is not None:
        if parent.id == node_id:
            raise CliError("A page cannot contain itself.")
        if is_descendant(node_id, parent.id):
            raise CliError("A page cannot be moved inside its own child.")
    return parent.id


def emit_node_updated(ctx, node):
    ctx.emit("node:updated", {**node.to_dict(), "origin": None, "edited_by": ctx.user.username})


# Markdown sections ------------------------------------------------------------------

def heading_lines(lines):
    """(index, level, title) for every Markdown heading outside fenced code blocks."""
    found, in_fence = [], False
    for index, line in enumerate(lines):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            match = HEADING_RE.match(line)
            if match:
                found.append((index, len(match.group(1)), match.group(2).strip()))
    return found


def find_section(text, heading):
    """(start, body_start, end, level) line indexes of a section, or None. `heading` is '## Title' or 'Title'."""
    lines = (text or "").split("\n")
    match = HEADING_RE.match(heading.strip())
    level = len(match.group(1)) if match else None
    title = (match.group(2) if match else heading).strip().lower()
    headings = heading_lines(lines)
    for position, (index, found_level, found_title) in enumerate(headings):
        if found_title.lower() == title and (level is None or level == found_level):
            end = len(lines)
            for later_index, later_level, _ in headings[position + 1:]:
                if later_level <= found_level:
                    end = later_index
                    break
            return index, index + 1, end, found_level
    return None


def get_section(text, heading):
    found = find_section(text, heading)
    if not found:
        return None
    _, body_start, end, _ = found
    return "\n".join((text or "").split("\n")[body_start:end]).strip("\n")


def set_section(text, heading, body, mode="replace", create=True):
    lines = (text or "").split("\n")
    found = find_section(text, heading)
    body = (body or "").strip("\n")
    if not found:
        if not create:
            raise CliError(f"Section '{heading}' not found.")
        title = heading.strip() if HEADING_RE.match(heading.strip()) else f"## {heading.strip()}"
        base = (text or "").rstrip("\n")
        return (base + "\n\n" if base else "") + title + "\n\n" + body + "\n"
    start, body_start, end, _ = found
    existing = "\n".join(lines[body_start:end]).strip("\n")
    if mode == "append":
        new_body = (existing + "\n\n" + body) if existing else body
    elif mode == "prepend":
        new_body = (body + "\n\n" + existing) if existing else body
    else:
        new_body = body
    tail = lines[end:]
    block = [""] + (new_body.split("\n") if new_body else []) + ([""] if tail else [])
    return "\n".join(lines[:body_start] + block + tail)


def delete_section(text, heading):
    found = find_section(text, heading)
    if not found:
        raise CliError(f"Section '{heading}' not found.")
    start, _, end, _ = found
    lines = (text or "").split("\n")
    return "\n".join(lines[:start] + lines[end:]).strip("\n") + "\n"


def outline(text):
    lines = (text or "").split("\n")
    return [{"line": index + 1, "level": level, "title": title} for index, level, title in heading_lines(lines)]


# --- Commands: identity / projects ---------------------------------------------

def cmd_whoami(ctx, a):
    user = ctx.load_user()
    return {"username": user.username, "id": user.id, "is_admin": user.is_admin,
            "allowlist": os.environ.get("REDOBSIDIAN_AGENT_PROJECTS") or None}


def cmd_projects(ctx, a):
    user = ctx.load_user()
    allow = [item.strip() for item in os.environ.get("REDOBSIDIAN_AGENT_PROJECTS", "").split(",") if item.strip()]
    result = []
    for project in accessible_projects(user, include_archived=a.archived):
        if allow and not ({project.name, str(project.id)} & set(allow)):
            continue
        result.append({**project.to_dict(), "role": project_role(user, project)})
    return result


def project_info(ctx):
    project = ctx.project
    pid = project.id
    return {
        **project.to_dict(),
        "role": ctx.role,
        "can_edit": role_allows(ctx.role, project, "editor"),
        "findings_enabled": project.findings_enabled,
        "report_enabled": project.report_enabled,
        "url": f"/p/{pid}/notes",
        "counts": {
            "pages": GraphNode.query.filter_by(project_id=pid, deleted_at=None).count(),
            "deleted_pages": GraphNode.query.filter_by(project_id=pid).filter(GraphNode.deleted_at.isnot(None)).count(),
            "links": GraphEdge.query.filter_by(project_id=pid).count(),
            "timeline_events": TimelineEvent.query.filter_by(project_id=pid).count(),
            "findings": Finding.query.filter_by(project_id=pid).count(),
            "uploads": Upload.query.filter_by(project_id=pid).count(),
            "members": ProjectMember.query.filter_by(project_id=pid).count(),
        },
    }


def cmd_project_info(ctx, a):
    ctx.load_project()
    return project_info(ctx)


def cmd_project_update(ctx, a):
    project = ctx.load_project("owner")
    changes = {}
    if a.name is not None:
        name = a.name.strip()[:120]
        if not name:
            raise CliError("The project needs a name.")
        if name != project.name:
            changes["name"] = [project.name, name]
            project.name = name
    description = read_text(a.description, a.description_file)
    if description is not None and description.strip() != (project.description or ""):
        project.description = description.strip()
        changes["description"] = "changed"
    if changes:
        ctx.audit("project.updated", "project", project.id, **changes)
    return project_info(ctx)


def cmd_project_feature(ctx, a):
    project = ctx.load_project("owner")
    enabled = on_off(a.state)
    flag = f"{a.feature}_enabled"
    if enabled != getattr(project, flag):
        setattr(project, flag, enabled)
        ctx.audit(f"project.{a.feature}_{'enabled' if enabled else 'disabled'}", "project", project.id)
    return {"feature": a.feature, "enabled": enabled}


def cmd_project_archive(ctx, a, archive=True):
    # Owners may unarchive an archived (read-only) project, so check the role directly
    ctx.load_project("viewer")
    if ROLE_LEVEL.get(ctx.role, 0) < ROLE_LEVEL["owner"]:
        raise CliError("Only an owner can archive or unarchive a project.")
    project = ctx.project
    if archive == (project.archived_at is not None):
        return {"archived": archive, "changed": False}
    project.archived_at = utcnow() if archive else None
    ctx.audit("project.archived" if archive else "project.unarchived", "project", project.id)
    return {"archived": archive, "changed": True}


def cmd_project_export(ctx, a):
    from app.transfer import TransferError, export_project

    ctx.load_project("viewer")
    if ROLE_LEVEL.get(ctx.role, 0) < ROLE_LEVEL["owner"]:
        raise CliError("Only an owner can export a project.")
    password = os.environ.get("REDOBSIDIAN_EXPORT_PASSWORD")
    if not password:
        raise CliError("Set REDOBSIDIAN_EXPORT_PASSWORD (8+ characters) to encrypt the export.")
    try:
        export_project(ctx.project, a.out, password)
    except TransferError as exc:
        raise CliError(str(exc)) from exc
    ctx.audit("project.exported", "project", ctx.project.id)
    return {"file": str(Path(a.out).resolve()), "bytes": os.path.getsize(a.out)}


def cmd_project_dump(ctx, a):
    """Everything in the project as one JSON document (live pages with full text)."""
    project = ctx.load_project()
    pid = project.id
    nodes = sorted(live_pages(pid), key=lambda n: n.id)
    live_ids = {n.id for n in nodes}
    data = {
        "project": project_info(ctx),
        "report": project.report,
        "pages": [node_out(n, ctx) for n in nodes],
        "links": [e.to_dict() for e in GraphEdge.query.filter_by(project_id=pid).order_by(GraphEdge.id)
                  if e.source_id in live_ids and e.target_id in live_ids],
        "timeline": [e.to_dict() for e in
                     TimelineEvent.query.filter_by(project_id=pid).order_by(TimelineEvent.order_index)],
        "findings": [finding_out(f) for f in sorted_findings(pid)],
        "files": [upload_out(u) for u in Upload.query.filter_by(project_id=pid).order_by(Upload.created_at)],
    }
    if a.out:
        Path(a.out).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"file": str(Path(a.out).resolve())}
    return data


def cmd_activity(ctx, a):
    ctx.load_project("viewer")
    if ROLE_LEVEL.get(ctx.role, 0) < ROLE_LEVEL["owner"]:
        raise CliError("Only an owner can read the project activity log.")
    query = AuditLog.query.filter_by(project_id=ctx.project.id)
    if a.action:
        query = query.filter(AuditLog.action.like(a.action.replace("*", "%")))
    entries = query.order_by(AuditLog.id.desc()).limit(a.limit).all()
    return [{"id": e.id, "at": isoformat_utc(e.at), "user": e.username, "action": e.action,
             "target_type": e.target_type, "target_id": e.target_id, "details": e.details} for e in entries]


# --- Commands: members ------------------------------------------------------------

def cmd_members_list(ctx, a):
    ctx.load_project()
    members = ProjectMember.query.filter_by(project_id=ctx.project.id).all()
    return sorted(({"username": m.user.username, "role": m.role, "user_id": m.user_id} for m in members),
                  key=lambda m: m["username"].lower())


def owner_count(project_id):
    return ProjectMember.query.filter_by(project_id=project_id, role="owner").count()


def cmd_member_set(ctx, a):
    project = ctx.load_project("owner")
    user = User.query.filter_by(username=a.username).first()
    if not user:
        raise CliError(f"User '{a.username}' not found.")
    member = ProjectMember.query.filter_by(project_id=project.id, user_id=user.id).first()
    if member is None:
        db.session.add(ProjectMember(project_id=project.id, user_id=user.id, role=a.role))
        ctx.audit("member.added", "user", user.id, member=user.username, role=a.role)
    elif member.role != a.role:
        if member.role == "owner" and owner_count(project.id) <= 1:
            raise CliError("A project needs at least one owner.")
        ctx.audit("member.role_changed", "user", user.id, member=user.username, old=member.role, new=a.role)
        member.role = a.role
    return {"username": user.username, "role": a.role}


def cmd_member_remove(ctx, a):
    project = ctx.load_project("owner")
    user = User.query.filter_by(username=a.username).first()
    member = ProjectMember.query.filter_by(project_id=project.id, user_id=user.id).first() if user else None
    if not member:
        raise CliError(f"'{a.username}' is not a member of this project.")
    if member.role == "owner" and owner_count(project.id) <= 1:
        raise CliError("A project needs at least one owner.")
    db.session.delete(member)
    ctx.audit("member.removed", "user", user.id, member=user.username)
    ctx.emit("project:access_revoked", {"user_id": user.id})
    return {"removed": user.username}


# --- Commands: pages -----------------------------------------------------------------

def tree_order(nodes):
    """Nodes depth-first in vault order, as (node, depth). Pages whose parent is gone are treated as top level."""
    ids = {n.id for n in nodes}
    children = {}
    for node in nodes:
        parent = node.parent_id if node.parent_id in ids else None
        children.setdefault(parent, []).append(node)
    ordered = []

    def walk(parent, depth, seen):
        for node in sorted(children.get(parent, []), key=sibling_key):
            if node.id in seen:
                continue
            ordered.append((node, depth))
            walk(node.id, depth + 1, seen | {node.id})

    walk(None, 0, frozenset())
    return ordered


def cmd_pages_list(ctx, a):
    ctx.load_project()
    if a.deleted:
        nodes = GraphNode.query.filter_by(project_id=ctx.project.id).filter(GraphNode.deleted_at.isnot(None)).all()
        return [{"id": n.id, "title": n.title, "parent_id": n.parent_id,
                 "deleted_at": isoformat_utc(n.deleted_at), "notes_chars": len(n.notes or "")}
                for n in sorted(nodes, key=lambda n: n.deleted_at, reverse=True)]
    ordered = tree_order(live_pages(ctx.project.id))
    if a.raw:
        return "\n".join(f"{'  ' * depth}- [{n.id}] {n.title}" for n, depth in ordered)
    return [{"id": n.id, "title": n.title, "depth": depth, "parent_id": n.parent_id, "path": page_path(n),
             "caption": n.caption or "", "order_index": n.order_index, "in_graph": n.in_graph,
             "notes_chars": len(n.notes or ""), "attachments": len(load_json_list(n.files_json)),
             "updated_at": isoformat_utc(n.updated_at)} for n, depth in ordered]


def page_links(ctx, node):
    live = {n.id: n for n in live_pages(ctx.project.id)}
    edges = GraphEdge.query.filter_by(project_id=ctx.project.id).filter(
        (GraphEdge.source_id == node.id) | (GraphEdge.target_id == node.id)).all()
    out = []
    for edge in edges:
        if edge.source_id in live and edge.target_id in live:
            other = edge.target_id if edge.source_id == node.id else edge.source_id
            out.append({"link_id": edge.id, "direction": "out" if edge.source_id == node.id else "in",
                        "page_id": other, "title": live[other].title})
    return out


def cmd_page_get(ctx, a):
    ctx.load_project()
    node = resolve_page(ctx, a.page, include_deleted=a.include_deleted)
    if a.section:
        text = get_section(node.notes, a.section)
        if text is None:
            raise CliError(f"Section '{a.section}' not found.")
        return text if a.raw else {"page_id": node.id, "section": a.section, "text": text}
    if a.raw:
        return node.notes or ""
    data = node_out(node, ctx)
    data["outline"] = outline(node.notes)
    data["children"] = [{"id": c.id, "title": c.title} for c in sorted(
        (c for c in node.children if c.deleted_at is None), key=sibling_key)]
    data["links"] = page_links(ctx, node)
    data["evidence_for_findings"] = [{"id": f.id, "title": f.title} for f in
                                     Finding.query.filter_by(project_id=ctx.project.id).all()
                                     if node.id in f.evidence_node_ids]
    data["revisions"] = NodeRevision.query.filter_by(node_id=node.id).count()
    return data


def cmd_page_create(ctx, a):
    project = ctx.load_project("editor")
    parent_id = check_parent(ctx, None, a.parent)
    notes = read_text(a.notes, a.notes_file)
    node = GraphNode(
        project_id=project.id,
        title=(a.title or "").strip()[:120] or "Untitled page",
        parent_id=parent_id,
        caption=(a.caption or "").strip()[:280],
        notes=notes or "",
        color=(a.color or "").strip()[:20],
        in_graph=bool(a.in_graph),
        x=a.x if a.x is not None else 120,
        y=a.y if a.y is not None else 120,
        user_id=ctx.user.id,
    )
    db.session.add(node)
    db.session.flush()
    place_page(ctx, node, parent_id, before=a.before, after=a.after)
    if node.notes or node.caption:
        record_revision(node, ctx.user, before=(node.title, "", ""))
    ctx.audit("page.created", "page", node.id, title=node.title)
    db.session.flush()
    ctx.emit("node:created", node.to_dict())
    return node_out(node, ctx, include_notes=False)


def apply_text_edits(node, a):
    """Apply the notes-editing options of `page update` / `page edit`. Returns True if any were given."""
    text = node.notes or ""
    touched = False
    new = read_text(a.notes, a.notes_file)
    if new is not None:
        text, touched = new, True
    for old, replacement in a.replace or []:
        count = text.count(old)
        if count == 0:
            raise CliError(f"Text to replace not found: {old[:80]!r}")
        if count > 1 and not a.replace_all:
            raise CliError(f"Text to replace occurs {count} times; make it unique or pass --replace-all: {old[:80]!r}")
        text = text.replace(old, replacement)
        touched = True
    if a.section:
        body = read_text(a.section_body, a.section_file)
        if a.delete_section:
            text = delete_section(text, a.section)
        else:
            if body is None:
                raise CliError("--section needs --section-body, --section-file or --delete-section.")
            text = set_section(text, a.section, body, mode=a.section_mode, create=not a.no_create)
        touched = True
    prepend = read_text(a.prepend, a.prepend_file)
    if prepend is not None:
        text = prepend.rstrip("\n") + ("\n\n" + text.lstrip("\n") if text.strip() else "\n")
        touched = True
    append = read_text(a.append, a.append_file)
    if append is not None:
        text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + append.strip("\n") + "\n"
        touched = True
    if touched:
        node.notes = text
    return touched


def cmd_page_update(ctx, a):
    ctx.load_project("editor")
    node = resolve_page(ctx, a.page)
    if a.expect_updated_at and isoformat_utc(node.updated_at) != a.expect_updated_at:
        raise CliError(f"Page changed since you read it (updated_at is now {isoformat_utc(node.updated_at)}). "
                       "Re-read it and retry.")
    before = (node.title, node.caption, node.notes)
    if a.title is not None:
        title = a.title.strip()[:120]
        if not title:
            raise CliError("A page title can't be empty.")
        node.title = title
    if a.caption is not None:
        node.caption = a.caption.strip()[:280]
    if a.color is not None:
        node.color = a.color.strip()[:20]
    if a.in_graph is not None:
        node.in_graph = on_off(a.in_graph)
    if a.x is not None:
        node.x = a.x
    if a.y is not None:
        node.y = a.y
    apply_text_edits(node, a)
    changed_text = before != (node.title, node.caption, node.notes)
    if changed_text:
        record_revision(node, ctx.user, before=before, force_new=a.new_revision)
    db.session.flush()
    emit_node_updated(ctx, node)
    result = node_out(node, ctx, include_notes=False)
    result["text_changed"] = changed_text
    return result


def cmd_page_move(ctx, a):
    ctx.load_project("editor")
    node = resolve_page(ctx, a.page)
    if a.parent is None and not (a.before or a.after):
        raise CliError("Give --parent, --before or --after.")
    if a.parent is not None:
        parent_id = check_parent(ctx, node.id, a.parent)
    else:
        anchor = resolve_page(ctx, a.before or a.after)
        parent_id = anchor.parent_id
    node.parent_id = parent_id
    place_page(ctx, node, parent_id, before=a.before, after=a.after)
    db.session.flush()
    emit_node_updated(ctx, node)
    return {"id": node.id, "parent_id": node.parent_id, "order_index": node.order_index, "path": page_path(node)}


def cmd_page_delete(ctx, a):
    project = ctx.load_project("editor")
    node = resolve_page(ctx, a.page)
    moved = []
    for child in node.children:
        if child.deleted_at is None:
            child.parent_id = node.parent_id
            moved.append(child.id)
    node.deleted_at = utcnow()
    ctx.audit("page.deleted", "page", node.id, title=node.title, children=moved)
    ctx.emit("node:deleted", {"id": node.id})
    db.session.flush()
    created = None
    if GraphNode.query.filter_by(project_id=project.id, deleted_at=None).count() == 0:
        created = GraphNode(project_id=project.id, title="Untitled page", in_graph=False, user_id=ctx.user.id)
        db.session.add(created)
        db.session.flush()
        ctx.emit("node:created", created.to_dict())
    return {"deleted": node.id, "title": node.title, "children_moved_up": moved,
            "placeholder_created": created.id if created else None,
            "note": "Soft delete: restore with `page restore`. Purged after the retention period."}


def cmd_page_restore(ctx, a):
    project = ctx.load_project("editor")
    node = resolve_page(ctx, a.page, deleted_only=True)
    node.deleted_at = None
    if node.parent_id:
        parent = db.session.get(GraphNode, node.parent_id)
        if not parent or parent.deleted_at is not None or parent.project_id != project.id:
            node.parent_id = None
    # Move back the children the delete moved up, unless they have been rearranged since
    entry = (AuditLog.query.filter_by(project_id=project.id, action="page.deleted", target_type="page",
                                      target_id=str(node.id)).order_by(AuditLog.id.desc()).first())
    restored = []
    for child_id in (entry.details.get("children") if entry else None) or []:
        child = db.session.get(GraphNode, child_id)
        if child and child.project_id == project.id and child.deleted_at is None and child.parent_id == node.parent_id:
            child.parent_id = node.id
            restored.append(child.id)
    ctx.audit("page.restored", "page", node.id, title=node.title)
    db.session.flush()
    ctx.emit("node:restored", {"node": node.to_dict(),
                               "children": [db.session.get(GraphNode, c).to_dict() for c in restored],
                               "edges": [e.to_dict() for e in GraphEdge.query.filter_by(project_id=project.id).filter(
                                   (GraphEdge.source_id == node.id) | (GraphEdge.target_id == node.id))]})
    return {"restored": node.id, "title": node.title, "children_restored": restored}


def cmd_page_history(ctx, a):
    ctx.load_project()
    node = resolve_page(ctx, a.page, include_deleted=True)
    revisions = NodeRevision.query.filter_by(node_id=node.id).order_by(NodeRevision.id.desc()).limit(a.limit).all()
    return {"page_id": node.id, "title": node.title, "revisions": [
        {"id": r.id, "created_at": isoformat_utc(r.created_at), "updated_at": isoformat_utc(r.updated_at),
         "user": r.user.username if r.user else None, "title": r.title, "notes_chars": len(r.notes or "")}
        for r in revisions]}


def page_revision(node, revision_id):
    revision = db.session.get(NodeRevision, revision_id)
    if not revision or revision.node_id != node.id:
        raise CliError(f"Revision {revision_id} not found for page {node.id}.")
    return revision


def cmd_page_revision(ctx, a):
    ctx.load_project()
    node = resolve_page(ctx, a.page, include_deleted=True)
    revision = page_revision(node, a.revision)
    if a.raw:
        return revision.notes or ""
    return {"id": revision.id, "page_id": node.id, "title": revision.title, "caption": revision.caption or "",
            "notes": revision.notes or "", "user": revision.user.username if revision.user else None,
            "created_at": isoformat_utc(revision.created_at), "updated_at": isoformat_utc(revision.updated_at)}


def cmd_page_diff(ctx, a):
    ctx.load_project()
    node = resolve_page(ctx, a.page, include_deleted=True)
    old = page_revision(node, a.revision)
    if a.to:
        new = page_revision(node, a.to)
        new_label, new_text = f"revision {new.id}", new.notes or ""
    else:
        new_label, new_text = "current", node.notes or ""
    diff = "".join(difflib.unified_diff((old.notes or "").splitlines(keepends=True), new_text.splitlines(keepends=True),
                                        fromfile=f"revision {old.id}", tofile=new_label))
    return diff if a.raw else {"from": old.id, "to": a.to or "current", "diff": diff}


def cmd_page_revert(ctx, a):
    ctx.load_project("editor")
    node = resolve_page(ctx, a.page)
    revision = page_revision(node, a.revision)
    before = (node.title, node.caption, node.notes)
    node.title, node.caption, node.notes = revision.title, revision.caption, revision.notes
    record_revision(node, ctx.user, before=before, force_new=True)
    ctx.audit("page.restored_revision", "page", node.id, revision=revision.id)
    db.session.flush()
    emit_node_updated(ctx, node)
    return node_out(node, ctx, include_notes=False)


def resolve_upload(ctx, ref):
    """An Upload by stored filename, URL, or original name."""
    ref = str(ref).strip()
    name = ref.rsplit("/", 1)[-1]
    uploads = Upload.query.filter_by(project_id=ctx.project.id)
    upload = uploads.filter_by(filename=name).first()
    if upload:
        return upload
    matches = uploads.filter_by(original_name=ref).all()
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise CliError(f"Several files are named '{ref}'; use the stored filename: "
                       + ", ".join(u.filename for u in matches))
    raise CliError(f"No file '{ref}' in this project.")


def cmd_page_attach(ctx, a):
    ctx.load_project("editor")
    node = resolve_page(ctx, a.page)
    files = load_json_list(node.files_json)
    if a.url:
        entry = {"name": a.name or a.file, "url": a.file}
    else:
        upload = resolve_upload(ctx, a.file)
        entry = {"name": a.name or upload.original_name, "url": upload_url(ctx.project.id, upload.filename)}
    if any(f.get("url") == entry["url"] for f in files):
        raise CliError("That file is already attached to this page.")
    files.append(entry)
    node.files_json = clean_files(files)
    db.session.flush()
    emit_node_updated(ctx, node)
    return {"page_id": node.id, "files": load_json_list(node.files_json)}


def cmd_page_detach(ctx, a):
    ctx.load_project("editor")
    node = resolve_page(ctx, a.page)
    files = load_json_list(node.files_json)
    key = a.file.rsplit("/", 1)[-1]
    kept = [f for f in files if f.get("url", "").rsplit("/", 1)[-1] != key and f.get("name") != a.file]
    if len(kept) == len(files):
        raise CliError("That file is not attached to this page.")
    node.files_json = clean_files(kept)
    db.session.flush()
    emit_node_updated(ctx, node)
    return {"page_id": node.id, "files": kept}


# --- Commands: links -------------------------------------------------------------------

def cmd_links_list(ctx, a):
    ctx.load_project()
    live = {n.id: n for n in live_pages(ctx.project.id)}
    edges = GraphEdge.query.filter_by(project_id=ctx.project.id).order_by(GraphEdge.id).all()
    page_id = resolve_page(ctx, a.page).id if a.page else None
    return [{"id": e.id, "source_id": e.source_id, "source": live[e.source_id].title,
             "target_id": e.target_id, "target": live[e.target_id].title}
            for e in edges if e.source_id in live and e.target_id in live
            and (page_id is None or page_id in (e.source_id, e.target_id))]


def cmd_link_add(ctx, a):
    project = ctx.load_project("editor")
    source, target = resolve_page(ctx, a.source), resolve_page(ctx, a.target)
    if source.id == target.id:
        raise CliError("Choose two different pages.")
    if GraphEdge.query.filter_by(source_id=source.id, target_id=target.id).first():
        raise CliError("Those pages are already linked.")
    edge = GraphEdge(project_id=project.id, source_id=source.id, target_id=target.id, user_id=ctx.user.id)
    db.session.add(edge)
    # Links are drawn on the graph, so both ends need to be on it
    for node in (source, target):
        if a.show_on_graph and not node.in_graph:
            node.in_graph = True
            emit_node_updated(ctx, node)
    db.session.flush()
    ctx.emit("edge:created", edge.to_dict())
    return {**edge.to_dict(), "source": source.title, "target": target.title}


def cmd_link_remove(ctx, a):
    project = ctx.load_project("editor")
    if a.target is None:
        if not str(a.source).isdigit():
            raise CliError("Pass a link ID, or SOURCE TARGET pages.")
        edge = db.session.get(GraphEdge, int(a.source))
    else:
        source, target = resolve_page(ctx, a.source), resolve_page(ctx, a.target)
        edge = GraphEdge.query.filter_by(source_id=source.id, target_id=target.id).first() or \
            GraphEdge.query.filter_by(source_id=target.id, target_id=source.id).first()
    if not edge or edge.project_id != project.id:
        raise CliError("Link not found.")
    edge_id = edge.id
    db.session.delete(edge)
    ctx.emit("edge:deleted", {"id": edge_id})
    return {"deleted": edge_id}


# --- Commands: timeline -------------------------------------------------------------------

def timeline_files(ctx, refs):
    files = []
    for ref in refs or []:
        upload = resolve_upload(ctx, ref)
        files.append({"name": upload.original_name, "url": upload_url(ctx.project.id, upload.filename)})
    return files


def project_event(ctx, event_id):
    event = db.session.get(TimelineEvent, event_id)
    if not event or event.project_id != ctx.project.id:
        raise CliError(f"Timeline event {event_id} not found.")
    return event


def cmd_timeline_list(ctx, a):
    ctx.load_project()
    query = TimelineEvent.query.filter_by(project_id=ctx.project.id)
    events = query.order_by(TimelineEvent.occurred_at if a.chronological else TimelineEvent.order_index).all()
    since, until = parse_time(a.since, "--since"), parse_time(a.until, "--until")
    events = [e for e in events if (not since or as_utc(e.occurred_at) >= since)
              and (not until or as_utc(e.occurred_at) <= until)]
    return [e.to_dict() for e in events]


def cmd_timeline_add(ctx, a):
    project = ctx.load_project("editor")
    title = (a.title or "").strip()
    if not title:
        raise CliError("Timeline title is required.")
    occurred_at = parse_time(a.start or "now", "--start")
    ended_at = parse_time(a.end, "--end")
    if ended_at and ended_at < occurred_at:
        raise CliError("The end must be after the start.")
    event = TimelineEvent(project_id=project.id, title=title[:160],
                          body=(read_text(a.body, a.body_file) or "").strip(),
                          files_json=clean_files(timeline_files(ctx, a.file)),
                          occurred_at=occurred_at, ended_at=ended_at, user_id=ctx.user.id)
    event.order_index = automatic_timeline_order(occurred_at, project.id)
    db.session.add(event)
    db.session.flush()
    ctx.audit("timeline.created", "timeline_event", event.id, title=event.title)
    ctx.emit("timeline:created", event.to_dict())
    return event.to_dict()


def cmd_timeline_update(ctx, a):
    project = ctx.load_project("editor")
    event = project_event(ctx, a.id)
    if a.title is not None:
        if not a.title.strip():
            raise CliError("Timeline title is required.")
        event.title = a.title.strip()[:160]
    body = read_text(a.body, a.body_file)
    if body is not None:
        event.body = body.strip()
    if a.append_body is not None:
        event.body = ((event.body or "").rstrip() + "\n\n" + a.append_body.strip()).strip()
    if a.start is not None:
        event.occurred_at = parse_time(a.start, "--start")
        if not event.manual_order:
            event.order_index = automatic_timeline_order(event.occurred_at, project.id, exclude_id=event.id)
    if a.end is not None:
        event.ended_at = parse_time(a.end, "--end")
    if a.clear_end:
        event.ended_at = None
    if a.file is not None:
        event.files_json = clean_files(load_json_list(event.files_json) + timeline_files(ctx, a.file))
    if a.clear_files:
        event.files_json = "[]"
    if event.ended_at and as_utc(event.ended_at) < as_utc(event.occurred_at):
        raise CliError("The end must be after the start.")
    db.session.flush()
    ctx.emit("timeline:updated", {**event.to_dict(), "origin": None, "edited_by": ctx.user.username})
    return event.to_dict()


def cmd_timeline_delete(ctx, a):
    ctx.load_project("editor")
    event = project_event(ctx, a.id)
    title = event.title
    db.session.delete(event)
    ctx.audit("timeline.deleted", "timeline_event", a.id, title=title)
    ctx.emit("timeline:deleted", {"id": a.id})
    return {"deleted": a.id, "title": title}


def cmd_timeline_move(ctx, a):
    ctx.load_project("editor")
    event = project_event(ctx, a.id)
    anchor = project_event(ctx, a.before or a.after)
    if anchor.id == event.id:
        raise CliError("An event can't be placed relative to itself.")
    others = sorted((e for e in TimelineEvent.query.filter_by(project_id=ctx.project.id) if e.id != event.id),
                    key=lambda e: (e.order_index, e.id))
    event.order_index = order_between(others, anchor, before=bool(a.before))
    event.manual_order = True
    db.session.flush()
    ctx.emit("timeline:updated", {**event.to_dict(), "origin": None, "edited_by": ctx.user.username})
    return event.to_dict()


def cmd_timeline_autosort(ctx, a):
    ctx.load_project("editor")
    events = reset_timeline_auto_order(ctx.project.id)
    db.session.flush()
    ctx.emit("timeline:reset", {"events": [e.to_dict() for e in events]})
    return {"events": len(events)}


# --- Commands: findings ---------------------------------------------------------------------

def require_feature(ctx, flag, label):
    if not getattr(ctx.project, flag):
        raise CliError(f"{label} is turned off for this project. An owner can run: project feature "
                       f"{label.lower()} on")


def finding_out(finding, full=True):
    data = {"id": finding.id, "title": finding.title, "severity": finding.severity,
            "cvss_score": finding.cvss_score, "cvss_vector": finding.cvss_vector, "status": finding.status,
            "evidence_page_ids": finding.evidence_node_ids,
            "author": finding.user.username if finding.user else "",
            "created_at": isoformat_utc(finding.created_at), "updated_at": isoformat_utc(finding.updated_at)}
    if full:
        for key in FINDING_TEXT_FIELDS:
            data[key] = getattr(finding, key) or ""
    return data


def project_finding(ctx, finding_id):
    finding = db.session.get(Finding, finding_id)
    if not finding or finding.project_id != ctx.project.id:
        raise CliError(f"Finding {finding_id} not found.")
    return finding


def evidence_ids(ctx, refs):
    ids = []
    for ref in refs or []:
        node_id = resolve_page(ctx, ref).id
        if node_id not in ids:
            ids.append(node_id)
    return ids


def finding_input(a, existing=None):
    data = finding_out(existing) if existing else {}
    if existing:
        data["cvss_score"] = existing.cvss_score if existing.cvss_score is not None else ""
    for key, attr in (("title", "title"), ("severity", "severity"), ("status", "status"),
                      ("cvss_vector", "cvss_vector"), ("cvss_score", "cvss_score")):
        value = getattr(a, attr)
        if value is not None:
            data[key] = value
    if (a.severity or "").lower() == "auto":
        data["severity"] = ""
    for key in FINDING_TEXT_FIELDS:
        value = read_text(getattr(a, key), getattr(a, f"{key}_file"))
        if value is not None:
            data[key] = value
    if data.get("severity") and str(data["severity"]).lower() not in SEVERITIES:
        raise CliError(f"Severity must be one of {', '.join(SEVERITIES)} or auto.")
    if data.get("status") and data["status"] not in FINDING_STATUSES:
        raise CliError(f"Status must be one of {', '.join(FINDING_STATUSES)}.")
    if data.get("cvss_vector") and cvss31_score(data["cvss_vector"]) is None:
        raise CliError("Invalid CVSS 3.x vector (expected e.g. CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H).")
    return clean_finding_fields(data)


def cmd_findings_list(ctx, a):
    ctx.load_project()
    require_feature(ctx, "findings_enabled", "Findings")
    findings = sorted_findings(ctx.project.id)
    if a.severity:
        findings = [f for f in findings if f.severity == a.severity]
    if a.status:
        findings = [f for f in findings if f.status == a.status]
    return [finding_out(f, full=a.full) for f in findings]


def cmd_finding_get(ctx, a):
    ctx.load_project()
    require_feature(ctx, "findings_enabled", "Findings")
    finding = project_finding(ctx, a.id)
    data = finding_out(finding)
    data["evidence"] = []
    for node_id in finding.evidence_node_ids:
        node = db.session.get(GraphNode, node_id)
        if node and node.deleted_at is None:
            data["evidence"].append({"id": node.id, "title": node.title})
    return data


def cmd_finding_create(ctx, a):
    project = ctx.load_project("editor")
    require_feature(ctx, "findings_enabled", "Findings")
    fields = finding_input(a)
    finding = Finding(project_id=project.id, user_id=ctx.user.id,
                      evidence_node_ids_json=json.dumps(evidence_ids(ctx, a.evidence)), **fields)
    db.session.add(finding)
    db.session.flush()
    ctx.audit("finding.created", "finding", finding.id, title=finding.title, severity=finding.severity)
    return finding_out(finding)


def cmd_finding_update(ctx, a):
    ctx.load_project("editor")
    require_feature(ctx, "findings_enabled", "Findings")
    finding = project_finding(ctx, a.id)
    fields = finding_input(a, existing=finding)
    changed = sorted(key for key, value in fields.items() if getattr(finding, key) != value)
    for key, value in fields.items():
        setattr(finding, key, value)
    ids = finding.evidence_node_ids
    if a.evidence is not None:
        ids = evidence_ids(ctx, a.evidence)
    ids = ids + [i for i in evidence_ids(ctx, a.add_evidence) if i not in ids]
    drop = set(evidence_ids(ctx, a.remove_evidence))
    ids = [i for i in ids if i not in drop]
    if ids != finding.evidence_node_ids:
        finding.evidence_node_ids_json = json.dumps(ids)
        changed.append("evidence")
    if changed:
        ctx.audit("finding.updated", "finding", finding.id, title=finding.title, fields=changed)
    db.session.flush()
    return {**finding_out(finding), "changed": changed}


def cmd_finding_delete(ctx, a):
    ctx.load_project("editor")
    require_feature(ctx, "findings_enabled", "Findings")
    finding = project_finding(ctx, a.id)
    title = finding.title
    db.session.delete(finding)
    ctx.audit("finding.deleted", "finding", a.id, title=title)
    return {"deleted": a.id, "title": title}


def cmd_cvss(ctx, a):
    score = cvss31_score(a.vector)
    if score is None:
        raise CliError("Invalid CVSS 3.x vector.")
    return {"vector": a.vector, "score": score, "severity": severity_for_score(score)}


# --- Commands: report ---------------------------------------------------------------------

def cmd_report_get(ctx, a):
    ctx.load_project()
    require_feature(ctx, "report_enabled", "Report")
    report = ctx.project.report
    return {**{key: report.get(key, "") for key in REPORT_FIELDS},
            **{flag: bool(report.get(flag)) for flag in REPORT_FLAGS}}


def cmd_report_set(ctx, a):
    project = ctx.load_project("editor")
    require_feature(ctx, "report_enabled", "Report")
    report = project.report
    updates = {}
    for item in a.set or []:
        key, sep, value = item.partition("=")
        if not sep:
            raise CliError(f"--set expects KEY=VALUE, got '{item}'.")
        updates[key.strip()] = value
    for key, path in a.set_file or []:
        updates[key.strip()] = read_text(file=path)
    for key in updates:
        if key not in REPORT_FIELDS:
            raise CliError(f"Unknown report field '{key}'. Fields: {', '.join(REPORT_FIELDS)}.")
    for key, value in updates.items():
        report[key] = value.strip()
    for flag in REPORT_FLAGS:
        value = on_off(getattr(a, flag))
        if value is not None:
            report[flag] = value
    for key in REPORT_FIELDS:
        report.setdefault(key, "")
    project.report_json = json.dumps(report)
    ctx.audit("report.updated", "project", project.id, fields=sorted(updates) or None)
    return cmd_report_get(ctx, a)


def report_context(ctx):
    project = ctx.project
    settings = project.report
    findings = sorted_findings(project.id, include_drafts=bool(settings.get("include_drafts"))) \
        if project.findings_enabled else []
    pages = {n.id: n for n in live_pages(project.id)}
    events = TimelineEvent.query.filter_by(project_id=project.id).order_by(TimelineEvent.occurred_at).all() \
        if settings.get("include_timeline") else []
    return {"findings_enabled": project.findings_enabled, "settings": settings,
            "numbered": [(f"F-{i:02d}", f) for i, f in enumerate(findings, start=1)], "pages": pages,
            "events": events, "counts": {lvl: sum(1 for f in findings if f.severity == lvl) for lvl in SEVERITIES},
            "generated_at": utcnow(), "generated_by": ctx.user.username}


def cmd_report_export(ctx, a):
    from app.reports import build_docx, build_markdown

    ctx.load_project()
    require_feature(ctx, "report_enabled", "Report")
    context = report_context(ctx)
    fmt = a.format
    if fmt == "docx" and not a.out:
        raise CliError("--out is required for docx.")
    if fmt == "md":
        text = build_markdown(ctx.project, context)
        if not a.out:
            ctx.audit("report.exported", "project", ctx.project.id, format="markdown")
            return text if a.raw else {"markdown": text}
        Path(a.out).write_text(text, encoding="utf-8")
    else:
        Path(a.out).write_bytes(build_docx(ctx.project, context).getvalue())
    ctx.audit("report.exported", "project", ctx.project.id, format="markdown" if fmt == "md" else "docx")
    return {"file": str(Path(a.out).resolve()), "bytes": os.path.getsize(a.out), "findings": len(context["numbered"])}


# --- Commands: files ------------------------------------------------------------------------

def upload_out(upload):
    return {"filename": upload.filename, "name": upload.original_name, "size": upload.size, "sha256": upload.sha256,
            "url": upload_url(upload.project_id, upload.filename),
            "is_image": os.path.splitext(upload.filename)[1].lower() in IMAGE_EXTENSIONS,
            "uploaded_by": upload.user.username if upload.user else "",
            "created_at": isoformat_utc(upload.created_at)}


def cmd_files_list(ctx, a):
    ctx.load_project()
    uploads = Upload.query.filter_by(project_id=ctx.project.id).order_by(Upload.created_at.desc()).all()
    items = [upload_out(u) for u in uploads]
    if a.images:
        items = [i for i in items if i["is_image"]]
    if a.documents:
        items = [i for i in items if not i["is_image"]]
    return items


def cmd_file_upload(ctx, a):
    from app.settings import get_setting

    project = ctx.load_project("editor")
    source = Path(a.path)
    if not source.is_file():
        raise CliError(f"File not found: {a.path}")
    limit = get_setting("max_upload_mb")
    if limit and source.stat().st_size > limit * 1024 * 1024:
        raise CliError(f"File is too large (limit {limit} MB).")
    display_name = (a.name or source.name)[:255]
    original = secure_filename(display_name) or "file"
    stem, ext = os.path.splitext(original)
    filename = f"{stem[:80]}_{uuid.uuid4().hex}{ext.lower()[:16]}"
    target = os.path.join(project_upload_dir(project.id), filename)
    with open(source, "rb") as src, open(target, "wb") as dst:
        while chunk := src.read(1024 * 1024):
            dst.write(chunk)
    ctx.on_rollback.append(lambda: os.path.exists(target) and os.remove(target))
    upload = Upload(project_id=project.id, filename=filename, original_name=display_name,
                    size=os.path.getsize(target), sha256=file_sha256(target), user_id=ctx.user.id)
    db.session.add(upload)
    ctx.audit("upload.created", "upload", filename, name=display_name, size=upload.size, sha256=upload.sha256)
    db.session.flush()
    result = upload_out(upload)
    markdown = f"![{display_name}]({result['url']})" if result["is_image"] else f"[{display_name}]({result['url']})"
    result["markdown"] = markdown
    if a.attach:
        node = resolve_page(ctx, a.attach)
        node.files_json = clean_files(load_json_list(node.files_json) + [{"name": display_name, "url": result["url"]}])
        emit_node_updated(ctx, node)
        result["attached_to"] = node.id
    if a.embed:
        node = resolve_page(ctx, a.embed)
        before = (node.title, node.caption, node.notes)
        node.notes = ((node.notes or "").rstrip("\n") + "\n\n" if (node.notes or "").strip() else "") + markdown + "\n"
        record_revision(node, ctx.user, before=before)
        emit_node_updated(ctx, node)
        result["embedded_in"] = node.id
    return result


def cmd_file_download(ctx, a):
    ctx.load_project()
    upload = resolve_upload(ctx, a.file)
    path = os.path.join(project_upload_dir(ctx.project.id, create=False), upload.filename)
    if not os.path.isfile(path):
        raise CliError("The file is registered but missing on disk.")
    out = Path(a.out)
    if out.is_dir():
        out = out / (secure_filename(upload.original_name) or upload.filename)
    with open(path, "rb") as src, open(out, "wb") as dst:
        while chunk := src.read(1024 * 1024):
            dst.write(chunk)
    return {"file": str(out.resolve()), "bytes": out.stat().st_size}


def cmd_file_delete(ctx, a):
    ctx.load_project("editor")
    upload = resolve_upload(ctx, a.file)
    path = os.path.join(project_upload_dir(ctx.project.id, create=False), upload.filename)
    ctx.on_commit.append(lambda: os.path.isfile(path) and os.remove(path))
    db.session.delete(upload)
    ctx.audit("upload.deleted", "upload", upload.filename, name=upload.original_name)
    return {"deleted": upload.filename, "name": upload.original_name,
            "note": "Links to this file in pages or findings now point at nothing."}


# --- Commands: search ----------------------------------------------------------------------

def snippets(text, pattern, limit=5):
    hits = []
    for number, line in enumerate((text or "").split("\n"), start=1):
        if pattern.search(line):
            hits.append({"line": number, "text": line.strip()[:300]})
            if len(hits) >= limit:
                break
    return hits


def cmd_search(ctx, a):
    ctx.load_project()
    try:
        pattern = re.compile(a.query if a.regex else re.escape(a.query), 0 if a.case_sensitive else re.IGNORECASE)
    except re.error as exc:
        raise CliError(f"Invalid regex: {exc}") from exc
    scopes = set(a.scope.split(",")) if a.scope else {"pages", "timeline", "findings", "files"}
    results = []
    pid = ctx.project.id
    if "pages" in scopes:
        for node in live_pages(pid):
            fields = {"title": node.title, "caption": node.caption, "notes": node.notes}
            hit = {k: snippets(v, pattern) for k, v in fields.items() if v and pattern.search(v)}
            if hit:
                results.append({"type": "page", "id": node.id, "title": node.title, "path": page_path(node), "hits": hit})
    if "timeline" in scopes:
        for event in TimelineEvent.query.filter_by(project_id=pid):
            hit = {k: snippets(v, pattern) for k, v in (("title", event.title), ("body", event.body))
                   if v and pattern.search(v)}
            if hit:
                results.append({"type": "timeline", "id": event.id, "title": event.title, "hits": hit})
    if "findings" in scopes and ctx.project.findings_enabled:
        for finding in Finding.query.filter_by(project_id=pid):
            fields = {"title": finding.title, "cvss_vector": finding.cvss_vector,
                      **{k: getattr(finding, k) for k in FINDING_TEXT_FIELDS}}
            hit = {k: snippets(v, pattern) for k, v in fields.items() if v and pattern.search(v)}
            if hit:
                results.append({"type": "finding", "id": finding.id, "title": finding.title, "hits": hit})
    if "files" in scopes:
        for upload in Upload.query.filter_by(project_id=pid):
            if pattern.search(upload.original_name or "") or pattern.search(upload.filename):
                results.append({"type": "file", "filename": upload.filename, "name": upload.original_name})
    return results[:a.limit]


# --- Parser ----------------------------------------------------------------------------------

def build_parser():
    parser = JsonArgumentParser(prog="redobsidian_agent.py", description=__doc__.split("\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", help="Username to act as (or REDOBSIDIAN_AGENT_USER)")
    parser.add_argument("--project", help="Project name or ID (or REDOBSIDIAN_AGENT_PROJECT)")
    parser.add_argument("--dry-run", action="store_true", help="Do everything, then roll back instead of saving")
    parser.add_argument("--compact", action="store_true", help="Single-line JSON output")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def add(name, func, help_text, parent=sub):
        p = parent.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(func=func)
        return p

    def group(name, help_text):
        p = sub.add_parser(name, help=help_text, description=help_text)
        return p.add_subparsers(dest="subcommand", metavar="ACTION", required=True)

    def text_opt(p, name, help_text):
        p.add_argument(f"--{name.replace('_', '-')}", dest=name, help=help_text)
        p.add_argument(f"--{name.replace('_', '-')}-file", dest=f"{name}_file", metavar="PATH",
                       help=f"Read {name.replace('_', ' ')} from a file ('-' = stdin)")

    add("help-agent", None, "Print a compact command reference for agents")
    add("whoami", cmd_whoami, "Show the acting user")
    p = add("projects", cmd_projects, "List projects the user can access (filtered by the allowlist)")
    p.add_argument("--archived", action="store_true", help="Include archived projects")

    # project
    g = group("project", "Project details, features, archive, export, full dump")
    add("info", cmd_project_info, "Project details, your role, feature flags and counts", g)
    p = add("update", cmd_project_update, "Rename or change the description (owner)", g)
    p.add_argument("--name")
    text_opt(p, "description", "New description")
    p = add("feature", cmd_project_feature, "Turn the Findings or Report beta on/off (owner)", g)
    p.add_argument("feature", choices=["findings", "report"])
    p.add_argument("state", choices=["on", "off"])
    add("archive", lambda c, a: cmd_project_archive(c, a, True), "Archive (read-only) the project (owner)", g)
    add("unarchive", lambda c, a: cmd_project_archive(c, a, False), "Unarchive the project (owner)", g)
    p = add("export", cmd_project_export, "Encrypted ZIP export; password from REDOBSIDIAN_EXPORT_PASSWORD (owner)", g)
    p.add_argument("--out", required=True)
    p = add("dump", cmd_project_dump, "Everything (pages with text, links, timeline, findings, files) as JSON", g)
    p.add_argument("--out", help="Write to a file instead of stdout")
    p = add("activity", cmd_activity, "Project audit log, newest first (owner)", g)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--action", help="Filter by action, '*' wildcard (e.g. 'page.*')")

    # members
    g = group("members", "Project members")
    add("list", cmd_members_list, "List members and roles", g)
    p = add("set", cmd_member_set, "Add a member or change their role (owner)", g)
    p.add_argument("username")
    p.add_argument("role", choices=PROJECT_ROLES)
    p = add("remove", cmd_member_remove, "Remove a member (owner)", g)
    p.add_argument("username")

    # pages
    g = group("pages", "List vault pages")
    p = add("list", cmd_pages_list, "All pages in vault (tree) order", g)
    p.add_argument("--deleted", action="store_true", help="List deleted pages (the trash) instead")
    p.add_argument("--raw", action="store_true", help="Indented text tree")

    g = group("page", "Read and edit one page (PAGE = ID, title, or 'Parent / Child' path)")
    p = add("get", cmd_page_get, "Page with text, outline, children, links, findings", g)
    p.add_argument("page")
    p.add_argument("--raw", action="store_true", help="Print only the Markdown text")
    p.add_argument("--section", help="Only this section, by heading ('## Recon' or 'Recon')")
    p.add_argument("--include-deleted", action="store_true")

    p = add("create", cmd_page_create, "Create a page", g)
    p.add_argument("title")
    p.add_argument("--parent", help="Parent page (default: top level)")
    p.add_argument("--caption")
    text_opt(p, "notes", "Markdown body")
    p.add_argument("--color", choices=PAGE_COLORS, help="Graph colour")
    p.add_argument("--in-graph", action="store_true", help="Also show it on the Graph tab")
    p.add_argument("--x", type=float)
    p.add_argument("--y", type=float)
    pos = p.add_mutually_exclusive_group()
    pos.add_argument("--before", metavar="PAGE", help="Place before this sibling")
    pos.add_argument("--after", metavar="PAGE", help="Place after this sibling")

    for name, func, help_text in (("update", cmd_page_update, "Change a page's fields and/or text"),
                                  ("edit", cmd_page_update, "Alias of update")):
        p = add(name, func, help_text, g)
        p.add_argument("page")
        p.add_argument("--title")
        p.add_argument("--caption")
        p.add_argument("--color", choices=PAGE_COLORS + [""], help="Graph colour ('' clears)")
        p.add_argument("--in-graph", choices=["on", "off"])
        p.add_argument("--x", type=float)
        p.add_argument("--y", type=float)
        text_opt(p, "notes", "Replace the whole Markdown body")
        text_opt(p, "append", "Append to the end")
        text_opt(p, "prepend", "Insert at the start")
        p.add_argument("--replace", nargs=2, action="append", metavar=("OLD", "NEW"),
                       help="Replace exact text (must be unique unless --replace-all). Repeatable")
        p.add_argument("--replace-all", action="store_true")
        p.add_argument("--section", help="Target a section by heading ('## Recon' or 'Recon')")
        p.add_argument("--section-body", help="New body for --section")
        p.add_argument("--section-file", metavar="PATH", help="Read the section body from a file ('-' = stdin)")
        p.add_argument("--section-mode", choices=["replace", "append", "prepend"], default="replace")
        p.add_argument("--delete-section", action="store_true", help="Remove the section and its heading")
        p.add_argument("--no-create", action="store_true", help="Fail if --section doesn't exist (default: add it)")
        p.add_argument("--expect-updated-at", help="Fail if the page's updated_at differs (optimistic lock)")
        p.add_argument("--new-revision", action="store_true",
                       help="Always start a new history entry (default: folds edits within 10 min)")

    p = add("move", cmd_page_move, "Move under another parent and/or reorder among siblings", g)
    p.add_argument("page")
    p.add_argument("--parent", help="New parent page, or 'root'")
    pos = p.add_mutually_exclusive_group()
    pos.add_argument("--before", metavar="PAGE")
    pos.add_argument("--after", metavar="PAGE")
    p = add("delete", cmd_page_delete, "Move a page to the trash (children move up a level)", g)
    p.add_argument("page")
    p = add("restore", cmd_page_restore, "Restore a page from the trash", g)
    p.add_argument("page")
    p = add("history", cmd_page_history, "List the page's saved versions", g)
    p.add_argument("page")
    p.add_argument("--limit", type=int, default=50)
    p = add("revision", cmd_page_revision, "Show one saved version", g)
    p.add_argument("page")
    p.add_argument("revision", type=int)
    p.add_argument("--raw", action="store_true")
    p = add("diff", cmd_page_diff, "Unified diff of a version against the current text (or --to another)", g)
    p.add_argument("page")
    p.add_argument("revision", type=int)
    p.add_argument("--to", type=int)
    p.add_argument("--raw", action="store_true")
    p = add("revert", cmd_page_revert, "Restore the page's text to a saved version", g)
    p.add_argument("page")
    p.add_argument("revision", type=int)
    p = add("attach", cmd_page_attach, "Attach an uploaded file (or --url) to the page", g)
    p.add_argument("page")
    p.add_argument("file", help="Stored filename, original name, upload URL, or URL with --url")
    p.add_argument("--name")
    p.add_argument("--url", action="store_true", help="FILE is an arbitrary URL")
    p = add("detach", cmd_page_detach, "Remove an attachment from the page", g)
    p.add_argument("page")
    p.add_argument("file")

    # links
    g = group("links", "Graph links between pages")
    p = add("list", cmd_links_list, "List links", g)
    p.add_argument("--page", help="Only links touching this page")
    p = add("add", cmd_link_add, "Link SOURCE -> TARGET", g)
    p.add_argument("source")
    p.add_argument("target")
    p.add_argument("--show-on-graph", action="store_true", help="Also put both pages on the Graph tab")
    p = add("remove", cmd_link_remove, "Remove a link by ID, or by SOURCE TARGET", g)
    p.add_argument("source")
    p.add_argument("target", nargs="?")

    # timeline
    g = group("timeline", "Engagement timeline")
    p = add("list", cmd_timeline_list, "Events in display order (newest first unless reordered)", g)
    p.add_argument("--chronological", action="store_true", help="Oldest first by start time")
    p.add_argument("--since")
    p.add_argument("--until")
    p = add("add", cmd_timeline_add, "Add an event", g)
    p.add_argument("title")
    text_opt(p, "body", "Details (Markdown)")
    p.add_argument("--start", help="ISO 8601 start (default now; naive = UTC)")
    p.add_argument("--end", help="ISO 8601 end")
    p.add_argument("--file", action="append", help="Attach an uploaded file. Repeatable")
    p = add("update", cmd_timeline_update, "Change an event", g)
    p.add_argument("id", type=int)
    p.add_argument("--title")
    text_opt(p, "body", "Replace details")
    p.add_argument("--append-body")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--clear-end", action="store_true")
    p.add_argument("--file", action="append", help="Attach another uploaded file. Repeatable")
    p.add_argument("--clear-files", action="store_true")
    p = add("delete", cmd_timeline_delete, "Delete an event", g)
    p.add_argument("id", type=int)
    p = add("move", cmd_timeline_move, "Manually reorder an event", g)
    p.add_argument("id", type=int)
    pos = p.add_mutually_exclusive_group(required=True)
    pos.add_argument("--before", type=int, metavar="EVENT_ID")
    pos.add_argument("--after", type=int, metavar="EVENT_ID")
    add("auto-sort", cmd_timeline_autosort, "Drop manual ordering; sort all events by time", g)

    # findings
    def finding_opts(p, create):
        if create:
            p.add_argument("title")
        else:
            p.add_argument("id", type=int)
            p.add_argument("--title")
        p.add_argument("--severity", help=f"{'|'.join(SEVERITIES)}|auto (auto = from CVSS score)")
        p.add_argument("--status", choices=FINDING_STATUSES)
        p.add_argument("--cvss-vector", dest="cvss_vector", help="CVSS:3.1/... (score is computed)")
        p.add_argument("--cvss-score", dest="cvss_score", help="Manual score when there's no vector")
        for key in FINDING_TEXT_FIELDS:
            text_opt(p, key, key.replace("_", " ").capitalize())
        p.add_argument("--evidence", nargs="*", metavar="PAGE",
                       help="Evidence pages (replaces the list)" if not create else "Evidence pages")
        if not create:
            p.add_argument("--add-evidence", nargs="+", metavar="PAGE", default=[])
            p.add_argument("--remove-evidence", nargs="+", metavar="PAGE", default=[])

    g = group("findings", "Findings (beta feature; must be on)")
    p = add("list", cmd_findings_list, "Findings by severity", g)
    p.add_argument("--severity", choices=SEVERITIES)
    p.add_argument("--status", choices=FINDING_STATUSES)
    p.add_argument("--full", action="store_true", help="Include the text fields")
    g = group("finding", "One finding")
    p = add("get", cmd_finding_get, "A finding with evidence pages", g)
    p.add_argument("id", type=int)
    finding_opts(add("create", cmd_finding_create, "Create a finding", g), create=True)
    finding_opts(add("update", cmd_finding_update, "Change a finding", g), create=False)
    p = add("delete", cmd_finding_delete, "Delete a finding", g)
    p.add_argument("id", type=int)
    p = add("cvss", cmd_cvss, "Score a CVSS 3.x vector (no project needed)")
    p.add_argument("vector")

    # report
    g = group("report", "Report front matter and export (beta feature; must be on)")
    add("get", cmd_report_get, "Report fields", g)
    p = add("set", cmd_report_set, "Change report fields (others are kept)", g)
    p.add_argument("--set", action="append", metavar="KEY=VALUE", help=f"Fields: {', '.join(REPORT_FIELDS)}")
    p.add_argument("--set-file", nargs=2, action="append", metavar=("KEY", "PATH"))
    p.add_argument("--include-timeline", choices=["on", "off"])
    p.add_argument("--include-drafts", choices=["on", "off"])
    p = add("export", cmd_report_export, "Build the report as Markdown or Word", g)
    p.add_argument("--format", choices=["md", "docx"], default="md")
    p.add_argument("--out", help="Output file (required for docx; md prints to stdout without it)")
    p.add_argument("--raw", action="store_true", help="Print Markdown as plain text")

    # files
    g = group("files", "Uploaded files")
    p = add("list", cmd_files_list, "List uploads", g)
    p.add_argument("--images", action="store_true")
    p.add_argument("--documents", action="store_true")
    g = group("file", "One uploaded file (FILE = stored filename, original name or URL)")
    p = add("upload", cmd_file_upload, "Upload a local file; returns its URL and a Markdown snippet", g)
    p.add_argument("path")
    p.add_argument("--name", help="Display name (default: the file name)")
    p.add_argument("--attach", metavar="PAGE", help="Also attach it to this page")
    p.add_argument("--embed", metavar="PAGE", help="Also append its Markdown (image/link) to this page")
    p = add("download", cmd_file_download, "Copy an upload to a local path", g)
    p.add_argument("file")
    p.add_argument("--out", required=True, help="File or directory")
    p = add("delete", cmd_file_delete, "Delete an upload", g)
    p.add_argument("file")

    # search / batch
    p = add("search", cmd_search, "Search pages, timeline, findings and file names")
    p.add_argument("query")
    p.add_argument("--regex", action="store_true")
    p.add_argument("--case-sensitive", action="store_true")
    p.add_argument("--scope", help="Comma list: pages,timeline,findings,files")
    p.add_argument("--limit", type=int, default=100)
    p = add("batch", None, "Run several commands in one transaction (all or nothing)")
    p.add_argument("--file", default="-", help="JSON array of commands, each an argv list or a command string "
                                              "(default stdin). --user/--project are inherited")
    return parser


AGENT_HELP = """RedObsidian agent CLI - quick reference
Global: --user U --project P [--dry-run] [--compact]   (or env REDOBSIDIAN_AGENT_USER / _PROJECT)
PAGE = page ID, exact title, or 'Parent / Child' path.  Text args have a --X-file PATH variant ('-' = stdin).

Read:   project info | project dump [--out f.json] | pages list [--raw] [--deleted]
        page get PAGE [--raw] [--section H] | page history PAGE | page revision PAGE REV | page diff PAGE REV [--to REV]
        links list [--page PAGE] | timeline list [--chronological] | files list | search QUERY [--regex]
        findings list [--full] | finding get ID | report get | report export [--format md|docx] [--out F]
Write:  page create TITLE [--parent P] [--notes-file F] [--before/--after P] [--in-graph]
        page update PAGE [--title T] [--caption C] [--notes-file F] [--append T] [--prepend T]
                         [--replace OLD NEW]... [--section H --section-body T [--section-mode append]]
                         [--delete-section] [--expect-updated-at TS] [--new-revision]
        page move PAGE --parent P|root [--before/--after P] | page delete PAGE | page restore PAGE
        page revert PAGE REV | page attach PAGE FILE | page detach PAGE FILE
        links add SRC TGT | links remove ID | links remove SRC TGT
        timeline add TITLE [--body T] [--start ISO] [--end ISO] | timeline update ID ... | timeline delete ID
        timeline move ID --before/--after ID | timeline auto-sort
        file upload PATH [--embed PAGE] [--attach PAGE] | file download FILE --out P | file delete FILE
        finding create TITLE [--cvss-vector V] [--severity S|auto] [--status S] [--description-file F]
                       [--impact ..] [--remediation ..] [--references ..] [--affected-assets ..] [--evidence PAGE..]
        finding update ID [same options] [--add-evidence P..] [--remove-evidence P..] | finding delete ID
        report set --set KEY=VALUE.. [--set-file KEY PATH] [--include-timeline on|off] [--include-drafts on|off]
Owner:  project update | project feature findings|report on|off | project archive|unarchive
        project export --out F.zip | project activity | members list|set U ROLE|remove U
Batch:  echo '[["page","create","Recon"],"page update Recon --append \\"- nmap done\\""]' | ... batch
Output: {"ok":true,"result":...} or {"ok":false,"error":"..."}; --raw prints plain text.
"""


# --- Runner ----------------------------------------------------------------------------------

def run_one(ctx, parser, argv):
    args = parser.parse_args(argv)
    if args.command == "help-agent":
        return AGENT_HELP
    if not getattr(args, "func", None):
        if args.command == "batch":
            raise CliError("batch can't be nested.")
        raise CliError("No command given. Run `help-agent` for the command list.")
    ref = args.project or os.environ.get("REDOBSIDIAN_AGENT_PROJECT")
    if ctx.project is not None and ref not in (ctx.project.name, str(ctx.project.id)):
        ctx.project = ctx.role = None
    ctx.args = args
    return args.func(ctx, args)


def run_batch(ctx, parser, args, base_argv):
    raw = read_text(file=args.file)
    try:
        steps = json.loads(raw)
    except ValueError as exc:
        raise CliError(f"batch input is not valid JSON: {exc}") from exc
    if not isinstance(steps, list):
        raise CliError("batch input must be a JSON array.")
    results = []
    for index, step in enumerate(steps):
        argv = shlex.split(step, posix=True) if isinstance(step, str) else [str(s) for s in step]
        if argv and argv[0] in ("batch",):
            raise CliError("batch can't be nested.")
        try:
            results.append({"step": index, "command": " ".join(argv[:3]), "result": run_one(ctx, parser, base_argv + argv)})
        except (SystemExit, ArgumentError) as exc:
            raise CliError(f"Step {index}: invalid arguments {argv}: {exc}. Nothing was saved.") from exc
        except CliError as exc:
            raise CliError(f"Step {index} ({' '.join(argv[:3])}): {str(exc).rstrip('.')}. Nothing was saved.") from exc
    return results


class ArgumentError(Exception):
    pass


class JsonArgumentParser(argparse.ArgumentParser):
    """Argument errors come back as JSON instead of usage text."""

    def error(self, message):
        raise ArgumentError(f"{self.prog}: {message}")


def emit_all(app, ctx):
    if not app.config.get("REDIS_URL") or not ctx.emits or ctx.project is None:
        return False
    try:
        for event, payload in ctx.emits:
            socketio.emit(event, payload, to=project_room(ctx.project.id))
        return True
    except Exception:
        return False


def print_out(payload, compact):
    if isinstance(payload, str):
        sys.stdout.write(payload if payload.endswith("\n") else payload + "\n")
    else:
        sys.stdout.write(json.dumps(payload, indent=None if compact else 2, ensure_ascii=False, default=str) + "\n")


def main(argv=None):
    for stream in (sys.stdout, sys.stdin):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    compact = "--compact" in argv
    try:
        args = parser.parse_args(argv)
    except ArgumentError as exc:
        print_out({"ok": False, "error": str(exc)}, compact)
        return 2
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "help-agent":
        print_out(AGENT_HELP, compact)
        return 0

    app = create_app()
    with app.app_context():
        ctx = Ctx(app, args)
        try:
            if args.command == "batch":
                base = []
                for flag in ("user", "project"):
                    if getattr(args, flag):
                        base += [f"--{flag}", getattr(args, flag)]
                result = run_batch(ctx, parser, args, base)
            else:
                result = run_one(ctx, parser, argv)
            db.session.flush()
            if args.dry_run:
                raise CliError("__dry_run__")
            db.session.commit()
        except (CliError, ArgumentError) as exc:
            db.session.rollback()
            for undo in ctx.on_rollback:
                undo()
            if str(exc) == "__dry_run__":
                print_out({"ok": True, "dry_run": True, "result": result}, compact)
                return 0
            print_out({"ok": False, "error": str(exc)}, compact)
            return 1
        except Exception as exc:  # unexpected: report it without a traceback on stdout
            db.session.rollback()
            for undo in ctx.on_rollback:
                undo()
            print_out({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, compact)
            return 1
        for action in ctx.on_commit:
            try:
                action()
            except OSError:
                pass
        live = emit_all(app, ctx)
        if isinstance(result, str):
            print_out(result, compact)
        else:
            payload = {"ok": True, "result": result}
            if ctx.emits and not live:
                payload["note"] = "Saved. Open browser tabs show the change after a reload (no REDIS_URL)."
            print_out(payload, compact)
        return 0


if __name__ == "__main__":
    sys.exit(main())
