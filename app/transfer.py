"""Encrypted project export/import (AES ZIP), used by the web UI and manage_projects.py."""
import json
import os
import re
import shutil
import uuid

import pyzipper
from werkzeug.utils import secure_filename

from . import db
from .main import parse_iso_datetime
from .maintenance import file_sha256, project_upload_dir
from .models import Finding, GraphEdge, GraphNode, Project, ProjectMember, TimelineEvent, Upload, isoformat_utc, load_json_list

EXPORT_FORMAT_VERSION = 2
# Refuse archives that would unpack to more than this (zip bombs)
MAX_IMPORT_BYTES = int(os.environ.get("REDOBSIDIAN_MAX_IMPORT_MB", "4096")) * 1024 * 1024
MIN_EXPORT_PASSWORD_LENGTH = 8


class TransferError(Exception):
    pass


def finding_to_export(finding):
    return {
        "id": finding.id, "title": finding.title, "severity": finding.severity, "cvss_score": finding.cvss_score,
        "cvss_vector": finding.cvss_vector, "status": finding.status, "affected_assets": finding.affected_assets,
        "description": finding.description, "impact": finding.impact, "remediation": finding.remediation,
        "references": finding.references, "evidence_node_ids": finding.evidence_node_ids,
        "created_at": isoformat_utc(finding.created_at),
    }


def export_project(project, output_path, password):
    if len(password or "") < MIN_EXPORT_PASSWORD_LENGTH:
        raise TransferError(f"The export password must be at least {MIN_EXPORT_PASSWORD_LENGTH} characters.")
    # Soft-deleted pages (and links to them) are not exported
    nodes = GraphNode.query.filter_by(project_id=project.id, deleted_at=None).all()
    live_ids = {node.id for node in nodes}
    data = {
        "format_version": EXPORT_FORMAT_VERSION,
        "project": {**project.to_dict(), "report": project.report, "findings_enabled": project.findings_enabled,
                    "report_enabled": project.report_enabled},
        "timeline_events": [event.to_dict() for event in TimelineEvent.query.filter_by(project_id=project.id)],
        "graph_nodes": [node.to_dict() for node in nodes],
        "graph_edges": [
            edge.to_dict() for edge in GraphEdge.query.filter_by(project_id=project.id)
            if edge.source_id in live_ids and edge.target_id in live_ids
        ],
        "findings": [finding_to_export(finding) for finding in Finding.query.filter_by(project_id=project.id)],
        "uploads": [
            {"filename": upload.filename, "original_name": upload.original_name}
            for upload in Upload.query.filter_by(project_id=project.id)
        ],
    }
    with pyzipper.AESZipFile(output_path, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as zf:
        zf.setpassword(password.encode("utf-8"))
        zf.writestr("project.json", json.dumps(data, indent=2))
        upload_dir = project_upload_dir(project.id, create=False)
        if os.path.isdir(upload_dir):
            for filename in sorted(os.listdir(upload_dir)):
                path = os.path.join(upload_dir, filename)
                if os.path.isfile(path):
                    zf.write(path, f"uploads/{filename}")


def _rewrite_upload_refs(text, project_id, renames):
    """Point upload links at the imported project's copies (old exports used /uploads/<name>)."""
    if not text or not renames:
        return text

    def replace(match):
        new_name = renames.get(match.group(2))
        return f"/p/{project_id}/uploads/{new_name}" if new_name else match.group(0)

    return re.sub(r"(/p/\d+)?/uploads/([A-Za-z0-9._-]+)", replace, text)


def _rewrite_files(files, project_id, renames):
    rewritten = []
    for item in files if isinstance(files, list) else []:
        if isinstance(item, dict):
            item = {**item, "url": _rewrite_upload_refs(str(item.get("url", "")), project_id, renames)}
            rewritten.append(item)
    return rewritten


def import_project(input_path, password, owner, new_name=None):
    """Create a new project from an export. The importing user becomes its owner."""
    try:
        zf = pyzipper.AESZipFile(input_path, "r")
    except (pyzipper.BadZipFile, OSError) as exc:
        raise TransferError("The file is not a valid project export.") from exc
    with zf:
        zf.setpassword((password or "").encode("utf-8"))
        total = sum(info.file_size for info in zf.infolist())
        if total > MAX_IMPORT_BYTES:
            raise TransferError("The export is too large to import.")
        try:
            with zf.open("project.json") as handle:
                data = json.load(handle)
        except KeyError as exc:
            raise TransferError("The file is not a valid project export.") from exc
        except (RuntimeError, ValueError, pyzipper.BadZipFile) as exc:
            raise TransferError("Wrong password or corrupted file.") from exc

        project_data = data.get("project") or {}
        name = (new_name or project_data.get("name") or "Imported project").strip()[:120]
        project = Project(name=name, description=project_data.get("description"), created_by_id=owner.id,
                          report_json=json.dumps(project_data.get("report") or {}),
                          findings_enabled=bool(project_data.get("findings_enabled", bool(data.get("findings")))),
                          report_enabled=bool(project_data.get("report_enabled",
                                                               any((project_data.get("report") or {}).values()))))
        db.session.add(project)
        db.session.flush()
        db.session.add(ProjectMember(project_id=project.id, user_id=owner.id, role="owner"))

        # Copy uploads under fresh names; only plain files directly under uploads/ are taken (no path traversal)
        upload_dir = project_upload_dir(project.id)
        original_names = {item.get("filename"): item.get("original_name") for item in data.get("uploads") or []}
        renames = {}
        try:
            for info in zf.infolist():
                if info.is_dir() or not info.filename.startswith("uploads/"):
                    continue
                old_name = info.filename[len("uploads/"):]
                if "/" in old_name or "\\" in old_name or not old_name or secure_filename(old_name) != old_name:
                    continue
                stem, ext = os.path.splitext(old_name)
                stem = re.sub(r"_[0-9a-f]{32}$", "", stem)[:80] or "file"
                new_name = f"{stem}_{uuid.uuid4().hex}{ext.lower()}"
                target = os.path.join(upload_dir, new_name)
                with zf.open(info) as source, open(target, "wb") as destination:
                    shutil.copyfileobj(source, destination)
                renames[old_name] = new_name
                db.session.add(Upload(project_id=project.id, filename=new_name,
                                      original_name=(original_names.get(old_name) or old_name)[:255],
                                      size=os.path.getsize(target), sha256=file_sha256(target), user_id=owner.id))
        except (RuntimeError, pyzipper.BadZipFile) as exc:
            db.session.rollback()
            shutil.rmtree(upload_dir, ignore_errors=True)
            raise TransferError("The export is corrupted.") from exc

        try:
            _import_records(project, data, owner, renames)
        except (KeyError, TypeError, ValueError) as exc:
            db.session.rollback()
            shutil.rmtree(upload_dir, ignore_errors=True)
            raise TransferError("The export is missing data or was made by an incompatible version.") from exc
        db.session.commit()
        return project


def _import_records(project, data, owner, renames):
    node_ids = {}
    for item in data.get("graph_nodes") or []:
        node = GraphNode(
            project_id=project.id,
            title=(item.get("title") or "Untitled page")[:120],
            caption=(item.get("caption") or "")[:280],
            notes=_rewrite_upload_refs(item.get("notes") or "", project.id, renames),
            files_json=json.dumps(_rewrite_files(item.get("files"), project.id, renames)),
            order_index=float(item.get("order_index") or 0),
            color=(item.get("color") or "")[:20],
            in_graph=bool(item.get("in_graph")),
            x=float(item.get("x") or 360),
            y=float(item.get("y") or 240),
            user_id=owner.id,
        )
        db.session.add(node)
        db.session.flush()
        node_ids[item["id"]] = node.id
    for item in data.get("graph_nodes") or []:
        new_parent = node_ids.get(item.get("parent_id"))
        if new_parent:
            db.session.get(GraphNode, node_ids[item["id"]]).parent_id = new_parent
    if not node_ids:
        db.session.add(GraphNode(project_id=project.id, title="Untitled page", in_graph=False, user_id=owner.id))

    for item in data.get("timeline_events") or []:
        db.session.add(TimelineEvent(
            project_id=project.id,
            title=(item.get("title") or "Event")[:160],
            body=_rewrite_upload_refs(item.get("body") or "", project.id, renames),
            files_json=json.dumps(_rewrite_files(item.get("files"), project.id, renames)),
            occurred_at=parse_iso_datetime(item.get("occurred_at")),
            ended_at=parse_iso_datetime(item["ended_at"]) if item.get("ended_at") else None,
            order_index=float(item.get("order_index") or 0),
            manual_order=bool(item.get("manual_order")),
            user_id=owner.id,
        ))

    for item in data.get("graph_edges") or []:
        source, target = node_ids.get(item.get("source_id")), node_ids.get(item.get("target_id"))
        if source and target and source != target:
            db.session.add(GraphEdge(project_id=project.id, source_id=source, target_id=target,
                                     joint_x=item.get("joint_x"), joint_y=item.get("joint_y"), user_id=owner.id))

    from .findings import clean_finding_fields
    for item in data.get("findings") or []:
        fields = clean_finding_fields(item)
        for key in ("description", "impact", "remediation", "references", "affected_assets"):
            fields[key] = _rewrite_upload_refs(fields.get(key) or "", project.id, renames)
        evidence = [node_ids[old] for old in load_json_list(json.dumps(item.get("evidence_node_ids") or [])) if old in node_ids]
        db.session.add(Finding(project_id=project.id, user_id=owner.id,
                               evidence_node_ids_json=json.dumps(evidence), **fields))
