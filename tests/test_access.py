import io

from app import db
from app.models import GraphNode, Project, ProjectMember
from app.settings import set_settings

from .conftest import received_named


def test_non_members_cannot_see_or_open_a_project(app, make_user, make_project, login_as):
    alice = make_user("alice")
    make_user("bob")
    project_id = make_project("Client A", {alice: "owner"}, findings=True, report=True)
    client = login_as("bob")
    assert b"Client A" not in client.get("/projects").data
    for path in ("notes", "graph", "timeline", "findings", "report", "settings", "api/state", "api/images"):
        assert client.get(f"/p/{project_id}/{path}").status_code == 404, path
    assert login_as("alice").get(f"/p/{project_id}/api/state").status_code == 200


def test_admin_sees_every_project(app, make_user, make_project, login_as):
    alice = make_user("alice")
    make_user("root", is_admin=True)
    project_id = make_project("Client A", {alice: "owner"})
    client = login_as("root")
    assert b"Client A" in client.get("/projects").data
    assert client.get(f"/p/{project_id}/api/state").json["role"] == "owner"


def test_legacy_urls_redirect_into_the_last_project(app, make_user, make_project, login_as):
    alice = make_user("alice")
    project_id = make_project("Client A", {alice: "owner"})
    client = login_as("alice")
    assert client.get("/notes").headers["Location"].endswith(f"/p/{project_id}/notes")


def upload(client, project_id, name, data=b"hello", content_type="text/plain"):
    return client.post(f"/p/{project_id}/api/upload", data={"file": (io.BytesIO(data), name, content_type)},
                       content_type="multipart/form-data")


def test_uploads_are_served_safely(app, make_user, make_project, login_as):
    alice = make_user("alice")
    project_id = make_project("Client A", {alice: "owner"})
    client = login_as("alice")

    html = upload(client, project_id, "payload.html", b"<script>alert(1)</script>", "text/html").json
    response = client.get(html["url"])
    assert response.headers["Content-Disposition"].startswith("attachment")
    assert "sandbox" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"

    svg = upload(client, project_id, "logo.svg", b"<svg onload=alert(1)>", "image/svg+xml").json
    assert client.get(svg["url"]).headers["Content-Disposition"].startswith("attachment")

    png = upload(client, project_id, "shot.png", b"\x89PNG\r\n\x1a\n", "image/png").json
    response = client.get(png["url"])
    assert response.mimetype == "image/png"
    assert not response.headers.get("Content-Disposition", "").startswith("attachment")

    names = [item["name"] for item in client.get(f"/p/{project_id}/api/files").json]
    assert names == ["payload.html"]


def test_uploads_are_isolated_between_projects(app, make_user, make_project, login_as):
    alice, bob = make_user("alice"), make_user("bob")
    project_a = make_project("A", {alice: "owner"})
    project_b = make_project("B", {bob: "owner"})
    secret = upload(login_as("alice"), project_a, "loot.txt", b"secret").json
    bob_client = login_as("bob")
    assert bob_client.get(secret["url"]).status_code == 404
    # Asking for A's file through B's URL, or through the legacy URL, doesn't work either
    assert bob_client.get(f"/p/{project_b}/uploads/{secret['filename']}").status_code == 404
    assert bob_client.get(f"/uploads/{secret['filename']}").status_code == 404
    assert login_as("alice").get(f"/uploads/{secret['filename']}").status_code == 200


def test_upload_size_limit(app, make_user, make_project, login_as):
    alice = make_user("alice")
    project_id = make_project("A", {alice: "owner"})
    with app.test_request_context():
        set_settings({"max_upload_mb": 1})
    response = upload(login_as("alice"), project_id, "big.bin", b"x" * (1024 * 1024 + 10))
    assert response.status_code == 413


def test_viewers_are_read_only(app, make_user, make_project, login_as, socket_for):
    alice, viv = make_user("alice"), make_user("viv")
    project_id = make_project("A", {alice: "owner", viv: "viewer"})
    client = login_as("viv")
    assert client.get(f"/p/{project_id}/api/state").json["can_edit"] is False
    assert upload(client, project_id, "x.txt").status_code == 403
    assert client.get(f"/p/{project_id}/findings/new").status_code == 403

    sock = socket_for(client, project_id)
    assert sock.is_connected()
    sock.emit("node:create", {"title": "nope"})
    assert received_named(sock, "error:message")
    with app.app_context():
        assert GraphNode.query.filter_by(title="nope").count() == 0


def test_editor_socket_writes_and_broadcasts(app, make_user, make_project, login_as, socket_for):
    alice, ed = make_user("alice"), make_user("ed")
    project_id = make_project("A", {alice: "owner", ed: "editor"})
    owner_socket = socket_for(login_as("alice"), project_id)
    editor_socket = socket_for(login_as("ed"), project_id)
    owner_socket.get_received()
    ack = editor_socket.emit("node:create", {"title": "Recon"}, callback=True)
    assert ack["title"] == "Recon"
    created = received_named(owner_socket, "node:created")
    assert created and created[0]["args"][0]["title"] == "Recon"


def test_socket_refuses_non_members_and_revoked_members(app, make_user, make_project, login_as, socket_for):
    alice, mallory = make_user("alice"), make_user("mallory")
    project_id = make_project("A", {alice: "owner", mallory: "editor"})
    outsider_project = make_project("B", {alice: "owner"})
    client = login_as("mallory")
    assert not socket_for(client, outsider_project).is_connected()

    sock = socket_for(client, project_id)
    assert sock.is_connected()
    with app.app_context():
        ProjectMember.query.filter_by(project_id=project_id, user_id=mallory).delete()
        db.session.commit()
    sock.emit("node:create", {"title": "after removal"})
    # The server tells the client and drops the connection
    assert not sock.is_connected()
    with app.app_context():
        assert GraphNode.query.filter_by(title="after removal").count() == 0


def test_socket_rejects_bad_numbers_without_leaking_errors(app, make_user, make_project, login_as, socket_for):
    alice = make_user("alice")
    project_id = make_project("A", {alice: "owner"})
    sock = socket_for(login_as("alice"), project_id)
    sock.get_received()
    sock.emit("node:create", {"title": "x", "x": "not a number"})
    messages = received_named(sock, "error:message")
    assert messages and "Traceback" not in messages[0]["args"][0]["message"]


def test_archived_projects_are_read_only(app, make_user, make_project, login_as, socket_for):
    alice = make_user("alice")
    project_id = make_project("A", {alice: "owner"})
    client = login_as("alice")
    assert client.post(f"/p/{project_id}/archive", data={"archive": "1"}).status_code == 302
    assert client.get(f"/p/{project_id}/api/state").json["can_edit"] is False
    assert upload(client, project_id, "x.txt").status_code == 403
    assert b"A" not in client.get("/projects").data.split(b"project-grid")[1]
    assert client.post(f"/p/{project_id}/archive", data={"archive": "0"}).status_code == 302
    assert client.get(f"/p/{project_id}/api/state").json["can_edit"] is True


def test_membership_management(app, make_user, make_project, login_as):
    alice, bob = make_user("alice"), make_user("bob")
    project_id = make_project("A", {alice: "owner"})
    owner = login_as("alice")
    owner.post(f"/p/{project_id}/members", data={"user_id": bob, "role": "viewer"})
    bob_client = login_as("bob")
    assert bob_client.get(f"/p/{project_id}/api/state").json["role"] == "viewer"
    # Non-owners can't manage members
    assert bob_client.post(f"/p/{project_id}/members", data={"user_id": bob, "role": "owner"}).status_code == 403
    # The last owner can't be demoted
    owner.post(f"/p/{project_id}/members/{alice}", data={"role": "viewer"})
    with app.app_context():
        assert ProjectMember.query.filter_by(project_id=project_id, user_id=alice).one().role == "owner"


def test_project_delete_requires_owner_and_name(app, make_user, make_project, login_as):
    alice, ed = make_user("alice"), make_user("ed")
    project_id = make_project("Client A", {alice: "owner", ed: "editor"})
    assert login_as("ed").post(f"/p/{project_id}/delete", data={"confirm_name": "Client A"}).status_code == 403
    owner = login_as("alice")
    owner.post(f"/p/{project_id}/delete", data={"confirm_name": "wrong"})
    with app.app_context():
        assert db.session.get(Project, project_id)
    owner.post(f"/p/{project_id}/delete", data={"confirm_name": "Client A"})
    with app.app_context():
        assert db.session.get(Project, project_id) is None


def test_only_admins_create_projects_setting(app, make_user, login_as):
    make_user("alice")
    client = login_as("alice")
    with app.test_request_context():
        set_settings({"only_admins_create_projects": True})
    assert client.post("/projects", data={"name": "X"}).status_code == 403
