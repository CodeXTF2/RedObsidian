import pyotp

from app import db
from app.models import AuditLog, User
from app.settings import set_settings

from .conftest import login


def test_login_and_logout(app, make_user):
    make_user("alice")
    client = app.test_client()
    assert login(client, "alice", "wrong").status_code == 200
    response = login(client, "alice")
    assert response.status_code == 302
    assert client.get("/projects").status_code == 200
    client.post("/logout")
    assert client.get("/projects").status_code == 302
    with app.app_context():
        actions = [entry.action for entry in AuditLog.query.order_by(AuditLog.id)]
    assert actions == ["auth.login_failed", "auth.login", "auth.logout"]


def test_lockout_off_by_default(app, make_user):
    make_user("alice")
    client = app.test_client()
    for _ in range(10):
        login(client, "alice", "wrong")
    assert login(client, "alice").status_code == 302


def test_lockout_when_enabled(app, make_user):
    make_user("alice")
    with app.test_request_context():
        set_settings({"lockout_enabled": True, "lockout_max_attempts": 3})
    client = app.test_client()
    for _ in range(3):
        login(client, "alice", "wrong")
    # Even the right password is refused while locked
    assert login(client, "alice").status_code == 429


def test_password_policy_applies_when_enabled(app, make_user, login_as):
    make_user("alice")
    client = login_as("alice")
    form = {"current_password": "correct horse battery", "new_password": "short", "confirm_password": "short"}
    # Policy off: any non-empty password is accepted
    assert client.post("/account/password", data=form).status_code == 302

    with app.test_request_context():
        set_settings({"password_policy_enabled": True, "password_min_length": 12, "password_require_digit": True})
    form = {"current_password": "short", "new_password": "longbutnodigits", "confirm_password": "longbutnodigits"}
    response = client.post("/account/password", data=form)
    assert response.status_code == 200
    assert b"must contain a number" in response.data
    form.update(new_password="longwithdigit1", confirm_password="longwithdigit1")
    assert client.post("/account/password", data=form).status_code == 302


def test_password_change_signs_out_other_sessions(app, make_user, login_as):
    make_user("alice")
    first = login_as("alice")
    second = login_as("alice")
    form = {"current_password": "correct horse battery", "new_password": "another long one", "confirm_password": "another long one"}
    assert first.post("/account/password", data=form).status_code == 302
    assert first.get("/account").status_code == 200
    assert second.get("/account").status_code == 302


def test_disabled_user_is_signed_out(app, make_user, login_as):
    user_id = make_user("alice")
    client = login_as("alice")
    with app.app_context():
        user = db.session.get(User, user_id)
        user.disabled = True
        user.rotate_session_token()
        db.session.commit()
    assert client.get("/projects").status_code == 302
    assert login(app.test_client(), "alice").status_code == 200


def enrol_totp(app, client):
    client.get("/account/2fa/setup")
    with client.session_transaction() as session:
        secret = session["totp_pending_secret"]
    response = client.post("/account/2fa/setup", data={"code": pyotp.TOTP(secret).now()})
    assert response.status_code == 200 and b"recovery codes" in response.data.lower()
    return secret


def test_totp_enrolment_and_login(app, make_user, login_as):
    make_user("alice")
    client = login_as("alice")
    secret = enrol_totp(app, client)

    fresh = app.test_client()
    response = login(fresh, "alice")
    assert response.headers["Location"].endswith("/login/2fa")
    assert fresh.get("/projects").status_code == 302  # not signed in yet
    assert fresh.post("/login/2fa", data={"code": "000000"}).status_code == 200
    # The code just used for enrolment is refused as a replay
    totp = pyotp.TOTP(secret)
    with app.app_context():
        used_step = User.query.filter_by(username="alice").one().totp_last_step
    assert fresh.post("/login/2fa", data={"code": totp.at(used_step * totp.interval)}).status_code == 200
    # Pretend the enrolment code was from an earlier time step; the current code then works
    with app.app_context():
        user = User.query.filter_by(username="alice").first()
        user.totp_last_step -= 2
        db.session.commit()
    assert fresh.post("/login/2fa", data={"code": pyotp.TOTP(secret).now()}).status_code == 302
    assert fresh.get("/projects").status_code == 200


def test_recovery_code_works_once(app, make_user, login_as):
    make_user("alice")
    client = login_as("alice")
    client.get("/account/2fa/setup")
    with client.session_transaction() as session:
        secret = session["totp_pending_secret"]
    page = client.post("/account/2fa/setup", data={"code": pyotp.TOTP(secret).now()}).get_data(as_text=True)
    import re
    code = re.findall(r"<code>([0-9a-f]{6}-[0-9a-f]{6})</code>", page)[0]

    first = app.test_client()
    login(first, "alice")
    assert first.post("/login/2fa", data={"code": code}).status_code == 302
    second = app.test_client()
    login(second, "alice")
    assert second.post("/login/2fa", data={"code": code}).status_code == 200


def test_totp_required_forces_enrolment(app, make_user, login_as):
    make_user("alice")
    with app.test_request_context():
        set_settings({"totp_required": True})
    client = login_as("alice")
    response = client.get("/projects")
    assert response.status_code == 302 and response.headers["Location"].endswith("/account/2fa/setup")
    assert client.get("/p/1/api/state").status_code == 403
    enrol_totp(app, client)
    assert client.get("/projects").status_code == 200


def test_must_change_password_is_forced(app, make_user, login_as):
    user_id = make_user("alice")
    with app.app_context():
        db.session.get(User, user_id).must_change_password = True
        db.session.commit()
    client = login_as("alice")
    assert client.get("/projects").headers["Location"].endswith("/account/password")


def test_idle_timeout(app, make_user, login_as):
    make_user("alice")
    with app.test_request_context():
        set_settings({"session_idle_timeout_minutes": 5})
    client = login_as("alice")
    assert client.get("/projects").status_code == 200
    with client.session_transaction() as session:
        session["last_seen"] -= 6 * 60
    assert client.get("/projects").status_code == 302


def test_open_redirect_blocked(app, make_user):
    make_user("alice")
    client = app.test_client()
    client.get("/login?next=//evil.example/")
    response = login(client, "alice")
    assert "evil.example" not in response.headers["Location"]


def test_security_headers(app):
    response = app.test_client().get("/login")
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_healthz(app):
    assert app.test_client().get("/healthz").json == {"status": "ok"}
