import hashlib
import json
import logging
import time
from urllib.parse import urlparse

from authlib.integrations.flask_client import OAuth
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash

from . import audit, db
from .models import User, utcnow
from .security import (
    clear_login_failures,
    generate_recovery_codes,
    is_locked_out,
    new_totp_secret,
    password_expired,
    password_policy_text,
    password_problems,
    record_login_failure,
    recovery_codes_left,
    totp_qr_svg,
    totp_uri,
    use_recovery_code,
    verify_totp,
)
from .settings import all_settings, get_setting

auth_bp = Blueprint("auth", __name__)
log = logging.getLogger(__name__)
oauth = OAuth()

# Compared against when the username doesn't exist, so both paths take about as long
_DUMMY_HASH = generate_password_hash("not-a-real-password")
PENDING_2FA_SECONDS = 300
LOCKED_MESSAGE = "Too many failed sign-in attempts. Try again later."


def safe_next(target):
    """Only follow same-site relative redirects after login."""
    if not target:
        return None
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    return target


def complete_login(user, method):
    next_url = safe_next(session.get("login_next"))
    session.clear()
    login_user(user)
    if method == "sso":
        session["sso_login"] = True
    user.last_login_at = utcnow()
    db.session.commit()
    clear_login_failures(user.username)
    audit.record("auth.login", user=user, method=method)
    return redirect(next_url or url_for("main.index"))


def login_failed(username, reason):
    record_login_failure(username, request.remote_addr)
    audit.record("auth.login_failed", user=None, username=username[:64], reason=reason)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))
    settings = all_settings()
    sso_on = sso_configured(settings)
    if request.method == "GET" and request.args.get("next"):
        session["login_next"] = request.args.get("next")

    if request.method == "POST":
        username = request.form.get("username", "").strip()[:64]
        password = request.form.get("password", "")
        if is_locked_out(username, request.remote_addr):
            audit.record("auth.login_blocked", user=None, username=username)
            flash(LOCKED_MESSAGE, "error")
            return render_template("auth.html", sso_enabled=sso_on, settings=settings), 429

        user = User.query.filter_by(username=username).first()
        valid = user.check_password(password) if user and user.has_password else check_password_hash(_DUMMY_HASH, password)
        if not user or not valid:
            login_failed(username, "bad credentials")
            flash("Invalid username or password.", "error")
        elif user.disabled:
            login_failed(username, "account disabled")
            flash("Invalid username or password.", "error")
        elif sso_on and settings["sso_disable_password_login"] and not user.is_admin:
            flash("Password sign-in is disabled. Use single sign-on.", "error")
        elif user.totp_enabled:
            session["pending_2fa"] = {"user_id": user.id, "token": user.session_token, "at": int(time.time())}
            return redirect(url_for("auth.login_2fa"))
        else:
            return complete_login(user, "password")

    return render_template("auth.html", sso_enabled=sso_on, settings=settings)


def pending_2fa_user():
    pending = session.get("pending_2fa")
    if not pending or time.time() - pending.get("at", 0) > PENDING_2FA_SECONDS:
        session.pop("pending_2fa", None)
        return None
    user = db.session.get(User, pending.get("user_id"))
    if not user or user.disabled or user.session_token != pending.get("token"):
        return None
    return user


@auth_bp.route("/login/2fa", methods=["GET", "POST"])
def login_2fa():
    user = pending_2fa_user()
    if not user:
        flash("Sign in again to continue.", "error")
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        if is_locked_out(user.username, request.remote_addr):
            session.pop("pending_2fa", None)
            flash(LOCKED_MESSAGE, "error")
            return redirect(url_for("auth.login"))
        code = request.form.get("code", "")
        if verify_totp(user, code):
            db.session.commit()
            return complete_login(user, "password+totp")
        if use_recovery_code(user, code):
            db.session.commit()
            audit.record("auth.recovery_code_used", user=user, remaining=recovery_codes_left(user))
            return complete_login(user, "password+recovery_code")
        login_failed(user.username, "bad 2fa code")
        flash("That code is not valid.", "error")

    return render_template("auth_2fa.html")


@auth_bp.post("/logout")
@login_required
def logout():
    audit.record("auth.logout")
    logout_user()
    session.clear()
    return redirect(url_for("auth.login"))


# --- Account self-service --------------------------------------------------

@auth_bp.get("/account")
@login_required
def account():
    return render_template(
        "account.html",
        active_page="account",
        totp_required=get_setting("totp_required"),
        recovery_left=recovery_codes_left(current_user),
        policy=password_policy_text(),
    )


@auth_bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    forced = current_user.must_change_password or password_expired(current_user)
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        problems = []
        if current_user.has_password and not current_user.check_password(current):
            problems.append("Current password is incorrect.")
        if new != confirm:
            problems.append("The new passwords don't match.")
        if current_user.has_password and new and current_user.check_password(new):
            problems.append("Choose a password different from the current one.")
        problems += password_problems(new, current_user.username)
        if problems:
            for problem in problems:
                flash(problem, "error")
        else:
            user = current_user._get_current_object()
            user.set_password(new)
            user.must_change_password = False
            db.session.commit()
            # The session token rotated with the password; sign this session back in with the new one
            login_user(user)
            audit.record("auth.password_changed", user=user)
            flash("Password changed. Other sessions have been signed out.", "success")
            return redirect(url_for("auth.account"))
    return render_template("change_password.html", forced=forced, policy=password_policy_text(), active_page="account")


@auth_bp.get("/account/2fa/setup")
@login_required
def totp_setup():
    if current_user.totp_enabled:
        return redirect(url_for("auth.account"))
    secret = session.get("totp_pending_secret") or new_totp_secret()
    session["totp_pending_secret"] = secret
    uri = totp_uri(current_user, secret)
    return render_template("totp_setup.html", secret=secret, qr_svg=totp_qr_svg(uri),
                           required=get_setting("totp_required"), active_page="account")


@auth_bp.post("/account/2fa/setup")
@login_required
def totp_confirm():
    secret = session.get("totp_pending_secret")
    if not secret:
        return redirect(url_for("auth.totp_setup"))
    user = current_user._get_current_object()
    if not verify_totp(user, request.form.get("code", ""), secret=secret):
        flash("That code is not valid. Check your device's clock and try again.", "error")
        return redirect(url_for("auth.totp_setup"))
    user.totp_secret = secret
    user.totp_enabled = True
    codes = generate_recovery_codes(user)
    db.session.commit()
    session.pop("totp_pending_secret", None)
    audit.record("auth.totp_enabled", user=user)
    return render_template("recovery_codes.html", codes=codes, active_page="account")


@auth_bp.post("/account/2fa/disable")
@login_required
def totp_disable():
    if get_setting("totp_required"):
        flash("Two-factor authentication is required by your administrator.", "error")
        return redirect(url_for("auth.account"))
    user = current_user._get_current_object()
    if not verify_totp(user, request.form.get("code", "")):
        flash("That code is not valid.", "error")
        return redirect(url_for("auth.account"))
    user.totp_enabled = False
    user.totp_secret = None
    user.totp_last_step = None
    user.recovery_codes_json = "[]"
    db.session.commit()
    audit.record("auth.totp_disabled", user=user)
    flash("Two-factor authentication turned off.", "success")
    return redirect(url_for("auth.account"))


@auth_bp.post("/account/2fa/recovery-codes")
@login_required
def regenerate_recovery_codes():
    user = current_user._get_current_object()
    if not user.totp_enabled or not verify_totp(user, request.form.get("code", "")):
        flash("That code is not valid.", "error")
        return redirect(url_for("auth.account"))
    codes = generate_recovery_codes(user)
    db.session.commit()
    audit.record("auth.recovery_codes_regenerated", user=user)
    return render_template("recovery_codes.html", codes=codes, active_page="account")


@auth_bp.post("/account/sign-out-everywhere")
@login_required
def sign_out_everywhere():
    user = current_user._get_current_object()
    user.rotate_session_token()
    db.session.commit()
    login_user(user)
    audit.record("auth.sessions_revoked", user=user)
    flash("All other sessions have been signed out.", "success")
    return redirect(url_for("auth.account"))


# --- Single sign-on (OpenID Connect) ----------------------------------------

def sso_configured(settings=None):
    settings = settings or all_settings()
    return bool(settings["sso_enabled"] and settings["sso_discovery_url"] and settings["sso_client_id"])


def sso_client():
    """The OIDC client for the current settings, re-registered whenever an admin changes them."""
    settings = all_settings()
    if not sso_configured(settings):
        return None
    if not getattr(oauth, "app", None):
        oauth.init_app(current_app._get_current_object())
    fingerprint = hashlib.sha256(json.dumps(
        [settings[key] for key in ("sso_discovery_url", "sso_client_id", "sso_client_secret", "sso_scopes")]
    ).encode()).hexdigest()
    if getattr(oauth, "_redobsidian_fingerprint", None) != fingerprint:
        oauth.register(
            "oidc",
            overwrite=True,
            server_metadata_url=settings["sso_discovery_url"],
            client_id=settings["sso_client_id"],
            client_secret=settings["sso_client_secret"] or None,
            client_kwargs={"scope": settings["sso_scopes"] or "openid email profile"},
        )
        oauth._redobsidian_fingerprint = fingerprint
    return oauth.create_client("oidc")


@auth_bp.get("/login/sso")
def sso_login():
    client = sso_client()
    if client is None:
        abort(404)
    return client.authorize_redirect(url_for("auth.sso_callback", _external=True))


@auth_bp.get("/login/sso/callback")
def sso_callback():
    client = sso_client()
    if client is None:
        abort(404)
    try:
        token = client.authorize_access_token()
        claims = token.get("userinfo") or client.userinfo(token=token)
    except Exception:
        log.exception("SSO callback failed")
        flash("Single sign-on failed. Try again or contact your administrator.", "error")
        return redirect(url_for("auth.login"))

    subject = str(claims.get("sub") or "")
    username_claim = get_setting("sso_username_claim") or "preferred_username"
    username = str(claims.get(username_claim) or claims.get("email") or "").strip()[:64]
    if not subject or not username:
        flash("Your identity provider didn't send a username.", "error")
        return redirect(url_for("auth.login"))

    user = User.query.filter_by(sso_subject=subject).first()
    if not user:
        user = User.query.filter_by(username=username).first()
        if user and user.sso_subject:
            # The name belongs to a different SSO identity
            user = None
            audit.record("auth.sso_rejected", user=None, username=username, reason="username linked to another identity")
            flash("That account is linked to a different identity.", "error")
            return redirect(url_for("auth.login"))
        if user:
            user.sso_subject = subject
            audit.record("auth.sso_linked", user=user, commit=False)
        elif get_setting("sso_auto_create_users"):
            user = User(username=username, password_hash="", sso_subject=subject)
            db.session.add(user)
            db.session.flush()
            audit.record("user.created", user=user, commit=False, via="sso", target=username)
        else:
            audit.record("auth.sso_rejected", user=None, username=username, reason="no matching account")
            flash("No RedObsidian account matches your identity. Ask an administrator to create one.", "error")
            return redirect(url_for("auth.login"))
    if user.disabled:
        audit.record("auth.sso_rejected", user=None, username=username, reason="account disabled")
        flash("Your account is disabled.", "error")
        return redirect(url_for("auth.login"))
    db.session.commit()
    return complete_login(user, "sso")
