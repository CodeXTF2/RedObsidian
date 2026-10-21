import io
import json
import secrets
import string
from datetime import timedelta

import pyotp
import qrcode
import qrcode.image.svg
from werkzeug.security import check_password_hash, generate_password_hash

from . import db
from .models import LoginFailure, as_utc, utcnow
from .settings import all_settings, get_setting

TOTP_ISSUER = "RedObsidian"
# Passwords longer than this are refused regardless of policy (hashing cost)
MAX_PASSWORD_LENGTH = 256


# --- Password policy -------------------------------------------------------

def password_policy_text():
    s = all_settings()
    if not s["password_policy_enabled"]:
        return ""
    parts = [f"at least {s['password_min_length']} characters"]
    for key, label in (("upper", "an uppercase letter"), ("lower", "a lowercase letter"),
                       ("digit", "a number"), ("symbol", "a symbol")):
        if s[f"password_require_{key}"]:
            parts.append(label)
    return "Passwords need " + ", ".join(parts) + "."


def password_problems(password, username=None):
    """Reasons the password breaks the current policy; empty when it is acceptable."""
    problems = []
    if not password:
        return ["Password is required."]
    if len(password) > MAX_PASSWORD_LENGTH:
        return [f"Password must be at most {MAX_PASSWORD_LENGTH} characters."]
    s = all_settings()
    if not s["password_policy_enabled"]:
        return problems
    if len(password) < s["password_min_length"]:
        problems.append(f"Password must be at least {s['password_min_length']} characters.")
    if s["password_require_upper"] and not any(c.isupper() for c in password):
        problems.append("Password must contain an uppercase letter.")
    if s["password_require_lower"] and not any(c.islower() for c in password):
        problems.append("Password must contain a lowercase letter.")
    if s["password_require_digit"] and not any(c.isdigit() for c in password):
        problems.append("Password must contain a number.")
    if s["password_require_symbol"] and all(c.isalnum() for c in password):
        problems.append("Password must contain a symbol.")
    if username and username.lower() in password.lower():
        problems.append("Password must not contain the username.")
    return problems


def password_expired(user):
    max_age = get_setting("password_max_age_days")
    if not max_age or not user.has_password:
        return False
    changed = user.password_changed_at or user.created_at
    return utcnow() - as_utc(changed) > timedelta(days=max_age)


def random_password(length=20):
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        # Make sure a generated password passes any policy the admin may have switched on
        if (any(c.isupper() for c in candidate) and any(c.islower() for c in candidate)
                and any(c.isdigit() for c in candidate) and not candidate.isalnum()):
            return candidate


# --- Lockout ---------------------------------------------------------------

def _window_start():
    return utcnow() - timedelta(minutes=max(1, get_setting("lockout_window_minutes")))


def is_locked_out(username, ip):
    if not get_setting("lockout_enabled"):
        return False
    max_attempts = max(1, get_setting("lockout_max_attempts"))
    since = _window_start()
    by_user = LoginFailure.query.filter(LoginFailure.username == username.lower(), LoginFailure.at >= since).count()
    # An address gets more headroom since several people may sit behind one NAT
    by_ip = LoginFailure.query.filter(LoginFailure.ip == ip, LoginFailure.at >= since).count()
    return by_user >= max_attempts or by_ip >= max_attempts * 5


def record_login_failure(username, ip):
    db.session.add(LoginFailure(username=username.lower()[:64], ip=ip or "unknown"))
    db.session.commit()


def clear_login_failures(username):
    LoginFailure.query.filter_by(username=username.lower()).delete()
    db.session.commit()


def purge_old_login_failures():
    cutoff = utcnow() - timedelta(days=1)
    LoginFailure.query.filter(LoginFailure.at < cutoff).delete()


# --- TOTP ------------------------------------------------------------------

def new_totp_secret():
    return pyotp.random_base32()


def totp_uri(user, secret):
    return pyotp.TOTP(secret).provisioning_uri(name=user.username, issuer_name=TOTP_ISSUER)


def totp_qr_svg(uri):
    image = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue().decode("utf-8")


def verify_totp(user, code, secret=None):
    """Check a 6-digit code, allowing one step of clock drift and refusing replays."""
    secret = secret or user.totp_secret
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if not secret or len(code) != 6:
        return False
    totp = pyotp.TOTP(secret)
    now = utcnow()
    current_step = int(now.timestamp()) // totp.interval
    for offset in (-1, 0, 1):
        step = current_step + offset
        if user.totp_last_step is not None and step <= user.totp_last_step:
            continue
        if secrets.compare_digest(totp.at(step * totp.interval), code):
            user.totp_last_step = step
            return True
    return False


def generate_recovery_codes(user, count=10):
    codes = [f"{secrets.token_hex(3)}-{secrets.token_hex(3)}" for _ in range(count)]
    user.recovery_codes_json = json.dumps([generate_password_hash(code) for code in codes])
    return codes


def use_recovery_code(user, code):
    code = (code or "").strip().lower()
    try:
        hashes = json.loads(user.recovery_codes_json or "[]")
    except (TypeError, ValueError):
        hashes = []
    for index, hashed in enumerate(hashes):
        if check_password_hash(hashed, code):
            del hashes[index]
            user.recovery_codes_json = json.dumps(hashes)
            return True
    return False


def recovery_codes_left(user):
    try:
        return len(json.loads(user.recovery_codes_json or "[]"))
    except (TypeError, ValueError):
        return 0
