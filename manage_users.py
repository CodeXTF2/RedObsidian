"""User management from the command line (everything here is also available under /admin).

Passwords are prompted for, never taken as arguments, so they don't end up in shell history or the
process list. For automation, set REDOBSIDIAN_PASSWORD instead.
"""
import argparse
import getpass
import os
import sys

from app import audit, create_app, db
from app.models import User
from app.security import password_problems, random_password


def read_password(username, generate=False):
    if os.environ.get("REDOBSIDIAN_PASSWORD"):
        password = os.environ["REDOBSIDIAN_PASSWORD"]
    elif generate:
        password = random_password()
        print(f"Generated password: {password}")
        return password
    else:
        password = getpass.getpass(f"Password for {username}: ")
        if password != getpass.getpass("Repeat password: "):
            sys.exit("Error: passwords don't match.")
    problems = password_problems(password, username)
    if problems:
        sys.exit("Error: " + " ".join(problems))
    return password


def get_user(username):
    user = User.query.filter_by(username=username).first()
    if not user:
        sys.exit(f"Error: user '{username}' not found.")
    return user


def create_user(args):
    if User.query.filter_by(username=args.username).first():
        sys.exit(f"Error: user '{args.username}' already exists.")
    first_user = User.query.count() == 0
    user = User(username=args.username, is_admin=args.admin or first_user)
    user.set_password(read_password(args.username, args.generate))
    user.must_change_password = args.generate or args.must_change
    db.session.add(user)
    db.session.flush()
    audit.record("user.created", user=None, username="cli", target_type="user", target_id=user.id,
                 target=user.username, is_admin=user.is_admin)
    note = " as an administrator" if user.is_admin else ""
    if first_user and not args.admin:
        note += " (the first account is always an administrator)"
    print(f"User '{user.username}' created{note}.")


def list_users(args):
    users = User.query.order_by(User.username).all()
    if not users:
        print("No users found.")
    for user in users:
        flags = [flag for flag, on in (("admin", user.is_admin), ("disabled", user.disabled),
                                       ("2fa", user.totp_enabled), ("sso", bool(user.sso_subject))) if on]
        print(f"- {user.username}{' [' + ', '.join(flags) + ']' if flags else ''} (created {user.created_at:%Y-%m-%d})")


def reset_password(args):
    user = get_user(args.username)
    user.set_password(read_password(args.username, args.generate))
    user.must_change_password = args.generate or args.must_change
    audit.record("user.password_reset", user=None, username="cli", target_type="user", target_id=user.id,
                 target=user.username)
    print(f"Password reset for '{user.username}'. Their existing sessions were signed out.")


def set_flag(args):
    user = get_user(args.username)
    if args.command == "disable":
        user.disabled = True
        user.rotate_session_token()
    elif args.command == "enable":
        user.disabled = False
    elif args.command == "make-admin":
        user.is_admin = True
    elif args.command == "remove-admin":
        if User.query.filter_by(is_admin=True, disabled=False).count() <= 1 and user.is_admin:
            sys.exit("Error: at least one active administrator must remain.")
        user.is_admin = False
    elif args.command == "reset-2fa":
        user.totp_enabled = False
        user.totp_secret = None
        user.totp_last_step = None
        user.recovery_codes_json = "[]"
        user.rotate_session_token()
    audit.record(f"user.{args.command.replace('-', '_')}", user=None, username="cli", target_type="user",
                 target_id=user.id, target=user.username)
    print(f"Done: {args.command} {user.username}.")


def delete_user(args):
    from app.admin import authored_content_count
    from app.models import Project, ProjectMember, Upload

    user = get_user(args.username)
    if authored_content_count(user.id) or Project.query.filter_by(created_by_id=user.id).count():
        sys.exit(f"Error: '{user.username}' has written project content. Disable the account instead.")
    ProjectMember.query.filter_by(user_id=user.id).delete()
    Upload.query.filter_by(user_id=user.id).update({"user_id": None})
    db.session.delete(user)
    audit.record("user.deleted", user=None, username="cli", target_type="user", target_id=user.id,
                 target=args.username)
    print(f"User '{args.username}' deleted.")


def main():
    parser = argparse.ArgumentParser(description="Manage RedObsidian users.")
    subparsers = parser.add_subparsers(dest="command")

    create = subparsers.add_parser("create", help="Create a user (prompts for the password)")
    create.add_argument("username")
    create.add_argument("--admin", action="store_true", help="Make the user an administrator")
    create.add_argument("--generate", action="store_true", help="Generate a temporary password instead of prompting")
    create.add_argument("--must-change", action="store_true", help="Require a password change at first sign-in")
    create.set_defaults(func=create_user)

    subparsers.add_parser("list", help="List users").set_defaults(func=list_users)

    reset = subparsers.add_parser("reset-password", help="Set a new password (signs out their sessions)")
    reset.add_argument("username")
    reset.add_argument("--generate", action="store_true")
    reset.add_argument("--must-change", action="store_true")
    reset.set_defaults(func=reset_password)

    for name, help_text in (("disable", "Block sign-in"), ("enable", "Allow sign-in again"),
                            ("make-admin", "Grant administrator rights"), ("remove-admin", "Remove administrator rights"),
                            ("reset-2fa", "Turn off two-factor authentication (lost device)")):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("username")
        sub.set_defaults(func=set_flag)

    delete = subparsers.add_parser("delete", help="Delete a user without project content")
    delete.add_argument("username")
    delete.set_defaults(func=delete_user)

    args = parser.parse_args()
    if not getattr(args, "func", None):
        parser.print_help()
        return
    app = create_app()
    with app.app_context():
        args.func(args)
        db.session.commit()


if __name__ == "__main__":
    main()
