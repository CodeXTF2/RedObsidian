import io
import json

import pyzipper

from app import db
from app.models import Finding, GraphNode, Project, Upload


def test_export_import_roundtrip(app, make_user, make_project, login_as):
    alice = make_user("alice")
    project_id = make_project("Client A", {alice: "owner"}, findings=True, report=True)
    client = login_as("alice")
    uploaded = client.post(f"/p/{project_id}/api/upload", data={"file": (io.BytesIO(b"\x89PNG data"), "shot.png", "image/png")},
                           content_type="multipart/form-data").json
    with app.app_context():
        node = GraphNode.query.filter_by(project_id=project_id).first()
        node.notes = f"See ![shot]({uploaded['url']}) and legacy /uploads/{uploaded['filename']}"
        db.session.commit()
        node_id = node.id
    client.post(f"/p/{project_id}/findings", data={"title": "XSS", "status": "open", "severity": "high",
                                                    "description": f"![x]({uploaded['url']})", "evidence": str(node_id)})

    export = client.post(f"/p/{project_id}/export", data={"password": "hunter2hunter2", "confirm_password": "hunter2hunter2"})
    assert export.status_code == 200 and export.mimetype == "application/zip"

    imported = client.post("/projects/import", data={"file": (io.BytesIO(export.data), "export.zip"),
                                                      "password": "hunter2hunter2", "name": "Copy"},
                           content_type="multipart/form-data")
    assert imported.status_code == 302
    with app.app_context():
        copy = Project.query.filter_by(name="Copy").one()
        upload = Upload.query.filter_by(project_id=copy.id).one()
        assert upload.filename != uploaded["filename"] and upload.original_name == "shot.png"
        notes = GraphNode.query.filter_by(project_id=copy.id).one().notes
        assert f"/p/{copy.id}/uploads/{upload.filename}" in notes
        assert uploaded["filename"] not in notes
        assert copy.findings_enabled and copy.report_enabled
        finding = Finding.query.filter_by(project_id=copy.id).one()
        assert finding.evidence_node_ids and f"/p/{copy.id}/uploads/" in finding.description
        copy_id = copy.id
    assert client.get(f"/p/{copy_id}/uploads/{upload.filename}").status_code == 200


def test_import_wrong_password(app, make_user, make_project, login_as):
    alice = make_user("alice")
    project_id = make_project("A", {alice: "owner"}, findings=True)
    client = login_as("alice")
    export = client.post(f"/p/{project_id}/export", data={"password": "hunter2hunter2", "confirm_password": "hunter2hunter2"})
    response = client.post("/projects/import", data={"file": (io.BytesIO(export.data), "e.zip"), "password": "nope"},
                           content_type="multipart/form-data", follow_redirects=True)
    assert b"Wrong password" in response.data
    with app.app_context():
        assert Project.query.count() == 1


def test_import_ignores_path_traversal(app, make_user, login_as, tmp_path):
    make_user("alice")
    buffer = io.BytesIO()
    with pyzipper.AESZipFile(buffer, "w", encryption=pyzipper.WZ_AES) as zf:
        zf.setpassword(b"hunter2hunter2")
        zf.writestr("project.json", json.dumps({"project": {"name": "Evil"}, "graph_nodes": [], "timeline_events": [],
                                                "graph_edges": []}))
        zf.writestr("uploads/../../../escape.txt", "pwned")
        zf.writestr("uploads/ok.txt", "fine")
    client = login_as("alice")
    response = client.post("/projects/import", data={"file": (io.BytesIO(buffer.getvalue()), "e.zip"),
                                                      "password": "hunter2hunter2"}, content_type="multipart/form-data")
    assert response.status_code == 302
    assert not list(tmp_path.rglob("escape.txt"))
    with app.app_context():
        assert [u.original_name for u in Upload.query.all()] == ["ok.txt"]


def test_export_password_minimum(app, make_user, make_project, login_as):
    alice = make_user("alice")
    project_id = make_project("A", {alice: "owner"}, findings=True)
    response = login_as("alice").post(f"/p/{project_id}/export", data={"password": "short", "confirm_password": "short"})
    assert response.status_code == 302
