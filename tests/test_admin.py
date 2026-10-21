import re

from app import db
from app.models import AuditLog, User
from app.settings import DEFAULTS, all_settings

from .conftest import login


def test_admin_pages_require_admin(app, make_user, login_as):
    make_user("alice")
    client = login_as("alice")
    for path in ("/admin/users", "/admin/settings", "/admin/audit", "/admin/projects"):
        assert client.get(path).status_code == 403, path


def test_all_policies_off_by_default(app):
    with app.test_request_context():
        settings = all_settings()
    for key in ("password_policy_enabled", "totp_required", "lockout_enabled", "sso_enabled",
                "only_admins_create_projects", "sso_auto_create_users", "sso_disable_password_login"):
        assert settings[key] is False, key
    assert settings["session_idle_timeout_minutes"] == 0 and settings["password_max_age_days"] == 0


def test_admin_pages_render(app, make_user, make_project, login_as):
    root = make_user("root", is_admin=True)
    make_project("A", {root: "owner"}, findings=True)
    client = login_as("root")
    for path in ("/admin/users", f"/admin/users/{root}", "/admin/settings", "/admin/audit", "/admin/projects",
                 "/account", "/account/password", "/account/2fa/setup", "/projects"):
        assert client.get(path).status_code == 200, path


def test_create_user_and_reset(app, make_user, login_as):
    make_user("root", is_admin=True)
    client = login_as("root")
    page = client.post("/admin/users", data={"username": "newbie", "password": ""}).get_data(as_text=True)
    password = re.search(r'<code class="secret">([^<]+)</code>', page).group(1)
    password = password.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&#34;", '"').replace("&#39;", "'")
    newbie = app.test_client()
    assert login(newbie, "newbie", password).status_code == 302
    assert newbie.get("/projects").headers["Location"].endswith("/account/password")

    with app.app_context():
        user_id = User.query.filter_by(username="newbie").one().id
    client.post(f"/admin/users/{user_id}/action", data={"action": "disable"})
    assert newbie.get("/account").status_code == 302


def test_cannot_lock_out_last_admin(app, make_user, login_as):
    root = make_user("root", is_admin=True)
    client = login_as("root")
    client.post(f"/admin/users/{root}/action", data={"action": "demote"})
    with app.app_context():
        assert db.session.get(User, root).is_admin


def test_settings_save_and_audit(app, make_user, login_as):
    make_user("root", is_admin=True)
    client = login_as("root")
    form = {key: value for key, value in DEFAULTS.items() if not isinstance(value, bool)}
    form.update(password_policy_enabled="on", password_min_length="14", sso_client_secret="s3cret")
    assert client.post("/admin/settings", data=form).status_code == 302
    with app.test_request_context():
        settings = all_settings()
    assert settings["password_policy_enabled"] is True and settings["password_min_length"] == 14
    assert settings["sso_client_secret"] == "s3cret"
    # A blank secret field keeps the stored secret
    form.pop("sso_client_secret")
    form["sso_client_secret"] = ""
    client.post("/admin/settings", data=form)
    with app.test_request_context():
        assert all_settings()["sso_client_secret"] == "s3cret"
    with app.app_context():
        entry = AuditLog.query.filter_by(action="settings.changed").first()
    assert "s3cret" not in entry.details_json
    # The settings page never echoes the secret back
    assert b"s3cret" not in client.get("/admin/settings").data


def test_audit_csv_neutralises_formulas(app, make_user, make_project, login_as):
    root = make_user("root", is_admin=True)
    project_id = make_project("A", {root: "owner"}, findings=True)
    client = login_as("root")
    client.post(f"/p/{project_id}/findings", data={"title": "=HYPERLINK(\"http://evil\")", "status": "open"})
    csv_text = client.get("/admin/audit.csv").get_data(as_text=True)
    assert "finding.created" in csv_text
    assert ',=HYPERLINK' not in csv_text


def test_sso_routes_hidden_when_disabled(app):
    client = app.test_client()
    assert client.get("/login/sso").status_code == 404
    assert b"Sign in with SSO" not in client.get("/login").data
