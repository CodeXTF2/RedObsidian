import re

import pytest

from app import create_app, db, socketio
from app.models import GraphNode, Project, ProjectMember, User


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("REDOBSIDIAN_INSTANCE_PATH", str(tmp_path / "instance"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False})
    yield app
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


@pytest.fixture
def make_user(app):
    def make(username, password="correct horse battery", is_admin=False):
        with app.app_context():
            user = User(username=username, is_admin=is_admin)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            return user.id
    return make


@pytest.fixture
def make_project(app):
    def make(name, members, findings=False, report=False):
        """members: {user_id: role}; the first owner is recorded as creator. findings / report turn on those beta features."""
        with app.app_context():
            creator = next((uid for uid, role in members.items() if role == "owner"), next(iter(members)))
            project = Project(name=name, created_by_id=creator, findings_enabled=findings,
                              report_enabled=report)
            db.session.add(project)
            db.session.flush()
            for user_id, role in members.items():
                db.session.add(ProjectMember(project_id=project.id, user_id=user_id, role=role))
            db.session.add(GraphNode(project_id=project.id, title="First page", in_graph=False, user_id=creator))
            db.session.commit()
            return project.id
    return make


def login(client, username, password="correct horse battery"):
    return client.post("/login", data={"username": username, "password": password})


@pytest.fixture
def login_as(app):
    def do(username, password="correct horse battery"):
        client = app.test_client()
        response = login(client, username, password)
        assert response.status_code == 302, response.data[:500]
        return client
    return do


@pytest.fixture
def socket_for(app):
    def connect(client, project_id):
        return socketio.test_client(app, flask_test_client=client, query_string=f"project={project_id}")
    return connect


def received_named(socket_client, name):
    return [message for message in socket_client.get_received() if message["name"] == name]


def csrf_free_text(response):
    return re.sub(r"\s+", " ", response.get_data(as_text=True))
