import json
import math
import re
from functools import wraps

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from markupsafe import Markup

from . import audit, db
from .access import project_route, role_allows
from .markdown_render import to_html
from .models import FINDING_STATUSES, SEVERITIES, Finding, GraphNode, utcnow

findings_bp = Blueprint("findings", __name__)

FINDING_TEXT_FIELDS = ("affected_assets", "description", "impact", "remediation", "references")
REPORT_FIELDS = ("title", "client", "assessment_type", "start_date", "end_date", "version", "classification",
                 "prepared_by", "executive_summary", "scope", "methodology", "conclusion")
REPORT_FLAGS = ("include_timeline", "include_drafts")
STATUS_LABELS = {"draft": "Draft", "open": "Open", "remediated": "Remediated", "accepted": "Risk accepted",
                 "false_positive": "False positive"}

# --- CVSS 3.1 base score -------------------------------------------------------

CVSS_WEIGHTS = {
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2},
    "AC": {"L": 0.77, "H": 0.44},
    "UI": {"N": 0.85, "R": 0.62},
    "C": {"H": 0.56, "L": 0.22, "N": 0.0},
    "I": {"H": 0.56, "L": 0.22, "N": 0.0},
    "A": {"H": 0.56, "L": 0.22, "N": 0.0},
}
CVSS_RE = re.compile(r"^CVSS:3\.[01]/(.+)$")


def _roundup(value):
    integer = round(value * 100000)
    if integer % 10000 == 0:
        return integer / 100000.0
    return (math.floor(integer / 10000) + 1) / 10.0


def cvss31_score(vector):
    """Base score for a CVSS:3.x vector string, or None if it isn't a complete, valid vector."""
    match = CVSS_RE.match((vector or "").strip())
    if not match:
        return None
    metrics = dict(part.split(":", 1) for part in match.group(1).split("/") if ":" in part)
    try:
        scope_changed = metrics["S"] == "C"
        if metrics["S"] not in ("U", "C"):
            return None
        pr = {"N": 0.85, "L": 0.68 if scope_changed else 0.62, "H": 0.5 if scope_changed else 0.27}[metrics["PR"]]
        av, ac, ui = (CVSS_WEIGHTS[key][metrics[key]] for key in ("AV", "AC", "UI"))
        c, i, a = (CVSS_WEIGHTS[key][metrics[key]] for key in ("C", "I", "A"))
    except KeyError:
        return None
    iss = 1 - (1 - c) * (1 - i) * (1 - a)
    impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15 if scope_changed else 6.42 * iss
    exploitability = 8.22 * av * ac * pr * ui
    if impact <= 0:
        return 0.0
    total = 1.08 * (impact + exploitability) if scope_changed else impact + exploitability
    return _roundup(min(total, 10))


def severity_for_score(score):
    if score is None:
        return None
    if score >= 9.0:
        return "critical"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "medium"
    if score >= 0.1:
        return "low"
    return "info"


def clean_finding_fields(data):
    """Validated column values from a form or an imported export."""
    def text(key, limit=None):
        value = str(data.get(key) or "").strip()
        return value[:limit] if limit else value

    fields = {"title": text("title", 200) or "Untitled finding"}
    fields["cvss_vector"] = text("cvss_vector", 200) or None
    computed = cvss31_score(fields["cvss_vector"]) if fields["cvss_vector"] else None
    try:
        manual = float(data.get("cvss_score")) if str(data.get("cvss_score") or "").strip() else None
    except (TypeError, ValueError):
        manual = None
    score = computed if computed is not None else manual
    fields["cvss_score"] = None if score is None else max(0.0, min(10.0, round(score, 1)))
    severity = str(data.get("severity") or "").lower()
    if severity not in SEVERITIES:
        severity = severity_for_score(fields["cvss_score"]) or "medium"
    fields["severity"] = severity
    status = str(data.get("status") or "draft")
    fields["status"] = status if status in FINDING_STATUSES else "draft"
    for key in FINDING_TEXT_FIELDS:
        fields[key] = text(key)
    return fields


def feature_route(flag, needed="viewer"):
    """project_route for the pages of a beta feature, which 404 until an owner turns it on."""
    def decorator(view):
        @project_route(needed)
        @wraps(view)
        def wrapper(project, *args, **kwargs):
            if not getattr(project, flag):
                abort(404)
            return view(project, *args, **kwargs)
        return wrapper
    return decorator


def findings_route(needed="viewer"):
    return feature_route("findings_enabled", needed)


def report_route(needed="viewer"):
    return feature_route("report_enabled", needed)


def sorted_findings(project_id, include_drafts=True):
    findings = Finding.query.filter_by(project_id=project_id).all()
    if not include_drafts:
        findings = [f for f in findings if f.status not in ("draft", "false_positive")]
    return sorted(findings, key=lambda f: (f.severity_rank, -(f.cvss_score or 0), f.id))


def project_pages(project_id):
    return GraphNode.query.filter_by(project_id=project_id, deleted_at=None).order_by(GraphNode.title).all()


@findings_bp.app_template_global()
def status_label(status):
    return STATUS_LABELS.get(status, status)


@findings_bp.app_template_global()
def markdown_html(text):
    # to_html escapes all text itself, so its output is safe to mark as HTML
    return Markup(to_html(text))  # noqa: S704


# --- Findings ------------------------------------------------------------------

@findings_bp.get("/p/<int:project_id>/findings")
@findings_route()
def list_findings(project):
    findings = sorted_findings(project.id)
    severity = request.args.get("severity")
    status = request.args.get("status")
    counts = {level: sum(1 for f in findings if f.severity == level and f.status not in ("false_positive",))
              for level in SEVERITIES}
    if severity in SEVERITIES:
        findings = [f for f in findings if f.severity == severity]
    if status in FINDING_STATUSES:
        findings = [f for f in findings if f.status == status]
    return render_template("findings.html", findings=findings, counts=counts, severities=SEVERITIES,
                           statuses=FINDING_STATUSES, severity_filter=severity, status_filter=status,
                           active_page="findings")


@findings_bp.get("/p/<int:project_id>/findings/new")
@findings_route("editor")
def new_finding(project):
    from_page = request.args.get("from_page", type=int)
    finding = Finding(project_id=project.id, title="", severity="medium", status="draft",
                      evidence_node_ids_json=json.dumps([from_page] if from_page else []))
    return render_template("finding_form.html", finding=finding, is_new=True, pages=project_pages(project.id),
                           severities=SEVERITIES, statuses=FINDING_STATUSES, active_page="findings")


def evidence_from_form(project):
    valid = {node.id for node in project_pages(project.id)}
    ids = []
    for value in request.form.getlist("evidence"):
        if value.isdigit() and int(value) in valid and int(value) not in ids:
            ids.append(int(value))
    return json.dumps(ids)


@findings_bp.post("/p/<int:project_id>/findings")
@findings_route("editor")
def create_finding(project):
    fields = clean_finding_fields(request.form)
    finding = Finding(project_id=project.id, user_id=current_user.id, evidence_node_ids_json=evidence_from_form(project),
                      **fields)
    db.session.add(finding)
    db.session.flush()
    audit.record("finding.created", project_id=project.id, target_type="finding", target_id=finding.id,
                 title=finding.title, severity=finding.severity)
    flash("Finding created.", "success")
    return redirect(url_for("findings.edit_finding", project_id=project.id, finding_id=finding.id))


def project_finding(project, finding_id):
    finding = db.session.get(Finding, finding_id)
    if not finding or finding.project_id != project.id:
        abort(404)
    return finding


@findings_bp.get("/p/<int:project_id>/findings/<int:finding_id>")
@findings_route()
def edit_finding(project, finding_id):
    finding = project_finding(project, finding_id)
    return render_template("finding_form.html", finding=finding, is_new=False, pages=project_pages(project.id),
                           severities=SEVERITIES, statuses=FINDING_STATUSES, active_page="findings")


@findings_bp.post("/p/<int:project_id>/findings/<int:finding_id>")
@findings_route("editor")
def update_finding(project, finding_id):
    finding = project_finding(project, finding_id)
    fields = clean_finding_fields(request.form)
    changed = sorted(key for key, value in fields.items() if getattr(finding, key) != value)
    for key, value in fields.items():
        setattr(finding, key, value)
    evidence = evidence_from_form(project)
    if evidence != finding.evidence_node_ids_json:
        changed.append("evidence")
        finding.evidence_node_ids_json = evidence
    db.session.commit()
    if changed:
        audit.record("finding.updated", project_id=project.id, target_type="finding", target_id=finding.id,
                     title=finding.title, fields=changed)
    flash("Finding saved.", "success")
    return redirect(url_for("findings.edit_finding", project_id=project.id, finding_id=finding.id))


@findings_bp.post("/p/<int:project_id>/findings/<int:finding_id>/delete")
@findings_route("editor")
def delete_finding(project, finding_id):
    finding = project_finding(project, finding_id)
    title = finding.title
    db.session.delete(finding)
    audit.record("finding.deleted", project_id=project.id, target_type="finding", target_id=finding_id, title=title)
    flash(f"Deleted “{title}”.", "success")
    return redirect(url_for("findings.list_findings", project_id=project.id))


@findings_bp.get("/api/cvss")
@login_required
def cvss_api():
    score = cvss31_score(request.args.get("vector", ""))
    return {"score": score, "severity": severity_for_score(score)}


# --- Report --------------------------------------------------------------------

@findings_bp.get("/p/<int:project_id>/report")
@report_route()
def report(project):
    findings = []
    if project.findings_enabled:
        findings = sorted_findings(project.id, include_drafts=bool(project.report.get("include_drafts")))
    return render_template("report.html", report=project.report, findings=findings, active_page="report")


@findings_bp.post("/p/<int:project_id>/report")
@report_route("editor")
def save_report(project):
    data = {key: request.form.get(key, "").strip() for key in REPORT_FIELDS}
    for flag in REPORT_FLAGS:
        data[flag] = request.form.get(flag) == "on"
    project.report_json = json.dumps(data)
    db.session.commit()
    audit.record("report.updated", project_id=project.id, target_type="project", target_id=project.id)
    flash("Report details saved.", "success")
    return redirect(url_for("findings.report", project_id=project.id))


def report_context(project):
    from .models import TimelineEvent

    settings = project.report
    findings = []
    if project.findings_enabled:
        findings = sorted_findings(project.id, include_drafts=bool(settings.get("include_drafts")))
    pages = {node.id: node for node in project_pages(project.id)}
    events = []
    if settings.get("include_timeline"):
        events = TimelineEvent.query.filter_by(project_id=project.id).order_by(TimelineEvent.occurred_at).all()
    numbered = [(f"F-{index:02d}", finding) for index, finding in enumerate(findings, start=1)]
    counts = {level: sum(1 for f in findings if f.severity == level) for level in SEVERITIES}
    return {"findings_enabled": project.findings_enabled, "settings": settings,
            "numbered": numbered, "pages": pages, "events": events, "counts": counts,
            "generated_at": utcnow(), "generated_by": current_user.username}


@findings_bp.get("/p/<int:project_id>/report/preview")
@report_route()
def report_preview(project):
    audit.record("report.previewed", project_id=project.id, target_type="project", target_id=project.id)
    return render_template("report_preview.html", **report_context(project))


@findings_bp.get("/p/<int:project_id>/report.docx")
@report_route()
def report_docx(project):
    from .reports import build_docx, report_filename

    stream = build_docx(project, report_context(project))
    audit.record("report.exported", project_id=project.id, target_type="project", target_id=project.id, format="docx")
    return send_file(stream, as_attachment=True, download_name=report_filename(project, "docx"),
                     mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@findings_bp.get("/p/<int:project_id>/report.md")
@report_route()
def report_markdown(project):
    from .reports import build_markdown, report_filename

    text = build_markdown(project, report_context(project))
    audit.record("report.exported", project_id=project.id, target_type="project", target_id=project.id, format="markdown")
    return Response(text, mimetype="text/markdown",
                    headers={"Content-Disposition": f'attachment; filename="{report_filename(project, "md")}"'})


@findings_bp.app_context_processor
def finding_globals():
    return {"severities_all": SEVERITIES, "g_role": g.get("project_role"), "role_allows": role_allows}
