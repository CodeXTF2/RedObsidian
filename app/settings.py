"""Admin-editable settings. Every security policy is off by default; admins turn them on in /admin/settings."""
import json

from flask import g, has_app_context

from . import db
from .models import Setting

DEFAULTS = {
    # Password policy
    "password_policy_enabled": False,
    "password_min_length": 12,
    "password_require_upper": False,
    "password_require_lower": False,
    "password_require_digit": False,
    "password_require_symbol": False,
    "password_max_age_days": 0,
    # Two-factor authentication
    "totp_required": False,
    # Brute-force protection
    "lockout_enabled": False,
    "lockout_max_attempts": 5,
    "lockout_window_minutes": 15,
    # Sessions
    "session_idle_timeout_minutes": 0,
    # Projects
    "only_admins_create_projects": False,
    # Retention (0 = keep forever)
    "deleted_page_retention_days": 30,
    "archived_project_retention_days": 0,
    "audit_log_retention_days": 0,
    # Uploads
    "max_upload_mb": 100,
    # Single sign-on (OpenID Connect)
    "sso_enabled": False,
    "sso_button_label": "Sign in with SSO",
    "sso_discovery_url": "",
    "sso_client_id": "",
    "sso_client_secret": "",
    "sso_scopes": "openid email profile",
    "sso_username_claim": "preferred_username",
    "sso_auto_create_users": False,
    "sso_disable_password_login": False,
}

# Settings that are never shown back in the admin form
SECRET_KEYS = {"sso_client_secret"}


def _coerce(key, value):
    default = DEFAULTS[key]
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.lower() in ("1", "true", "on", "yes")
        return bool(value)
    if isinstance(default, int):
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return default
    return "" if value is None else str(value).strip()


def all_settings():
    if has_app_context() and "settings_cache" in g:
        return g.settings_cache
    values = dict(DEFAULTS)
    for row in Setting.query.all():
        if row.key in DEFAULTS:
            try:
                values[row.key] = _coerce(row.key, json.loads(row.value_json))
            except (TypeError, ValueError):
                pass
    if has_app_context():
        g.settings_cache = values
    return values


def get_setting(key):
    return all_settings()[key]


def set_settings(updates):
    """Save the given settings and return {key: (old, new)} for the ones that changed."""
    current = all_settings()
    changed = {}
    for key, value in updates.items():
        if key not in DEFAULTS:
            continue
        new_value = _coerce(key, value)
        if new_value == current[key]:
            continue
        row = db.session.get(Setting, key) or Setting(key=key)
        row.value_json = json.dumps(new_value)
        db.session.add(row)
        changed[key] = (current[key], new_value)
    db.session.commit()
    g.pop("settings_cache", None)
    return changed
