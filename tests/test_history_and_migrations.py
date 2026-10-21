import sqlite3
from datetime import timedelta

from app import create_app, db
from app.models import GraphNode, NodeRevision, Project, ProjectMember, User, utcnow
from app.settings import set_settings


def test_revisions_coalesce_and_restore(app, make_user, make_project, login_as, socket_for):
    alice, bob = make_user("alice"), make_user("bob")
    project_id = make_project("A", {alice: "owner", bob: "editor"})
    with app.app_context():
        node_id = GraphNode.query.filter_by(project_id=project_id).first().id
    alice_client = login_as("alice")
    alice_socket = socket_for(alice_client, project_id)
    bob_socket = socket_for(login_as("bob"), project_id)

    alice_socket.emit("node:update", {"id": node_id, "notes": "v1"})
    alice_socket.emit("node:update", {"id": node_id, "notes": "v2"})
    with app.app_context():
        assert [r.notes for r in NodeRevision.query.order_by(NodeRevision.id)] == ["v2"]
    bob_socket.emit("node:update", {"id": node_id, "notes": "bob's"})
    with app.app_context():
        revisions = NodeRevision.query.order_by(NodeRevision.id).all()
        assert [r.notes for r in revisions] == ["v2", "bob's"]
        first_id = revisions[0].id

    assert alice_client.get(f"/p/{project_id}/nodes/{node_id}/history").status_code == 200
    alice_client.post(f"/p/{project_id}/nodes/{node_id}/history/{first_id}/restore")
    with app.app_context():
        assert db.session.get(GraphNode, node_id).notes == "v2"
        assert NodeRevision.query.count() == 3


def test_pre_history_text_is_kept(app, make_user, make_project, login_as, socket_for):
    alice = make_user("alice")
    project_id = make_project("A", {alice: "owner"})
    with app.app_context():
        node = GraphNode.query.filter_by(project_id=project_id).first()
        node.notes = "written before history existed"
        db.session.commit()
        node_id = node.id
    socket_for(login_as("alice"), project_id).emit("node:update", {"id": node_id, "notes": "new"})
    with app.app_context():
        assert [r.notes for r in NodeRevision.query.order_by(NodeRevision.id)] == ["written before history existed", "new"]


def test_deleted_page_purge_uses_setting(app, make_user, make_project):
    from app.maintenance import purge_deleted_nodes

    alice = make_user("alice")
    project_id = make_project("A", {alice: "owner"})
    with app.test_request_context():
        node = GraphNode(project_id=project_id, title="old", user_id=alice, deleted_at=utcnow() - timedelta(days=10))
        db.session.add(node)
        db.session.commit()
        set_settings({"deleted_page_retention_days": 30})
        assert purge_deleted_nodes() == 0
        set_settings({"deleted_page_retention_days": 7})
        assert purge_deleted_nodes() == 1


def test_archived_project_purge(app, make_user, make_project):
    from app.maintenance import purge_archived_projects

    alice = make_user("alice")
    project_id = make_project("A", {alice: "owner"})
    with app.test_request_context():
        db.session.get(Project, project_id).archived_at = utcnow() - timedelta(days=100)
        db.session.commit()
        assert purge_archived_projects() == 0  # off by default
        set_settings({"archived_project_retention_days": 90})
        assert purge_archived_projects() == 1
        assert db.session.get(Project, project_id) is None


LEGACY_SCHEMA = """
CREATE TABLE user (id INTEGER NOT NULL PRIMARY KEY, username VARCHAR(64) NOT NULL, password_hash VARCHAR(255) NOT NULL,
                   created_at DATETIME NOT NULL);
CREATE TABLE project (id INTEGER NOT NULL PRIMARY KEY, name VARCHAR(120) NOT NULL, description TEXT,
                      created_at DATETIME NOT NULL, created_by_id INTEGER NOT NULL);
CREATE TABLE timeline_event (id INTEGER NOT NULL PRIMARY KEY, title VARCHAR(160) NOT NULL, body TEXT,
                             occurred_at DATETIME NOT NULL, created_at DATETIME NOT NULL, user_id INTEGER NOT NULL);
CREATE TABLE graph_node (id INTEGER NOT NULL PRIMARY KEY, title VARCHAR(120) NOT NULL, caption VARCHAR(280), notes TEXT,
                         color VARCHAR(20), in_graph BOOLEAN NOT NULL, x FLOAT NOT NULL, y FLOAT NOT NULL,
                         created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, user_id INTEGER NOT NULL);
CREATE TABLE graph_edge (id INTEGER NOT NULL PRIMARY KEY, source_id INTEGER NOT NULL, target_id INTEGER NOT NULL,
                         created_at DATETIME NOT NULL, user_id INTEGER NOT NULL,
                         CONSTRAINT no_self_edge CHECK (source_id != target_id),
                         CONSTRAINT unique_directed_edge UNIQUE (source_id, target_id));
INSERT INTO user VALUES (1, 'first', 'x', '2026-01-01'), (2, 'second', 'x', '2026-01-01');
INSERT INTO project VALUES (1, 'Old project', NULL, '2026-01-01', 2);
INSERT INTO graph_node VALUES (1, 'Old page', NULL, 'notes', NULL, 0, 0, 0, '2026-01-01', '2026-01-01', 1);
"""


def test_legacy_database_is_upgraded(tmp_path, monkeypatch):
    instance = tmp_path / "instance"
    instance.mkdir()
    connection = sqlite3.connect(instance / "redteam_obsidian.sqlite")
    connection.executescript(LEGACY_SCHEMA)
    connection.close()
    monkeypatch.setenv("REDOBSIDIAN_INSTANCE_PATH", str(instance))
    app = create_app({"TESTING": True})
    with app.app_context():
        assert db.session.get(User, 1).is_admin and not db.session.get(User, 2).is_admin
        roles = {m.user_id: m.role for m in ProjectMember.query.filter_by(project_id=1)}
        assert roles == {1: "editor", 2: "owner"}
        assert db.session.get(GraphNode, 1).project_id == 1
        assert all(len(user.session_token) == 32 for user in User.query.all())
        # The bridge leaves no table, column or index missing. (Constraints come from the old app's own
        # create_all and aren't reproduced in this hand-written fixture; nullability differences are harmless.)
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext
        with db.engine.connect() as connection:
            diff = compare_metadata(MigrationContext.configure(connection), db.metadata)
        flat = [item for entry in diff for item in (entry if isinstance(entry, list) else [entry])]
        assert [item for item in flat if item[0] in ("add_table", "add_column", "add_index")] == []
        db.session.remove()
        db.engine.dispose()
